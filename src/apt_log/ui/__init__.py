"""Status and signature UI.

`app` is re-exported so aptlog-ui.service can keep pointing at
`uvicorn apt_log.ui:app` — the unit predates this becoming a package.
"""

from apt_log.ui.app import app

__all__ = ["app"]
