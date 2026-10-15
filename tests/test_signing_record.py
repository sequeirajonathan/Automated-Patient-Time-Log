"""The signing recorder: keeps the moment, and never gets in its way."""

import io
import json

from PIL import Image

import pytest

from apt_log import signing_record as rec


@pytest.fixture(autouse=True)
def _pictures_written_inline(monkeypatch):
    """The JPEG is written on a thread in service; inline here so a test
    can look for it the moment the call returns."""
    monkeypatch.setattr(rec, "_spawn", lambda fn, *args: fn(*args))

PAGE = '<node resource-id="com.tellus.evv.v2:id/compose_view_visit_sign_off"/>'
PAD = '<node resource-id="com.tellus.evv.v2:id/buttonClearSignature"/>'
LIST = '<node resource-id="com.tellus.evv.v2:id/container"/>'
FOCUS = "com.tellus.evv.v2/com.tellus.evv.activities.DashboardActivity"


def _png(colour=(10, 20, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 80), colour).save(out, "PNG")
    return out.getvalue()


def _sessions(root):
    return sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []


def _events(d):
    return [json.loads(line)
            for line in (d / "events.jsonl").read_text().splitlines()]


class TestWhatIsKept:
    def test_nothing_is_kept_away_from_the_signing_moment(self, tmp_path):
        rec.note_frame(_png(), LIST, FOCUS, root=tmp_path, now=1000.0)
        assert _sessions(tmp_path) == []

    def test_the_page_and_the_pad_are_both_kept(self, tmp_path):
        rec.note_frame(_png(), PAGE, FOCUS, density=300,
                       root=tmp_path, now=1000.0)
        rec.note_frame(_png((200, 0, 0)), PAD, FOCUS, density=300,
                       root=tmp_path, now=1002.0)
        [d] = _sessions(tmp_path)
        assert len(list(d.glob("*.jpg"))) == 2
        assert len(list(d.glob("*.xml"))) == 2
        lines = _events(d)
        assert [e["signing"] for e in lines] == [True, True]
        assert lines[0]["density"] == 300

    def test_what_confirm_led_to_is_kept_for_the_tail(self, tmp_path):
        """THE POINT OF THE RECORDER: the seconds after Confirmar firma are
        the ones nobody could see."""
        rec.note_frame(_png(), PAD, FOCUS, root=tmp_path, now=1000.0)
        rec.note_frame(_png((0, 200, 0)), LIST, FOCUS, root=tmp_path,
                       now=1000.0 + rec.TAIL - 1)
        rec.note_frame(_png((0, 0, 200)), LIST, FOCUS, root=tmp_path,
                       now=1000.0 + rec.TAIL + 1)
        [d] = _sessions(tmp_path)
        assert [e["signing"] for e in _events(d)] == [True, False]

    def test_an_unchanged_picture_is_written_once(self, tmp_path):
        for t in range(3):
            rec.note_frame(_png(), PAGE, FOCUS, root=tmp_path, now=1000.0 + t)
        [d] = _sessions(tmp_path)
        assert len(list(d.glob("*.jpg"))) == 1
        assert len(_events(d)) == 3

    def test_typed_text_never_reaches_the_disk(self, tmp_path):
        tree = PAGE + '<node class="android.widget.EditText" text="hunter2"/>'
        rec.note_frame(_png(), tree, FOCUS, root=tmp_path, now=1000.0)
        [d] = _sessions(tmp_path)
        [xml] = d.glob("*.xml")
        assert "hunter2" not in xml.read_text()

    def test_a_long_gap_opens_a_new_session(self, tmp_path):
        rec.note_frame(_png(), PAGE, FOCUS, root=tmp_path, now=1000.0)
        rec.note_frame(_png(), PAGE, FOCUS, root=tmp_path,
                       now=1000.0 + rec.GAP + 5)
        assert len(_sessions(tmp_path)) == 2

    def test_frames_are_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rec, "MAX_FRAMES", 2)
        for t in range(5):
            rec.note_frame(_png(), PAGE, FOCUS, root=tmp_path, now=1000.0 + t)
        [d] = _sessions(tmp_path)
        assert len(_events(d)) == 2


class TestPresses:
    def test_a_press_inside_the_moment_is_recorded(self, tmp_path):
        rec.note_frame(_png(), PAD, FOCUS, root=tmp_path, now=1000.0)
        rec.note_press("tap", root=tmp_path, now=1003.0, rid="", xy=[5, 6],
                       at="a detail named like the timestamp")
        [d] = _sessions(tmp_path)
        presses = [e for e in _events(d) if "press" in e]
        assert [p["press"] for p in presses] == ["tap"]
        assert presses[0]["xy"] == [5, 6]
        from datetime import datetime

        assert presses[0]["at"] == datetime.fromtimestamp(1003.0).isoformat(
            timespec="milliseconds"), "a detail clobbered the timestamp"

    def test_a_press_outside_any_moment_is_not(self, tmp_path):
        rec.note_frame(_png(), PAD, FOCUS, root=tmp_path, now=1000.0)
        rec.note_press("tap", root=tmp_path, now=1000.0 + rec.TAIL + 60)
        [d] = _sessions(tmp_path)
        assert not [e for e in _events(d) if "press" in e]

    def test_no_session_at_all_is_silent(self, tmp_path):
        rec.note_press("tap", root=tmp_path, now=1000.0)
        assert _sessions(tmp_path) == []


class TestNeverInTheWay:
    def test_a_broken_disk_does_not_raise(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        rec.note_frame(_png(), PAGE, FOCUS, root=blocker / "sub", now=1000.0)
        rec.note_press("tap", root=blocker / "sub", now=1000.0)

    def test_an_unreadable_picture_does_not_raise(self, tmp_path):
        rec.note_frame(b"not a png", PAGE, FOCUS, root=tmp_path, now=1000.0)

    def test_the_feed_records_a_signing_frame(self, tmp_path, monkeypatch):
        """Wired where frames are written, so every captured frame passes
        through it."""
        import inspect

        from apt_log import feed

        assert "signing_record.note_frame" in inspect.getsource(feed.write_frame)
        assert "signing_record.note_press" in inspect.getsource(feed.tap)
