"""Sequential power-symbol references (#PWR01, #PWR02, ...) in place of MCP Pro's random ones.

MCP Pro names a placed power symbol ``#PWR<n>`` with n the first 16 bits of a fresh uuid. Two builds of one intent then
differ in every power reference, parallel builds can collide (two ``#PWR32462`` broke a build), and anything ordered by
reference (the field solve) follows the randomness. The upstream package keeps that behaviour, so the seam is patched
here, at server build time: ``power_reference_from_uuid`` is rebound on the modules that import it, to a function that
numbers from the highest ``#PWR`` already on the active sheet.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

_PWR = re.compile(r'\(property "Reference" "#PWR(\d+)"')
_lock = threading.Lock()
# path -> (file signature when last issued, last number issued). References are handed out before the block is written,
# so a batch that places several asks repeatedly while the sheet is unchanged; a write changes the signature.
_issued: dict[str, tuple[tuple[int, int], int]] = {}


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def highest_power_number(text: str) -> int:
    return max((int(n) for n in _PWR.findall(text)), default=0)


def format_reference(number: int) -> str:
    return f"#PWR{number:02d}"


def next_power_reference(path: Path) -> str:
    key = str(path)
    with _lock:
        signature = _signature(path)
        known = _issued.get(key)
        if known is not None and known[0] == signature:
            number = known[1] + 1
        else:
            number = highest_power_number(path.read_text(encoding="utf-8")) + 1
        _issued[key] = (signature, number)
    return format_reference(number)


def install(schematic_tools: Any) -> None:
    """Rebind ``power_reference_from_uuid`` in every kicad_mcp module that imported it."""
    import kicad_mcp.schematic.basic_authoring as basic
    import kicad_mcp.schematic.connectivity_authoring as connectivity
    import kicad_mcp.schematic.references as references

    def sequential(_uuid_value: str) -> str:
        return next_power_reference(schematic_tools._get_schematic_file())

    for module in (references, basic, connectivity):
        module.power_reference_from_uuid = sequential
