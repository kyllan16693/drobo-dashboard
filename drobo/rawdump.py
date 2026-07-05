"""Full, faithful dump of a Drobo NASD ``<ESATMUpdate>`` document.

Where :mod:`drobo.parser` distils the status stream down to the handful of
fields the overview page needs, this module does the opposite: it converts the
*entire* document into a nested, JSON-serialisable structure so the ``/stats``
details page can surface literally everything the device sends — every
top-level element, the full per-slot detail in ``mSlotsExp``, the
``mLUNUpdates`` entries, the ``DroboApps`` block, firmware feature/threshold
fields and so on.

Design choices (documented, per the spec):

* **All leaf values are returned as strings**, exactly as they appear on the
  wire (whitespace-stripped). This is lossless and — importantly — avoids
  JavaScript's 2^53 integer-precision problem for the very large identifiers
  the device emits (e.g. ``mDiskPackID`` = ``8003085745683487579``). The
  frontend parses the numeric fields it needs. Empty/self-closing elements
  (``<mTargetName/>``) become ``""``.
* **Repeated ``<nX>`` child nodes become a JSON list**, ordered by their
  numeric suffix. Both ``mSlotsExp`` (``n0``..``n5``) and ``mLUNUpdates``
  (``n0``..) use this pattern, so they surface as arrays of objects.
* Every other element becomes a dict keyed by tag. On the off chance a tag
  repeats within the same parent, the values are collected into a list.

The billion-laughs guard is copied verbatim from :mod:`drobo.parser`: the
NASD port is unauthenticated, so a spoofed/compromised device could otherwise
feed us an entity-expansion bomb. A genuine Drobo document never carries a DTD.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Matches the ``n0``, ``n1``, ... list-item nodes used inside ``mSlotsExp`` and
# ``mLUNUpdates``.
_N_NODE = re.compile(r"^n\d+$")

# A genuine NASD document nests only a handful of levels deep (root ->
# mSlotsExp -> nX -> leaf). Cap recursion well above that so a spoofed device
# on this unauthenticated port cannot feed us a deeply-nested (yet DTD-free,
# so it slips past the entity guard) document that overflows the interpreter's
# recursion limit and 500s the endpoint.
_MAX_DEPTH = 64


class RawDumpError(Exception):
    """Raised when the status XML cannot be dumped (bad/spoofed document)."""


def _leaf_value(el: ET.Element) -> str:
    """Return a leaf element's text as a stripped string ("" if empty)."""
    text = el.text
    if text is None:
        return ""
    return text.strip()


def _element_to_obj(el: ET.Element, depth: int = 0):
    """Recursively convert an element into dict / list / str.

    - No children -> string leaf value.
    - Children all named ``nX`` -> list (ordered by numeric suffix).
    - Otherwise -> dict keyed by child tag (repeated tags collapse to a list).
    """
    if depth > _MAX_DEPTH:
        raise RawDumpError("status document nested too deeply; refusing to parse")

    children = list(el)
    if not children:
        return _leaf_value(el)

    child_tags = [child.tag for child in children]
    if all(_N_NODE.match(tag) for tag in child_tags):
        ordered = sorted(children, key=lambda c: int(c.tag[1:]))
        return [_element_to_obj(child, depth + 1) for child in ordered]

    result: dict = {}
    for child in children:
        value = _element_to_obj(child, depth + 1)
        if child.tag in result:
            existing = result[child.tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[child.tag] = [existing, value]
        else:
            result[child.tag] = value
    return result


def raw_dump(xml_text: str) -> dict:
    """Convert a NASD ``<ESATMUpdate>`` document into a full nested dict.

    Args:
        xml_text: The raw XML string returned by :func:`drobo.client.read_raw`.

    Returns:
        A JSON-serialisable dict of every top-level element in the document.
        Nested containers (``mSlotsExp``, ``mLUNUpdates``, ``DroboApps``) are
        preserved; ``nX`` node groups become lists.

    Raises:
        RawDumpError: if the document carries a DTD (rejected outright), is not
            well-formed XML, or does not have the expected ``<ESATMUpdate>``
            root element.
    """
    # A genuine Drobo document never contains a DTD. Reject one outright so a
    # spoofed/compromised device on this unauthenticated port cannot trigger an
    # entity-expansion ("billion laughs") memory-exhaustion attack.
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise RawDumpError("status document contains a DTD; refusing to parse")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RawDumpError(f"invalid status XML: {exc}") from exc

    if root.tag != "ESATMUpdate":
        raise RawDumpError(f"unexpected root element <{root.tag}>")

    obj = _element_to_obj(root)
    if not isinstance(obj, dict):
        # A well-formed <ESATMUpdate> always has child elements; guard anyway so
        # callers can rely on a dict.
        raise RawDumpError("status document has no elements to dump")
    return obj
