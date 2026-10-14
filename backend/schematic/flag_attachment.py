"""Where a PWR_FLAG goes: on a wire of the net it marks.

A power flag tells ERC that a net has a source. It carries no signal, so a drawing needs it nowhere in particular
on its net, and a person puts it on a wire that is already there, standing off the wire with its diamond and name.
The engine used to give each flag a stub and a supply or ground symbol of its own, which left a floating pair of
symbols beside the circuit for every flag (FINDINGS section 13).

This planner finds the place: a grid point along one of the net's wires (which the caller splits there, for KiCad
joins a pin to the ends of wires only), and the way the flag stands off the wire
(up or down from a horizontal wire, left or right from a vertical one), where its graphic and its name keep clear of
everything else. It is pure: wires, boxes and pins in, a point and a rotation out.
"""
from __future__ import annotations

from dataclasses import dataclass

from .presentation_geometry import Bounds
from .terminal_placement import _touches_bbox

Point = tuple[float, float]
Segment = tuple[Point, Point]

# PWR_FLAG upright (rotation 0) stands up the page from its pin: a 2.54 mm stem and diamond, 2.03 mm wide, and its name
# centred 3.81 mm above the pin. Rotation 90 stands it to the left, 180 down, 270 to the right (measured on placed flags).
FLAG_STEM_MM = 2.54
FLAG_HALF_WIDTH_MM = 1.016
FLAG_TEXT_OFFSET_MM = 3.81
FLAG_TEXT_HEIGHT_MM = 1.27
DIRECTIONS: dict[int, Point] = {0: (0.0, -1.0), 90: (-1.0, 0.0), 180: (0.0, 1.0), 270: (1.0, 0.0)}

DISTANCE_COST_PER_MM = 0.5
TEXT_CROSSING_COST_MM = 8.0
END_COST_MM = 2.0
DOWN_COST_MM = 2.0
# A flag standing off a vertical wire has its name beside it, and beside a vertical wire there is often a symbol's text
# or another wire; off a horizontal wire it stands up with its name above, where there is more room.
SIDEWAYS_COST_MM = 4.0
# A flag marks its net at a pin; it may attach only this close (Manhattan) to the tip of one of the net's pins.
MAX_FLAG_REACH_MM = 10.16


@dataclass(frozen=True)
class FlagPlan:
    point: Point
    rotation: int
    cost: float
    # The wire the flag lands on. KiCad joins a pin to wire ends only, never to the middle of a wire (ERC: "pin not
    # connected"), so the wire is split at the point and the flag's pin sits where the two halves meet.
    segment: Segment


def flag_footprint(point: Point, rotation: int, text_width_mm: float) -> Bounds:
    """What the flag occupies: its graphic, and its name above or below it when it stands up or hangs down.

    A flag that stands off a vertical wire sideways has its name moved by the field placement, so only its graphic
    is reserved.
    """
    x, y = point
    dx, dy = DIRECTIONS[rotation]
    if dy:
        half = max(FLAG_HALF_WIDTH_MM, text_width_mm / 2.0)
        far = FLAG_TEXT_OFFSET_MM + FLAG_TEXT_HEIGHT_MM / 2.0
        return Bounds(x - half, min(y, y + dy * far), x + half, max(y, y + dy * far))
    return Bounds(
        min(x, x + dx * FLAG_STEM_MM), y - FLAG_HALF_WIDTH_MM, max(x, x + dx * FLAG_STEM_MM), y + FLAG_HALF_WIDTH_MM,
    )


def _candidates(segment: Segment, grid_mm: float) -> list[tuple[Point, tuple[int, ...], float]]:
    """Grid points strictly inside the segment, the rotations that stand off it, and how far each is from the nearer end."""
    (ax, ay), (bx, by) = segment
    out: list[tuple[Point, tuple[int, ...], float]] = []
    if abs(ay - by) < 1e-6:
        low, high = sorted((ax, bx))
        position = (int(low / grid_mm) + 1) * grid_mm
        while position < high - grid_mm * 0.5:
            if position > low + grid_mm * 0.5:
                out.append(((round(position, 6), ay), (0, 180), min(position - low, high - position)))
            position += grid_mm
    elif abs(ax - bx) < 1e-6:
        low, high = sorted((ay, by))
        position = (int(low / grid_mm) + 1) * grid_mm
        while position < high - grid_mm * 0.5:
            if position > low + grid_mm * 0.5:
                out.append(((ax, round(position, 6)), (90, 270), min(position - low, high - position)))
            position += grid_mm
    return out


def plan_flag_attachment(
    *,
    wires: tuple[Segment, ...],
    prefer: Point,
    text_width_mm: float,
    foreign_bounds: tuple[Bounds, ...] = (),
    foreign_pin_points: tuple[Point, ...] = (),
    other_wires: tuple[Segment, ...] = (),
    text_boxes: tuple[Bounds, ...] = (),
    grid_mm: float = 1.27,
    host_points: tuple[Point, ...] = (),
) -> FlagPlan | None:
    """The point on one of ``wires`` and the rotation for a flag there, or None when no wire has a free place.

    ``wires`` are the wires of the net the flag marks; ``other_wires`` are all the rest. The flag may not touch any
    other symbol, pin or wire; a text box under it costs. Nearer to ``prefer`` (where the flag sits now) is better,
    the middle of a wire is better than its ends, and standing up is better than hanging down.
    """
    pin_set = {(round(px, 3), round(py, 3)) for px, py in foreign_pin_points}
    best: tuple[tuple[float, float, Point, int], FlagPlan] | None = None
    for index, segment in enumerate(wires):
        for point, rotations, from_end in _candidates(segment, grid_mm):
            if (round(point[0], 3), round(point[1], 3)) in pin_set:
                continue
            # a point where another wire of the net already joins is a junction: leave it alone
            if any(
                other is not segment and any(
                    abs(end[0] - point[0]) < 1e-3 and abs(end[1] - point[1]) < 1e-3 for end in other
                )
                for other in wires
            ):
                continue
            reach = min(
                (abs(point[0] - hx) + abs(point[1] - hy) for hx, hy in host_points), default=0.0,
            )
            if reach > MAX_FLAG_REACH_MM + 1e-6:
                continue
            for rotation in rotations:
                box = flag_footprint(point, rotation, text_width_mm)
                if any(box.overlaps(foreign) for foreign in foreign_bounds):
                    continue
                if any(box.left <= px <= box.right and box.top <= py <= box.bottom for px, py in foreign_pin_points):
                    continue
                if any(_touches_bbox(wire, box, 0.0) for wire in other_wires):
                    continue
                if any(
                    _touches_bbox(wire, box, 0.0) for other_index, wire in enumerate(wires) if other_index != index
                ):
                    continue
                cost = (
                    (abs(point[0] - prefer[0]) + abs(point[1] - prefer[1])) * DISTANCE_COST_PER_MM
                    + sum(TEXT_CROSSING_COST_MM for text in text_boxes if box.overlaps(text))
                    + (END_COST_MM if from_end < 2.0 * grid_mm else 0.0)
                    + (DOWN_COST_MM if rotation in (180, 270) else 0.0)
                    + (SIDEWAYS_COST_MM if rotation in (90, 270) else 0.0)
                )
                key = (round(reach, 3), round(cost, 6), point, rotation)
                if best is None or key < best[0]:
                    best = (key, FlagPlan(point, rotation, round(cost, 4), segment))
    return best[1] if best is not None else None
