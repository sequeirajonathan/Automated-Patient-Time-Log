"""Page objects.

Selectors live **only** in this package (REQUIREMENTS §5, maintainability). An app
update will break them; when it does, the blast radius should be one file per
screen rather than a search across the codebase.
"""

from apt_log.screens.login import LoginScreen, LoginSelectors

__all__ = ["LoginScreen", "LoginSelectors"]
