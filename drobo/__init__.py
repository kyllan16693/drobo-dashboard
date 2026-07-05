"""Drobo 5N status client and parser.

Reads the unauthenticated NASD XML status stream a Drobo 5N serves on TCP
port 5000 and turns it into structured Python objects. Pure standard library.
"""

from .client import DEFAULT_PORT, DroboUnreachable, read_raw
from .models import DiskSlot, DroboStatus
from .parser import DroboParseError, parse

__all__ = [
    "DEFAULT_PORT",
    "DroboUnreachable",
    "read_raw",
    "DiskSlot",
    "DroboStatus",
    "DroboParseError",
    "parse",
]
