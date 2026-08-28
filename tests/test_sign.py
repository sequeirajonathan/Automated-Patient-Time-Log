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


@pytest.fixture(autouse=True)
def _debug_dump_stays_out_of_production(tmp_path, monkeypatch):
    """Refusals in these tests must not overwrite the live evidence file —
    the deploy gate runs this suite on the Pi, and a test refusal clobbered
    the recording of a real one mid-field-test."""
    monkeypatch.setattr(sign, "DEBUG_PATH", tmp_path / "sign-debug.json")


class TestValidate:
    def test_a_signature_shape_passes(self):
        assert sign.validate(STROKES) is True

    def test_the_pads_object_shape_also_passes(self):
        assert sign.validate([{"width": 2, "points": [[0.1, 0.2, 0]]}]) is True

    # -0.1 and 1.04 USED to be here and are now accepted on purpose: a
    # finger sliding a little past the pad's edge is still signing, and
    # refusing the whole payload for one such point bounced entire
    # signatures. See TestAFingerPastTheEdge. What stays refused is a
    # coordinate no pad could produce.
    @pytest.mark.parametrize("bad", [
        None, [], "strokes", [[]], [[[1.5, 0.5, 0]]], [[[0.5]]],
        [[["x", 0.5, 0]]], [[[0.5, -0.6, 0]]],
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
        measured = []

        def stub(_d, paths, bounds=None, serial=None):
            performed.extend(paths)
            measured.append(bounds)
            # `_perform` answers with the canvas readings either side of the
            # replay and a per-stroke tally, so the log can say whether the
            # ink landed rather than leaving it to be guessed at from a
            # screenshot afterwards.
            return (0, 900, "+900/1")

        with patch.object(sign, "_perform", side_effect=stub), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.execute(self._payload(), tmp_path / "s.json")
        assert status.state == "done"
        assert performed, "nothing was drawn"
        # The canvas the finder produced is also the window each stroke is
        # measured in; a replay that did not pass it would lose the check
        # that a stroke actually landed.
        assert measured and measured[0], "the canvas was not handed over"
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
        written = json.loads((tmp_path / "s.json").read_text())
        # Every field EXCEPT the timestamp: an ISO timestamp contains "0.1"
        # whenever the second ends in 0 and the microseconds begin with 1
        # ("…:30.123456"), so asserting against the raw file failed about one
        # run in a hundred — on a suite that is the deploy gate, which turns a
        # coincidence of the clock into a rolled-back good deploy.
        body = json.dumps({k: v for k, v in written.items() if k != "at"})
        assert "0.1" not in body, "a stroke coordinate reached the status"
        assert "strokes" not in body
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
        """One chain for the whole signature was tried against the real
        device and UiAutomator2 refuses it — a chain holding more than one
        touch cycle comes back "Unable to perform W3C actions". A stroke is
        a chain; the SEAM between chains is what had to be made safe."""
        calls, _ = self._replay([[(10, 10), (20, 20)], [(50, 50), (60, 60)]])
        chains = [c for c in calls if c and "actions" in c]
        assert len(chains) == 2

    def test_the_pen_settles_between_positioning_and_touching(self):
        calls, _ = self._replay([[(10, 10), (20, 20)]])
        chain = next(c for c in calls if c and "actions" in c)
        kinds = [a["type"] for a in self._pointer_actions(chain)]
        assert kinds[:4] == ["pointerMove", "pause", "pointerDown", "pause"]
        assert kinds[-2:] == ["pause", "pointerUp"]

    def test_moves_are_fast_not_the_w3c_default(self):
        calls, _ = self._replay([[(10, 10), (20, 20), (30, 30)]])
        chain = next(c for c in calls if c and "actions" in c)
        moves = [a for a in self._pointer_actions(chain)
                 if a["type"] == "pointerMove"]
        assert all(m["duration"] == sign.MOVE_MS for m in moves)

    def test_every_stroke_is_one_touch_down_and_up(self):
        calls, _ = self._replay([[(10, 10)], [(20, 20)], [(30, 30)]])
        for chain in [c for c in calls if c and "actions" in c]:
            kinds = [a["type"] for a in self._pointer_actions(chain)]
            assert kinds.count("pointerDown") == 1
            assert kinds.count("pointerUp") == 1

    def test_strokes_are_separated_by_a_gap_in_time(self):
        _, slept = self._replay([[(10, 10)], [(20, 20)], [(30, 30)]])
        assert slept.call_count == 2
        slept.assert_called_with(sign.STROKE_GAP)

    def test_the_pointer_is_released_between_strokes(self):
        """perform() returns when the driver ACCEPTED the chain, not when the
        phone finished injecting it. Left unreleased, the next chain inherits
        a half-alive input of the same id — the state a dropped pen-down
        comes out of, and the shape of the signature that lost its middle
        stroke in the field."""
        driver = MagicMock()
        releases = []
        driver.execute = lambda cmd, params=None: releases.append(cmd)
        with patch.object(sign.time, "sleep"):
            sign._perform(driver, [[(10, 10)], [(20, 20)]])
        assert sum(1 for c in releases if "CLEAR" in str(c).upper()) >= 2

    def test_the_gap_outlasts_a_slow_frame(self):
        """Sixteen milliseconds is one frame at 60Hz; the gap has to survive
        several of them on a phone that is busy."""
        assert sign.STROKE_GAP >= 0.4
        assert sign.PEN_SETTLE >= 0.1


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


class TestSidewaysCanvasApps:
    """The tree shows NONE of the legacy app's quarter turn: the sideways
    captions are laid out as ordinary wide boxes and rotated only in the
    drawing pass uiautomator cannot see — a real signature replayed
    unturned past the label heuristic (seen live, first field test). For
    these apps, a signature MOMENT is the evidence: the finder accepts the
    screen and the canvas dominates it. A page that merely REMEMBERS its
    signatures (the completed visit detail keeps their wrappers in its
    tree) held the peek sideways on an upright page — marks alone are not
    a moment."""

    PORTRAIT_CANVAS = (
        '<node class="android.widget.FrameLayout" '
        'resource-id="app:id/gestureSignature" text="" '
        'bounds="[28,90][700,1560]" />'
        '<node class="android.widget.TextView" text="Sadia Amselem" '
        'bounds="[0,1151][358,1164]" />')

    LEGACY = "com.hhaexchange.caregiver"

    def test_a_portrait_canvas_in_the_legacy_app_is_sideways(self):
        assert sign.sideways(self.PORTRAIT_CANVAS, package=self.LEGACY) \
            is True

    def test_the_tab_variant_canvas_is_also_sideways(self):
        tab = ('<node class="android.widget.FrameLayout" '
               'resource-id="app:id/gesturePatientSignature" text="" '
               'bounds="[0,492][720,1142]" />'
               '<node class="android.widget.TextView" text="footer" '
               'bounds="[0,1550][720,1568]" />')
        assert sign.sideways(tab, package=self.LEGACY) is True

    def test_the_truly_landscape_variant_needs_no_turn(self):
        wide = ('<node class="android.view.View" '
                'resource-id="a:id/signature" text="" '
                'bounds="[0,0][1600,720]" />')
        assert sign.sideways(wide, package=self.LEGACY) is False

    def test_other_apps_are_not_turned_by_the_canvas_alone(self):
        assert sign.sideways(self.PORTRAIT_CANVAS,
                             package="com.hhaexchange.uma") is False

    def test_no_canvas_means_no_turn(self):
        page = ('<node class="android.widget.Button" text="Registrar" '
                'bounds="[5,148][354,174]" />'
                '<node class="android.widget.TextView" text="footer" '
                'bounds="[0,1550][720,1568]" />')
        assert sign.sideways(page, package=self.LEGACY) is False

    def test_an_ambiguous_screen_is_not_a_signature_moment(self):
        """Two side-by-side canvases (the completed page remembering both
        signatures) refuse the finder — and an upright peek follows."""
        two = ('<node class="android.widget.FrameLayout" '
               'resource-id="app:id/gestureSignature" text="" '
               'bounds="[10,200][710,800]" />'
               '<node class="android.widget.FrameLayout" '
               'resource-id="app:id/gesturePatientSignature" text="" '
               'bounds="[10,900][710,1500]" />')
        assert sign.sideways(two, package=self.LEGACY) is False

    def test_a_small_remembered_thumbnail_does_not_turn_the_page(self):
        thumb = ('<node class="android.widget.FrameLayout" '
                 'resource-id="app:id/signature_preview" text="" '
                 'bounds="[500,1400][700,1500]" />'
                 '<node class="android.widget.TextView" text="footer" '
                 'bounds="[0,1550][720,1568]" />')
        assert sign.sideways(thumb, package=self.LEGACY) is False

    def test_feed_cadence_refusals_do_not_touch_the_evidence_file(self,
                                                                  tmp_path):
        import unittest.mock as mock
        with mock.patch.object(sign, "_dump_refusal") as dump:
            sign.find_canvas("<node class='x' bounds='[0,0][720,100]'/>",
                             dump=False)
        dump.assert_not_called()


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
        # `_perform` answers (ink before, ink after) so the log can say
        # whether the ink landed; a bare mock unpacks to nothing.
        with patch.object(sign, "_perform", return_value=(0, 900, "+900/1")), \
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
        with patch.object(sign, "_perform", return_value=(0, 900, "+900/1")), \
             patch.object(sign, "build_paths",
                          side_effect=lambda *a, **kw: built.update(kw) or
                          real(*a, **kw)), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            sign.execute({"id": "r", "strokes": STROKES, "aspect": 2.2},
                         tmp_path / "s.json")
        assert built.get("rotate") is False


class TestAppButtons:
    """The signature pages draw Borrar and Salvar in a rotated pass the
    tree never shows — the field test's recordings hold the full
    underlying visit page and not one signature control. Their place is
    derived from the canvas the finder CAN see: the strip left of it, at
    its foot. A press is her tap relayed; off the signature moment there
    is nothing to press and the answer is a refusal."""

    LEGACY = "com.hhaexchange.caregiver"
    PAGE = ('<node class="android.widget.FrameLayout" '
            'resource-id="app:id/gestureSignature" text="" '
            'bounds="[28,90][700,1560]" />'
            '<node class="android.widget.TextView" text="Sadia Amselem" '
            'bounds="[0,1151][358,1164]" />')

    def test_targets_sit_in_the_strip_at_the_canvas_foot(self):
        t = sign.button_targets(self.PAGE, self.LEGACY)
        assert t is not None
        for target in t.values():
            x, y = target["point"]     # this screen names neither button
            assert 0 < x < 28          # the strip left of the canvas
            assert y < 1560            # above the canvas foot
        # Borrar rides above Salvar
        assert t["clear"]["point"][1] < t["confirm"]["point"][1]

    def test_off_the_moment_there_is_nothing_to_press(self):
        assert sign.button_targets(self.PAGE, "com.hhaexchange.uma") is None
        assert sign.button_targets(
            '<node class="android.widget.Button" text="Registrar" '
            'bounds="[5,148][354,174]" />'
            '<node class="android.widget.TextView" text="footer" '
            'bounds="[0,1550][720,1568]" />', self.LEGACY) is None

    def test_a_canvas_flush_to_the_edge_leaves_no_strip(self):
        flush = ('<node class="android.widget.FrameLayout" '
                 'resource-id="app:id/gestureSignature" text="" '
                 'bounds="[0,90][720,1560]" />')
        assert sign.button_targets(flush, self.LEGACY) is None

    def test_action_round_trip(self, tmp_path):
        target = tmp_path / "act.json"
        rid = sign.request_action("clear", path=target)
        taken = sign.take_action(target)
        assert taken["id"] == rid and taken["kind"] == "clear"
        assert not target.exists()

    def test_a_stale_action_is_ignored(self, tmp_path):
        target = tmp_path / "act.json"
        sign.request_action("confirm", path=target)
        payload = json.loads(target.read_text())
        payload["at"] = time.time() - sign.ACTION_MAX_AGE - 1
        target.write_text(json.dumps(payload))
        assert sign.take_action(target) is None

    def test_garbage_kinds_are_refused(self, tmp_path):
        with pytest.raises(ValueError):
            sign.request_action("press_save_forever",
                                path=tmp_path / "act.json")

    def test_do_action_presses_exactly_the_target(self, tmp_path):
        driver = MagicMock()
        driver.current_package = self.LEGACY
        driver.page_source = self.PAGE
        performed = []
        with patch.object(sign, "_perform",
                          side_effect=lambda _d, paths:
                          performed.extend(paths)), \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.do_action({"id": "a1", "kind": "clear"},
                                    tmp_path / "s.json")
        assert status.state == "done" and status.kind == "clear"
        (path,) = performed
        (point,) = path
        # This screen names neither button, so the clear comes from the
        # canvas-relative fallback and is still pressed as a point.
        target = sign.button_targets(self.PAGE, self.LEGACY)["clear"]
        assert tuple(point) == tuple(target["point"])

    # A page that REMEMBERS a signature: the canvas is present in the tree but
    # small, the way a completed visit keeps its saved signatures' wrappers.
    # This is what "off the moment" actually means — it is not about the screen
    # being upright, which is what this test used to assert with a fixture
    # (CANVAS + CHROME) that is in fact a perfectly good landscape pad.
    REMEMBERED = (
        '<node class="android.view.View" resource-id="com.x:id/signature_view"'
        ' text="" clickable="false" bounds="[100,200][400,300]" />'
        '<node class="android.widget.Button" resource-id="com.x:id/btn_save"'
        ' text="Salvar" clickable="true" bounds="[0,660][200,720]" />'
        '<node class="android.widget.TextView" resource-id="com.x:id/foot"'
        ' text="footer" clickable="false" bounds="[0,700][1600,720]" />')

    def test_do_action_refuses_off_the_signature_screen(self, tmp_path):
        driver = MagicMock()
        driver.current_package = self.LEGACY
        driver.page_source = self.REMEMBERED    # a canvas, but not a pad
        with patch.object(sign, "_perform") as perform, \
             patch("apt_log.resident.run", side_effect=lambda w: w(driver)):
            status = sign.do_action({"id": "a2", "kind": "confirm"},
                                    tmp_path / "s.json")
        assert status.state == "failed" and status.reason == "no_canvas"
        perform.assert_not_called()

    def test_a_page_that_only_remembers_a_signature_is_not_a_moment(self):
        assert sign.signing_moment(self.REMEMBERED) is False
        assert sign.button_targets(self.REMEMBERED, self.LEGACY) is None

    def test_a_landscape_pad_still_offers_the_apps_own_buttons(self):
        """HHAeXchange+'s check-out signature pages, and the bug they found.

        Seen on the real check-out: action_bar_root [0,0][1600,720], a
        `signature_screen_signature_pad` the app names outright, and Borrar and
        Enviar as captionless Views with the words drawn over them. The screen
        needs NO quarter turn, so the old `sideways` gate answered None and the
        portal's step two came up empty on the one screen it exists for.
        """
        assert sign.signing_moment(CANVAS + CHROME) is True
        assert sign.sideways(CANVAS + CHROME, "com.hhaexchange.uma") is False
        targets = sign.button_targets(CANVAS + CHROME, "com.hhaexchange.uma")
        assert targets is not None
        # Pressed as an ELEMENT. The canvas-relative offsets were measured on
        # the legacy rotated page and mean nothing here, so they must not
        # appear — a guess must never replace a fact the tree publishes.
        assert "element" in targets["confirm"]
        assert "point" not in targets["confirm"]

    def test_the_status_kind_survives_the_file(self, tmp_path):
        sign.write_status(sign.Status(id="a3", state="done", kind="confirm"),
                          tmp_path / "s.json")
        assert sign.read_status(tmp_path / "s.json").kind == "confirm"


class TestTwoNamesForOneRectangle:
    """The live refusal on a real clock-out, and the reason a caregiver ended
    up pressing the app's own button by hand at ten at night.

    The screen carried BOTH `layout_tab_content_signature_skip` and
    `layout_tab_content_signature` at IDENTICAL bounds, with no inner canvas
    to fall through to. The de-nesting rule asks whether one candidate wraps
    another; on equal rectangles each wraps the other, so both were dropped
    and the finder said `no_canvas` — which the page rendered as "This screen
    has no signature box" over a screen plainly showing one.
    """

    PAIR = ('<node class="android.widget.FrameLayout" '
            'resource-id="app:id/layout_tab_content_signature_skip" '
            'bounds="[0,120][720,1532]"/>'
            '<node class="android.widget.FrameLayout" '
            'resource-id="app:id/layout_tab_content_signature" '
            'bounds="[0,120][720,1532]"/>')

    def test_identical_candidates_resolve_to_that_one_rectangle(self):
        bounds, reason = sign.find_canvas(self.PAIR, dump=False)
        assert reason == ""
        assert bounds == [0, 120, 720, 1532]

    def test_a_genuinely_ambiguous_pair_still_refuses(self):
        """De-duplicating must not become "pick one of two real candidates".
        Two DIFFERENT rectangles, neither inside the other, is still the case
        where a wrong choice puts ink on the wrong control.

        Both are canvas-SIZED, which is what makes them two real candidates
        rather than two labels: a hinted name too small to sign on is not a
        candidate at all any more, and would refuse for the other reason."""
        two = ('<node class="android.widget.FrameLayout" '
               'resource-id="app:id/signature_one" bounds="[0,0][700,700]"/>'
               '<node class="android.widget.FrameLayout" '
               'resource-id="app:id/signature_two" bounds="[800,0][1500,700]"/>')
        bounds, reason = sign.find_canvas(two, dump=False)
        assert bounds is None and reason == "ambiguous"

    def test_the_innermost_still_wins_when_there_is_an_inner_one(self):
        """The case the de-nesting rule was written for is untouched."""
        xml = self.PAIR + ('<node class="android.widget.FrameLayout" '
                           'resource-id="app:id/gesturePatientSignature" '
                           'bounds="[0,492][720,1142]"/>')
        bounds, _ = sign.find_canvas(xml, dump=False)
        assert bounds == [0, 492, 720, 1142]


class TestTheAppsOwnSaveIsAnElementNotAPixel:
    """Read off the live signature screen: the save this path exists to press
    was never a pixel that had to be guessed at.

        Button  btn_left     'Anular'  clickable  [10,75][100,109]
        Button  button_save  'Salvar'  clickable  [624,75][714,109]

    Pressing the element survives a density change — the caregiver had to
    change the density by hand one night to reach the button — survives a
    redesign that moves the strip, and cannot land on the drawing.
    """

    PACKAGE = "com.hhaexchange.caregiver"
    CANVAS = TestTwoNamesForOneRectangle.PAIR
    HEADER_SIGNING = ('<node class="android.widget.Button" '
                      'resource-id="app:id/btn_left" text="Anular" '
                      'clickable="true" bounds="[10,75][100,109]"/>'
                      '<node class="android.widget.Button" '
                      'resource-id="app:id/button_save" text="Salvar" '
                      'clickable="true" bounds="[624,75][714,109]"/>')
    # The SAME id on the ordinary visit page, with no caption: it is the ✎.
    HEADER_VISIT = ('<node class="android.widget.Button" '
                    'resource-id="app:id/btn_left" text="Atrás" '
                    'clickable="true" bounds="[0,71][69,113]"/>'
                    '<node class="android.widget.Button" '
                    'resource-id="app:id/button_save" text="" '
                    'clickable="true" bounds="[642,74][678,110]"/>')

    def test_the_captioned_save_is_found_as_an_element(self):
        found = sign._app_buttons(self.HEADER_SIGNING)
        assert found["confirm"]["rid"].endswith("button_save")
        assert found["confirm"]["txt"] == "Salvar"

    def test_the_uncaptioned_button_of_the_same_id_is_not_a_save(self):
        """The trap this nearly walked into. Both screens of this activity
        publish a `button_save`; on the visit page it is an icon with a
        different job, sitting on a real record."""
        assert "confirm" not in sign._app_buttons(self.HEADER_VISIT)

    def test_cancel_is_never_mistaken_for_either(self):
        """Anular throws the signature away. A rule loose enough to catch it
        would eventually press it."""
        found = sign._app_buttons(self.HEADER_SIGNING)
        assert "clear" not in found

    def test_the_signature_moment_needs_the_pad_not_just_the_container(self):
        """`layout_tab_content_signature` is on the visit page whether or not
        the pad is showing. Once the finder stopped annihilating itself, the
        container alone would have turned the peek sideways over an ordinary
        visit page and offered a Save with nothing to save."""
        assert sign.sideways(self.CANVAS + self.HEADER_SIGNING,
                             package=self.PACKAGE) is True
        assert sign.sideways(self.CANVAS + self.HEADER_VISIT,
                             package=self.PACKAGE) is False

    def test_targets_press_the_element_and_refuse_off_the_moment(self):
        targets = sign.button_targets(self.CANVAS + self.HEADER_SIGNING,
                                      package=self.PACKAGE)
        assert list(targets) == ["confirm"]
        assert targets["confirm"]["element"]["txt"] == "Salvar"
        assert sign.button_targets(self.CANVAS + self.HEADER_VISIT,
                                   package=self.PACKAGE) is None

    def test_a_kind_the_app_did_not_name_is_simply_absent(self):
        """This screen names its save and draws its clear, and the canvas runs
        the full width so there is no left strip to derive one from. Save must
        still work — answering each kind independently is the whole point."""
        targets = sign.button_targets(self.CANVAS + self.HEADER_SIGNING,
                                      package=self.PACKAGE)
        assert "confirm" in targets and "clear" not in targets


class TestClearIsTheOtherHeaderButton:
    """The recorder was asked, as it should have been the first time.

    Both signature frames from the live clock-out hold exactly 33 elements —
    two header buttons, two dividers, twenty-eight checkboxes and the
    clock-out — and NOT ONE of them is a Borrar, by id, by caption or as an
    anonymous clickable. So the clear cannot be found by hunting for a node
    that is not there.

    What the field screenshot shows is a PAIR at the foot of the pad, and the
    only left-hand header button in the tree is `btn_left`. On the screen the
    caregiver was looking at, that button reads "Borrar"; on the one captured
    an instant earlier it read "Anular". Same element, different caption,
    opposite meanings — which is precisely why the caption decides and the id
    never does.
    """

    PACKAGE = "com.hhaexchange.caregiver"
    CANVAS = TestTwoNamesForOneRectangle.PAIR

    def _header(self, left_caption):
        return ('<node class="android.widget.Button" '
                f'resource-id="app:id/btn_left" text="{left_caption}" '
                'clickable="true" bounds="[10,75][100,109]"/>'
                '<node class="android.widget.Button" '
                'resource-id="app:id/button_save" text="Salvar" '
                'clickable="true" bounds="[624,75][714,109]"/>')

    def test_borrar_is_taken_as_the_clear(self):
        found = sign._app_buttons(self._header("Borrar"))
        assert found["clear"]["txt"] == "Borrar"
        assert found["clear"]["rid"].endswith("btn_left")

    def test_anular_is_taken_as_neither(self):
        """It cancels. It throws the signature away. Same element, same id,
        one word apart from the button we DO press."""
        found = sign._app_buttons(self._header("Anular"))
        assert "clear" not in found
        assert found["confirm"]["txt"] == "Salvar"

    def test_both_actions_are_offered_when_the_app_captions_both(self):
        targets = sign.button_targets(self.CANVAS + self._header("Borrar"),
                                      package=self.PACKAGE)
        assert sorted(targets) == ["clear", "confirm"]
        assert targets["clear"]["element"]["txt"] == "Borrar"
        assert targets["confirm"]["element"]["txt"] == "Salvar"


class TestPressingAnElementNotAPoint:
    """The tree describes the UNROTATED layout while this app rotates its
    signature page in the drawing pass, so a coordinate taken from these
    bounds is not reliably where the button is drawn. Letting Android
    dispatch to the element sidesteps the question — and on a screen where a
    wrong press saves the wrong thing, that is the only acceptable answer."""

    def test_the_press_requires_the_id_and_the_caption_together(self):
        pressed = []

        class El:
            def click(self_inner): pressed.append(True)

        class Driver:
            def __init__(self, n): self.n = n
            def find_elements(self, how, what):
                self.what = what
                return [El() for _ in range(self.n)]

        el = {"rid": "com.x:id/button_save", "txt": "Salvar"}
        d = Driver(1)
        assert sign._click_element(d, el) is True
        assert "button_save" in d.what and "Salvar" in d.what
        assert pressed == [True]

    def test_it_refuses_when_the_screen_moved_underneath(self):
        class Driver:
            def find_elements(self, how, what): return []
        assert sign._click_element(Driver(), {"rid": "a:id/b",
                                              "txt": "Salvar"}) is False

    def test_it_refuses_when_two_things_match(self):
        class Driver:
            def find_elements(self, how, what): return [object(), object()]
        assert sign._click_element(Driver(), {"rid": "a:id/b",
                                              "txt": "Salvar"}) is False

    def test_a_captionless_target_with_an_ordinary_id_is_never_pressed(self):
        """The bare `button_save` on the visit page is the ✎ on a real
        record. Nothing about its id says signature, so the caption is the
        only evidence there is and its absence is disqualifying."""
        class Driver:
            def find_elements(self, how, what):
                raise AssertionError("should not have looked")
        assert sign._click_element(Driver(), {"rid": "a:id/button_save",
                                              "txt": ""}) is False

    def test_a_captionless_button_whose_id_names_the_signature_screen_presses(self):
        """THE RULE THAT FOUND IT AND THE RULE THAT PRESSES IT MUST AGREE.

        `_app_buttons` accepts `signature_screen_submit_button` without a
        caption on purpose — the id names the screen as well as the action,
        which is exactly the evidence a bare `button_save` lacks. This
        function used to re-check with the opposite rule and refuse, so on
        HHAeXchange+ — where Borrar and Enviar are captionless Views with
        their words drawn over them as separate TextViews — the app-side
        press could never succeed. Reported as the pad's step-two button
        doing nothing.
        """
        pressed = []

        class El:
            def click(self_inner): pressed.append(True)

        class Driver:
            def find_elements(self, how, what):
                self.what = what
                return [El()]

        d = Driver()
        el = {"rid": "com.hhaexchange.uma:id/signature_screen_submit_button",
              "txt": ""}
        assert sign._click_element(d, el) is True
        assert "signature_screen_submit_button" in d.what
        assert "@text" not in d.what      # there is no caption to match on
        assert pressed == [True]

    def test_even_a_signature_id_refuses_when_two_things_match(self):
        class Driver:
            def find_elements(self, how, what): return [object(), object()]
        assert sign._click_element(
            Driver(),
            {"rid": "x:id/signature_screen_submit_button", "txt": ""}) is False


class TestTheRecordCanSayWhichButtonIsWhich:
    """An evening was spent inferring button identity from screenshots and
    rotation arithmetic, and getting it wrong twice — first "the pad's Borrar
    is btn_left", then the same thing again with more confidence. The record
    held bounds and ids and no captions, so it could not settle it.

    It keeps captions now, from a closed vocabulary of chrome words. A
    patient's name is not in that list and cannot get into the file through
    it, which is the property that made the record textless to begin with.
    """

    def test_a_control_caption_is_kept(self):
        for word in ("Borrar", "Salvar", "Anular", "Clear", "Save"):
            node = f'<node text="{word}" bounds="[0,0][10,10]"/>'
            assert sign._control_word(node) == word

    def test_the_word_on_the_submit_button_is_kept(self):
        """ENVIAR WAS MISSING FROM THE VOCABULARY, AND IT IS THE ONE THAT
        MATTERS.

        The live capture of the HHAeXchange+ signature screen came back with
        "Borrar" beside the clear button and an empty string beside the
        submit one — which reads as "the app does not name that button" when
        the app names it perfectly well. `_SAVE_WORDS` has held "enviar" all
        along; the two lists were never reconciled.
        """
        for word in ("Enviar", "enviar", "Send"):
            node = f'<node text="{word}" bounds="[0,0][10,10]"/>'
            assert sign._control_word(node) == word

    def test_every_word_the_finder_acts_on_can_be_recorded(self):
        """The record exists to hand over a repair. A caption the matcher
        uses and the dump blanks is exactly the gap that cost an hour."""
        missing = [w for w in (sign._SAVE_WORDS + sign._CLEAR_WORDS)
                   if w not in sign._CONTROL_WORDS]
        assert missing == []

    def test_a_name_is_not(self):
        """The whole reason the record carries no text."""
        for name in ("Sadia Amselem", "CARIDAD ROJAS BATISTA", "Firma Paciente",
                     "08/18 at 08:00PM"):
            node = f'<node text="{name}" bounds="[0,0][10,10]"/>'
            assert sign._control_word(node) == ""

    def test_the_dump_carries_the_word_and_no_other_text(self, tmp_path,
                                                          monkeypatch):
        import json as _json

        path = tmp_path / "sign-debug.json"
        monkeypatch.setattr(sign, "DEBUG_PATH", path)
        xml = ('<node class="android.widget.Button" resource-id="a:id/btn_left"'
               ' text="Borrar" clickable="true" bounds="[10,75][100,109]"/>'
               '<node class="android.widget.TextView" text="Sadia Amselem"'
               ' bounds="[0,1151][358,1164]"/>')
        sign._dump_refusal(sign._nodes_of(xml), "no_canvas")
        doc = _json.loads(path.read_text(encoding="utf-8"))
        words = [n["word"] for n in doc["nodes"]]
        assert "Borrar" in words
        assert "Sadia Amselem" not in _json.dumps(doc)


class TestAFingerPastTheEdge:
    """A signature that strays off the pad.

    "If I try to sign a bit out of bounds it can't send it to the phone."
    The pointer keeps reporting while it is captured, so a finger sliding
    past the edge produced points outside 0..1 — and the old rule refused
    the WHOLE payload for one of them, bouncing an entire signature with a
    bare "failed" and leaving the strokes sitting on the pad.

    Reproduced while testing: a curve straying 4% off the left edge came
    back {"error": "empty"}.
    """

    def test_a_stroke_that_strays_is_accepted(self):
        assert sign.validate([[[-0.04, 0.5], [0.2, 0.5], [0.4, 0.5]]])

    def test_and_one_that_strays_off_the_far_edge(self):
        assert sign.validate([[[0.6, 0.5], [1.04, 0.5]]])

    def test_a_payload_that_is_not_a_signature_is_still_refused(self):
        """The check exists to refuse nonsense, and still does."""
        assert not sign.validate([[[5.0, 0.5], [0.2, 0.5]]])
        assert not sign.validate([[[0.5, -3.0]]])

    def test_the_stray_ink_rides_the_edge(self):
        """Clamped onto the pad BEFORE the canvas mapping, so it lands on the
        edge of the drawn box rather than on the wrong margin."""
        bounds = [100, 200, 500, 400]
        out = sign.build_paths([[[-0.2, 0.5], [0.5, 0.5]]], bounds, aspect=1.0)
        xs = [x for x, _ in out[0]]
        inset = (bounds[2] - bounds[0]) * sign.CANVAS_INSET
        assert min(xs) >= bounds[0] + inset - 1

    def test_every_stroke_still_arrives(self):
        """Two strokes in, two paths out — the A is drawn with two."""
        out = sign.build_paths(
            [[[0.1, 0.1], [0.2, 0.2]], [[0.3, 0.3], [0.4, 0.4]]],
            [0, 0, 400, 400])
        assert len(out) == 2

    def test_the_pad_clamps_at_the_source(self):
        """Clamped in the browser too, so the preview shows exactly what is
        sent rather than something the server will quietly move."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        body = src[src.index("function padPoint"):]
        body = body[:body.index("}")]
        assert "clamp" in body

    def test_a_refusal_says_which_one(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
        assert "out.error === 'empty'" in src


class TestASheetThatNamesNothing:
    """inMyTeam's signature sheet, as the phone actually reports it.

    It names nothing — no resource-id, no telling class — so it takes the
    SHAPED path, and for a while only the NAMED path was de-duplicated and
    de-nested. Compose hands the shaped path the bottom sheet's own wrapper
    TWICE at identical bounds plus the real canvas inside it, so the finder
    refused "ambiguous" and she could not sign at all. Intermittent, because
    how many wrappers Compose emits varies.
    """

    @staticmethod
    def _node(b, cls="android.view.View", clickable="false", rid=""):
        return (f'<node class="{cls}" resource-id="{rid}" '
                f'clickable="{clickable}" text="" '
                f'bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]" />')

    def _sheet(self, wrappers=2):
        parts = [self._node([0, 0, 720, 1600], "android.widget.FrameLayout"),
                 self._node([0, 64, 720, 1561], rid="touch_outside",
                            clickable="true")]
        parts += [self._node([0, 923, 720, 1561]) for _ in range(wrappers)]
        parts.append(self._node([13, 998, 707, 1469]))
        return "<hierarchy>" + "".join(parts) + "</hierarchy>"

    def test_the_canvas_is_found_through_two_identical_wrappers(self):
        assert sign.find_canvas(self._sheet(2), dump=False) == (
            [13, 998, 707, 1469], "")

    def test_and_through_one(self):
        assert sign.find_canvas(self._sheet(1), dump=False) == (
            [13, 998, 707, 1469], "")

    def test_and_through_none(self):
        assert sign.find_canvas(self._sheet(0), dump=False) == (
            [13, 998, 707, 1469], "")

    def test_the_scrim_is_never_the_canvas(self):
        """touch_outside spans 93% of the screen and would win on area."""
        found, _ = sign.find_canvas(self._sheet(), dump=False)
        assert found != [0, 64, 720, 1561]

    def test_two_canvases_side_by_side_still_refuse(self):
        """The check-out page shows a patient box AND a staff box. Neither
        wraps the other, and guessing between them is exactly what this
        finder exists not to do."""
        xml = ("<hierarchy>"
               + self._node([16, 200, 704, 900])
               + self._node([16, 950, 704, 1500])
               + "</hierarchy>")
        assert sign.find_canvas(xml, dump=False) == (None, "ambiguous")


class TestAStrokeThatDidNotLandIsDrawnAgain:
    """Three attempts at making the seam between strokes reliable did not
    hold, and every field report had the same shape: the FIRST stroke lands
    and a later one does not. perform() returns when the driver ACCEPTED the
    chain, and nothing in that answer says the ink was drawn — only the
    screen does. So the screen is asked.
    """

    BOUNDS = [13, 998, 707, 1469]

    def _replay(self, ink_readings):
        """ink_readings: what the canvas measures, in call order.

        A FINAL reading is taken after the last stroke — see INK_PAINT — so
        every list here carries one more entry than there are strokes. That
        last look is what makes "done" honest rather than announced over a
        canvas the ink has not reached.
        """
        driver = MagicMock()
        drawn = []
        readings = list(ink_readings)
        with patch.object(sign, "_draw",
                          side_effect=lambda d, path: drawn.append(path)), \
             patch.object(sign, "_canvas_ink",
                          side_effect=lambda b, s=None: readings.pop(0)), \
             patch.object(sign.time, "sleep"):
            sign._perform(driver, [[(1, 1)], [(2, 2)]], bounds=self.BOUNDS)
        return drawn

    def test_a_stroke_that_landed_is_drawn_once(self):
        # start=0, after s1=500, after s2=900 — one reading per stroke, the
        # answer for one being the baseline for the next.
        drawn = self._replay([0, 500, 900, 900])
        assert drawn == [[(1, 1)], [(2, 2)]]

    def test_a_stroke_that_left_no_ink_is_drawn_again(self):
        """The reported failure, exactly: stroke one lands, stroke two does
        not."""
        # start=0, after s1=500, after s2=500 (nothing gained), retry -> 900.
        drawn = self._replay([0, 500, 500, 900, 900])
        assert drawn == [[(1, 1)], [(2, 2)], [(2, 2)]]

    def test_only_the_missing_stroke_is_redrawn(self):
        """Never the whole signature, and the canvas is never cleared — so a
        retry cannot double a stroke that landed."""
        drawn = self._replay([0, 500, 500, 900, 900])
        assert drawn.count([(1, 1)]) == 1

    def test_it_gives_up_rather_than_hammering(self):
        drawn = self._replay([0] * 12)
        # Two strokes, each drawn once and retried at most STROKE_RETRIES
        # times: a canvas that never takes ink costs a bounded number of
        # attempts, not an endless one.
        assert len(drawn) == 2 * (1 + sign.STROKE_RETRIES)

    def test_a_screenshot_that_fails_is_not_read_as_no_ink(self):
        """None is not zero. A failing screencap must never make a working
        replay redraw everything."""
        drawn = self._replay([None, None, None])
        assert drawn == [[(1, 1)], [(2, 2)]]

    def test_without_bounds_it_behaves_exactly_as_before(self):
        driver = MagicMock()
        drawn = []
        with patch.object(sign, "_draw",
                          side_effect=lambda d, path: drawn.append(path)), \
             patch.object(sign, "_canvas_ink") as measured, \
             patch.object(sign.time, "sleep"):
            sign._perform(driver, [[(1, 1)], [(2, 2)]])
        assert drawn == [[(1, 1)], [(2, 2)]]
        assert measured.call_count == 0

    def test_the_replay_hands_the_canvas_over_to_be_measured(self):
        """The bounds the finder already produced are the measuring window;
        a replay that did not pass them would silently lose the check."""
        import inspect

        body = inspect.getsource(sign.execute)
        assert "bounds=bounds" in body


class TestHHAeXchangePlusPatientSignature:
    """The pad HHAeXchange+ puts up at step 2 of 3 of a check-out.

    Caught with the caregiver standing on it: the finder refused `ambiguous`
    and she could not sign at all.

    The app names its two BUTTONS after signatures and leaves the canvas
    anonymous. Measured on the live screen, rotated, at 1534x681:

        (no id)                          [18,171,1518,525]     50.83%
        signature_screen_clear_button    [14,656,44,681]         0.07%
        signature_screen_submit_button   [1474,656,1529,681]     0.13%

    Both buttons matched the id hint, neither wrapped the other, and the
    thing that draws was never looked at.
    """

    CANVAS = [18, 171, 1518, 525]

    def _screen(self, canvas_clickable="true", with_buttons=True):
        def node(b, rid="", clickable="false", cls="android.view.View"):
            return (f'<node class="{cls}" resource-id="{rid}" '
                    f'clickable="{clickable}" text="" '
                    f'bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]"/>')

        parts = [node(self.CANVAS, clickable=canvas_clickable)]
        if with_buttons:
            parts += [
                node([14, 656, 44, 681], "app:id/signature_screen_clear_button",
                     "true"),
                node([1474, 656, 1529, 681],
                     "app:id/signature_screen_submit_button", "true"),
            ]
        parts += [node([6, 17, 31, 42], "app:id/menu_top_bar_back_button", "true"),
                  node([1507, 17, 1534, 43], "app:id/help_button", "true")]
        return "<hierarchy>" + "".join(parts) + "</hierarchy>"

    def test_the_canvas_is_found(self):
        bounds, reason = sign.find_canvas(self._screen(), dump=False)
        assert reason == ""
        assert bounds == self.CANVAS

    def test_a_button_named_after_signatures_is_not_a_canvas(self):
        """0.07% of the screen. Nobody signs on that."""
        bounds, _ = sign.find_canvas(self._screen(), dump=False)
        assert bounds != [14, 656, 44, 681]

    def test_a_clickable_canvas_is_still_a_canvas(self):
        """A drawing surface is usually inert in the tree, and that quietness
        is what tells it from a big clickable container. This one is not —
        requiring quiet left the screen with no candidate at all."""
        assert sign.find_canvas(self._screen("true"), dump=False)[0] \
            == self.CANVAS

    def test_and_so_is_a_quiet_one(self):
        assert sign.find_canvas(self._screen("false"), dump=False)[0] \
            == self.CANVAS

    def test_a_clickable_surface_needs_the_app_to_have_said_signature(self):
        """Without the hinted controls, a big clickable View is just a big
        clickable View — a page's own scroll container, a card, a wrapper.
        The evidence is what makes it a canvas, and with none there is none."""
        bounds, reason = sign.find_canvas(
            self._screen("true", with_buttons=False), dump=False)
        assert bounds is None and reason == "no_canvas"

    def test_the_replay_would_stay_inside_it(self):
        """The paths the finder's answer produces land on the canvas, not on
        the buttons under it."""
        paths = sign.build_paths([[[0.1, 0.5], [0.9, 0.5]]], self.CANVAS,
                                 aspect=(1518 - 18) / (525 - 171))
        xs = [x for x, _ in paths[0]]
        ys = [y for _, y in paths[0]]
        assert min(xs) >= self.CANVAS[0] and max(xs) <= self.CANVAS[2]
        assert min(ys) >= self.CANVAS[1] and max(ys) <= self.CANVAS[3]
        # Clear of the button row entirely.
        assert max(ys) < 656


class TestNamingTheAppsOwnSubmit:
    """HHAeXchange+'s pad buttons carry no text of their own — "Borrar" and
    "Enviar" are separate TextViews laid over them — so a rule that needed
    the caption could not see either one, on the very screen the pad exists
    for.

    The caption was earning its keep, though, and the guard below is why
    this is not simply "an id is enough": the legacy app's ORDINARY VISIT
    PAGE carries `button_save` with no text, and that is a different save
    entirely.
    """

    UMA = ('<node class="android.view.View" '
           'resource-id="app:id/signature_screen_clear_button" '
           'clickable="true" text="" bounds="[14,656][44,681]"/>'
           '<node class="android.view.View" '
           'resource-id="app:id/signature_screen_submit_button" '
           'clickable="true" text="" bounds="[1474,656][1529,681]"/>')

    def test_a_submit_that_names_the_signature_screen_is_a_save(self):
        assert sign._app_buttons(self.UMA)["confirm"]["rid"] \
            .endswith("signature_screen_submit_button")

    def test_and_its_clear_is_a_clear(self):
        assert sign._app_buttons(self.UMA)["clear"]["rid"] \
            .endswith("signature_screen_clear_button")

    def test_a_bare_button_save_with_no_caption_is_still_not_one(self):
        """The legacy visit page's own save. Its pad's Salvar appears and
        disappears WITH the pad, which is what makes the caption the
        evidence there — and what stops the portal offering a Save over a
        page with nothing to save."""
        page = ('<node class="android.widget.Button" '
                'resource-id="app:id/button_save" clickable="true" text="" '
                'bounds="[642,74][678,110]"/>')
        assert "confirm" not in sign._app_buttons(page)

    def test_a_captioned_save_is_a_save_wherever_it_is(self):
        page = ('<node class="android.widget.Button" '
                'resource-id="app:id/button_save" clickable="true" '
                'text="Salvar" bounds="[642,74][678,110]"/>')
        assert "confirm" in sign._app_buttons(page)

    @pytest.mark.parametrize("word", ["Enviar", "Submit", "Send"])
    def test_the_words_this_app_uses_for_saving_count(self, word):
        page = ('<node class="android.widget.Button" resource-id="" '
                f'clickable="true" text="{word}" bounds="[642,74][678,110]"/>')
        assert "confirm" in sign._app_buttons(page)


class TestRotatedLabelsScaleWithTheScreen:
    """The legacy signature page draws its UI a quarter turn inside a
    portrait activity, and the tell is single-line captions in boxes taller
    than they are wide. The two box tests were measured in pixels on a
    720-wide screen; a denser panel draws the same caption bigger in every
    dimension, so a fixed 40px "narrow" ceiling would stop matching the very
    labels it was written for."""

    @staticmethod
    def _rotated(width, height):
        """A portrait window with two tall, narrow captions in it."""
        narrow = int(width * 0.045)          # comfortably under the ceiling
        tall = int(width * 0.14)             # and over the floor, and > 3x wide
        labels = "".join(
            f'<node class="android.widget.TextView" '
            f'text="Firma del cuidador" '
            f'bounds="[{80 + i * 200},400][{80 + i * 200 + narrow},{400 + tall}]"/>'
            for i in range(2))
        return (f'<hierarchy><node bounds="[0,0][{width},{height}]">'
                f'{labels}</node></hierarchy>')

    @pytest.mark.parametrize("width,height", [(720, 1600), (1080, 2400),
                                              (1440, 3120)])
    def test_the_rotated_page_is_recognised_on_every_panel(self, width,
                                                           height):
        assert sign.presentation_rotated(self._rotated(width, height)) is True

    @pytest.mark.parametrize("width,height", [(720, 1600), (1080, 2400)])
    def test_an_ordinary_portrait_page_is_not(self, width, height):
        """Wide captions, the way horizontal text actually sits."""
        rows = "".join(
            f'<node class="android.widget.TextView" text="Registrar entrada" '
            f'bounds="[20,{300 + i * 120}][{width - 20},{360 + i * 120}]"/>'
            for i in range(4))
        tree = (f'<hierarchy><node bounds="[0,0][{width},{height}]">'
                f'{rows}</node></hierarchy>')
        assert sign.presentation_rotated(tree) is False

    def test_a_truly_sideways_window_needs_no_help(self):
        """Injected coordinates already follow a rotated display, so the
        turn must not be applied twice."""
        assert sign.presentation_rotated(self._rotated(1600, 720)) is False

    def test_one_tall_label_is_not_enough(self):
        tree = ('<hierarchy><node bounds="[0,0][720,1600]">'
                '<node class="android.widget.TextView" text="Firma del cuidador" '
                'bounds="[80,400][112,500]"/></node></hierarchy>')
        assert sign.presentation_rotated(tree) is False

    def test_nothing_to_read_is_not_rotated(self):
        for empty in ("", "<hierarchy/>"):
            assert sign.presentation_rotated(empty) is False


class TestThePerStrokeRecord:
    """Four experiments failed to reproduce the one failure that matters: a
    real signature lost its middle stroke, twice, and every synthetic replay
    of the same shape, position, order, point count and clumping landed. Two
    fixes were shipped on theories that turned out to be wrong.

    So the next real signature is answered from the record instead. This is
    the instrument: per stroke, what the canvas gained and how many attempts
    it took.
    """

    BOUNDS = [13, 998, 707, 1469]

    def _tally(self, ink_readings):
        driver = MagicMock()
        readings = list(ink_readings)
        with patch.object(sign, "_draw"), \
             patch.object(sign, "_canvas_ink",
                          side_effect=lambda b, s=None: readings.pop(0)), \
             patch.object(sign.time, "sleep"):
            _first, _last, tally = sign._perform(
                driver, [[(1, 1)], [(2, 2)]], bounds=self.BOUNDS)
        return tally

    def test_a_clean_replay_records_a_gain_for_each_stroke(self):
        # start 0, stroke one -> 500, stroke two -> 900, final look 900.
        assert self._tally([0, 500, 900, 900]) == "+500/1,+400/1"

    def test_a_stroke_that_took_three_goes_says_so(self):
        """The shape of the real failure: one stroke gains nothing however
        many times it is drawn, while its neighbours gain normally."""
        # start 0, s1 -> 500, s2 -> 500, 500, 500 (three goes, no gain),
        # then the final look.
        tally = self._tally([0, 500, 500, 500, 500])
        assert tally == "+500/1,+0/3"

    def test_the_record_carries_no_coordinates(self):
        """Deltas and counts only. A pixel total cannot reconstruct a
        signature, which is the line REQ-10.6 draws around everything here."""
        tally = self._tally([0, 500, 900, 900])
        for part in tally.split(","):
            gain, tries = part.split("/")
            int(gain)
            int(tries)

    def test_an_unreadable_canvas_records_nothing_rather_than_zero(self):
        """None is not zero — the distinction the whole retry rule rests on."""
        assert self._tally([None, None, None]) == ""


class TestTheWalkerKeepsOffAReplay:
    """ON A SIGNATURE CANVAS A SWIPE IS INK.

    The page walker asks whether a MACRO is running, and a replay is not a
    macro — it writes its own status file — so a walk could begin in the
    middle of one and scroll straight down the canvas. This file already
    records the symptom from the first field test: "the scroll gesture drew a
    straight line down the canvas she was about to sign". The owner described
    it again, unprompted: "too straight of a line, like something I didn't
    draw at all".

    It is also why the failure only ever happened while he was WATCHING. The
    walk needs a viewer, so every signature he watched was eligible to be
    walked over, and every replay requested from a script with nobody
    watching landed whole. Four experiments could not reproduce it because
    none of them was watched.
    """

    def test_a_queued_replay_counts(self, tmp_path):
        req = tmp_path / "sign-request.json"
        req.write_text("{}", encoding="utf-8")
        assert sign.in_flight(tmp_path / "none.json", req) is True

    def test_a_running_replay_counts(self, tmp_path):
        path = tmp_path / "s.json"
        sign.write_status(sign.Status(id="x", state="running"), path)
        assert sign.in_flight(path, tmp_path / "none.json") is True

    def test_the_tail_after_it_finishes_counts(self, tmp_path):
        """`perform()` returns before the phone has finished painting, so
        "not running any more" is not yet "safe to touch"."""
        path = tmp_path / "s.json"
        sign.write_status(sign.Status(id="x", state="done"), path)
        assert sign.in_flight(path, tmp_path / "none.json") is True

    def test_an_old_replay_does_not_count_for_ever(self, tmp_path):
        from datetime import datetime, timedelta

        path = tmp_path / "s.json"
        long_ago = (datetime.now()
                    - timedelta(seconds=sign.IN_FLIGHT_TAIL + 30)).isoformat()
        sign.write_status(sign.Status(id="x", state="done", at=long_ago), path)
        assert sign.in_flight(path, tmp_path / "none.json") is False

    def test_a_machine_that_never_replayed_is_not_held_off_for_ever(self,
                                                                   tmp_path):
        """`read_status` answers a DEFAULT Status when no file exists, and
        its `at` defaults to NOW — so a machine that has never replayed a
        signature looked like one that had just finished, and the walker
        would have been blocked permanently."""
        assert sign.in_flight(tmp_path / "none.json",
                              tmp_path / "gone.json") is False

    def test_the_walker_and_the_warm_sweep_both_ask(self):
        """The guard is worth nothing if nobody reads it, and BOTH of these
        scroll the phone."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "src/apt_log/macros.py").read_text(encoding="utf-8")
        for name in ("def maybe_stitch", "def maybe_warm"):
            body = source[source.index(name):]
            body = body[:body.index("\n    def ", 10)]
            assert "sign_mod.in_flight()" in body, name


class TestWhoseSignatureThisScreenWants:
    """The app says it, and until now nobody read it.

    inMyTeam's exit puts two signature pads back to back — `Paso 2 de 3` for
    the patient, `Paso 3 de 3` for the caregiver. Same canvas, same buttons,
    same everything except the title bar, which carries the signer's name.

    With one inMyTeam patient that could be guessed at. With two it cannot,
    and the failure it produces is the worst this feature has: an adopted
    signature applied under the wrong person's name, on a record an auditor
    reads.
    """

    def doc(self, *lines):
        return {"statics": [{"txt": t} for t in lines]}

    def test_the_apps_own_broken_word_order(self):
        """It renders *Firma de «name»* as `«NAME» Firma de` — the name is the
        PREFIX. Read off the live handset."""
        assert sign.signer_named(
            self.doc("Paso 2 de 3", "MARIA X GARCIA Firma de")) == \
            "MARIA X GARCIA"

    def test_the_order_a_bug_fix_would_produce(self):
        """Accepted too. A release that fixed the concatenation would
        otherwise silently stop naming anybody."""
        assert sign.signer_named(self.doc("Firma de Ana Ruiz")) == "Ana Ruiz"

    def test_english_says_it_too(self):
        assert sign.signer_named(
            self.doc("Signature of Ana Ruiz")) == "Ana Ruiz"

    def test_an_ordinary_screen_names_nobody(self):
        assert sign.signer_named(self.doc("Visitas", "Hoy", "Mis Viajes")) == ""

    def test_the_phrase_alone_names_nobody(self):
        """A title that is only the words is not a person, and returning the
        empty string is what stops the pad claiming one."""
        assert sign.signer_named(self.doc("Firma de")) == ""

    def test_punctuation_is_not_a_person(self):
        assert sign.signer_named(self.doc("— Firma de")) == ""

    def test_a_paragraph_is_not_a_title(self):
        """A consent blurb mentioning the phrase must not be mistaken for the
        title bar. Length is the cheap discriminator and the bar is short."""
        long = ("Al continuar usted acepta que la firma de la persona que "
                "recibe el servicio se registre electronicamente conforme a "
                "las normas del programa y de la agencia correspondiente.")
        assert sign.signer_named(self.doc(long)) == ""

    def test_the_first_naming_caption_wins(self):
        """One screen, one signer. Two captions is a screen this does not
        understand, and taking the first is at least deterministic."""
        got = sign.signer_named(self.doc("Ana Ruiz Firma de",
                                         "Firma de Beto Sosa"))
        assert got == "Ana Ruiz"

    def test_no_statics_is_not_an_error(self):
        assert sign.signer_named({}) == ""
