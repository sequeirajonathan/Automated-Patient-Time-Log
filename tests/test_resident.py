"""The resident Appium session, and the timeout that keeps one hung command
from freezing the whole runner."""

from unittest.mock import MagicMock

from apt_log import resident


class TestBoundCommandTimeout:
    """A hung driver call once held the session lock for an hour. Every
    command now carries a socket timeout so a dead request raises instead."""

    def test_the_timeout_lands_on_the_client_config(self):
        driver = MagicMock()
        resident._bound_command_timeout(driver)
        assert (driver.command_executor.client_config.timeout
                == resident.COMMAND_HTTP_TIMEOUT)

    def test_a_driver_without_a_command_executor_is_left_alone(self):
        class Bare:
            command_executor = None

        resident._bound_command_timeout(Bare())  # must not raise

    def test_it_falls_back_to_set_timeout_when_no_client_config(self):
        """Older clients expose the socket timeout as a method, not a
        config attribute. The bound must still land."""
        driver = MagicMock()
        driver.command_executor.client_config = None
        resident._bound_command_timeout(driver)
        driver.command_executor.set_timeout.assert_called_once_with(
            resident.COMMAND_HTTP_TIMEOUT)
