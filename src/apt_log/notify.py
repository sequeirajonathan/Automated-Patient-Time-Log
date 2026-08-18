"""Reach somebody who is not looking at the portal.

There is exactly one thing this system cannot do for itself and cannot wait
out: a login code texted to a real phone. Everything else it either does or
refuses; this one it has to ask for, and the asking only works if it arrives
where she already is — her phone's notification shade, not a web page she
would have to think to open.

**This is the alerting path REQ-9 already built**, not a new one. `alert.sh`
speaks ntfy and Pushover, both of which deliver to iOS, and it is already
wired into the deploy gate and every unit's OnFailure. Adding a service
worker and web push to send one sentence would mean a second outbound
channel to configure, break and forget — and the phone view has gone without
a service worker on purpose since it was written.

Two rules, inherited from the script and worth restating because callers
depend on them:

- **It never raises.** A notification that fails a sign-in is worse than a
  missed notification.
- **It is never silent about failing.** With no channel configured the script
  says so in the journal, because a quiet alerting path is indistinguishable
  from a healthy one, which is the failure this whole project exists to
  avoid.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Beside this package on the Pi: /opt/aptlog/scripts/alert.sh, with the code
# at /opt/aptlog/src. Resolved rather than hard-coded so a checkout anywhere
# finds its own copy.
ALERT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "alert.sh"

# Long enough to cross the internet, short enough that nothing waits on it.
TIMEOUT = 20.0


def send(message: str, url: str = "") -> bool:
    """Push one sentence outward. True if the script was reached at all.

    `url` makes the notification tappable — the whole point on a phone, where
    the alternative is remembering which bookmark opens the portal.
    """
    script = ALERT_SCRIPT
    if not script.exists():
        log.warning("no alert script at %s — nothing sent", script)
        return False
    cmd = [str(script)]
    if url:
        cmd += ["--url", url]
    cmd.append(message)
    try:
        subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, check=False)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        # Deliberately swallowed. Callers use this on paths where failing
        # would cost more than the message is worth.
        log.warning("could not send an alert (%s)", exc)
        return False


def available() -> bool:
    """Whether an alert could go out at all — the script exists and curl is
    there to carry it. Not whether a channel is CONFIGURED: only the script
    can see /etc/aptlog/alert.env, and it says so itself when it is empty."""
    return ALERT_SCRIPT.exists() and bool(shutil.which("curl"))
