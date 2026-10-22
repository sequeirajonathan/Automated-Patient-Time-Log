"""HHAeXchange+ signs in with Chrome's saved login before the stored one.

21 October: the stored login was refused twice; the caregiver then signed in
from the portal by tapping the suggestion Chrome shows under the email box.
That suggestion's text was 22 characters and the stored email 23 — the login
that works is the one Chrome saved, and Chrome keeps it current.
"""

import itertools
from unittest.mock import MagicMock, patch

from apt_log import macros
from apt_log.secrets import APP_PASSWORD, APP_USERNAME, MemorySecretProvider


def _form(chip_offered: bool):
    driver = MagicMock()
    driver.current_package = "com.hhaexchange.uma"
    driver.current_activity = "com.hhaexchange.uma.HomeActivity"
    driver.page_source = '<a clickable="true"/>' * 5

    state = {"filled": False}
    email = MagicMock()
    email.get_attribute.side_effect = \
        lambda _a: "a saved login" if state["filled"] else ""
    password = MagicMock()
    password.rect = {"x": 90, "y": 877, "width": 900, "height": 93}
    submit = MagicMock()
    submit.rect = {"x": 90, "y": 1150, "width": 900, "height": 110}
    eye = MagicMock()
    eye.rect = {"x": 891, "y": 897, "width": 79, "height": 56}
    chip = MagicMock()
    chip.click.side_effect = lambda: state.__setitem__("filled", True)

    def find_elements(by, selector):
        if by == "xpath" and selector == '//*[@resource-id="email"]':
            return [email]
        if by == "xpath" and selector == '//*[@resource-id="password"]':
            return [password]
        if by == "xpath" and selector == macros.SAVED_LOGIN_CHIP:
            return [chip] if chip_offered else []
        if by == "class name":
            return [eye, submit]
        return []
    driver.find_elements.side_effect = find_elements
    return driver, email, password, submit, chip


def _run(driver):
    provider = MemorySecretProvider(**{APP_USERNAME: "stored@example.com",
                                       APP_PASSWORD: "stored-pw"})
    with patch("apt_log.macros.wake_display"), \
         patch("apt_log.secrets.FileSecretProvider", return_value=provider), \
         patch("apt_log.macros.time.sleep"), \
         patch("apt_log.macros.time.monotonic",
               side_effect=itertools.count(step=0.5)):
        macros.MACROS["hhax_uma_login"].run(driver, lambda _k: None)


class TestChromesSavedLoginFirst:
    def test_the_saved_login_is_pressed_and_nothing_is_typed(self):
        driver, email, password, submit, chip = _form(chip_offered=True)
        _run(driver)
        email.click.assert_called()          # focus, which offers the chip
        chip.click.assert_called_once()
        email.send_keys.assert_not_called()
        password.send_keys.assert_not_called()
        submit.click.assert_called_once()

    def test_without_one_the_stored_login_is_typed_as_before(self):
        driver, email, password, submit, chip = _form(chip_offered=False)
        _run(driver)
        chip.click.assert_not_called()
        email.send_keys.assert_called_once_with("stored@example.com")
        password.send_keys.assert_called_once_with("stored-pw")
        submit.click.assert_called_once()

    def test_the_chip_is_known_by_its_key_icon_not_by_position(self):
        """The accessory has other buttons (the key menu, cards, addresses);
        only the saved-login chip holds the start icon."""
        assert "chip_view_start_icon" in macros.SAVED_LOGIN_CHIP
        assert '@clickable="true"' in macros.SAVED_LOGIN_CHIP
