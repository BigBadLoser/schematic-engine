"""A linear ``_extract_wires`` in place of MCP Pro's quadratic one.

MCP Pro finds each wire block with ``content[cursor:].startswith("(wire")`` at every character, which copies the rest of the
sheet each time: on a 28-part sheet with its embedded libraries one ``sch_move_symbol`` spent 5.6 of its 5.75 s there (four
scans of the sheet), and the whole placement stage outlasted the client's timeout. The seam is patched here, at server build
time, with a function that returns the same records by searching for the marker (the scan is the only change).
"""

from __future__ import annotations

import re
from typing import Any

_PTS = re.compile(r"\(pts\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)\s*\)")
_UUID = re.compile(r'\(uuid\s+"([^"]+)"\)')


def install(schematic_tools: Any) -> None:
    extract_block = schematic_tools._extract_block

    def extract_wires(content: str) -> list[dict[str, Any]]:
        wires: list[dict[str, Any]] = []
        cursor = content.find("(wire")
        while cursor >= 0:
            block, length = extract_block(content, cursor)
            if block:
                pts = _PTS.search(block)
                if pts is not None:
                    record: dict[str, Any] = {
                        "x1": float(pts.group(1)), "y1": float(pts.group(2)),
                        "x2": float(pts.group(3)), "y2": float(pts.group(4)),
                    }
                    uuid = _UUID.search(block)
                    if uuid is not None:
                        record["uuid"] = uuid.group(1)
                    wires.append(record)
                cursor = content.find("(wire", cursor + length)
            else:
                cursor = content.find("(wire", cursor + 1)
        return wires

    schematic_tools._extract_wires = extract_wires
