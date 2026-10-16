"""The drawer shows what landed, and asks before throwing it away.

Recorded on 15 October (signing-record/20261015-200605): a saved signature
pressed from the pencil drawer landed on the phone six times out of six. The
presses that followed were the name again (2s after one landed), Descartar
firma twice, and the pad's ✕ — never because the ink was missing, always
because nothing in the drawer showed it arriving.
"""

import inspect
from pathlib import Path
from unittest.mock import patch

from apt_log import sign

JS = Path("src/apt_log/ui/static/phone.js").read_text(encoding="utf-8")
PAGE = Path("src/apt_log/ui/templates/phone.html").read_text(encoding="utf-8")

PAD_TREE = ('<node bounds="[0,56][2250,1080]"/>'
            '<node resource-id="x:id/drawing_view_participant" '
            'bounds="[59,179][2211,787]"/>')


class TestWhereTheInkWent:
    def test_the_box_is_a_fraction_of_the_whole_screen(self):
        """The panel's size, not the tree's: the tree stops short of the
        system bars and the mirror's picture does not."""
        with patch("apt_log.feed.screen_size", return_value=[1080, 2340]):
            box = sign._screen_fractions([59, 179, 2211, 787], PAD_TREE)
        assert box == [round(59 / 2340, 4), round(179 / 1080, 4),
                       round(2211 / 2340, 4), round(787 / 1080, 4)]

    def test_an_upright_page_is_measured_upright(self):
        tree = '<node bounds="[0,0][1080,2250]"/>'
        with patch("apt_log.feed.screen_size", return_value=[1080, 2340]):
            box = sign._screen_fractions([0, 1170, 1080, 2340], tree)
        assert box == [0.0, 0.5, 1.0, 1.0]

    def test_an_unknown_screen_gives_no_box_rather_than_a_wrong_one(self):
        with patch("apt_log.feed.screen_size", return_value=[0, 0]):
            assert sign._screen_fractions([1, 2, 3, 4], PAD_TREE) == []

    def test_the_status_carries_it_across_processes(self, tmp_path):
        path = tmp_path / "s.json"
        sign.write_status(sign.Status(id="a", state="done",
                                      box=[0.1, 0.2, 0.9, 0.7]), path)
        assert sign.read_status(path).box == [0.1, 0.2, 0.9, 0.7]

    def test_an_old_status_file_still_reads(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text('{"id": "a", "state": "done", "reason": "", '
                        '"digest": "", "kind": "", "at": ""}')
        assert sign.read_status(path).box == []

    def test_the_replay_records_it(self):
        assert "status.box = _screen_fractions(bounds, xml)" in \
            inspect.getsource(sign.execute)

    def test_only_a_finished_replay_pushes_it(self):
        src = Path("src/apt_log/ui/app.py").read_text(encoding="utf-8")
        assert ('if sig.state == "done" and not sig_kind and sig.box:'
                in src)


class TestTheDrawerShowsIt:
    def test_the_proof_sits_between_the_hint_and_step_two(self):
        hint = PAGE.index('id="sign-hint"')
        proof = PAGE.index('id="sign-proof"')
        step_two = PAGE.index('id="sign-approw"')
        assert hint < proof < step_two

    def test_a_landed_replay_shows_it(self):
        body = JS[JS.index("function applySign("):JS.index("function markDrawn(")]
        assert "showProof(s.box)" in body

    def test_it_goes_when_the_signature_does(self):
        body = JS[JS.index("function markDrawn("):]
        body = body[:body.index("\n  }\n")]
        assert "if (!on) stopProof();" in body

    def test_and_when_the_pad_does(self):
        assert "if (!padHere && isDrawn()) markDrawn(false);" in JS

    def test_the_toast_names_the_apps_own_confirm(self):
        assert "i18n.signNowConfirm.replace('{btn}', confirmWord)" in JS


class TestItAsksBeforeThrowingItAway:
    def test_descartar_asks_once_a_signature_has_landed(self):
        assert 'button[data-kind="discard"]' in JS
        guard = JS[JS.index('button[data-kind="discard"]'):]
        guard = guard[:guard.index("}, true);")]
        assert "isDrawn()" in guard and "window.confirm" in guard
        assert "stopImmediatePropagation" in guard

    def test_the_drawers_x_asks_too_because_it_presses_the_apps_x(self):
        close = JS[JS.index("padClose.addEventListener('click'"):]
        close = close[:close.index("body.classList.remove('signing');")]
        assert "padDismiss && isDrawn()" in close
        assert "window.confirm" in close

    def test_pressing_the_name_again_asks_first(self):
        row = JS[JS.index("adoptedRow.addEventListener('click'"):]
        row = row[:row.index("applyAdopted(b.dataset.name);")]
        assert "isDrawn()" in row and "window.confirm" in row

    def test_every_new_sentence_is_in_both_languages(self):
        import json

        for lang in ("en", "es"):
            d = json.loads(Path(f"src/apt_log/ui/locales/{lang}.json")
                           .read_text(encoding="utf-8"))
            for key in ("sign.proof", "sign.now_confirm", "sign.again_ask",
                        "sign.discard_ask"):
                assert d[key], (lang, key)
            for key in ("sign.now_confirm", "sign.again_ask",
                        "sign.discard_ask"):
                assert "{btn}" in d[key], (lang, key)
