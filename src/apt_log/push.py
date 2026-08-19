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

# Who the push service should complain to. Not a real inbox — it is a contact
# of last resort in the VAPID spec, and this fleet is one machine.
VAPID_SUBJECT = os.environ.get("APTLOG_VAPID_SUBJECT", "mailto:aptlog@invalid")

_lock = threading.Lock()


# ------------------------------------------------------------------- the keys
def _generate() -> dict[str, str]:
    """A fresh keypair, in the two shapes its two readers need.

    The browser wants the public key as the raw uncompressed EC point,
    base64url — that is what `applicationServerKey` is. The sender wants the
    private key as PEM, which is what pywebpush takes. They are the same key
    written two ways, and writing both down beats deriving one at every use.
    """
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    from py_vapid.utils import b64urlencode

    vapid = Vapid01()
    vapid.generate_keys()
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return {"public": b64urlencode(raw),
            "private": vapid.private_pem().decode("utf-8")}


def keys() -> dict[str, str]:
    """The server's keypair, generated and saved the first time it is asked
    for. Returns {} if it cannot be produced, so callers degrade to "push is
    not available here" rather than failing."""
    with _lock:
        try:
            return json.loads(KEY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            pair = _generate()
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot generate VAPID keys (%s)", exc)
            return {}
        try:
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = KEY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(pair), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, KEY_PATH)
        except OSError as exc:
            # A key that cannot be saved is not a key: the next call would
            # generate a different one, the browser would be subscribed
            # against the old one, and every push would be rejected by the
            # push service with nothing on this side saying why. Better to
            # report no push at all, loudly, than a push that silently
            # cannot work.
            log.error("cannot save VAPID keys to %s (%s) — push disabled",
                      KEY_PATH, exc)
            return {}
        return pair


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
    with _lock:
        doc = _load()
        doc["subs"][endpoint] = {"endpoint": endpoint, "auth": auth,
                                 "p256dh": p256dh, "device": device_id,
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

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"auth": sub["auth"], "p256dh": sub["p256dh"]},
                },
                data=payload,
                vapid_private_key=pair["private"],
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=15,
            )
            sent += 1
        except WebPushException as exc:  # noqa: PERF203
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status in (404, 410):
                log.info("push endpoint retired — forgetting it")
                unsubscribe(sub["endpoint"])
            else:
                log.warning("push failed (%s)", status or exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("push failed (%s)", exc)
    return sent
