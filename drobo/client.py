"""Low-level reader for the Drobo NASD status stream (TCP port 5000).

A Drobo 5N (and the older DroboFS) run a status daemon that, the moment a TCP
client connects, emits a short binary frame header (the ASCII ``DRINASD`` plus
a couple of framing bytes) immediately followed by an XML ``<ESATMUpdate>``
document. If the socket is left open the device re-sends an updated document
roughly every 10-20 seconds.

This module connects, reads until it has one complete ``</ESATMUpdate>``
document, strips the binary prefix, and returns the XML as text. A socket
timeout is used as a safety net so an unresponsive device never blocks the
caller forever.

The framing approach here was cross-checked against two open-source readers of
the same stream:
  * AndrewMobbs/drobomon    - read-all with a read deadline, then find ``<?xml``.
  * cosmouser/drobo_exporter - line-scan, capture from ``<ESATMUpdate>`` and
                               stop at ``</ESATMUpdate>``.
We combine both: read until the closing tag (a definite terminator) with a
timeout fallback, then slice from the XML declaration.
"""

from __future__ import annotations

import socket

DEFAULT_PORT = 5000
DEFAULT_TIMEOUT = 5.0

_XML_START = b"<?xml"
_DOC_OPEN = b"<ESATMUpdate>"
_DOC_CLOSE = b"</ESATMUpdate>"

_CHUNK = 8192
_MAX_BYTES = 1 << 20  # 1 MiB hard cap; a real document is only ~30 KiB.


class DroboUnreachable(Exception):
    """Raised when the Drobo status port cannot be reached or fully read."""


def read_raw(
    host: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Return one complete ``<ESATMUpdate>`` XML document from ``host`` as text.

    Args:
        host: Drobo IP or hostname.
        port: NASD status port (default 5000).
        timeout: Per-operation socket timeout, in seconds.

    Raises:
        DroboUnreachable: on connect/read failure, timeout, or if no complete
            document arrives.
    """
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise DroboUnreachable(f"cannot connect to {host}:{port}: {exc}") from exc

    buf = bytearray()
    try:
        conn.settimeout(timeout)
        while _DOC_CLOSE not in buf:
            try:
                chunk = conn.recv(_CHUNK)
            except TimeoutError as exc:
                raise DroboUnreachable(
                    f"timed out reading status from {host}:{port} after {timeout}s"
                ) from exc
            except OSError as exc:
                raise DroboUnreachable(f"read error from {host}:{port}: {exc}") from exc
            if not chunk:
                break  # peer closed the connection
            buf += chunk
            if len(buf) > _MAX_BYTES:
                raise DroboUnreachable(
                    f"status document from {host}:{port} exceeded {_MAX_BYTES} bytes"
                )
    finally:
        conn.close()

    close_idx = buf.find(_DOC_CLOSE)
    if close_idx == -1:
        raise DroboUnreachable(f"no complete <ESATMUpdate> document received from {host}:{port}")
    end = close_idx + len(_DOC_CLOSE)

    # Drop the leading binary "DRINASD" framing by starting at the XML
    # declaration, falling back to the root element if it is absent.
    start = buf.find(_XML_START)
    if start == -1 or start > end:
        start = buf.find(_DOC_OPEN)
    if start == -1 or start > end:
        raise DroboUnreachable(f"malformed status document from {host}:{port}")

    return buf[start:end].decode("utf-8", errors="replace")
