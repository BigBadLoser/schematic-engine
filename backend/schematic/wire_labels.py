"""Names on wires and captions over blocks: which nets deserve a name, and where the name and the caption go.

Engineers write a net's name on its wire (the LM317 drawing writes ``rect``, ``adj``, ``out``) and put a caption over
each functional block. The engine drew neither. This module is pure: it knows no file and no tool, only intent and
millimetre geometry.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence

from .intent import NetIntent, SchematicIntent
from .presentation_geometry import Bounds, label_text_bounds

GRID_MM = 1.27
SLIDE_MM = 2.54
CAPTION_GAP_MM = 2.54
CAPTION_MAX_LIFTS = 4
END_MARGIN_MM = 1.27
CLEARANCE_MM = 0.2
_AUTO_NAME = re.compile(r"^(N_|Net-|unconnected)|^N\$")

Point = tuple[float, float]
Segment = tuple[Point, Point]


def is_auto_name(name: str) -> bool:
    return bool(_AUTO_NAME.match(name.strip()))


def nets_to_name(intent: SchematicIntent) -> list[NetIntent]:
    """Wire-drawn signal nets with a name a person chose that carry three or more pins or cross a block's edge."""
    edge = {name for block in intent.blocks for name in (*block.inputs, *block.outputs)}
    return [
        net for net in intent.all_nets
        if net.kind == "signal" and net.routing != "label" and not is_auto_name(net.name)
        and (len(net.endpoints) >= 3 or net.name in edge)
    ]


def _snap(value: float) -> float:
    return round(round(value / GRID_MM) * GRID_MM, 4)


def _label_box(point: Point, vertical: bool, width: float, height: float) -> Bounds:
    """The text of a label at ``point``: where the measured model puts it, joined with where KiCad runs it (from the anchor)."""
    model = label_text_bounds(point[0], point[1], width, height, vertical=vertical)
    if vertical:
        return Bounds(model.left, point[1] - width, model.right, model.bottom)
    return Bounds(model.left, model.top, point[0] + width, model.bottom)


def _segment_box(segment: Segment) -> Bounds:
    (ax, ay), (bx, by) = segment
    return Bounds(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _length(segment: Segment) -> float:
    (ax, ay), (bx, by) = segment
    return abs(ax - bx) + abs(ay - by)


def _clear(box: Bounds, obstacles: Sequence[Bounds]) -> bool:
    return not any(_hits(box, item) for item in obstacles)


def _hits(box: Bounds, item: Bounds) -> bool:
    return (
        box.left < item.right + CLEARANCE_MM and box.right > item.left - CLEARANCE_MM
        and box.top < item.bottom + CLEARANCE_MM and box.bottom > item.top - CLEARANCE_MM
    ) or (
        # a wire is a line of no thickness: it hits a box it passes through
        (item.right - item.left < 1e-6 or item.bottom - item.top < 1e-6)
        and box.left <= item.right and box.right >= item.left and box.top <= item.bottom and box.bottom >= item.top
    )


def plan_wire_label(
    net_name: str,
    segments: Sequence[Segment],
    obstacles: Sequence[Bounds],
    text_width_mm: float,
    text_height_mm: float,
) -> tuple[Point, int] | None:
    """Where to write ``net_name`` on its wire, as (point, rotation), or None when no stretch of it is clear.

    The longest horizontal segment first (text reads along it), then the longest vertical. The label starts at the
    segment's midpoint on the grid and slides along it in 2.54 mm steps, both ways, until its text clears every
    obstacle and every other segment of the same net.
    """
    usable = [s for s in segments if _length(s) >= 2 * END_MARGIN_MM + GRID_MM and (s[0][0] == s[1][0] or s[0][1] == s[1][1])]
    horizontal = sorted((s for s in usable if s[0][1] == s[1][1]), key=_length, reverse=True)
    vertical = sorted((s for s in usable if s[0][0] == s[1][0]), key=_length, reverse=True)
    for segment in (*horizontal, *vertical):
        is_vertical = segment[0][0] == segment[1][0]
        lo = min(segment[0][1], segment[1][1]) if is_vertical else min(segment[0][0], segment[1][0])
        hi = max(segment[0][1], segment[1][1]) if is_vertical else max(segment[0][0], segment[1][0])
        lo, hi = lo + END_MARGIN_MM, hi - END_MARGIN_MM
        middle = _snap((lo + hi) / 2)
        others = [_segment_box(s) for s in segments if s != segment]
        steps = int(math.ceil((hi - lo) / SLIDE_MM))
        offsets = [0.0]
        for step in range(1, steps + 1):
            offsets += [step * SLIDE_MM, -step * SLIDE_MM]
        for offset in offsets:
            along = _snap(middle + offset)
            if not lo - 1e-6 <= along <= hi + 1e-6:
                continue
            point = (segment[0][0], along) if is_vertical else (along, segment[0][1])
            box = _label_box(point, is_vertical, text_width_mm, text_height_mm)
            if _clear(box, obstacles) and _clear(box, others):
                return point, 90 if is_vertical else 0
    return None


def plan_caption(
    block_bounds: Bounds,
    text: str,
    obstacles: Sequence[Bounds],
    text_width_mm: float,
    text_height_mm: float,
) -> Point | None:
    """The left-bottom point of a caption centred above a block, lifted by 2.54 mm until clear (at most four times)."""
    x = _snap((block_bounds.left + block_bounds.right) / 2 - text_width_mm / 2)
    for lift in range(CAPTION_MAX_LIFTS + 1):
        y = _snap(block_bounds.top - CAPTION_GAP_MM - lift * SLIDE_MM)
        if _clear(Bounds(x, y - text_height_mm, x + text_width_mm, y), obstacles):
            return x, y
    return None
