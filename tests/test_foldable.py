"""The portal on the iPhone Duo: one column closed, two panes open.

Closed the Duo is a 466x678 phone and nothing changes. Open it is a landscape
890x626 whose fold runs straight down the middle, so the screen view puts the
phone's photograph on one side and the controls on the other, and every sheet
opens over the controls — never over the picture of the pad it is drawing on.
"""

import re
from pathlib import Path

PAGE = Path("src/apt_log/ui/templates/phone.html").read_text(encoding="utf-8")
JS = Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
QUERY = "(min-width: 820px) and (min-aspect-ratio: 5/4)"


def _split_block() -> str:
    start = PAGE.index("@media " + QUERY)
    return PAGE[start:PAGE.index("</style>", start)]


def _matches(width: int, height: int) -> bool:
    return width >= 820 and width / height >= 5 / 4


class TestWhichScreensSplit:
    def test_open_landscape_splits(self):
        assert _matches(890, 626)

    def test_closed_and_open_portrait_do_not(self):
        for w, h in ((466, 678), (678, 466), (626, 890), (390, 844),
                     (440, 956)):
            assert not _matches(w, h), (w, h)


class TestCssAndScriptAgree:
    def test_the_script_uses_the_same_query_as_the_styles(self):
        """The photograph is only fetched while it is showing; if the two
        disagreed the left pane would sit on a stale or empty picture."""
        assert f"const SPLIT_QUERY = '{QUERY}';" in JS
        assert "@media " + QUERY in PAGE

    def test_frames_refresh_the_photo_when_split(self):
        body = JS[JS.index("function applyFrame("):]
        body = body[:body.index("\n  }\n")]
        assert "split()" in body and "refreshPeek()" in body

    def test_the_split_rules_come_last_so_nothing_overrides_them(self):
        """`.sheet` and `#peekwrap` have base rules of the same weight; a
        media block is no heavier, so it has to come after them."""
        split_at = PAGE.index("@media " + QUERY)
        assert split_at > PAGE.index(".sheet { position:fixed")
        assert split_at > PAGE.index("#peekwrap { position:absolute")


class TestTheTwoPanes:
    def test_controls_and_sheets_sit_right_of_the_fold(self):
        block = _split_block()
        assert "#stage { margin-left:calc(50% + 14px); }" in block
        assert ".navbar { left:calc(50% + 14px); }" in block
        assert ".sheet { left:calc(50% + 14px); }" in block

    def test_the_photograph_holds_the_left_pane(self):
        block = _split_block()
        rule = re.search(r"#peekwrap \{[^}]*\}", block).group(0)
        assert "position:fixed" in rule and "left:0" in rule
        assert "width:calc(50% - 14px)" in rule

    def test_no_photograph_of_a_password_screen(self):
        assert "body.blocked #peekwrap" in _split_block()

    def test_the_home_screen_keeps_the_crease_clear(self):
        block = _split_block()
        assert "#launcher { display:grid; grid-template-columns:1fr 1fr;" \
            in block
        assert "#launcher > footer { grid-column:2;" in block
