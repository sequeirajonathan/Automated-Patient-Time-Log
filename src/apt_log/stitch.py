"""Stitch viewport captures into one whole-page document.

The accessibility tree only carries the viewport, and the owner's spec is
that the portal should never leave anyone wondering whether there is more:
every row of a scrollable page, mapped out as components. So the runner
walks the page with mid-screen swipes, capturing the tree at each step, and
this module joins the captures into a single document.

Two coordinate systems live in every stitched item, on purpose:

- ``vb`` is the virtual position on the page as a whole — layout truth,
  what the reflow sorts and groups by.
- ``b`` stays the bounds AS CAPTURED at that item's scroll step — tap
  truth. A tap below the fold replays the walk's swipes to that step and
  re-verifies the element against a fresh dump before touching anything,
  which is the same refuse-if-moved promise the tap machinery has always
  made, extended past the fold.

The offset between steps is measured, not assumed: items present in two
consecutive captures are anchors, and the median shift of the anchors is
the scroll distance. A swipe's momentum makes the nominal distance a lie
often enough that assuming it would misplace every band after the first.
"""

from __future__ import annotations

from statistics import median

# An item is the same item in two captures when identity and horizontal
# geometry agree; vertical position is what scrolling changes.
def _key(item: dict) -> tuple:
    return (item.get("cls", ""), item.get("rid", ""),
            item.get("txt", ""), item["b"][0], item["b"][2])


def _shift(prev: list[dict], cur: list[dict]) -> float | None:
    """The scroll distance between two captures, from shared anchors."""
    where = {}
    for item in prev:
        where.setdefault(_key(item), item["b"][1])
    deltas = [where[_key(item)] - item["b"][1]
              for item in cur if _key(item) in where]
    # Strictly positive: fixed chrome — a tab bar, a pinned header — shares
    # identity across captures at delta zero, and letting it vote would
    # report a page that never scrolls.
    deltas = [d for d in deltas if d > 0]
    return median(deltas) if deltas else None


def stitch(captures: list[dict], nominal_dy: int) -> dict:
    """Join per-step captures into one page.

    Each capture is {"elements": [...], "statics": [...]} in device
    coordinates. Items gain ``vb`` (virtual bounds), ``step`` (which
    capture they were seen in, for the tap replay), and duplicates from
    capture overlap are dropped — an item at the same virtual place with
    the same identity was simply seen twice.
    """
    offset = 0.0
    seen: set[tuple] = set()
    out_elements: list[dict] = []
    out_statics: list[dict] = []

    for step, capture in enumerate(captures):
        prev_pos: dict[tuple, int] = {}
        shift = None
        if step:
            everything_prev = (captures[step - 1].get("elements") or []) + \
                              (captures[step - 1].get("statics") or [])
            everything_cur = (capture.get("elements") or []) + \
                             (capture.get("statics") or [])
            for item in everything_prev:
                prev_pos.setdefault(_key(item), item["b"][1])
            shift = _shift(everything_prev, everything_cur)
            offset += shift if shift is not None else nominal_dy

        for kind, sink in (("elements", out_elements),
                           ("statics", out_statics)):
            for item in capture.get(kind) or []:
                # Fixed chrome scrolls nowhere: an item still at the same
                # device position while the content shifted is a pinned
                # header or tab bar, already recorded at step zero.
                if (step and shift
                        and prev_pos.get(_key(item)) == item["b"][1]):
                    continue
                vy1 = int(item["b"][1] + offset)
                vy2 = int(item["b"][3] + offset)
                identity = (kind, *_key(item), round(vy1 / 40))
                if identity in seen:
                    continue
                seen.add(identity)
                sink.append({**item,
                             "vb": [item["b"][0], vy1, item["b"][2], vy2],
                             "step": step})
    return {"elements": out_elements, "statics": out_statics,
            "steps": len(captures)}


def locate(aim: dict, elements: list[dict],
           y_tolerance: int = 90) -> dict | None:
    """The aimed element in a fresh capture at its scroll step, or None.

    Identity must match exactly; horizontal geometry must match exactly;
    the vertical position may drift by the swipe's momentum, which is why
    the replayed tap uses the FOUND bounds, never the recorded ones.
    """
    for e in elements:
        if (e.get("cls") == aim.get("cls")
                and e.get("rid") == aim.get("rid", "")
                and e["b"][0] == aim["b"][0]
                and e["b"][2] == aim["b"][2]
                and abs(e["b"][1] - aim["b"][1]) <= y_tolerance):
            return e
    return None
