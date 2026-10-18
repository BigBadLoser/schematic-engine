"""Collision-safe orthogonal terminal exits for arbitrary electrical nets.

This planner is independent of the connected components, their library,
rail names and circuit topology. It considers all four directions and returns
a finite best route or an explicit infeasibility error; *never* an unchecked
long wire through existing electrical geometry.

A terminal is a stub plus the symbol at its end. With an ``EndSymbol`` the planner
also keeps that symbol, graphic and text, clear of every other symbol and text box,
and keeps the stub from running through it or through text. Without one it checks
the stub and the end point only.

A rail shared by several pins has one symbol on it instead of one per pin;
``choose_rail_symbol`` finds it a free place along the rail or just beyond its ends.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from .kicad_geometry import text_width_mm
from .presentation_geometry import Bounds, flagged_label_bounds, label_text_bounds
from .route_simplification import connects_or_overlaps


Point = tuple[float, float]
Segment = tuple[Point, Point]

# Coordinates are multiples of the grid read from a file, and the planner compares sums of them with clearances that are
# multiples of the grid too: a symbol 3.81 mm from a stub is on the boundary of a 3.81 mm clearance, and which side of it
# the float noise puts the answer on depends on where on the sheet the pair stands (the same opamp, moved 178 mm, got three
# terminals elsewhere and a third more wire: `auto-demo8`). Every boundary comparison carries this slack, so that
# touching is touching and a tie is a tie wherever the geometry is.
_EPS = 1e-6

# A stub that crosses another wire in the interior of both connects nothing, but it reads badly. It is allowed, and
# costs this much length (mm) per crossing, so a longer way round that avoids it wins unless it is much longer.
CROSSING_COST_MM = 15.0
# A stub that runs through a text box costs this much per box. The host symbol's own value text is often drawn right
# over its pin tip, so every stub from that pin touches it; refusing them all would leave the pin with no terminal.
TEXT_CROSSING_COST_MM = 8.0
# A label at the end of a vertical stub is drawn vertically. Engineers draw labels horizontally (none of the 91 human-drawn
# KiCad demo sheets measured turns one, FINDINGS section 13), so a stub that can end horizontally by turning once, at the
# cost of a few millimetres, does.
VERTICAL_LABEL_COST_MM = 14.0
# A stub that turns toward the pin beside it runs across that pin's way out, so the neighbour's own stub either crosses
# it or has to go the other way round. Pins on one edge are planned one after another and the first has no way to know,
# so a path that cuts the straight line out of a pin on the same edge costs this much (and wins only when much shorter).
EXIT_BLOCK_COST_MM = 6.0
EXIT_REACH_MM = 12.7
# A label or power symbol laid over a wire drawn earlier reads as struck through (more than a crossing: the wire does not
# cross anything, it cuts the text).
WIRE_UNDER_END_COST_MM = 16.0
# A power symbol is drawn on the end of a vertical wire: a ground symbol hangs from the wire above it, a supply symbol
# stands on the wire below. 9 in 10 of the human-drawn demo sheets' power symbols are entered that way and the engine's
# were entered from the side more often than not (FINDINGS section 13), so a stub that arrives sideways costs this much,
# and one that turns once to arrive along the symbol's own axis wins when there is room for it.
SIDEWAYS_POWER_COST_MM = 8.0
# How far inside a symbol's bounds a stub has to run to count as going through the symbol.
_OWN_BODY_SLACK_MM = 0.3


# A power symbol reaches this far from its anchor: its 2.54 mm graphic and the Value text beyond it, at KiCad's
# default sizes. Measured on placed GND, +3V3 and similar symbols; all reach 5.72 mm.
POWER_SYMBOL_REACH_MM = 5.72
_POWER_TEXT_HEIGHT_MM = 1.27


@dataclass(frozen=True)
class EndSymbol:
    """What the symbol at the end of a terminal occupies around its anchor, upright."""

    half_width_mm: float
    reach_mm: float
    direction: int          # +1: reaches down the page from the anchor (a ground symbol), -1: up (a supply)

    # A power symbol stands upright whichever way its stub arrives, and the wire must not run through it.
    stub_enters = False

    def heading_cost(self, heading: Point) -> float:
        """Arriving along the symbol's own axis (down into a ground symbol, up into a supply symbol) costs nothing."""
        arrives = (0.0, 1.0) if self.direction > 0 else (0.0, -1.0)
        return 0.0 if (float(heading[0]), float(heading[1])) == arrives else SIDEWAYS_POWER_COST_MM

    def footprint_at(self, point: Point, heading: Point = (0.0, 0.0)) -> Bounds:
        x, y = point
        top, bottom = (y, y + self.reach_mm) if self.direction > 0 else (y - self.reach_mm, y)
        return Bounds(x - self.half_width_mm, top, x + self.half_width_mm, bottom)


@dataclass(frozen=True)
class LabelEnd:
    """A net label at the end of a stub.

    A local label's text is centred on its anchor and sits above the wire; a global label's flag and text run
    away from the anchor, along the stub (see ``presentation_geometry`` for the measured geometry). Either way
    the wire runs into the label, which is how KiCad draws a labelled stub, so the stub is allowed to overlap
    its own label.
    """

    text_width_mm: float
    size_mm: float
    flagged: bool = False
    stub_enters = True

    def heading_cost(self, heading: Point) -> float:
        """Arriving vertically turns the text; that costs, so a horizontal arrival wins when one is nearly as short."""
        return VERTICAL_LABEL_COST_MM if abs(heading[1]) > abs(heading[0]) else 0.0

    def footprint_at(self, point: Point, heading: Point = (1.0, 0.0)) -> Bounds:
        x, y = point
        if self.flagged:
            return flagged_label_bounds(x, y, self.text_width_mm, self.size_mm, heading)
        return label_text_bounds(
            x, y, self.text_width_mm, self.size_mm, vertical=abs(heading[1]) > abs(heading[0]),
        )


def power_symbol_footprint(name: str, pin_normal_y: float) -> EndSymbol:
    """The footprint of a power symbol from its net name and the outward direction of its pin.

    A symbol reaches away from its pin: a ground symbol's pin points up the page, so it reaches down.
    """
    text_width = text_width_mm(name, _POWER_TEXT_HEIGHT_MM)
    return EndSymbol(
        half_width_mm=max(1.27, text_width / 2.0),
        reach_mm=POWER_SYMBOL_REACH_MM,
        direction=1 if pin_normal_y < 0 else -1,
    )


def hung_from_pin(end: EndSymbol, hanging: bool) -> tuple[EndSymbol, int]:
    """The footprint and the rotation of a supply symbol that hangs from its pin, upside down, like a ground symbol.

    A symbol that stands up (it reaches up the page) is turned half a way round and then reaches down; one that
    does not hang, or already reaches down, is left as it is.
    """
    if hanging and end.direction < 0:
        return replace(end, direction=1), 180
    return end, 0


@dataclass(frozen=True)
class TerminalPlan:
    position: Point
    segments: tuple[Segment, ...]
    strategy: str
    length_mm: float
    bends: int


def _snap(x: float, grid: float) -> float:
    return round(round(x / grid) * grid, 6)


def _step(point: Point, heading: Point, length: float, grid: float) -> Point:
    """Move ``length`` along an axis heading: the coordinate that moves is snapped, the other is left exactly as it
    was, so a path built from steps is orthogonal whatever float noise the start carries."""
    hx, hy = heading
    return (
        _snap(point[0] + hx * length, grid) if hx else point[0],
        _snap(point[1] + hy * length, grid) if hy else point[1],
    )


def _overlap(a: Bounds, b: Bounds) -> bool:
    """Do two boxes share area? Boxes that touch along an edge do not, by more than float noise."""
    return a.left < b.right - _EPS and a.right > b.left + _EPS and a.top < b.bottom - _EPS and a.bottom > b.top + _EPS


def _touches_bbox(segment: Segment, box: Bounds, padding: float) -> bool:
    (ax, ay), (bx, by) = segment
    left, top, right, bottom = (
        box.left - padding, box.top - padding,
        box.right + padding, box.bottom + padding,
    )
    if abs(ax - bx) < 1e-6:
        return left - _EPS <= ax <= right + _EPS and max(min(ay, by), top) <= min(max(ay, by), bottom) + _EPS
    if abs(ay - by) < 1e-6:
        return top - _EPS <= ay <= bottom + _EPS and max(min(ax, bx), left) <= min(max(ax, bx), right) + _EPS
    raise ValueError("Electrical terminal exits must be orthogonal")


def _segments_intersect(first: Segment, second: Segment) -> bool:
    (ax, ay), (bx, by) = first
    (cx, cy), (dx, dy) = second
    if abs(ax - bx) < 1e-6 and abs(cx - dx) < 1e-6:
        return abs(ax - cx) < 1e-6 and max(min(ay, by), min(cy, dy)) <= min(max(ay, by), max(cy, dy)) + _EPS
    if abs(ay - by) < 1e-6 and abs(cy - dy) < 1e-6:
        return abs(ay - cy) < 1e-6 and max(min(ax, bx), min(cx, dx)) <= min(max(ax, bx), max(cx, dx)) + _EPS
    if abs(ax - bx) < 1e-6:
        return min(cx, dx) - _EPS <= ax <= max(cx, dx) + _EPS and min(ay, by) - _EPS <= cy <= max(ay, by) + _EPS
    return min(ax, bx) - _EPS <= cx <= max(ax, bx) + _EPS and min(cy, dy) - _EPS <= ay <= max(cy, dy) + _EPS


def _runs_through(segment: Segment, box: Bounds) -> bool:
    """Does the segment pass through the interior of the box? Lying along an edge does not count."""
    (ax, ay), (bx, by) = segment
    eps = 1e-6
    if abs(ax - bx) < eps:
        return (
            box.left + eps < ax < box.right - eps
            and min(max(ay, by), box.bottom) - max(min(ay, by), box.top) > eps
        )
    return (
        box.top + eps < ay < box.bottom - eps
        and min(max(ax, bx), box.right) - max(min(ax, bx), box.left) > eps
    )


def _blocked_exits(
    segments: tuple[Segment, ...], normal: Point, source: Point, pins: tuple[Point, ...], grid_mm: float,
) -> int:
    """How many pins beside the source, on the same edge, have their straight way out cut by these segments."""
    nx, ny = normal
    count = 0
    for px, py in pins:
        if abs(px - source[0]) + abs(py - source[1]) < 1e-6:
            continue
        # beside it: level with the source along its direction out, not on another edge of another symbol
        if abs((px - source[0]) * nx + (py - source[1]) * ny) > grid_mm * 1.5 + _EPS:
            continue
        way_out = ((px, py), (px + nx * EXIT_REACH_MM, py + ny * EXIT_REACH_MM))
        if any(_segments_intersect(segment, way_out) for segment in segments):
            count += 1
    return count


def _clean_path(raw: tuple[Point, ...]) -> tuple[Segment, ...]:
    result: list[Segment] = []
    for a, b in zip(raw, raw[1:]):
        if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
            continue
        if abs(a[0] - b[0]) > 1e-9 and abs(a[1] - b[1]) > 1e-9:
            raise ValueError("Non-orthogonal candidate")
        result.append((a,b))
    return tuple(result)


def choose_safe_terminal(
    *,
    source: Point,
    preferred_normal: Point,
    own_bounds: Bounds | None,
    foreign_bounds: tuple[Bounds, ...],
    foreign_pin_points: tuple[Point, ...] = (),
    existing_wire_segments: tuple[Segment, ...] = (),
    occupied_terminals: tuple[Point, ...] = (),
    grid_mm: float = 1.27,
    clearance_mm: float = 2.54,
    end_symbol: EndSymbol | None = None,
    avoid_boxes: tuple[Bounds, ...] = (),
    soft_boxes: tuple[Bounds, ...] = (),
) -> TerminalPlan:
    """Choose the stub and end point for one terminal.

    ``avoid_boxes`` and ``soft_boxes`` matter only when ``end_symbol`` is given. Nothing may overlap the end
    symbol, and a stub may not run through an ``avoid_boxes`` box (another terminal's symbol). A stub may run
    through a ``soft_boxes`` box (text) but pays for it, so a way round wins unless it is much longer.
    """
    # Pins of one symbol can share a tip (a power transistor's three source pins): a pin at the source is the source, not
    # an obstacle in the way of every stub that leaves it.
    foreign_pin_points = tuple(
        point for point in foreign_pin_points
        if abs(point[0] - source[0]) + abs(point[1] - source[1]) > 1e-6
    )
    if grid_mm <= 0 or clearance_mm < 0 or not all(
        isfinite(x) for x in (*source, *preferred_normal)
    ):
        raise ValueError("Invalid terminal coordinates or spacing")

    x, y = source
    dx, dy = preferred_normal
    if abs(dx) > abs(dy):
        preferred = (1,0) if dx > 0 else (-1,0)
    else:
        preferred = (0,1) if dy >= 0 else (0,-1)
    cardinal = (preferred, (-preferred[1], preferred[0]),
                (preferred[1], -preferred[0]), (-preferred[0], -preferred[1]))
    candidate_paths: list[tuple[str, tuple[Point, ...], float]] = []
    distances = (5.08, 7.62, 10.16, 12.7, 15.24, 20.32, 25.4)
    escapes = (2.54, 5.08, 7.62, 10.16)
    for heading_index, (hx, hy) in enumerate(cardinal):
        for length in distances:
            target = _step(source, (hx, hy), length, grid_mm)
            candidate_paths.append((f"direct_{heading_index}_{length}", (source, target),
                                    heading_index * grid_mm * 8.0))
        for escape in escapes:
            mid = _step(source, (hx, hy), escape, grid_mm)
            for lateral in (-1,1):
                lx, ly = -hy * lateral, hx * lateral
                for distance in distances:
                    target = _step(mid, (lx, ly), distance, grid_mm)
                    candidate_paths.append((
                        f"dogleg_{heading_index}_{escape}_{lateral}_{distance}",
                        (source, mid, target),
                        heading_index * grid_mm * 8.0,
                    ))
    # Include a dogleg that exits laterally first, then continues along the
    # preferred normal. This is essential when a second pin lies straight
    # ahead of the subject pin.
    for lateral in (-1,1):
        hx,hy=-preferred[1]*lateral,preferred[0]*lateral
        for escape in escapes:
            mid=_step(source,(hx,hy),escape,grid_mm)
            for length in distances:
                target=_step(mid,preferred,length,grid_mm)
                candidate_paths.append((f"lateral_first_{lateral}_{escape}_{length}",
                                        (source,mid,target), grid_mm * 8.0))
    # The end symbol's own footprint is checked precisely below, so a wide margin around other symbols would
    # only turn away places where it fits (in a dense layout, nearly all of them). One grid is kept for air.
    point_clearance=clearance_mm if end_symbol is None else min(clearance_mm, grid_mm)
    own_inside: Bounds | None = None
    if end_symbol is not None and own_bounds is not None:
        slack = _OWN_BODY_SLACK_MM
        if own_bounds.right - own_bounds.left > 2 * slack and own_bounds.bottom - own_bounds.top > 2 * slack:
            own_inside = Bounds(
                own_bounds.left+slack, own_bounds.top+slack, own_bounds.right-slack, own_bounds.bottom-slack,
            )
    best: tuple[float, str, TerminalPlan] | None = None
    # What turned candidates away, so that a terminal with no exit can say why.
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason]=rejected.get(reason,0)+1

    for strategy, points, direction_cost in candidate_paths:
        segments=_clean_path(points)
        if not segments:
            continue
        terminal=points[-1]
        if own_bounds and (
            own_bounds.left-point_clearance-_EPS <= terminal[0] <= own_bounds.right+point_clearance+_EPS
            and own_bounds.top-point_clearance-_EPS <= terminal[1] <= own_bounds.bottom+point_clearance+_EPS
        ):
            reject("end point too close to its own symbol")
            continue
        # With an end symbol the bounds are the body and the pin tips, so a stub that runs inside them goes through the
        # part it leaves, which a ground symbol arriving from above made attractive for a pin that points up: the stub
        # ran straight back through the connector. (The legacy box is larger than the part and has the pins inside.)
        if own_inside is not None and any(_runs_through(segment, own_inside) for segment in segments):
            reject("stub through its own symbol")
            continue
        if any(
            box.left-point_clearance-_EPS <= terminal[0] <= box.right+point_clearance+_EPS
            and box.top-point_clearance-_EPS <= terminal[1] <= box.bottom+point_clearance+_EPS
            for box in foreign_bounds
        ):
            reject("end point too close to another symbol")
            continue
        if any(abs(terminal[0]-px)<point_clearance-_EPS and abs(terminal[1]-py)<point_clearance-_EPS
               for px,py in occupied_terminals):
            reject("end point too close to another terminal")
            continue
        if end_symbol is not None:
            (lax,lay),(lbx,lby)=segments[-1]
            heading=((lbx>lax)-(lbx<lax),(lby>lay)-(lby<lay))
            footprint=end_symbol.footprint_at(terminal,heading)
            if own_bounds and _overlap(footprint, own_bounds):
                reject("end symbol on its own symbol")
                continue
            if any(_overlap(footprint, box) for box in foreign_bounds):
                reject("end symbol on another symbol")
                continue
            if any(_overlap(footprint, box) for box in (*avoid_boxes,*soft_boxes)):
                reject("end symbol on text")
                continue
            # The stub may not run through a power symbol it ends in (arriving from its far side), nor through
            # text. A label is drawn on the end of its wire, so that overlap is by design.
            if not end_symbol.stub_enters and any(_runs_through(segment,footprint) for segment in segments):
                reject("stub through its own end symbol")
                continue
            if any(_touches_bbox(segment,box,0.0) for segment in segments for box in avoid_boxes):
                reject("stub through another terminal")
                continue
        blocked=False
        crossings=0
        orientation_cost=0.0
        wire_under=0
        if end_symbol is not None:
            (lax,lay),(lbx,lby)=segments[-1]
            orientation_cost=end_symbol.heading_cost(((lbx>lax)-(lbx<lax),(lby>lay)-(lby<lay)))
            wire_under=sum(1 for prior in existing_wire_segments if _runs_through(prior,footprint))
        text_hits=len({id(box) for segment in segments for box in soft_boxes if end_symbol is not None
                       and _touches_bbox(segment,box,0.0)})
        for segment in segments:
            if any(_touches_bbox(segment, box, grid_mm*0.5) for box in foreign_bounds):
                reject("stub through another symbol")
                blocked=True
                break
            if any(_touches_bbox(segment,
                                Bounds(px-grid_mm*0.5,py-grid_mm*0.5,
                                       px+grid_mm*0.5,py+grid_mm*0.5),0)
                   for px,py in foreign_pin_points):
                reject("stub through another pin")
                blocked=True
                break
            # An electrical intersection with any previously routed wire can
            # create an unintentional junction. The only permitted shared point
            # is the source pin, not an overlapping segment.
            for prior in existing_wire_segments:
                if not _segments_intersect(segment,prior):
                    continue
                if (segment[0] == prior[0] or segment[0] == prior[1]) and (
                    segment[0] == source
                ):
                    # Source attachment is valid only if the first segment
                    # immediately departs in an axis different from prior.
                    (ax,ay),(bx,by)=segment
                    (cx,cy),(ex,ey)=prior
                    if (abs(ax-bx)<1e-6) != (abs(cx-ex)<1e-6):
                        continue
                if connects_or_overlaps(segment,prior):
                    reject("stub touches a wire")
                    blocked=True
                    break
                crossings+=1
            if blocked:
                break
        if blocked:
            continue
        distance=sum(abs(b[0]-a[0])+abs(b[1]-a[1]) for a,b in segments)
        bends=max(0,len(segments)-1)
        exit_blocks=(_blocked_exits(segments,preferred,source,foreign_pin_points,grid_mm)
                     if len(segments)>1 and end_symbol is not None else 0)
        score=round(distance+bends*grid_mm*4.0+direction_cost+crossings*CROSSING_COST_MM+text_hits*TEXT_CROSSING_COST_MM
                    +orientation_cost+exit_blocks*EXIT_BLOCK_COST_MM+wire_under*WIRE_UNDER_END_COST_MM, 6)
        plan=TerminalPlan(terminal,segments,strategy,round(distance,4),bends)
        if best is None or (score,strategy)<(best[0],best[1]):
            best=(score,strategy,plan)
    if best is None:
        why=", ".join(f"{reason} {count}" for reason,count in sorted(rejected.items(),key=lambda kv:-kv[1])[:4])
        raise ValueError(
            f"No unobstructed power terminal exit from {source}; "
            "reposition its source or nearby geometry"
            + (f" (turned away: {why})" if why else "")
        )
    return best[2]


def rail_obstructions(
    *,
    horizontal: bool,
    rail: float,
    pins: tuple[Point, ...],
    foreign_bounds: tuple[Bounds, ...] = (),
    foreign_pin_points: tuple[Point, ...] = (),
    existing_wire_segments: tuple[Segment, ...] = (),
    grid_mm: float = 1.27,
) -> dict[str, int]:
    """What a shared rail would run into, by reason; empty when it is clear.

    The rail is the straight wire at ``rail`` (a y for a horizontal rail, an x for a vertical one) that spans the
    pins, plus the wire from each pin to it. One that meets a wire already on the sheet joins two nets or crosses
    them, and one that runs through a symbol or a pin reads as if it touched it. Pins are better off with a
    symbol each than with a rail that does that: a decoupling bank whose ground rail cuts across the supply wires
    to its own capacitors is the usual case.
    """
    along = [pin[0] if horizontal else pin[1] for pin in pins]
    low, high = min(along), max(along)
    trunk: Segment = ((low, rail), (high, rail)) if horizontal else ((rail, low), (rail, high))
    stubs = [((px, py), (px, rail)) if horizontal else ((px, py), (rail, py)) for px, py in pins]
    found: dict[str, int] = {}

    def note(reason: str) -> None:
        found[reason] = found.get(reason, 0) + 1

    own = set(pins)
    for segment in (trunk, *stubs):
        if segment[0] == segment[1]:
            continue
        for prior in existing_wire_segments:
            if _segments_intersect(segment, prior):
                note("rail meets a wire")
        for box in foreign_bounds:
            if _touches_bbox(segment, box, grid_mm * 0.5):
                note("rail runs through a symbol")
        for px, py in foreign_pin_points:
            if (px, py) not in own and _touches_bbox(
                segment, Bounds(px - grid_mm * 0.5, py - grid_mm * 0.5, px + grid_mm * 0.5, py + grid_mm * 0.5), 0.0,
            ):
                note("rail runs through a pin")
    return found


@dataclass(frozen=True)
class RailSymbolPlan:
    position: Point
    # The wire that carries the rail to the symbol when the symbol sits beyond the pins' span, else None.
    extension: Segment | None
    length_mm: float


def choose_rail_symbol(
    *,
    horizontal: bool,
    rail: float,
    low: float,
    high: float,
    end_symbol: EndSymbol,
    foreign_bounds: tuple[Bounds, ...],
    foreign_pin_points: tuple[Point, ...] = (),
    existing_wire_segments: tuple[Segment, ...] = (),
    avoid_boxes: tuple[Bounds, ...] = (),
    text_boxes: tuple[Bounds, ...] = (),
    grid_mm: float = 1.27,
    reach_steps: int = 8,
    rejected: dict[str, int] | None = None,
) -> RailSymbolPlan | None:
    """Where on a rail the symbol goes, or None when nowhere along it is free.

    The rail is the straight wire at ``rail`` (a y for a horizontal rail, an x for a vertical one) that joins
    the pins spanning ``low`` to ``high`` along it. The middle is preferred; a symbol that does not fit there
    slides along the rail and then past its ends (the rail is extended to reach it), as far as ``reach_steps``
    grids. The symbol's graphic and text must keep clear of every other symbol, pin, wire and text box. When
    ``rejected`` is given it receives how many places each reason turned away.
    """
    def reject(reason: str) -> None:
        if rejected is not None:
            rejected[reason] = rejected.get(reason, 0) + 1


    middle = (low + high) / 2.0
    first = _snap(low - reach_steps * grid_mm, grid_mm)
    last = _snap(high + reach_steps * grid_mm, grid_mm)
    candidates: list[tuple[float, float]] = []
    position = first
    while position <= last + 1e-9:
        beyond = max(low - position, position - high, 0.0)
        candidates.append((round(abs(position - middle) + 2.0 * beyond, 6), position))
        position = _snap(position + grid_mm, grid_mm)
    for _cost, along in sorted(candidates):
        point = (along, rail) if horizontal else (rail, along)
        footprint = end_symbol.footprint_at(point)
        if any(_overlap(footprint, box) for box in foreign_bounds):
            reject("symbol on another symbol")
            continue
        if any(_overlap(footprint, box) for box in (*avoid_boxes, *text_boxes)):
            reject("symbol on text")
            continue
        if any(
            footprint.left - _EPS <= px <= footprint.right + _EPS and footprint.top - _EPS <= py <= footprint.bottom + _EPS
            for px, py in foreign_pin_points
        ):
            reject("symbol on a pin")
            continue
        if any(_touches_bbox(segment, footprint, 0.0) for segment in existing_wire_segments):
            reject("symbol on a wire")
            continue
        extension: Segment | None = None
        if along < low - 1e-9 or along > high + 1e-9:
            end = low if along < low else high
            extension = ((end, rail), (along, rail)) if horizontal else ((rail, end), (rail, along))
            if any(_touches_bbox(extension, box, grid_mm * 0.5) for box in foreign_bounds):
                reject("rail extension through another symbol")
                continue
            if any(
                _touches_bbox(extension, Bounds(px - grid_mm * 0.5, py - grid_mm * 0.5,
                                                px + grid_mm * 0.5, py + grid_mm * 0.5), 0.0)
                for px, py in foreign_pin_points
            ):
                reject("rail extension through a pin")
                continue
            if any(connects_or_overlaps(extension, wire) for wire in existing_wire_segments):
                reject("rail extension touches a wire")
                continue
        return RailSymbolPlan(point, extension, abs(along - middle))
    return None


def can_attach_directly(
    *,
    source: Point,
    foreign_bounds: tuple[Bounds, ...],
    foreign_pin_points: tuple[Point, ...] = (),
    existing_wire_segments: tuple[Segment, ...] = (),
    tolerance_mm: float = 0.01,
) -> bool:
    """Safe zero-wire alternative for a crowded attachment.

    An actual symbol at the pin, unlike a long default stub, cannot cross a
    neighboring pin or wire *between* endpoints. Reject coincident foreign
    anchors or an existing routed segment at the source itself.
    """
    x, y = source
    if any(
        box.left + tolerance_mm < x < box.right - tolerance_mm
        and box.top + tolerance_mm < y < box.bottom - tolerance_mm
        for box in foreign_bounds
    ):
        return False
    if any(
        abs(x - px) <= tolerance_mm and abs(y - py) <= tolerance_mm
        for px, py in foreign_pin_points
    ):
        return False
    if any(
        _touches_bbox(segment,
                      Bounds(x - tolerance_mm, y - tolerance_mm,
                             x + tolerance_mm, y + tolerance_mm),
                      0.0)
        for segment in existing_wire_segments
    ):
        return False
    return True
