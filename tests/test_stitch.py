"""Whole-page stitching: layout truth and tap truth, kept separate.

The owner's spec: the portal should never leave anyone wondering whether a
scrollable page has more below the fold. The stitcher joins per-step
captures into one page; the locator re-finds an aimed element in a fresh
capture at its step, because a below-the-fold tap replays the scroll and
must verify before touching anything.
"""

from __future__ import annotations

from apt_log import stitch


def el(rid, cls, b, txt=""):
    return {"rid": rid, "cls": cls, "b": b, "txt": txt}


class TestStitching:
    def test_offsets_are_measured_from_anchors_not_assumed(self):
        """The swipe's momentum makes the nominal distance a lie: the shared
        row moved 500, not the nominal 300, and the stitch must follow it."""
        doc = stitch.stitch([
            {"elements": [el("a", "Button", [0, 100, 200, 160]),
                          el("b", "Button", [0, 700, 200, 760])],
             "statics": []},
            {"elements": [el("b", "Button", [0, 200, 200, 260]),
                          el("c", "Button", [0, 700, 200, 760])],
             "statics": []},
        ], nominal_dy=300)
        by_rid = {e["rid"]: e for e in doc["elements"]}
        assert by_rid["c"]["vb"][1] == 1200      # 700 + measured 500
        assert by_rid["c"]["step"] == 1

    def test_overlap_is_deduplicated(self):
        doc = stitch.stitch([
            {"elements": [el("b", "Button", [0, 700, 200, 760])],
             "statics": []},
            {"elements": [el("b", "Button", [0, 200, 200, 260])],
             "statics": []},
        ], nominal_dy=300)
        assert len(doc["elements"]) == 1

    def test_twins_in_one_capture_are_both_kept(self):
        """Two same-identity items close together in the SAME capture are
        genuinely two items — the dump saw them side by side. The
        schedule's stacked EVV check glyphs sit 32 px apart with identical
        identity, and the dedup silently erased the check-out record."""
        doc = stitch.stitch([
            {"elements": [],
             "statics": [el("", "TextView", [33, 269, 42, 280], ""),
                         el("", "TextView", [33, 301, 42, 312], "")]},
        ], nominal_dy=300)
        assert len(doc["statics"]) == 2

    def test_the_same_twin_reseen_across_captures_still_dedups(self):
        """And when the overlapping next capture sees both twins again,
        each collapses onto its own first sighting — two on the page, not
        four."""
        doc = stitch.stitch([
            {"elements": [el("anchor", "Button", [0, 700, 200, 760])],
             "statics": [el("", "TextView", [33, 669, 42, 680], ""),
                         el("", "TextView", [33, 701, 42, 712], "")]},
            {"elements": [el("anchor", "Button", [0, 200, 200, 260])],
             "statics": [el("", "TextView", [33, 169, 42, 180], ""),
                         el("", "TextView", [33, 201, 42, 212], "")]},
        ], nominal_dy=300)
        assert len(doc["statics"]) == 2

    def test_fixed_chrome_is_recorded_once(self):
        """A tab bar shares identity across captures at the same device
        position; re-adding it per step sprinkled tab bars through the
        page."""
        doc = stitch.stitch([
            {"elements": [el("tabs", "LinearLayout", [0, 1500, 720, 1600]),
                          el("row1", "Button", [0, 700, 200, 760])],
             "statics": []},
            {"elements": [el("tabs", "LinearLayout", [0, 1500, 720, 1600]),
                          el("row1", "Button", [0, 200, 200, 260]),
                          el("row2", "Button", [0, 700, 200, 760])],
             "statics": []},
        ], nominal_dy=300)
        tabs = [e for e in doc["elements"] if e["rid"] == "tabs"]
        assert len(tabs) == 1
        assert tabs[0]["step"] == 0

    def test_without_anchors_the_nominal_distance_stands_in(self):
        doc = stitch.stitch([
            {"elements": [el("a", "Button", [0, 100, 200, 160])],
             "statics": []},
            {"elements": [el("z", "Button", [0, 100, 200, 160])],
             "statics": []},
        ], nominal_dy=300)
        by_rid = {e["rid"]: e for e in doc["elements"]}
        assert by_rid["z"]["vb"][1] == 400


class TestLocate:
    def test_identity_and_x_must_match_and_y_may_drift(self):
        """The replayed scroll never lands pixel-equal; the tap must use the
        FOUND bounds, so the locator tolerates vertical drift only."""
        aim = {"rid": "row", "cls": "Button", "b": [0, 700, 200, 760]}
        fresh = [el("row", "Button", [0, 745, 200, 805])]
        assert stitch.locate(aim, fresh)["b"][1] == 745

    def test_too_much_drift_is_not_the_same_element(self):
        aim = {"rid": "row", "cls": "Button", "b": [0, 700, 200, 760]}
        fresh = [el("row", "Button", [0, 1000, 200, 1060])]
        assert stitch.locate(aim, fresh) is None

    def test_a_different_identity_is_never_it(self):
        aim = {"rid": "row", "cls": "Button", "b": [0, 700, 200, 760]}
        fresh = [el("other", "Button", [0, 700, 200, 760])]
        assert stitch.locate(aim, fresh) is None
