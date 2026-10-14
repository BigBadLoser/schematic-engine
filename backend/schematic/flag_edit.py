"""Putting a power flag on a wire in the schematic file's text.

KiCad joins a pin to the ends of wires, never to a point along one (ERC reports "pin not connected"), so a flag that
stands on a wire needs the wire cut at the flag's pin: the two halves end where the pin is. The edit works on the file's
text with MCP Pro's own block helpers, so the symbol, its fields and the new wires are written the way MCP Pro writes
them.
"""
from __future__ import annotations

import re

Point = tuple[float, float]
Segment = tuple[Point, Point]

_WIRE = re.compile(r"\(wire\b")
_XY = re.compile(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)")
_SYMBOL_ANGLE = re.compile(r"(\(at\s+[-\d.]+\s+[-\d.]+\s+)[-\d.]+(\))")


def split_wire(content: str, segment: Segment, point: Point) -> str:
    """``content`` with the wire from one end of ``segment`` to the other cut in two at ``point``."""
    from kicad_mcp.tools import schematic as schematic_tools

    (ax, ay), (bx, by) = segment
    wanted = {(round(ax, 3), round(ay, 3)), (round(bx, 3), round(by, 3))}
    for found in _WIRE.finditer(content):
        wire, length = schematic_tools._extract_block(content, found.start())
        ends = [(float(x), float(y)) for x, y in _XY.findall(wire or "")]
        if len(ends) != 2 or {(round(x, 3), round(y, 3)) for x, y in ends} != wanted:
            continue
        # the first half takes the old wire's place (which the file indents already), the second follows it
        return (
            content[: found.start()]
            + schematic_tools.wire_block(ends[0][0], ends[0][1], point[0], point[1]).lstrip("\t") + "\n"
            + schematic_tools.wire_block(point[0], point[1], ends[1][0], ends[1][1])
            + content[found.start() + length:]
        )
    raise ValueError(f"No wire from ({ax}, {ay}) to ({bx}, {by}) to cut")


def attach_flag(content: str, reference: str, point: Point, rotation: int, segment: Segment) -> str:
    """``content`` with ``segment`` cut at ``point`` and the flag ``reference`` moved there, turned to ``rotation``.

    The flag's pin is its origin, so moving the symbol puts the pin on the cut. Its fields move with it (the field
    placement that follows chooses where its name is drawn).
    """
    from kicad_mcp.tools import schematic as schematic_tools

    content = split_wire(content, segment, point)
    match = schematic_tools._find_placed_symbol_block(content, reference)
    if match is None:
        raise ValueError(f"Reference not found: {reference}")
    block, start, end, parsed = match
    moved = schematic_tools._shift_symbol_block(block, point[0] - float(parsed["x"]), point[1] - float(parsed["y"]))
    # the symbol's own angle is the first (at x y angle) of its block
    moved = _SYMBOL_ANGLE.sub(lambda found: f"{found.group(1)}{rotation}{found.group(2)}", moved, count=1)
    moved = _hide_value(moved)
    return content[:start] + moved + content[end:]


def _hide_value(block: str) -> str:
    """``block`` with its Value property hidden (``(hide yes)`` after the ``(at ...)``, as KiCad writes it)."""
    return re.sub(
        r'(\(property\s+"Value"\s+"[^"]*"\s*\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\)(\s*))(?!\(hide)',
        lambda found: f"{found.group(1)}(hide yes){found.group(2)}", block, count=1,
    )
