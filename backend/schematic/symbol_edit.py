"""Edits to a placed symbol in the schematic file's text.

``set_mirror`` writes KiCad's ``(mirror x|y)`` line into a symbol instance: ``y`` flips the symbol left to right about its
anchor, ``x`` flips it top to bottom. KiCad applies the mirror after the rotation, in page space. The edit uses MCP Pro's
block helpers, so the instance stays as MCP Pro wrote it apart from the one line.
"""
from __future__ import annotations

import re

from .designators import split_unit

_MIRROR = re.compile(r"\n[ \t]*\(mirror\s+[xy]\)")
_AT = re.compile(r"(\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\))")


def find_symbol_block(content: str, reference: str):
    """The placed instance ``reference`` names, as MCP Pro's ``_find_placed_symbol_block`` returns it, or None.

    ``reference`` may name one unit of a multi-unit part (``U5:2``, spec 4c): the instance of ``U5`` drawn as unit 2. A plain
    reference is its first instance, as MCP Pro finds it."""
    from kicad_mcp.tools import schematic as schematic_tools

    part, unit = split_unit(reference)
    if unit is not None:
        for match in schematic_tools._find_placed_symbol_blocks(content, part):
            if int(match[3].get("unit", 1) or 1) == unit:
                return match
    return schematic_tools._find_placed_symbol_block(content, reference)


_ANY_AT = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(\s+-?[\d.]+)?\)")


def move_symbol(content: str, reference: str, x_mm: float, y_mm: float) -> str:
    """``content`` with the instance ``reference`` names (a unit, ``U5:2``, or a plain reference) moved so that its anchor is at
    ``(x_mm, y_mm)``: its own ``(at ...)`` and those of its field texts shift together, as MCP Pro's move does."""
    match = find_symbol_block(content, reference)
    if match is None:
        raise ValueError(f"Reference not found: {reference}")
    block, start, end, _parsed = match
    anchor = _ANY_AT.search(block)
    dx, dy = x_mm - float(anchor.group(1)), y_mm - float(anchor.group(2))
    moved = _ANY_AT.sub(
        lambda found: f"(at {_fmt(float(found.group(1)) + dx)} {_fmt(float(found.group(2)) + dy)}{found.group(3) or ''})", block)
    return content[:start] + moved + content[end:]


def set_mirror(content: str, reference: str, axis: str) -> str:
    """``content`` with the symbol ``reference`` mirrored about ``axis`` ("x", "y"), or not mirrored when ``axis`` is empty."""
    if axis not in ("", "x", "y"):
        raise ValueError(f"A mirror axis is x or y, not {axis!r}")
    match = find_symbol_block(content, reference)
    if match is None:
        raise ValueError(f"Reference not found: {reference}")
    block, start, end, _parsed = match
    cleared = _MIRROR.sub("", block, count=1)
    if axis:
        # the instance's own (at x y angle) is the first in its block; KiCad writes the mirror on the next line
        indent = re.search(r"\n([ \t]*)\(at\s", cleared)
        pad = indent.group(1) if indent else "\t\t"
        cleared = _AT.sub(lambda found: f"{found.group(1)}\n{pad}(mirror {axis})", cleared, count=1)
    return content[:start] + cleared + content[end:]


_SYMBOL_HEAD = re.compile(
    r'\n([ \t]*)\(symbol\s*\n[ \t]*\(lib_id "([^"]*)"\)\s*\n[ \t]*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
)
_VALUE_AT = re.compile(r'(\(property "Value"\s+"[^"]*"\s*)\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\)')


def upright_value_text(content: str, lib_id: str, x_mm: float, y_mm: float, beyond_mm: float = 3.81) -> str:
    """``content`` with the Value text of the power symbol ``lib_id`` at (x, y) set at angle 0, past the symbol's far end.

    KiCad draws a power symbol turned half a way round (a rail that hangs from its pin) with its Value text at the
    rotated angle, which reads upside down. In KiCad's own sheets the text keeps angle 0 and sits past the symbol:
    below the anchor for a symbol at 180, above it otherwise. The text stays centred on the symbol's axis.
    """
    for head in _SYMBOL_HEAD.finditer(content):
        if head.group(2) != lib_id or abs(float(head.group(3)) - x_mm) > 0.01 or abs(float(head.group(4)) - y_mm) > 0.01:
            continue
        angle = float(head.group(5)) % 360
        end = content.find("\n" + head.group(1) + ")", head.end())
        block_end = len(content) if end < 0 else end
        y = y_mm + beyond_mm if angle == 180 else y_mm - beyond_mm
        place = f"(at {head.group(3)} {_fmt(y)} 0)"
        block = content[head.end():block_end]
        edited = _VALUE_AT.sub(lambda found: found.group(1) + place, block, count=1)
        if edited != block:
            return content[:head.end()] + edited + content[block_end:]
    return content


def _fmt(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")
