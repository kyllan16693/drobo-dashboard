"""Minimal, safe client for the Drobo NASD *command* channel (TCP 5001).

Where :mod:`drobo.client` only *reads* the status stream on port 5000, this
module can *send* a small, curated set of commands to the ``nasd`` daemon on
port 5001. The protocol (``DIRNETTM`` framing) is the one the defunct Drobo
Dashboard used; it was reverse-engineered publicly by ISE Labs (``nasty.py``,
CVE-2018-14709) and is completely unauthenticated — the only "credential" is the
device serial, which port 5000 hands out for free.

Deliberate scope: this module exposes **only** actions we consider safe:

* read-only getters — :func:`get_sysinfo` (opcode 61: device temperature +
  uptime) and :func:`get_network` (opcode 30) — which change nothing;
* :func:`identify` / :func:`stop_identify` (opcode 26: blink the LEDs; cosmetic,
  self-reverting);
* :func:`restart` (opcode 21).

It intentionally does **not** implement the dangerous opcodes (set-admin 31,
install-app 78, "popit" root shell) even though the protocol supports them. The
web layer additionally gates the write helpers behind the ``DROBO_ENABLE_CONTROL``
flag and a typed confirmation for restart; see app.py.

Framing (from nasty.py, the authoritative PoC):

    preamble = b"DIRNETTM" + <type byte> + b"\\x01\\x00\\x00" + <4-byte BE size>
      handshake type byte = 0x07, command type byte = 0x0a
    handshake body = 16s(serial) + u32(0) + 16s(serial) + 184x
    command  body  = b" <opcode> <args...> <serial> " + b"\\x00"

The stat port (5000) connection must be open for the cmd port to respond, so we
open both, keep the stat socket open for the exchange, send exactly one command,
then close both (no long-lived sockets).
"""

from __future__ import annotations

import re
import socket
import struct

_HANDSHAKE_PREAMBLE = b"\x44\x52\x49\x4e\x45\x54\x54\x4d\x07\x01\x00\x00"  # DIRNETTM + 07 01 00 00
_CMD_PREAMBLE = b"\x44\x52\x49\x4e\x45\x54\x54\x4d\x0a\x01\x00\x00"        # DIRNETTM + 0a 01 00 00
_PREAMBLE_LEN = 16  # 12 static bytes + 4-byte big-endian size

DEFAULT_STAT_PORT = 5000
DEFAULT_CMD_PORT = 5001
DEFAULT_TIMEOUT = 8.0

_RECV_CHUNK = 4096
_MAX_MSG = 1 << 20  # never buffer more than 1 MiB from an untrusted device
_SERIAL_RE = re.compile(rb"<mSerial>([^<]+)</mSerial>")
# A genuine Drobo serial is short and alphanumeric. Refuse anything else so a
# spoofed stat stream can't smuggle extra tokens/spaces into a command body.
_SERIAL_OK = re.compile(r"^[A-Za-z0-9]{4,32}$")


class DroboControlError(Exception):
    """Raised when a command to the Drobo cmd channel cannot be completed."""


def _validate_serial(serial: str | None) -> str:
    serial = (serial or "").strip()
    if not _SERIAL_OK.match(serial):
        raise DroboControlError(f"refusing to use implausible serial {serial!r}")
    return serial


def _recv_message(sock: socket.socket) -> str:
    """Read one framed reply: 16-byte preamble (size in last 4) then the body."""
    preamble = b""
    while len(preamble) < _PREAMBLE_LEN:
        chunk = sock.recv(_PREAMBLE_LEN - len(preamble))
        if not chunk:
            raise DroboControlError("connection closed during preamble")
        preamble += chunk
    msg_len = struct.unpack(">I", preamble[-4:])[0]
    if msg_len <= 0:
        return ""
    if msg_len > _MAX_MSG:
        raise DroboControlError(f"reply too large ({msg_len} bytes); refusing")
    body = b""
    while len(body) < msg_len:
        chunk = sock.recv(min(_RECV_CHUNK, msg_len - len(body)))
        if not chunk:
            break
        body += chunk
    return body.decode("utf-8", errors="replace").rstrip("\x00").strip()


def _send_handshake(sock: socket.socket, serial: str) -> None:
    serial_bytes = serial.encode("utf-8")
    body = struct.pack("16s", serial_bytes)
    body += struct.pack(">I", 0)
    body += struct.pack("16s", serial_bytes)
    body += struct.pack("184x")
    sock.sendall(_HANDSHAKE_PREAMBLE + struct.pack(">I", len(body)) + body)


def _send_command(sock: socket.socket, body_str: str) -> None:
    body = body_str.encode("utf-8") + b"\x00"
    sock.sendall(_CMD_PREAMBLE + struct.pack(">I", len(body)) + body)


def _read_serial_from(sock: socket.socket) -> str:
    """Extract ``mSerial`` from the stat stream this socket is receiving."""
    buf = b""
    while len(buf) < 65536:
        chunk = sock.recv(_RECV_CHUNK)
        if not chunk:
            break
        buf += chunk
        m = _SERIAL_RE.search(buf)
        if m:
            return m.group(1).decode("utf-8", errors="replace").strip()
    raise DroboControlError("could not read serial from stat port")


def _exchange(
    host: str,
    template: str,
    serial: str | None = None,
    stat_port: int = DEFAULT_STAT_PORT,
    cmd_port: int = DEFAULT_CMD_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Open stat+cmd, handshake, send one command, return the reply string.

    ``template`` is an ASCII command body containing the literal token
    ``{serial}`` (e.g. ``" 26 900 {serial} "``); the serial is substituted after
    validation. If ``serial`` is None it is read from the stat stream.
    """
    try:
        stat = socket.create_connection((host, stat_port), timeout)
    except OSError as exc:
        raise DroboControlError(f"cannot connect to stat port {host}:{stat_port}: {exc}") from exc
    try:
        stat.settimeout(timeout)
        resolved = _validate_serial(serial if serial is not None else _read_serial_from(stat))
        body_str = template.replace("{serial}", resolved)
        try:
            cmd = socket.create_connection((host, cmd_port), timeout)
        except OSError as exc:
            raise DroboControlError(f"cannot connect to cmd port {host}:{cmd_port}: {exc}") from exc
        try:
            cmd.settimeout(timeout)
            _send_handshake(cmd, resolved)
            _recv_message(cmd)  # handshake ack — discarded
            _send_command(cmd, body_str)
            return _recv_message(cmd)
        finally:
            cmd.close()
    finally:
        stat.close()


# --- curated safe commands --------------------------------------------------

def get_sysinfo(host: str, serial: str | None = None, **kw) -> str:
    """Opcode 61 — read-only device system info (temperature + uptime)."""
    return _exchange(host, " 61 {serial} ", serial=serial, **kw)


def get_network(host: str, serial: str | None = None, **kw) -> str:
    """Opcode 30 — read-only network configuration."""
    return _exchange(host, " 30 {serial} Network ", serial=serial, **kw)


def identify(host: str, seconds: int = 900, serial: str | None = None, **kw) -> str:
    """Opcode 26 — blink all LEDs for ``seconds`` (cosmetic, self-reverting)."""
    return _exchange(host, f" 26 {max(0, int(seconds))} {{serial}} ", serial=serial, **kw)


def stop_identify(host: str, serial: str | None = None, **kw) -> str:
    """Opcode 26 with 0 seconds — stop blinking early."""
    return _exchange(host, " 26 0 {serial} ", serial=serial, **kw)


def restart(host: str, serial: str | None = None, **kw) -> str:
    """Opcode 21 — restart the device. The caller MUST confirm/guard this."""
    return _exchange(host, " 21 {serial} ", serial=serial, **kw)
