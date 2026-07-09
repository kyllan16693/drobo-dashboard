"""Best-effort push notifications via ntfy (https://ntfy.sh or self-hosted).

Reads the target from the NTFY_URL environment variable. If unset, every call
is a silent no-op — this feature is entirely optional and must never affect
polling or request handling if the server is slow/unreachable/misconfigured.
"""

from __future__ import annotations

import os
import urllib.request

_TIMEOUT = 5.0


def notify(title: str, message: str, priority: str = "4", tags: str = "warning") -> None:
    """POST a notification to NTFY_URL. No-ops if unset; never raises."""
    url = os.environ.get("NTFY_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url,
            data=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT)
    except Exception:
        pass
