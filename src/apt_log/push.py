"""Notifications from the portal itself, so tapping one opens the portal.

There is exactly one thing this system cannot do for itself and cannot wait
out: the login code inMyTeam texts. It stops, asks, and this is how the asking
reaches somebody who is not looking at the page.

**Why not the alert channel.** REQ-9's `alert.sh` (ntfy/Pushover) is still
here and still right for the machine's own failures — a deploy that rolled
back, a unit that died — because those must go out even when this portal is
the thing that is broken. But a relay's notification belongs to the relay's
app: tapping it opens Safari at a URL, not the portal she installed on her
home screen. For the one notification whose entire purpose is "come here and
type this", that is the wrong app and the wrong window.

A notification sent by the portal, through a service worker the portal
registered, opens the portal. That is the whole reason this exists.

**The service worker caches nothing.** The objection written into phone.html
— that offline caching would keep copies of what the phone's screen said —
is answered by not caching: `sw.js` handles `push` and `notificationclick`
and has no `fetch` handler at all.

**iOS is strict and worth stating.** Web Push needs iOS 16.4+, the site added
to the Home Screen, a real certificate (tailscale serve provides one), and a
permission prompt raised from a user gesture. Everything here no-ops cleanly
where any of that is missing, because a portal that throws on a phone that
cannot subscribe is worse than one that quietly cannot notify.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The keypair identifies this server to the push services. Public half goes to
# the browser, private half signs; losing the file only costs everybody a
# re-subscribe, which is why it is generated on demand rather than being a
# deployment step somebody has to remember.
#
# In /var/lib rather than /etc, which is where it went first: /etc/aptlog is
# root-owned and these services run as `apt`, so the save failed silently and
# every call generated a FRESH keypair. That is worse than not working — the
# browser subscribes against one key and the server signs with another, so
# every push is rejected by the push service and nothing anywhere says why.
# Caught by asking the live machine for the key twice and comparing.
KEY_PATH = Path(os.environ.get("APTLOG_VAPID_PATH",
                               "/var/lib/aptlog/vapid.json"))

# Where subscriptions live. Beside the other state, and 0600: an endpoint plus
# its keys is enough to push to that phone, which is not a secret worth much
# but is not nothing either.
STORE_PATH = Path(os.environ.get("APTLOG_PUSH_PATH",
                                 "/var/lib/aptlog/push.json"))

# Where this portal answers, which is also the only address it has to give.
PORTAL_ORIGIN = os.environ.get("APTLOG_PORTAL_ORIGIN",
                               "https://aptlog-fl.tailf012c7.ts.net")

# Who the push service should contact about this sender. RFC 8292 allows a
# `mailto:` or an `https:` origin, and the origin is the honest answer here:
# there is no inbox, and this URL is genuinely where the sender lives.
#
# APPLE REFUSES A `.invalid` DOMAIN. The first version used
# `mailto:aptlog@aptlog.invalid` — chosen because RFC 2606 reserves that TLD
# for addresses that must never resolve, which felt like the honest way to
# say "no inbox". Apple answers that with 403 BadJwtToken, a message that
# names nothing and sent this looking at keys, clocks and claim shapes for
# an hour. Established by signing tokens with four different subjects and
# posting each to a deliberately meaningless Apple endpoint: the refused one
# comes back 403, the accepted ones come back 400 BadDeviceToken, which is
# Apple saying the TOKEN was fine and only the fake endpoint was not.
#
# py_vapid has its own opinion on top: its regex allows an https origin but
# no path, so this must stay a bare origin.
VAPID_SUBJECT = os.environ.get("APTLOG_VAPID_SUBJECT", PORTAL_ORIGIN)

_lock = threading.Lock()


# ------------------------------------------------------------------- the keys
def _generate() -> dict[str, str]:
    """A fresh keypair, in the two shapes its two readers need.

    BOTH HALVES ARE base64url OF THE RAW KEY, and neither is PEM. The public
    half is the uncompressed EC point, which is what a browser's
    `applicationServerKey` is. The private half is the 32-byte scalar, which
    is what pywebpush's `vapid_private_key` takes when it is given a string —
    hand it PEM text and it tries to read the base64 as DER and fails with
    "ASN.1 parsing error: invalid length", which says nothing about what it
    actually wanted.

    That was a real push, refused, with the failure landing in a log nobody
    was reading. Established by handing pywebpush both forms against a dead
    endpoint and watching which one got as far as the network.
    """
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    from py_vapid.utils import b64urlencode, num_to_bytes

    vapid = Vapid01()
    vapid.generate_keys()
    point = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    scalar = vapid.private_key.private_numbers().private_value
    return {"public": b64urlencode(point),
            "private": b64urlencode(num_to_bytes(scalar, 32))}


def _reencode(stored: dict[str, str]) -> dict[str, str]:
    """Turn a PEM private half into the base64url one pywebpush wants,
    keeping the public half exactly as it is."""
    try:
        from py_vapid import Vapid01
        from py_vapid.utils import b64urlencode, num_to_bytes

        vapid = Vapid01.from_pem(stored["private"].encode("utf-8"))
        scalar = vapid.private_key.private_numbers().private_value
        return {"public": stored["public"],
                "private": b64urlencode(num_to_bytes(scalar, 32))}
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot re-encode the stored VAPID key (%s)", exc)
        return {}


def _write(pair: dict[str, str]) -> bool:
    try:
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = KEY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(pair), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, KEY_PATH)
        return True
    except OSError as exc:
        # A key that cannot be saved is not a key: the next call would
        # generate a different one, the browser would be subscribed against
        # the old one, and every push would be rejected by the push service
        # with nothing on this side saying why. Better to report no push at
        # all, loudly, than a push that silently cannot work.
        log.error("cannot save VAPID keys to %s (%s) — push disabled",
                  KEY_PATH, exc)
        return False


def keys() -> dict[str, str]:
    """The server's keypair, generated and saved the first time it is asked
    for. Returns {} if it cannot be produced, so callers degrade to "push is
    not available here" rather than failing."""
    with _lock:
        try:
            stored = json.loads(KEY_PATH.read_text(encoding="utf-8"))
            if not str(stored.get("private", "")).startswith("-----"):
                return stored
            # A key written by the version that stored PEM. Converted rather
            # than replaced: the PUBLIC half is unchanged and correct, so
            # every browser already subscribed against it stays subscribed.
            # Regenerating here would silently unsubscribe her instead.
            fixed = _reencode(stored)
            if fixed:
                _write(fixed)
                log.info("re-encoded the VAPID private key; subscriptions kept")
                return fixed
        except (OSError, json.JSONDecodeError):
            pass
        try:
            pair = _generate()
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot generate VAPID keys (%s)", exc)
            return {}
        return pair if _write(pair) else {}


def public_key() -> str:
    """What the browser needs to subscribe. Empty means push is unavailable,
    and the page hides its own control rather than offering one that cannot
    work."""
    return keys().get("public", "")


# ---------------------------------------------------------- the subscriptions
def _load() -> dict[str, Any]:
    try:
        doc = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"subs": {}}
    return doc if isinstance(doc, dict) and "subs" in doc else {"subs": {}}


def _save(doc: dict[str, Any]) -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, STORE_PATH)
    except OSError as exc:
        log.warning("cannot save push subscriptions (%s)", exc)


def subscribe(subscription: dict[str, Any], device_id: str = "") -> bool:
    """Remember a browser that wants to be told. Keyed by endpoint, so
    re-subscribing the same phone replaces rather than doubles it."""
    endpoint = (subscription or {}).get("endpoint")
    auth = ((subscription or {}).get("keys") or {}).get("auth")
    p256dh = ((subscription or {}).get("keys") or {}).get("p256dh")
    if not (endpoint and auth and p256dh):
        return False
    # Read the key BEFORE taking the lock: keys() takes the same lock, and it
    # is not reentrant, so doing this inside would hang every subscribe.
    current = keys().get("public", "")
    with _lock:
        doc = _load()
        # The server key in force when this browser subscribed. A push is
        # signed with the CURRENT key, and a push service refuses a signature
        # from any other one — Apple says 403 BadJwtToken, which names the
        # symptom and not the cause. Recording it turns that into a fact this
        # code can check before it wastes a send.
        doc["subs"][endpoint] = {"endpoint": endpoint, "auth": auth,
                                 "p256dh": p256dh, "device": device_id,
                                 "key": current,
                                 "at": time.time()}
        _save(doc)
    return True


def unsubscribe(endpoint: str) -> None:
    with _lock:
        doc = _load()
        if doc["subs"].pop(endpoint, None) is not None:
            _save(doc)


def subscriptions() -> list[dict[str, Any]]:
    return list(_load()["subs"].values())


def count() -> int:
    return len(_load()["subs"])


# ------------------------------------------------------------------- sending
def send(title: str, body: str, url: str = "/app", tag: str = "aptlog") -> int:
    """Push one notification to every subscribed browser. Returns how many
    took it.

    Never raises. A notification that fails a sign-in is worse than a missed
    notification — the same rule the alert channel holds.

    An endpoint the push service has retired (404/410) is dropped here rather
    than retried forever: a phone that reinstalled the app is not a phone that
    is briefly unreachable.
    """
    subs = subscriptions()
    if not subs:
        return 0
    pair = keys()
    if not pair:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush is not installed — nothing pushed")
        return 0

    # How long the signed token stays good for. py_vapid would use 24 hours,
    # which is the outside edge of what the spec allows and what Apple
    # accepts — and an edge is a bad place to sit when the only symptom is
    # "BadJwtToken", a message that names nothing. Twelve hours is as valid
    # and not on any boundary.
    claims = {"sub": VAPID_SUBJECT, "exp": int(time.time()) + 12 * 3600}

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in subs:
        # A subscription made against a different server key can never be
        # signed for. Dropped here rather than sent and refused, so the log
        # says what is actually wrong and the browser is free to subscribe
        # again — which it does by itself, see phone.js.
        stored_key = sub.get("key")
        if stored_key and stored_key != pair["public"]:
            log.info("subscription predates the current key — forgetting it")
            unsubscribe(sub["endpoint"])
            continue
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"auth": sub["auth"], "p256dh": sub["p256dh"]},
                },
                data=payload,
                vapid_private_key=pair["private"],
                vapid_claims=dict(claims),
                timeout=15,
            )
            sent += 1
        except WebPushException as exc:  # noqa: PERF203
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", 0)
            detail = (getattr(response, "text", "") or "")[:120]
            if status in (404, 410):
                log.info("push endpoint retired — forgetting it")
                unsubscribe(sub["endpoint"])
            elif status == 403:
                # NOT dropped, however it is worded. This code read Apple's
                # BadJwtToken as "wrong key" and threw the subscription away
                # — and then the same refusal arrived for a subscription
                # whose recorded key matched the current one exactly, on a
                # machine with a synced clock and a verified keypair. So the
                # message does not mean only that, and acting on it as if it
                # did cost a real subscriber and a trip back to the phone to
                # tap the toggle again.
                #
                # A subscription this code can PROVE is stale — one whose
                # recorded key differs from the key in force — is still
                # dropped, above, before anything is sent. That is a fact
                # this side owns. A refusal it cannot explain is reported
                # and kept.
                log.warning("push refused (403) and the subscription kept: "
                            "%s", detail or "no detail")
            else:
                log.warning("push failed (%s) %s", status or exc, detail)
        except Exception as exc:  # noqa: BLE001
            log.warning("push failed (%s)", exc)
    return sent
