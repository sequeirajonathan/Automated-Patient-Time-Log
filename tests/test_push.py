"""Notifications from the portal itself.

The point of this over the relay channel is one thing and it is not
redundancy: a notification sent by THIS server, through a worker THIS server
registered, opens the app she installed on her home screen. A relay's
notification opens Safari at a URL — the wrong app, on the one notice whose
entire job is to be tapped. Reported from the field exactly that way.

Everything here degrades to "push is not available" rather than raising. iOS
grants Web Push only to a site added to the Home Screen, on 16.4+, over a real
certificate, and only from a genuine press; a portal that throws on a phone
that cannot subscribe is worse than one that quietly cannot notify.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apt_log import push
from apt_log.ui.app import app

# pywebpush pulls http-ece, which has no wheel and does not build against
# every setuptools. It installs fine on the Pi — twelve seconds, checked
# before the dependency was added — and the Pi is where the suite runs as the
# deploy gate. Where it is missing, the crypto-bearing tests skip and the rest
# still hold: the worker, the routes and the store need no crypto at all.
try:
    import pywebpush  # noqa: F401

    HAVE_PUSH = True
except ImportError:
    HAVE_PUSH = False

needs_push = pytest.mark.skipif(not HAVE_PUSH, reason="pywebpush not installed")


def strip_js_comments(source: str) -> str:
    """Code without its prose.

    A guard that cannot tell the two apart forbids describing the bug it
    exists to prevent — this project has now been bitten by that three times
    (form.action, the type bar's constant, and the word "caches" in the very
    comment explaining that nothing is cached).
    """
    import re

    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)

SUBSCRIPTION = {
    "endpoint": "https://web.push.apple.com/abc123",
    "keys": {"auth": "YXV0aA", "p256dh": "cDI1NmRo"},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "STORE_PATH", tmp_path / "push.json")
    monkeypatch.setattr(push, "KEY_PATH", tmp_path / "vapid.json")
    return tmp_path


@pytest.fixture
def client(store):
    return TestClient(app)


class TestTheWorkerIsServedWhereItCanWork:
    """A service worker's scope is its own directory and below. Under
    /static it could only ever control /static — never /app, which is the
    only page a tapped notification is worth opening."""

    def test_it_is_served_from_the_root(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_it_claims_the_whole_site(self, client):
        assert client.get("/sw.js").headers.get("Service-Worker-Allowed") == "/"

    def test_it_caches_nothing(self, client):
        """The objection phone.html has carried since it was written —
        offline caching would keep copies of what the phone's screen said —
        is answered by not caching, not by going without the file."""
        code = strip_js_comments(client.get("/sw.js").text)
        assert 'addEventListener("fetch"' not in code
        assert "caches" not in code
        assert "cache" not in code

    def test_it_opens_the_app_rather_than_a_new_window_when_it_can(self, client):
        body = client.get("/sw.js").text
        assert "notificationclick" in body
        assert "client.focus()" in body
        assert "openWindow" in body


class TestSubscribing:
    def test_a_browser_can_ask_to_be_told(self, client):
        r = client.post("/api/push/subscribe",
                        json={"subscription": SUBSCRIPTION})
        assert r.status_code == 200
        assert push.count() == 1

    def test_the_same_phone_twice_is_one_subscriber(self, client):
        """Re-subscribing replaces rather than doubles — otherwise every
        reinstall would earn her another copy of every notification."""
        for _ in range(3):
            client.post("/api/push/subscribe",
                        json={"subscription": SUBSCRIPTION})
        assert push.count() == 1

    def test_a_malformed_subscription_is_refused(self, client):
        r = client.post("/api/push/subscribe",
                        json={"subscription": {"endpoint": "x"}})
        assert r.status_code == 400
        assert push.count() == 0

    def test_it_can_be_turned_off_again(self, client):
        client.post("/api/push/subscribe", json={"subscription": SUBSCRIPTION})
        r = client.request("DELETE", "/api/push/subscribe",
                           json={"endpoint": SUBSCRIPTION["endpoint"]})
        assert r.status_code == 200
        assert push.count() == 0

    def test_the_stored_file_is_not_world_readable(self, client, store):
        client.post("/api/push/subscribe", json={"subscription": SUBSCRIPTION})
        assert (push.STORE_PATH.stat().st_mode & 0o077) == 0

    def test_the_subscription_is_remembered_against_its_device(self, client):
        client.post("/api/push/subscribe", json={"subscription": SUBSCRIPTION})
        (stored,) = push.subscriptions()
        assert stored["device"]


@needs_push
class TestTheKey:
    def test_it_is_generated_once_and_kept(self, store):
        first = push.public_key()
        assert first
        assert push.public_key() == first

    def test_it_is_the_shape_a_browser_can_subscribe_with(self, store):
        """`applicationServerKey` is the raw uncompressed EC point, base64url
        — 65 bytes, which is 87 characters. A PEM here fails in the browser
        with nothing useful said about why."""
        key = push.public_key()
        assert len(key) == 87
        assert key.startswith("B")
        assert "-----" not in key

    def test_the_private_half_is_never_served(self, client, store):
        body = client.get("/api/push/key").json()
        assert set(body) == {"key"}
        assert "PRIVATE" not in json.dumps(body)

    def test_the_key_file_is_not_world_readable(self, store):
        push.public_key()
        assert (push.KEY_PATH.stat().st_mode & 0o077) == 0

    def test_a_key_that_cannot_be_saved_is_no_key_at_all(self, tmp_path,
                                                         monkeypatch):
        """Found on the live machine: the file went to /etc, which is
        root-owned while these services run as `apt`, so the save failed
        silently and every call minted a FRESH pair. The browser subscribes
        against one and the server signs with another — every push rejected,
        nothing on this side saying why. Reporting no push is the honest
        answer."""
        monkeypatch.setattr(push, "KEY_PATH",
                            tmp_path / "nope" / "vapid.json")
        monkeypatch.setattr(push.Path, "mkdir",
                            lambda *a, **k: (_ for _ in ()).throw(OSError()))
        assert push.public_key() == ""

    def test_the_private_half_is_what_the_sender_can_actually_read(self):
        """Not PEM. pywebpush reads a string private key as base64url of the
        raw 32-byte scalar; hand it PEM and it fails with "ASN.1 parsing
        error: invalid length", which says nothing about what it wanted. That
        cost a real push, refused, into a log nobody was reading."""
        private = push.keys()["private"]
        assert "-----" not in private
        assert len(private) == 43          # 32 bytes, base64url, unpadded

    def test_a_key_stored_as_pem_is_converted_not_replaced(self, store):
        """The PUBLIC half was always right, so every browser subscribed
        against it stays subscribed. Regenerating would have silently
        unsubscribed her instead."""
        from py_vapid import Vapid01
        from py_vapid.utils import b64urlencode
        from cryptography.hazmat.primitives import serialization

        v = Vapid01()
        v.generate_keys()
        point = v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        old_public = b64urlencode(point)
        push.KEY_PATH.write_text(json.dumps(
            {"public": old_public,
             "private": v.private_pem().decode("utf-8")}), encoding="utf-8")

        pair = push.keys()
        assert pair["public"] == old_public       # the subscription survives
        assert "-----" not in pair["private"]     # and the sender can read it
        assert json.loads(push.KEY_PATH.read_text())["private"] == pair["private"]

    def test_the_subject_passes_the_signer_own_check(self):
        """py_vapid validates `sub` against its own regex and refuses a
        domain with no dot, reporting "Missing 'sub' from claims" — which is
        true of nothing: the claim was present and rejected for its shape.
        Asserted against the signer's own validator rather than a regex of
        mine, so it stays true if theirs changes."""
        from py_vapid import _check_sub

        assert _check_sub(push.VAPID_SUBJECT)

    def test_the_subject_is_not_an_address_apple_refuses(self, store):
        """Apple answers `.invalid` with 403 BadJwtToken — a message naming
        nothing, which sent this looking at keys, clocks and claim shapes.
        Established by posting tokens signed with four different subjects to
        a deliberately meaningless Apple endpoint: the refused one came back
        403, the accepted ones 400 BadDeviceToken, which is Apple saying the
        token was fine and only the endpoint was not."""
        assert ".invalid" not in push.VAPID_SUBJECT
        assert ".test" not in push.VAPID_SUBJECT
        assert ".example" not in push.VAPID_SUBJECT

    def test_the_subject_is_a_bare_origin_with_no_path(self, store):
        """py_vapid's regex allows an https origin and no path, and rejects
        anything else as "Missing 'sub'" — which is not what is wrong."""
        from urllib.parse import urlparse

        if push.VAPID_SUBJECT.startswith("https://"):
            assert urlparse(push.VAPID_SUBJECT).path in ("", "/")

    def test_the_notification_link_and_the_subject_share_one_origin(self):
        """Two copies of a hostname is one wrong hostname waiting to happen,
        and this one ends up on a lock screen."""
        from apt_log import macros

        assert macros.PORTAL_URL.startswith(push.PORTAL_ORIGIN)

    def test_it_lives_where_the_services_can_actually_write(self):
        """/var/lib/aptlog is the service user's; /etc/aptlog is root's."""
        assert "/var/lib/" in str(push.KEY_PATH)


@needs_push
class TestSending:
    def test_a_subscription_from_an_older_key_is_dropped_not_sent(self, store):
        """Apple answers 403 BadJwtToken for a signature from any key but the
        one the browser subscribed against — naming the symptom, not the
        cause. The key in force is recorded at subscribe time so this is a
        fact the sender can check instead of a send it wastes."""
        push.subscribe(SUBSCRIPTION)
        stored = push._load()
        (only,) = stored["subs"].values()
        only["key"] = "a-key-from-before"
        push._save(stored)

        with patch("pywebpush.webpush") as sent:
            assert push.send("t", "b") == 0
        assert not sent.called
        assert push.count() == 0

    def test_a_subscription_from_the_current_key_is_sent(self, store):
        push.subscribe(SUBSCRIPTION)
        with patch("pywebpush.webpush"):
            assert push.send("t", "b") == 1

    def test_subscribing_records_the_key_it_was_made_against(self, store):
        push.subscribe(SUBSCRIPTION)
        (stored,) = push.subscriptions()
        assert stored["key"] == push.public_key()

    def test_subscribing_does_not_deadlock_on_its_own_lock(self, store):
        """subscribe() holds the lock and needs the key; keys() takes the
        same lock, and it is not reentrant. Reading the key first is the
        whole fix, and this is the test that would notice it coming back."""
        import threading

        done = threading.Event()
        threading.Thread(
            target=lambda: (push.subscribe(SUBSCRIPTION), done.set()),
            daemon=True).start()
        assert done.wait(5), "subscribe() hung — the lock is not reentrant"

    def test_no_403_ever_drops_a_subscriber(self, store):
        """This code read Apple's BadJwtToken as "wrong key" and threw the
        subscription away — then the same refusal arrived for a subscription
        whose recorded key matched the current one exactly, on a machine with
        a synced clock and a verified keypair. The message does not mean only
        that, and acting on it as if it did cost a real subscriber and a trip
        back to the phone.

        What this side can PROVE stale is still dropped, before anything is
        sent. A refusal it cannot explain is reported and kept."""
        from pywebpush import WebPushException

        for reason in ("BadJwtToken", "VapidPkHashMismatch", "TooManyRequests"):
            push.subscribe(SUBSCRIPTION)

            class Refused:
                status_code = 403
                text = '{"reason":"%s"}' % reason

            with patch("pywebpush.webpush",
                       side_effect=WebPushException("no", response=Refused())):
                assert push.send("t", "b") == 0
            assert push.count() == 1, f"{reason} dropped a subscriber"

    def test_the_token_does_not_sit_on_the_24_hour_edge(self, store):
        """py_vapid would use exactly 24 hours, which is the outside edge of
        what the spec allows and what Apple accepts. An edge is a bad place
        to sit when the only symptom is a message that names nothing."""
        import time as clock

        push.subscribe(SUBSCRIPTION)
        with patch("pywebpush.webpush") as sent:
            push.send("t", "b")
        exp = sent.call_args.kwargs["vapid_claims"]["exp"]
        ahead = exp - clock.time()
        assert 0 < ahead < 24 * 3600 - 3600

    def test_nothing_subscribed_is_not_an_error(self, store):
        assert push.send("t", "b") == 0

    def test_it_reaches_every_subscriber(self, store):
        push.subscribe(SUBSCRIPTION)
        with patch("pywebpush.webpush") as sent:
            assert push.send("Title", "Body", url="/app") == 1
        payload = json.loads(sent.call_args.kwargs["data"])
        assert payload == {"title": "Title", "body": "Body",
                           "url": "/app", "tag": "aptlog"}

    def test_a_retired_endpoint_is_forgotten_rather_than_retried(self, store):
        """A phone that reinstalled the app is not a phone that is briefly
        unreachable, and pushing at it forever is how a store fills with
        addresses nobody is at."""
        from pywebpush import WebPushException

        push.subscribe(SUBSCRIPTION)

        class Gone:
            status_code = 410

        with patch("pywebpush.webpush",
                   side_effect=WebPushException("gone", response=Gone())):
            assert push.send("t", "b") == 0
        assert push.count() == 0

    def test_an_ordinary_failure_keeps_the_subscriber(self, store):
        from pywebpush import WebPushException

        push.subscribe(SUBSCRIPTION)

        class Down:
            status_code = 503

        with patch("pywebpush.webpush",
                   side_effect=WebPushException("later", response=Down())):
            assert push.send("t", "b") == 0
        assert push.count() == 1

    def test_a_push_that_cannot_be_sent_never_raises(self, store):
        push.subscribe(SUBSCRIPTION)
        with patch("pywebpush.webpush", side_effect=OSError("no network")):
            assert push.send("t", "b") == 0


@needs_push
class TestTheCodeNoticeGoesBothWays:
    """Push reaches only phones that subscribed and only where iOS granted
    it; the relay reaches whoever configured it, from a machine that does not
    care whether this portal is healthy. Neither is enough alone."""

    def test_both_roads_are_taken(self, store):
        from apt_log import macros

        with patch("apt_log.push.send", return_value=1) as pushed, \
             patch("apt_log.notify.send", return_value=True) as relayed:
            macros._say_the_code_is_waiting()
        assert pushed.called and relayed.called

    def test_the_push_opens_the_app_not_a_web_page(self, store):
        from apt_log import macros

        with patch("apt_log.push.send", return_value=1) as pushed, \
             patch("apt_log.notify.send"):
            macros._say_the_code_is_waiting()
        assert pushed.call_args.kwargs["url"] == "/app"

    def test_a_failing_push_does_not_stop_the_relay(self, store):
        from apt_log import macros

        with patch("apt_log.push.send", side_effect=RuntimeError("boom")), \
             patch("apt_log.notify.send") as relayed:
            macros._say_the_code_is_waiting()
        assert relayed.called
