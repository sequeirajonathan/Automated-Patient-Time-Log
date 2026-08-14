"""Status UI.

Only /healthz exists so far. It is not a placeholder: scripts/manager.sh gates
every deploy on this endpoint, and heartbeat.sh will not ping without it, so the
deploy path cannot roll forward until it answers.

Binds loopback; `tailscale serve` publishes it to the tailnet (ARCHITECTURE §2).
"""

from __future__ import annotations

from fastapi import FastAPI

from apt_log import __version__

app = FastAPI(title="APT Log", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
