"""Replaying a locally-drawn signature onto the app's canvas.

The property that matters most: the replay draws or refuses. It never taps a
button, never presses the app's save, and every point it produces is inside
the one canvas rectangle it found. A wrong rectangle is ink on the wrong
control, which is why zero candidates and two candidates are the same answer.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from apt_log import sign

CANVAS = ('<node class="android.view.View" resource-id="com.x:id/signature_view"'
          ' text="" clickable="false" bounds="[100,200][1500,650]" />')
CHROME = ('<node class="android.widget.Button" resource-id="com.x:id/btn_save"'
          ' text="Salvar" clickable="true" bounds="[0,660][200,720]" />'
          '<node class="android.widget.TextView" resource-id="com.x:id/lbl"'
          ' text="Firma del cuidador" clickable="false" bounds="[0,0][1600,80]" />')

STROKES = [[[0.1, 0.2, 0], [0.5, 0.5, 40], [0.9, 0.3, 90]]]


class TestValidate:
    def test_a_signature_shape_passes(self):
        assert sign.validate(STROKES) is True

    def test_the_pads_object_shape_also_passes(self):
        assert sign.validate([{"width": 2, "points": [[0.1, 0.2, 0]]}]) is True

    @pytest.mark.parametrize("bad", [
        None, [], "strokes", [[]], [[[1.5, 0.5, 0]]], [[[0.5]]],
        [[["x", 0.5, 0]]], [[[0.5, -0.1, 0]]],
    ])
    def test_everything_else_is_refused(self, bad):
        assert sign.validate(bad) is False

    def test_a_flood_of_points_is_refused(self):
        flood = [[[0.5, 0.5, i] for i in range(sign.MAX_POINTS + 1)]]
        assert sign.validate(flood) is False


class TestFinder:
    def test_one_canvas_is_found_by_its_id(self):
        bounds, why = sign.find_canvas(CANVAS + CHROME)
        assert bounds == [100, 200, 1500, 650] and why == ""

    def test_a_nameless_canvas_is_found_by_its_shape(self):
        anon = CANVAS.replace('resource-id="com.x:id/signature_view"',
                              'resource-id=""')
        bounds, why = sign.find_canvas(anon + CHROME)
        assert bounds == [100, 200, 1500, 650] and why == ""

    def test_an_ordinary_screen_is_a_refusal_not_a_guess(self):
        """The home screen has big layouts and no canvas. Layouts are
        containers, not drawing surfaces, and must not match."""
        home = ('<node class="android.widget.RelativeLayout" resource-id=""'
                ' text="" clickable="true" bounds="[0,538][720,752]" />'
                '<node class="android.widget.TextView" resource-id="com.x:id/t"'
                ' text="Horario" clickable="false" bounds="[0,460][720,538]" />')
        bounds, why = sign.find_canvas(home)
        assert bounds is None and why == "no_canvas"

    def test_two_candidates_refuse_rather_than_pick(self):
        second = CANVAS.replace("signature_view", "signature_view2").replace(
            "[100,200]", "[100,700]").replace("[1500,650]", "[1500,1150]")
        bounds, why = sign.find_canvas(CANVAS + second)
        assert bounds is None and why == "ambiguous"

    def test_a_small_blank_view_is_not_a_canvas(self):
        tiny = ('<node class="android.view.View" resource-id="" text=""'
                ' clickable="false" bounds="[0,0][50,50]" />')
        bounds, why = sign.find_canvas(tiny + CHROME)
        assert bounds is None and why == "no_canvas"


class TestPaths:
    def test_every_point_lands_inside_the_canvas(self):
        corners = [[[0.0, 0.0, 0], [1.0, 1.0, 10], [1.0, 0.0, 20],
                    [0.0, 1.0, 30]]]
        for path in sign.build_paths(corners, [100, 200, 1500, 650], 2.2):
            for x, y in path:
                assert 100 <= x <= 1500 and 200 <= y <= 650

    def test_the_inset_keeps_ink_off_the_border(self):
        corners = [[[0.0, 0.0, 0]]]
        (x, y), = sign.build_paths(corners, [0, 0, 1000, 500], 2.0)[0]
        assert x > 0 and y > 0

    def test_the_shape_is_scaled_uniformly(self):
        """A square drawn on the pad stays square on the canvas — one scale
        for both axes, whatever the two rectangles' shapes."""
        square = [[[0.2, 0.2, 0], [0.8, 0.2, 1], [0.8, 0.8, 2], [0.2, 0.8, 3]]]
        (a, b, c, d), = sign.build_paths(square, [0, 0, 2000, 500], 1.0)
        assert abs((b[0] - a[0]) - (c[1] - b[1])) <= 1

    def test_stroke_structure_survives(self):
        two = [[[0.1, 0.1, 0], [0.2, 0.2, 1]], [[0.8, 0.8, 0]]]
        paths = sign.build_paths(two, [0, 0, 1000, 500])
        assert len(paths) == 2 and len(paths[0]) == 2 and len(paths[1]) == 1


class TestRequestLifecycle:
    def test_round_trip(self, tmp_path):
        target = tmp_path / "req.json"
        rid = sign.request(STROKES, aspect=2.2, path=target)
        taken = sign.take_request(target)
        assert taken["id"] == rid and taken["aspect"] == 2.2
        assert not target.exists(), "strokes must not persist (REQ-10.6)"

    def test_a_stale_request_is_ignored(self, tmp_path):
        target = tmp_path / "req.json"
        sign.request(STROKES, path=target)
        payload = json.loads(target.read_text())
        payload["at"] = time.time() - sign.REQUEST_MAX_AGE - 1
        target.write_text(json.dumps(payload))
        assert sign.take_request(target) is None
        assert not target.exists()

    def test_garbage_cannot_be_requested(self, tmp_path):
        with pytest.raises(ValueError):
            sign.request([[[5, 5, 0]]], path=tmp_path / "req.json")


class TestExecute:
    def _payload(self):
        return {"id": "abc", "strokes": STROKES, "aspect": 2.2}

    def test_it_draws_on_the_canvas_and_only_draws(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.hhaexchange.caregiver"
        driver.page_source = CANVAS + CHROME
        performed = []
        with patch.object(sign, "_perform",
                          side_effect=lambda _d, paths: performed.extend(paths)), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.execute(self._payload(), tmp_path / "s.json")
        assert status.state == "done"
        assert performed, "nothing was drawn"
        for x, y in [p for path in performed for p in path]:
            assert 100 <= x <= 1500 and 200 <= y <= 650
        driver.find_element.assert_not_called()   # no button was looked for,
        # let alone pressed: the save stays her tap.

    def test_the_wrong_app_in_front_refuses(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.android.launcher3"
        with patch.object(sign, "_perform") as perform, \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.execute(self._payload(), tmp_path / "s.json")
        assert status.state == "failed" and status.reason == "wrong_app"
        perform.assert_not_called()

    def test_no_canvas_refuses_before_any_gesture(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.hhaexchange.caregiver"
        driver.page_source = CHROME
        with patch.object(sign, "_perform") as perform, \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.execute(self._payload(), tmp_path / "s.json")
        assert status.state == "failed" and status.reason == "no_canvas"
        perform.assert_not_called()

    def test_the_status_carries_a_digest_never_the_strokes(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.hhaexchange.caregiver"
        driver.page_source = CANVAS + CHROME
        with patch.object(sign, "_perform"), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            sign.execute(self._payload(), tmp_path / "s.json")
        raw = (tmp_path / "s.json").read_text()
        assert "0.1" not in raw and "strokes" not in raw
        assert sign.read_status(tmp_path / "s.json").digest


class TestCanvasClassHints:
    """The only replay ever attempted against the real screen refused with
    no_canvas — consistent with a canvas shipped as a CUSTOM CLASS with no
    resource-id (SignatureView, SignaturePad), which the id hints never
    see and the bare-View rule rejects. The class's own name now counts,
    and a refusal keeps the screen's textless structure so one failed
    attempt hands over the fix."""

    def test_a_custom_signature_class_is_the_canvas(self):
        xml = ('<node class="com.hhaexchange.caregiver.ui.SignatureView" '
               'bounds="[100,60][1500,660]" clickable="true"/>'
               '<node class="android.widget.Button" text="Salvar" '
               'bounds="[20,600][120,660]"/>')
        bounds, reason = sign.find_canvas(xml)
        assert reason == ""
        assert bounds == [100, 60, 1500, 660]

    def test_a_refusal_records_the_structure_without_text(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(sign, "DEBUG_PATH", tmp_path / "sign-debug.json")
        xml = ('<node class="android.widget.TextView" text="CARIDAD R" '
               'bounds="[10,10][200,40]"/>')
        bounds, reason = sign.find_canvas(xml)
        assert bounds is None and reason == "no_canvas"
        doc = json.loads((tmp_path / "sign-debug.json").read_text())
        assert doc["reason"] == "no_canvas"
        assert doc["nodes"][0]["cls"] == "TextView"
        assert "CARIDAD" not in json.dumps(doc)


class TestNestedSignatureWrappers:
    """The live refusal, verbatim: two full-page layout wrappers with
    "signature" in their ids around the one real canvas. A wrapper that
    wholly contains another candidate is wrapping, not drawing."""

    def test_the_innermost_candidate_is_the_canvas(self):
        xml = ('<node class="android.widget.FrameLayout" '
               'resource-id="app:id/layout_tab_content_signature_skip" '
               'bounds="[0,90][720,1568]"/>'
               '<node class="android.widget.FrameLayout" '
               'resource-id="app:id/layout_tab_content_signature" '
               'bounds="[0,90][720,1568]"/>'
               '<node class="android.widget.FrameLayout" '
               'resource-id="app:id/gesturePatientSignature" '
               'bounds="[0,492][720,1142]"/>')
        bounds, reason = sign.find_canvas(xml)
        assert reason == ""
        assert bounds == [0, 492, 720, 1142]


class TestStrokeSeparation:
    """The first live replay drew a connector line between strokes: a touch
    pointer cannot hover, so the positioning move was not guaranteed to land
    before the down, and ACTION_DOWN fired at the previous stroke's end.
    The sequence that prevents it: position, settle, down, settle, draw —
    and a real gap between strokes so the canvas sees distinct gestures."""

    @staticmethod
    def _replay(paths):
        driver = MagicMock()
        calls = []
        driver.execute = lambda cmd, params=None: calls.append(params)
        with patch.object(sign.time, "sleep") as slept:
            sign._perform(driver, paths)
        return calls, slept

    @staticmethod
    def _pointer_actions(call):
        (device,) = [d for d in call["actions"] if d["type"] == "pointer"]
        return device["actions"]

    def test_each_stroke_is_its_own_performed_chain(self):
        calls, _ = self._replay([[(10, 10), (20, 20)], [(50, 50), (60, 60)]])
        assert len(calls) == 2

    def test_the_pen_settles_between_positioning_and_touching(self):
        calls, _ = self._replay([[(10, 10), (20, 20)]])
        kinds = [a["type"] for a in self._pointer_actions(calls[0])]
        assert kinds[:4] == ["pointerMove", "pause", "pointerDown", "pause"]
        assert kinds[-2:] == ["pause", "pointerUp"]

    def test_moves_are_fast_not_the_w3c_default(self):
        calls, _ = self._replay([[(10, 10), (20, 20), (30, 30)]])
        moves = [a for a in self._pointer_actions(calls[0])
                 if a["type"] == "pointerMove"]
        assert all(m["duration"] == sign.MOVE_MS for m in moves)

    def test_strokes_are_separated_by_a_gap_in_time(self):
        _, slept = self._replay([[(10, 10)], [(20, 20)], [(30, 30)]])
        assert slept.call_count == 2
        slept.assert_called_with(sign.STROKE_GAP)


# The legacy caregiver-signature page, as photographed live: a portrait
# activity whose whole UI is drawn turned a quarter turn — the baseline up
# the left edge, "Firma del cuidador" down the right, name label sideways.
# Rotated single-line labels sit in boxes taller than they are wide.
SIDEWAYS_LABELS = (
    '<node class="android.widget.TextView" resource-id="" clickable="false"'
    ' text="Firma del cuidador" bounds="[700,90][716,160]" />'
    '<node class="android.widget.TextView" resource-id="" clickable="false"'
    ' text="Sadia Amselem" bounds="[20,95][36,160]" />')
SIDEWAYS_CANVAS = (
    '<node class="android.view.View" resource-id="app:id/gestureSignature"'
    ' text="" clickable="false" bounds="[28,90][700,1560]" />')


class TestSidewaysPresentation:
    def test_rotated_labels_on_a_portrait_screen_mean_sideways(self):
        assert sign.presentation_rotated(SIDEWAYS_CANVAS + SIDEWAYS_LABELS)

    def test_an_ordinary_portrait_page_is_not_sideways(self):
        assert not sign.presentation_rotated(CANVAS + CHROME)

    def test_one_tall_label_alone_is_not_enough(self):
        one = SIDEWAYS_LABELS.split("/>")[0] + "/>"
        assert not sign.presentation_rotated(SIDEWAYS_CANVAS + one)

    def test_a_truly_landscape_screen_needs_no_turn(self):
        wide = ('<node class="android.view.View" resource-id="a:id/signature"'
                ' text="" clickable="false" bounds="[0,0][1600,720]" />')
        assert not sign.presentation_rotated(wide + SIDEWAYS_LABELS)


class TestRotatedPaths:
    def test_a_pad_horizontal_stroke_runs_down_the_device(self):
        """Writing left-to-right on the pad must travel top-to-bottom on the
        device, holding one x — that is the stroke the sideways page reads
        as horizontal."""
        line = [[[0.0, 0.5, 0], [1.0, 0.5, 1]]]
        (a, b), = sign.build_paths(line, [0, 0, 720, 1440],
                                   aspect=2.0, rotate=True)
        assert a[0] == b[0]
        assert a[1] < b[1]

    def test_pad_top_lands_on_the_device_right(self):
        """The page's caption runs down the device's right edge — pad-top
        (v=0) must map nearer that edge than pad-bottom (v=1)."""
        tops = [[[0.5, 0.0, 0]], [[0.5, 1.0, 1]]]
        (top,), (bottom,) = sign.build_paths(tops, [0, 0, 720, 1440],
                                             aspect=2.0, rotate=True)
        assert top[0] > bottom[0]

    def test_rotation_still_confines_ink_to_the_canvas(self):
        corners = [[[0.0, 0.0, 0], [1.0, 1.0, 1], [1.0, 0.0, 2],
                    [0.0, 1.0, 3]]]
        for path in sign.build_paths(corners, [28, 90, 700, 1560],
                                     aspect=2.2, rotate=True):
            for x, y in path:
                assert 28 <= x <= 700 and 90 <= y <= 1560

    def test_execute_turns_strokes_for_the_sideways_page(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.hhaexchange.caregiver"
        driver.page_source = SIDEWAYS_CANVAS + SIDEWAYS_LABELS
        built = {}
        real = sign.build_paths
        with patch.object(sign, "_perform"), \
             patch.object(sign, "build_paths",
                          side_effect=lambda *a, **kw: built.update(kw) or
                          real(*a, **kw)), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.execute({"id": "r", "strokes": STROKES,
                                   "aspect": 2.2}, tmp_path / "s.json")
        assert status.state == "done"
        assert built.get("rotate") is True

    def test_execute_leaves_an_upright_page_unturned(self, tmp_path):
        driver = MagicMock()
        driver.current_package = "com.hhaexchange.caregiver"
        driver.page_source = CANVAS + CHROME
        built = {}
        real = sign.build_paths
        with patch.object(sign, "_perform"), \
             patch.object(sign, "build_paths",
                          side_effect=lambda *a, **kw: built.update(kw) or
                          real(*a, **kw)), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            sign.execute({"id": "r", "strokes": STROKES, "aspect": 2.2},
                         tmp_path / "s.json")
        assert built.get("rotate") is False
