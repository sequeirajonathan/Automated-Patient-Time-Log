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

    def test_it_lives_where_the_services_can_actually_write(self):
        """/var/lib/aptlog is the service user's; /etc/aptlog is root's."""
        assert "/var/lib/" in str(push.KEY_PATH)


@needs_push
class TestSending:
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
