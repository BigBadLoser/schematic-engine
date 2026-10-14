"""Where the symbols no relation places go, and which way they face.

A relation puts a support part beside the pin it serves and the floorplan puts a block where its connections pull it, but
the symbols between them (a connector, a series resistor) were left wherever MCP Pro's auto-layout dropped them, turned
by a separate step that did not know where they stood, and pulled toward the centre of what they connect to. The result
is wires that wrap round a connector whose pins face the wrong way, bus wires that cross because the pins reach the chip
in the opposite order, staircases between two parts that could have shared a row, and no room in front of a pin for the
stub, label or power symbol that has to be drawn from it.

This module chooses position and orientation of those symbols together, from the geometry the later stages will meet:

* every wire is estimated as the route the router will draw from pin to pin, leaving along each pin's direction: a
  straight run when the pins face each other on one line, an L, a Z or a U otherwise, and a net of three or more pins is
  one run with T joins. Bends and length cost, so two pins that line up on a row or a column are worth more than two that
  are merely close;
* crossings between those routes (and the stubs of labelled and power pins) cost, and so do a route through a symbol and
  a route over a pin that is not on its net;
* the room in front of every pin that a stub, a label or a power symbol will be drawn from is kept clear of every other
  symbol and of other nets' wires, in the units the terminal planner uses, and a power symbol is entered along its axis;
* a connector's pins read in order.

Three entry points share one cost. ``refine_placement`` chooses position and orientation of the movable symbols (a part a
relation placed may be nudged within a window, not turned). ``seed_placement`` brings in beside their partners the free
symbols that MCP Pro's auto-layout scattered, before the blocks are measured. ``align_groups`` slides whole blocks to
where their connections run straightest, never to the far side of a block they feed.

It is pure: positions, pin geometry and nets in, positions and orientations out. Nothing here knows a circuit.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import product

from backend.schematic.orientation import Orientation, order_inversions

Point = tuple[float, float]
Box = tuple[float, float, float, float]          # left, top, right, bottom

GRID_MM = 1.27
# How far a support part a relation placed beside the pin it serves may be nudged from where the relation put it.
RELATION_WINDOW_MM = 6.35
# A wire leaves a pin along its direction this far before it may turn.
STUB_MM = 2.54
_EPS = 1e-6

# How far in front of a pin the room is kept clear, by what is drawn there.
WIRE_REACH_MM = 4.445       # the router's portal, its buffer and its inflation
POWER_REACH_MM = 7.62       # a stub that may turn, then the symbol
LABEL_STUB_MM = 5.08        # the shortest stub the terminal planner draws to a label
ZONE_HALF_WIDTH_MM = 1.27
# A wire may meet a pin tip from the side when the tips share a row or a column (the router draws it, KiCad connects it),
# but a person draws it along the pin's axis: it costs more than a bend and less than a Z. Counted in bends (the routers
# drew 0.6 corners for it on the sheets of `auto-v27` and `auto-demo10`), so that it follows the price of a bend.
SIDEWAYS_BENDS = 1.5
# A leg shorter than this, between two turns or as the stub of a T, is a jog: two wires one grid step off a line.
JOG_MM = 2.54
ATTACH_BEND_MM = 5.0        # what a net's own estimate weighs a bend at, to choose how its pins are joined
# A wire never runs closer than this to a body: along the line of a symbol's pin tips it touches every one of them.
ROUTE_MARGIN_MM = 0.5
TIP_TOLERANCE_MM = 0.1


@dataclass(frozen=True)
class PinGeometry:
    point: Point            # the pin's connection point, relative to the symbol's anchor
    normal: Point           # unit vector pointing out of the symbol


@dataclass(frozen=True)
class OrientedGeometry:
    pins: Mapping[str, PinGeometry]
    box: Box                # what placement keeps clear around the symbol, relative to its anchor
    # the symbol's body and pin tips without the layout policy's padding: what a route or the room in front of a pin
    # must not run into (the policy's box is at least 20 x 15 mm in the legacy layout)
    body: Box | None = None


@dataclass(frozen=True)
class PlacedSymbol:
    reference: str
    position: Point                                        # the anchor, in mm
    orientation: Orientation
    geometry: Mapping[Orientation, OrientedGeometry]       # every orientation it may take (a fixed symbol: its own)
    movable: bool = False
    ordered: bool = False                                  # its pins should read in order (a connector)
    # a symbol a relation placed beside the pin it serves may be nudged, but only this far (mm) from where it stands:
    # it stays where the relation put it, give or take what the wires and the room in front of the pins ask for
    window: float | None = None
    # what each orientation costs by how rarely a person draws the part that way (orientation_prior); when given, it
    # stands in for the turn and flip prices
    orientation_cost: Mapping[Orientation, float] | None = None
    # how far along the signal path it stands (signal_flow.flow_ranks); None: not on the path
    flow_rank: int | None = None


@dataclass(frozen=True)
class Connection:
    net: str
    pins: tuple[tuple[str, str], ...]                      # (reference, pin)
    style: str = "wire"                                    # "wire", "label" (stubs and labels) or "power"
    weight: float = 1.0
    # for power symbols: the way a pin points when the symbol is entered along its own axis, which is how it is drawn
    # (down into a ground symbol, up into a supply symbol); a pin pointing any other way has to turn the stub
    facing: Point | None = None


@dataclass(frozen=True)
class Weights:
    """What each thing costs, in millimetres of wire."""

    bend: float = 5.0           # a bend costs more than the ranking's 2.54: a straight run is what a person draws
    crossing: float = 15.0
    through: float = 30.0       # an estimated route through a symbol: the body of a part it joins is no way through either
    short: float = 60.0         # an estimated route over the pin of another net: no router draws that
    clearance: float = 40.0     # a symbol in the room in front of a pin
    overlap: float = 100.0
    order: float = 8.0          # per pair of a connector's pins out of order
    label_distance: float = 0.1
    label_facing: float = 6.0   # a pin that points away from the other end of its labelled net
    vertical_label: float = 4.0     # a label at the end of a vertical stub is drawn turned unless the stub bends first
    power_sideways: float = 8.0     # the terminal planner's price for entering a power symbol from the side
    power_away: float = 24.0        # ... and for a stub that has to turn right round to enter it (it ends up crossing what is beside)
    extent: float = 0.08  # was 0.17: human neighbour gap median 7.7 mm vs ours 5.3
    move: float = 0.05
    jog: float = 6.0            # a leg shorter than JOG_MM between two turns, or a stub of one: it reads as a mistake
    turn: float = 3.0           # per quarter turn away from the orientation the symbol was planned to have
    flip: float = 4.0           # a mirror it was not planned to have
    flow: float = 0.3           # per mm a net runs against the signal path (its later end left of its earlier end)
    flow_cap: float = 40.0


@dataclass(frozen=True)
class Route:
    points: tuple[Point, ...]
    bends: float
    length: float
    pins: tuple[int, ...] = ()      # which of the net's pins it starts and ends at (a T ends on a run, not a pin)


@dataclass(frozen=True)
class Cost:
    total: float
    length: float
    bends: float
    crossings: int
    clearance: int
    overlaps: int
    order: int
    through: int = 0
    shorts: int = 0
    jogs: int = 0


@dataclass(frozen=True)
class Placement:
    position: Point
    orientation: Orientation


@dataclass(frozen=True)
class Refinement:
    placements: dict[str, Placement]      # the movable symbols that moved or turned
    before: Cost
    after: Cost
    evaluated: int = 0
    details: dict | None = None           # what each term found, before and after: which symbols, pins and nets


# ---------------------------------------------------------------------------------------------------------------------
# geometry


def _turn(vector: Point, rotation: int, mirror: str) -> Point:
    """Turn a page-space vector the way KiCad draws a symbol: counter-clockwise by ``rotation``, then mirror."""
    x, y = vector
    for _ in range((rotation // 90) % 4):
        x, y = y, -x
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return (x + 0.0, y + 0.0)


def rotate_geometry(geometry: OrientedGeometry, rotation: int, mirror: str = "") -> OrientedGeometry:
    """The geometry of a symbol drawn upright, as it lies at another rotation and mirror about its anchor."""
    pins = {
        pin: PinGeometry(_turn(item.point, rotation, mirror), _turn(item.normal, rotation, mirror))
        for pin, item in geometry.pins.items()
    }
    def turned(box: Box) -> Box:
        left, top, right, bottom = box
        corners = [_turn(corner, rotation, mirror) for corner in ((left, top), (right, top), (right, bottom), (left, bottom))]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    return OrientedGeometry(pins, turned(geometry.box), None if geometry.body is None else turned(geometry.body))


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS


def _add(a: Point, b: Point, scale: float = 1.0) -> Point:
    return (a[0] + b[0] * scale, a[1] + b[1] * scale)


def _perpendicular(vector: Point) -> Point:
    return (-vector[1], vector[0])


def _polyline_length(points: Sequence[Point]) -> float:
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:]))


def count_jogs(route: "Route") -> int:
    """How many legs of a route are too short to read as a leg: between two turns, or the stub of a T."""
    legs = [abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(route.points, route.points[1:])]
    if len(legs) < 2 and len(route.pins) != 1:
        return 0
    return sum(1 for length in legs if _EPS < length < JOG_MM - _EPS)


def _route(points: Sequence[Point], bends: int) -> Route:
    return Route(tuple(points), bends, _polyline_length(points))


def estimate_route(p: Point, np_: Point, q: Point, nq: Point, stub: float = STUB_MM) -> Route:
    """The route a wire takes from pin ``p`` (pointing ``np_``) to pin ``q`` (pointing ``nq``), as few bends as the
    geometry allows: the router leaves a pin along its direction and arrives along the other's."""
    d = (q[0] - p[0], q[1] - p[1])
    if _same(np_, (-nq[0], -nq[1])):                      # the pins face each other's axis
        t = _dot(d, np_)                                  # how far ahead of p along its direction q is
        side = _dot(d, _perpendicular(np_))
        if t > _EPS and abs(side) < _EPS:
            return _route([p, q], 0)
        if t >= 2 * stub - _EPS:
            m1 = _add(p, np_, t / 2)
            m2 = _add(m1, _perpendicular(np_), side)
            return _route([p, m1, m2, q], 2)
        # facing away, or too close to turn in between: out, round and back
        lane = side + (stub if side >= 0 else -stub) * 2
        a, b = _add(p, np_, stub), _add(q, nq, stub)
        return _route([p, a, _add(a, _perpendicular(np_), lane), _add(b, _perpendicular(np_), lane), b, q], 4)
    if abs(_dot(np_, nq)) < _EPS:                         # one pin points along x and the other along y
        a, b = _dot(d, np_), -_dot(d, nq)
        if a > _EPS and b > _EPS:
            return _route([p, _add(p, np_, a), q], 1)
        if (abs(a) < _EPS and b > _EPS) or (abs(b) < _EPS and a > _EPS):
            straight = _route([p, q], 0)                  # the tips share a row or a column: along it, meeting one sideways
            return Route(straight.points, SIDEWAYS_BENDS, straight.length)
        pa, pb = _add(p, np_, stub), _add(q, nq, stub)
        corner = (pa[0], pb[1]) if abs(np_[0]) > 0.5 else (pb[0], pa[1])
        return _route([p, pa, corner, pb, q], 3)
    # the pins point the same way: out along it, across, and back to the second
    t = _dot(d, np_)
    lane = max(t, 0.0) + stub
    m1 = _add(p, np_, lane)
    m2 = _add(q, np_, lane - t)
    return _route([p, m1, m2, q], 2)


def _attach(p: Point, np_: Point, tree: Sequence[tuple[Point, Point]]) -> Route | None:
    """The cheapest way for pin ``p`` to join a run already drawn: at right angles to it, a T."""
    best: tuple[float, Route] | None = None
    for a, b in tree:
        if abs(a[0] - b[0]) < _EPS:                       # a vertical run
            lo, hi = sorted((a[1], b[1]))
            side = p[0] - a[0]
            foot = (a[0], min(max(p[1], lo), hi))
            facing = (1.0 if side > 0 else -1.0, 0.0)
        else:
            lo, hi = sorted((a[0], b[0]))
            side = p[1] - a[1]
            foot = (min(max(p[0], lo), hi), a[1])
            facing = (0.0, 1.0 if side > 0 else -1.0)
        if abs(side) < _EPS:
            continue                                      # on the run's own line: nothing to join across
        route = estimate_route(p, np_, foot, facing)
        cost = route.length + ATTACH_BEND_MM * route.bends
        if best is None or cost < best[0] - _EPS:
            best = (cost, route)
    return best[1] if best else None


def _distance_to_tree(p: Point, tree: Sequence[tuple[Point, Point]]) -> float:
    best = math.inf
    for a, b in tree:
        x = min(max(p[0], min(a[0], b[0])), max(a[0], b[0]))
        y = min(max(p[1], min(a[1], b[1])), max(a[1], b[1]))
        best = min(best, abs(p[0] - x) + abs(p[1] - y))
    return best


def _estimate_tree(pins: Sequence[tuple[Point, Point]]) -> list[Route]:
    """The tree estimate: one run between two pins and the others joined to what is drawn."""
    count = len(pins)
    if count < 2:
        return []

    def build(first: int, second: int) -> list[Route]:
        routes = [replace(
            estimate_route(pins[first][0], pins[first][1], pins[second][0], pins[second][1]), pins=(first, second),
        )]
        tree = _segments(routes[0].points)
        joined = {first, second}
        while len(joined) < count:
            following = min(
                (i for i in range(count) if i not in joined), key=lambda i: (_distance_to_tree(pins[i][0], tree), i),
            )
            route = _attach(pins[following][0], pins[following][1], tree)
            if route is None:
                nearest = min(
                    joined,
                    key=lambda j: (abs(pins[j][0][0] - pins[following][0][0]) + abs(pins[j][0][1] - pins[following][0][1]), j),
                )
                route = replace(
                    estimate_route(pins[following][0], pins[following][1], pins[nearest][0], pins[nearest][1]),
                    pins=(following, nearest),
                )
            else:
                route = replace(route, pins=(following,))
            routes.append(route)
            tree += _segments(route.points)
            joined.add(following)
        return routes

    if count <= 6:
        firsts = [(i, j) for i in range(count) for j in range(i + 1, count)]
    else:
        firsts = [min(
            ((i, j) for i in range(count) for j in range(i + 1, count)),
            key=lambda pair: abs(pins[pair[0]][0][0] - pins[pair[1]][0][0]) + abs(pins[pair[0]][0][1] - pins[pair[1]][0][1]),
        )]
    best: tuple[float, list[Route]] | None = None
    for first, second in firsts:
        routes = build(first, second)
        cost = sum(route.length + ATTACH_BEND_MM * route.bends for route in routes)
        if best is None or cost < best[0] - _EPS:
            best = (cost, routes)
    assert best is not None
    return best[1]


TRUNK_STEP_MM = 2.54       # how far in front of a vertical pin the first candidate trunk row is, and the step after
TRUNK_ROWS = 4              # how many rows in front of each vertical pin a trunk is tried on


def _trunk_rows(pins: Sequence[tuple[Point, Point]]) -> list[Route] | None:
    """The cheapest horizontal trunk over the candidate rows (the rows the pins sit on, and a few steps in front of the
    vertical ones), or None."""
    rows: set[float] = set()
    for (p, n) in pins:
        if abs(n[0]) > 0.5:
            rows.add(round(p[1], 4))
        else:
            rows.update(round(p[1] + n[1] * k * TRUNK_STEP_MM, 4) for k in range(1, TRUNK_ROWS + 1))
    best: tuple[float, list[Route]] | None = None
    for y in sorted(rows):
        built = _trunk_on_row(pins, y)
        if built is None:
            continue
        cost = sum(r.length + ATTACH_BEND_MM * r.bends for r in built)
        if best is None or cost < best[0] - _EPS:
            best = (cost, built)
    return best[1] if best else None


def _trunk_on_row(pins: Sequence[tuple[Point, Point]], y: float) -> list[Route] | None:
    arrivals: list[Route] = []
    feet: list[tuple[float, bool, int, Point]] = []        # (x, is_drop, pin, normal)
    for i, (p, n) in enumerate(pins):
        if abs(n[1]) > 0.5:                                # vertical pin
            dy = y - p[1]
            if abs(dy) < _EPS:
                return None
            foot = (p[0], y)
            if dy * n[1] > 0:
                route = _route([p, foot], 0)
            else:
                route = estimate_route(p, n, foot, (0.0, 1.0 if p[1] > y else -1.0))
            arrivals.append(replace(route, pins=(i,)))
            feet.append((p[0], True, i, n))
        elif abs(p[1] - y) < _EPS:                         # horizontal pin on the row: joins along it
            feet.append((p[0], False, i, n))
        else:                                              # leaves outward, then turns to the row
            fx = p[0] + n[0] * TRUNK_STEP_MM
            arrivals.append(replace(_route([p, (fx, p[1]), (fx, y)], 1), pins=(i,)))
            feet.append((fx, True, i, n))
    if len(feet) < 2:
        return None
    xl, xr = min(f[0] for f in feet), max(f[0] for f in feet)
    bends = 0
    for end, sign in ((xl, 1.0), (xr, -1.0)):
        at_end = [f for f in feet if abs(f[0] - end) < _EPS]
        for x, drop, _i, n in at_end:
            if not drop and (n[0] * sign < 0.5 or xr - xl < _EPS):   # an on-row pin must face into the trunk
                return None
        if all(f[1] for f in at_end):
            bends += 1
    for x, drop, _i, n in feet:                            # an on-row pin in the middle would have its stub run through itself
        if not drop and xl + _EPS < x < xr - _EPS:
            return None
    trunk = Route(((xl, y), (xr, y)), bends, xr - xl) if xr - xl > _EPS else None
    return ([trunk] if trunk else []) + arrivals


def estimate_net(pins: Sequence[tuple[Point, Point]]) -> list[Route]:
    """The routes that join a net's pins, each given as ``(point, direction)``: one run between two of them and the others
    joined to what is drawn, as a router draws a net, so a pin beside a straight run is a T and not a second wire.

    A net of three or more pins may instead be one trunk with a straight drop from each pin, as a person draws a rail: the
    LM317 regulator's ``out`` is a row with R4, D3 and C3 hanging from it, where a tree from the nearest pair staircases.
    The trunk (along either axis) is kept only when it costs less than the tree, so a tie stays the tree."""
    tree = _estimate_tree(pins)
    if len(pins) < 3:
        return tree
    best_cost = sum(r.length + ATTACH_BEND_MM * r.bends for r in tree)
    best = tree
    swapped = [((p[1], p[0]), (n[1], n[0])) for p, n in pins]
    candidates = [_trunk_rows(pins)]
    flipped = _trunk_rows(swapped)
    if flipped is not None:
        candidates.append([replace(r, points=tuple((q[1], q[0]) for q in r.points)) for r in flipped])
    for cand in candidates:
        if cand is None:
            continue
        cost = sum(r.length + ATTACH_BEND_MM * r.bends for r in cand)
        if cost < best_cost - _EPS:
            best_cost, best = cost, cand
    return best


def _segments(points: Sequence[Point]) -> list[tuple[Point, Point]]:
    return [(a, b) for a, b in zip(points, points[1:]) if not _same(a, b)]


def _crosses(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    """Do two orthogonal segments cross in the interior of both? A touch, a T and a collinear overlap do not count."""
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    a_vertical, b_vertical = abs(ax1 - ax2) < _EPS, abs(bx1 - bx2) < _EPS
    if a_vertical == b_vertical:
        return False
    vertical, horizontal = (a, b) if a_vertical else (b, a)
    (vx, vy1), (_, vy2) = vertical
    (hx1, hy), (hx2, _) = horizontal
    return (
        min(hx1, hx2) + _EPS < vx < max(hx1, hx2) - _EPS
        and min(vy1, vy2) + _EPS < hy < max(vy1, vy2) - _EPS
    )


def count_crossings(polylines: Sequence[tuple[str, Sequence[Point]]]) -> int:
    """Crossings between the polylines of different nets."""
    total = 0
    segments = [(net, _segments(points)) for net, points in polylines]
    for i, (net_a, segs_a) in enumerate(segments):
        for net_b, segs_b in segments[i + 1:]:
            if net_a == net_b:
                continue
            total += sum(1 for a in segs_a for b in segs_b if _crosses(a, b))
    return total


def _box_overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS


def _segment_through(segment: tuple[Point, Point], box: Box, margin: float = ROUTE_MARGIN_MM) -> bool:
    """Does an orthogonal segment run through a box, or along its edge, where the pin tips are, close enough to touch?"""
    (x1, y1), (x2, y2) = segment
    left, top, right, bottom = box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin
    if abs(x1 - x2) < _EPS:
        return left + _EPS < x1 < right - _EPS and min(max(y1, y2), bottom) - max(min(y1, y2), top) > _EPS
    return top + _EPS < y1 < bottom - _EPS and min(max(x1, x2), right) - max(min(x1, x2), left) > _EPS


def _segment_near(segment: tuple[Point, Point], box: Box, margin: float = ROUTE_MARGIN_MM) -> bool:
    """Does an orthogonal segment touch a box grown by ``margin``?"""
    (x1, y1), (x2, y2) = segment
    return (
        min(x1, x2) <= box[2] + margin and max(x1, x2) >= box[0] - margin
        and min(y1, y2) <= box[3] + margin and max(y1, y2) >= box[1] - margin
    )


def _on_segment(point: Point, segment: tuple[Point, Point]) -> bool:
    """Is a point on an orthogonal segment (to within a tenth of a millimetre)?"""
    (x1, y1), (x2, y2) = segment
    return (
        min(x1, x2) - TIP_TOLERANCE_MM <= point[0] <= max(x1, x2) + TIP_TOLERANCE_MM
        and min(y1, y2) - TIP_TOLERANCE_MM <= point[1] <= max(y1, y2) + TIP_TOLERANCE_MM
    )


def _zone(tip: Point, normal: Point, reach: float, half_width: float = ZONE_HALF_WIDTH_MM) -> Box:
    """The room in front of a pin: ``reach`` along its direction, as wide as what is drawn there (a wire, or a power
    symbol and its name)."""
    x, y = tip
    if abs(normal[0]) > 0.5:
        far = x + normal[0] * reach
        return (min(x, far), y - half_width, max(x, far), y + half_width)
    far = y + normal[1] * reach
    return (x - half_width, min(y, far), x + half_width, max(y, far))


# ---------------------------------------------------------------------------------------------------------------------
# the cost of a placement


Pose = Mapping[str, tuple[Point, Orientation]]


class _Model:
    def __init__(
        self,
        symbols: Sequence[PlacedSymbol],
        nets: Sequence[Connection],
        reach: Mapping[tuple[str, str], float],
        weights: Weights,
        half_widths: Mapping[tuple[str, str], float] | None = None,
    ) -> None:
        self.symbols = {symbol.reference: symbol for symbol in symbols}
        known = {
            (symbol.reference, pin)
            for symbol in symbols for geometry in symbol.geometry.values() for pin in geometry.pins
        }
        # a net keeps the pins the geometry knows (a hidden or unplaced pin has no place to estimate from)
        self.nets = []
        for net in nets:
            pins = tuple((ref, pin) for ref, pin in net.pins if (ref, pin) in known)
            if len(pins) >= 2 or (net.style != "wire" and pins):
                self.nets.append(replace(net, pins=pins))
        self.reach = dict(reach)
        self.half_widths = dict(half_widths or {})
        self.net_of = {pin: net.net for net in self.nets for pin in net.pins}
        self._nets: dict[tuple, list[Route]] = {}
        self.weights = weights
        self.initial = {s.reference: s.position for s in symbols}
        self.initial_orientation = {s.reference: s.orientation for s in symbols}
        self.evaluated = 0
        self.nets_of: dict[str, list[Connection]] = {}
        for net in self.nets:
            for reference in {reference for reference, _pin in net.pins}:
                self.nets_of.setdefault(reference, []).append(net)
        self.partners: dict[tuple[str, str], set[str]] = {}
        for net in self.nets:
            members = {reference for reference, _pin in net.pins}
            for reference, pin in net.pins:
                self.partners.setdefault((reference, pin), set()).update(members - {reference})

    def net_routes(self, tips: Sequence[tuple[Point, Point]]) -> list[Route]:
        key = tuple(tips)
        found = self._nets.get(key)
        if found is None:
            found = self._nets[key] = estimate_net(tips)
        return found

    # geometry of one symbol at a pose
    def world(self, reference: str, position: Point, orientation: Orientation):
        geometry = self.symbols[reference].geometry[orientation]
        pins = {
            pin: (_add(position, item.point), item.normal) for pin, item in geometry.pins.items()
        }
        box = (
            position[0] + geometry.box[0], position[1] + geometry.box[1],
            position[0] + geometry.box[2], position[1] + geometry.box[3],
        )
        solid = geometry.body or geometry.box
        body = (
            position[0] + solid[0], position[1] + solid[1], position[0] + solid[2], position[1] + solid[3],
        )
        return pins, box, body

    def cost(
        self, pose: Pose, active: frozenset[str] | None = None, found: dict[str, list] | None = None,
        ignore: frozenset[str] = frozenset(),
    ) -> Cost:
        """The cost of a pose. With ``active`` only what involves one of those symbols is counted, which is all that
        changes when only they move. With ``found`` every crossing, route through a symbol, short, symbol in the way of a
        pin and overlap is listed in it, to see why a pose costs what it does. The symbols in ``ignore`` are not on the
        sheet yet: nothing of theirs is counted, and they are in nobody's way."""
        self.evaluated += 1
        w = self.weights
        worlds = {
            ref: self.world(ref, position, orientation) for ref, (position, orientation) in pose.items()
            if ref not in ignore
        }

        def involves(references: Iterable[str]) -> bool:
            return active is None or any(ref in active for ref in references)

        def note(kind: str, *item: object) -> None:
            if found is not None:
                found.setdefault(kind, []).append(list(item))

        length = 0.0
        bends = 0
        jogs = 0
        label_cost = 0.0
        flow_cost = 0.0
        # every line the sheet will carry: (net, segments, symbols at its ends, the net's pins, is it a stub). Those of
        # nets that involve what moves are counted; the others are only in the way.
        Line = tuple[str, list[tuple[Point, Point]], frozenset[str], frozenset[tuple[str, str]], bool]
        lines: list[Line] = []
        fixed_lines: list[Line] = []
        for net in self.nets:
            if ignore:
                kept = tuple((ref, pin) for ref, pin in net.pins if ref not in ignore)
                if len(kept) < 2 and (net.style == "wire" or not kept):
                    continue
                net = replace(net, pins=kept)
            members = frozenset(ref for ref, _pin in net.pins)
            mine = involves(members)
            own = frozenset(net.pins)
            tips = [(worlds[ref][0][pin][0], worlds[ref][0][pin][1], ref) for ref, pin in net.pins]
            target = lines if mine else fixed_lines
            if net.style == "wire":
                if mine and w.flow:
                    ranked = sorted(
                        (self.symbols[ref].flow_rank, ref, pin) for ref, pin in net.pins
                        if self.symbols[ref].flow_rank is not None
                    )
                    if ranked and ranked[0][0] != ranked[-1][0]:
                        up = ranked[0]
                        down = min((r for r in ranked if r[0] == ranked[-1][0]), key=lambda r: (r[1], r[2]))
                        back = worlds[up[1]][0][up[2]][0][0] - worlds[down[1]][0][down[2]][0][0]
                        if back > 0:
                            flow_cost += min(w.flow_cap, w.flow * back)
                            note("backward", net.net, up[1], down[1], round(back, 2))
                for route in self.net_routes([(t[0], t[1]) for t in tips]):
                    if mine:
                        length += net.weight * route.length
                        bends += route.bends
                        jogs += count_jogs(route)
                    ends = frozenset(tips[i][2] for i in route.pins) or members
                    target.append((net.net, _segments(route.points), ends, own, False))
                continue
            for point, normal, ref in tips:                   # the stub drawn from each pin to its label or symbol
                stub = _segments([point, _add(point, normal, LABEL_STUB_MM)])
                target.append((net.net, stub, frozenset((ref,)), own, True))
            if not mine:
                continue
            if net.style == "power" and net.facing is not None:
                for point, normal, ref in tips:
                    if involves((ref,)):
                        alignment = _dot(normal, net.facing)
                        if alignment < -0.5:
                            label_cost += w.power_away
                        elif alignment < 0.5:
                            label_cost += w.power_sideways
            if net.style == "label" and len(tips) >= 2:
                xs = [t[0][0] for t in tips]
                ys = [t[0][1] for t in tips]
                label_cost += w.label_distance * net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
                for index, (point, normal, ref) in enumerate(tips):
                    if not involves((ref,)):
                        continue
                    if abs(normal[1]) > abs(normal[0]) + _EPS:
                        label_cost += w.vertical_label
                    others = [t[0] for k, t in enumerate(tips) if k != index and t[2] != ref]
                    if not others:
                        continue
                    cx = sum(o[0] for o in others) / len(others) - point[0]
                    cy = sum(o[1] for o in others) / len(others) - point[1]
                    norm = math.hypot(cx, cy)
                    if norm > _EPS:
                        label_cost += w.label_facing * (1.0 - _dot(normal, (cx / norm, cy / norm))) / 2.0

        # lines cross each other (the others are still obstacles to those that are counted)
        crossings = 0
        for i, (net_a, segs_a, _ends_a, _own_a, _stub_a) in enumerate(lines):
            for net_b, segs_b, _ends_b, _own_b, _stub_b in lines[i + 1:] + fixed_lines:
                if net_a == net_b:
                    continue
                hits = sum(1 for a in segs_a for b in segs_b if _crosses(a, b))
                if hits:
                    crossings += hits
                    note("crossing", net_a, net_b)

        # a wire runs through a symbol that is not at its end, or over a pin that is not on its net
        through = 0
        shorts = 0

        def check(net: str, segs: list, ends: frozenset[str], own: frozenset[tuple[str, str]], only: Iterable[str]) -> None:
            nonlocal through, shorts
            for a in segs:
                for ref in only:
                    pins, _box, body = worlds[ref]
                    if not _segment_near(a, body):
                        continue
                    touched = [pin for pin, (tip, _normal) in pins.items() if (ref, pin) not in own and _on_segment(tip, a)]
                    for pin in touched:                 # over the tip of a pin that is not on this net: a short, whoever's it is
                        shorts += 1
                        note("short", net, ref, pin)
                    if touched:
                        continue
                    if ref in ends:
                        # A wire cannot run across the body of a symbol it joins any more than across another's. The leg
                        # that leaves one of its own pins is the way out (a drawing may reach past the pin's tip: an LED's
                        # arrows), and elsewhere it keeps to the body's edge.
                        if any(_same(tip, a[0]) or _same(tip, a[1]) for pin, (tip, _n) in pins.items() if (ref, pin) in own):
                            continue
                        margin = 0.0
                    else:
                        margin = ROUTE_MARGIN_MM
                    if _segment_through(a, body, margin):
                        through += 1
                        note("through", net, ref)

        for net_name, segs, ends, own, stub in lines:
            if not stub:
                check(net_name, segs, ends, own, worlds)
        if active is not None:
            moved_refs = [ref for ref in active if ref in worlds]
            for net_name, segs, ends, own, stub in fixed_lines:
                if not stub:
                    check(net_name, segs, ends, own, moved_refs)

        # symbols may not stand on each other, nor in the room a pin needs in front of it
        overlaps = 0
        clearance = 0
        references = sorted(worlds)
        for i, ref_a in enumerate(references):
            for ref_b in references[i + 1:]:
                if involves((ref_a, ref_b)) and _box_overlap(worlds[ref_a][1], worlds[ref_b][1]):
                    overlaps += 1
                    note("overlap", ref_a, ref_b)
        for ref, (pins, _box, _body) in worlds.items():
            for pin, (tip, normal) in pins.items():
                depth = self.reach.get((ref, pin), 0.0)
                if depth <= 0:
                    continue
                zone = _zone(tip, normal, depth, self.half_widths.get((ref, pin), ZONE_HALF_WIDTH_MM))
                partners = self.partners.get((ref, pin), set())
                for other, (_other_pins, _other_box, other_body) in worlds.items():
                    if other == ref or not involves((ref, other)):
                        continue
                    if other in partners and depth <= WIRE_REACH_MM + _EPS:
                        continue                    # the part a wire runs to is meant to be in front of the pin
                    if _box_overlap(zone, other_body):
                        clearance += 1
                        note("clearance", f"{ref}.{pin}", other)
                # a wire of another net in that room makes the stub or symbol drawn there cross it, or go round
                own_net = self.net_of.get((ref, pin))
                for net_name, segs, _ends, _own, _stub in (lines + fixed_lines if involves((ref,)) else lines):
                    if net_name == own_net:
                        continue
                    if any(_segment_through(seg, zone, 0.0) for seg in segs):
                        clearance += 1
                        note("clearance", f"{ref}.{pin}", f"wire {net_name}")

        order = 0
        order_cost = 0.0
        for ref, symbol in self.symbols.items():
            if symbol.ordered and ref in worlds and involves((ref,)):
                pins, _box, _body = worlds[ref]
                inversions = order_inversions(
                    {pin: tip for pin, (tip, _normal) in pins.items()}, {pin: n for pin, (_tip, n) in pins.items()},
                )
                order += inversions
                order_cost += w.order * inversions

        boxes = [box for _pins, box, _body in worlds.values()]
        extent = (
            (max(b[2] for b in boxes) - min(b[0] for b in boxes)) + (max(b[3] for b in boxes) - min(b[1] for b in boxes))
            if boxes else 0.0
        )
        moved = sum(
            abs(position[0] - self.initial[ref][0]) + abs(position[1] - self.initial[ref][1])
            for ref, (position, _orientation) in pose.items()
            if ref not in ignore and self.symbols[ref].movable and involves((ref,))
        )
        turned = 0.0
        for ref, (_position, orientation) in pose.items():
            if ref in ignore or not self.symbols[ref].movable or not involves((ref,)):
                continue
            learned = self.symbols[ref].orientation_cost
            if learned is not None:
                turned += learned[orientation]
                continue
            first = self.initial_orientation[ref]
            quarter = ((orientation.rotation - first.rotation) // 90) % 4
            turned += w.turn * min(quarter, 4 - quarter) + (w.flip if orientation.mirror != first.mirror else 0.0)
        total = (
            length + w.bend * bends + w.crossing * crossings + w.through * through + w.short * shorts
            + w.clearance * clearance + w.overlap * overlaps + order_cost + label_cost + w.extent * extent + w.move * moved
            + w.jog * jogs + turned + flow_cost
        )
        return Cost(total, length, bends, crossings, clearance, overlaps, order, through, shorts, jogs)


# ---------------------------------------------------------------------------------------------------------------------
# the search


def _cells(value: float) -> int:
    return round(value / GRID_MM)


class _Search:
    """The candidate poses of one symbol: every orientation it may take, over a lattice of positions around a centre and
    at every position that lines one of its pins up with a pin it connects to."""

    def __init__(self, model: _Model, movable: Sequence[PlacedSymbol], radius: int, step_cells: int, budget: int) -> None:
        self.model = model
        self.radius = radius
        self.step_cells = step_cells
        self.budget = budget
        self.window_cells = {s.reference: math.ceil(s.window / GRID_MM - 1e-9) for s in movable if s.window is not None}

    def candidates(
        self, reference: str, current: Pose, center: Point | None = None, ignore: frozenset[str] = frozenset(),
        span_cells: int | None = None,
    ) -> list[tuple[float, Point, Orientation]]:
        """Poses of one symbol, best first, with everything else where ``current`` has it. The lattice is centred on where
        the symbol stood to begin with, or on ``center``; ``ignore`` are symbols not placed yet."""
        model = self.model
        symbol = model.symbols[reference]
        ox, oy = center if center is not None else symbol.position
        cx, cy = _cells(ox), _cells(oy)
        partner_pins = [
            model.world(ref, *current[ref])[0][pin][0]
            for net in model.nets_of.get(reference, ())
            for ref, pin in net.pins if ref != reference and ref not in ignore
        ]
        found: list[tuple[float, int, Point, Orientation]] = []
        seen: set[tuple[int, int, Orientation]] = set()
        # a symbol with a window is searched cell by cell, within it; one without, on the coarser lattice
        reach_cells = self.window_cells.get(reference)
        step = 1 if reach_cells is not None else self.step_cells
        span = span_cells if span_cells is not None else (self.radius if reach_cells is None else reach_cells)
        limit = span if reach_cells is not None else span * 2
        active = PLAN.active(reference)
        is_att = reference in PLAN.att
        BI = 2 if is_att else 1
        row_cell = PLAN.row_cell(reference)
        others = [
            (ref, model.world(ref, *current[ref])[BI]) for ref in model.symbols
            if ref != reference and ref not in ignore and ref not in active and not (is_att and ref == PLAN.att[reference][0])
        ]  # (reference, policy box)
        for orientation in PLAN.orients(reference, symbol):
            geometry = symbol.geometry[orientation]
            xs = {cx + k * step for k in range(-(span // step), span // step + 1)}
            ys = {cy + k * step for k in range(-(span // step), span // step + 1)}
            for pin in geometry.pins.values():
                for point in partner_pins:
                    xs.add(_cells(point[0] - pin.point[0]))
                    ys.add(_cells(point[1] - pin.point[1]))
            xs.add(cx)
            ys.add(cy)
            if row_cell is not None:
                ys = {row_cell}
            if is_att:
                xs, ys = PLAN.att_lattice(reference, orientation, current, model)
            for ix, iy in product(sorted(xs), sorted(ys)):
                key = (ix, iy, orientation)
                if key in seen or (not is_att and (abs(ix - cx) > limit or (row_cell is None and abs(iy - cy) > limit))):
                    continue
                if row_cell is not None and not PLAN.x_ok(reference, ix * GRID_MM, current, orientation):
                    continue
                if model.evaluated >= self.budget:
                    break
                seen.add(key)
                position = (ix * GRID_MM, iy * GRID_MM)
                solid = (geometry.body or geometry.box) if is_att else geometry.box
                box = (position[0] + solid[0], position[1] + solid[1], position[0] + solid[2], position[1] + solid[3])
                if any(_box_overlap(box, other_box) for _ref, other_box in others):
                    continue
                if is_att and not PLAN.att_ok(reference, orientation, position, current, model):
                    continue
                trial = dict(current)
                trial[reference] = (position, orientation)
                PLAN.settle(trial, reference, model)
                cost = model.cost(trial, active, None, ignore)
                found.append((cost.total, len(found), position, orientation))
        here = dict(current)
        found.append((model.cost(here, active, None, ignore).total, -1, current[reference][0], current[reference][1]))
        found.sort(key=lambda item: (round(item[0], 6), item[1]))
        return [(total, position, orientation) for total, _index, position, orientation in found]



# ---------------------------------------------------------------------------------------------------------------------
# prototype: attachments (A) and pinned path (B), iteration 2

ATTACH = True
CHAIN = True
PITCH_MM = 7.62
HANG_MM = 2.54
SPAN_MM = 15.24
CHAIN_GAP_MM = 2.54
DX_SLOTS = (0.0, PITCH_MM, -PITCH_MM, 2 * PITCH_MM, -2 * PITCH_MM)
SEED_PREFIX = "VIJ"


class Plan:
    def __init__(self, symbols, nets, model):
        self.att = {}      # attachment -> (host, host pin, rail kind, wire pin)
        self.orient = {}
        self.dx = {}
        self.hosts = {}
        self.chain = []
        self.pos = {}
        self.noslot = set()
        self.y = None
        self.symbols = {s.reference: s for s in symbols}
        if ATTACH:
            self._attachments(symbols, model)
        if CHAIN:
            self._chain(model)
        if ATTACH and self.chain:
            self._retarget(model)

    @staticmethod
    def _npins(s):
        return len(next(iter(s.geometry.values())).pins)

    def _attachments(self, symbols, model):
        kind = {}
        for n in model.nets:
            if n.style == "power":
                for p in n.pins:
                    kind[p] = "ground" if n.facing is not None and tuple(n.facing) == (0, 1) else "supply"
        wire = {p: n for n in model.nets if n.style == "wire" for p in n.pins}
        cand = {}
        for s in symbols:
            if not s.movable or s.reference[0] in SEED_PREFIX:
                continue
            pins = sorted(next(iter(s.geometry.values())).pins)
            if len(pins) != 2:
                continue
            ks = [kind.get((s.reference, p)) for p in pins]
            ws = [wire.get((s.reference, p)) for p in pins]
            for i in (0, 1):
                if ks[i] and ws[1 - i] is not None and ks[1 - i] is None:
                    cand[s.reference] = (pins[i], pins[1 - i], ks[i], ws[1 - i])
        slots = {}
        for ref in sorted(cand):
            rail_pin, wire_pin, rk, net = cand[ref]
            others = sorted({r for r, _p in net.pins if r != ref})
            pool = [r for r in others if r not in cand]
            if not pool:
                continue
            best = max(self._npins(self.symbols[r]) for r in pool)
            host = next(r for r in pool if self._npins(self.symbols[r]) == best)   # ties: lowest reference
            want = (0, 1) if rk == "ground" else (0, -1)
            s = self.symbols[ref]
            good = [o for o, g in s.geometry.items() if tuple(round(v) for v in g.pins[rail_pin].normal) == want]
            if not good:
                continue
            cost = s.orientation_cost or {}
            o = min(good, key=lambda o: (0 if o == s.orientation else 1, cost.get(o, 0)))
            hpin = next(p for r, p in net.pins if r == host)
            h = self.symbols[host]
            vertical = abs(h.geometry[h.orientation].pins[hpin].normal[1]) > 0.5
            key = (host, hpin) if vertical else (host,)
            k = slots.get(key, 0)
            slots[key] = k + 1
            self.att[ref] = (host, hpin, rk, wire_pin)
            self.orient[ref] = o
            self.dx[ref] = DX_SLOTS[k % len(DX_SLOTS)]
            self.hosts.setdefault(host, []).append(ref)

    def _chain(self, model):
        adj = {}
        for n in model.nets:
            if n.style != "wire":
                continue
            members = sorted({r for r, _p in n.pins if r not in self.att})
            for a in members:
                adj.setdefault(a, set()).update(set(members) - {a})
        if not adj:
            return
        seeds = [r for r in sorted(adj) if r[0] in SEED_PREFIX] or [min(adj, key=lambda r: (self.symbols[r].position[0], r))]
        pool = [r for r in sorted(adj) if r not in seeds]
        actives = [r for r in pool if r[0] in "UQ"] or pool      # D1/T1 of the LM317 have 4 pins to U1's 3: ICs and transistors only
        top = max(self._npins(self.symbols[r]) for r in actives)
        musts = [r for r in actives if self._npins(self.symbols[r]) == top]

        def bfs(start, banned=()):
            dist, par, q = {start: 0}, {}, [start]
            for r in q:
                for m in sorted(adj[r]):
                    if m not in dist and m not in banned:
                        dist[m] = dist[r] + 1
                        par[m] = r
                        q.append(m)
            return dist, par

        def path(par, end, start):
            p = [end]
            while p[-1] != start:
                p.append(par[p[-1]])
            return p[::-1]
        best = []
        for s in seeds:
            d1, p1 = bfs(s)
            for m in musts:
                if m not in d1:
                    continue
                head = path(p1, m, s)
                d2, p2 = bfs(m, banned=set(head) - {m})
                far = max(sorted(d2), key=lambda r: d2[r])
                tail = path(p2, far, m)
                chain = head + tail[1:]
                if len(chain) > len(best):
                    best = chain
        if len(best) >= 3:
            self.chain = best
            self.pos = {r: i for i, r in enumerate(best)}

    # -- chain
    def row_cell(self, ref):
        return None if self.y is None or ref not in self.pos or not self.symbols[ref].movable else _cells(self.y)

    def _bx(self, ref, pose, o=None):
        b = self.symbols[ref].geometry[o or pose[ref][1]].box
        return b[0], b[2]

    def x_ok(self, ref, x, pose, o=None):
        """Neighbouring chain parts keep their boxes CHAIN_GAP_MM apart (not just their centres)."""
        i = self.pos[ref]
        l, r = self._bx(ref, pose, o)
        if i > 0:
            n = self.chain[i - 1]
            if x + l < pose[n][0][0] + self._bx(n, pose)[1] + CHAIN_GAP_MM - 1e-6:
                return False
        if i + 1 < len(self.chain):
            n = self.chain[i + 1]
            if pose[n][0][0] + self._bx(n, pose)[0] < x + r + CHAIN_GAP_MM - 1e-6:
                return False
        return True

    def combo_ok(self, members, trial):
        return all(self.x_ok(r, trial[r][0][0], trial, trial[r][1]) for r in members if r in self.pos and self.row_cell(r) is not None)

    def keeps(self, pose):
        if self.y is None:
            return True
        for r in self.chain:
            if self.symbols[r].movable and abs(pose[r][0][1] - self.y) > 1e-6:
                return False
        return all(self.x_ok(r, pose[r][0][0], pose) for r in self.chain)

    # -- attachments (hung from the net's trunk, not from a pin)
    def _retarget(self, model):
        for ref, (host, hpin, rk, wpin) in list(self.att.items()):
            net = next((n for n in model.nets if n.style == "wire" and (ref, wpin) in n.pins), None)
            if net is None:
                continue
            for _i, r, p in sorted((self.pos[r], r, p) for r, p in net.pins if r in self.pos):
                h = self.symbols[r]
                normal = h.geometry[h.orientation].pins[p].normal
                if abs(normal[0]) > 0.5:
                    self.hosts[host].remove(ref)
                    self.hosts.setdefault(r, []).append(ref)
                    self.att[ref] = (r, p, rk, wpin)
                    break

    def active(self, ref):
        return frozenset((ref,)) if ref in self.att else frozenset((ref, *self.hosts.get(ref, ())))

    def orients(self, ref, symbol):
        if ref in self.att:
            return [self.orient[ref]]
        if ref in self.pos and len(symbol.geometry[symbol.orientation].pins) == 2 and self.y is not None:
            flat = [o for o, g in symbol.geometry.items() if len({round(p.point[1], 3) for p in g.pins.values()}) == 1]
            if flat:
                return flat
        return list(symbol.geometry)

    def _anchor(self, ref, pose, model):
        """(x of the first slot, y of the trunk, direction the trunk runs along x)."""
        host, hpin, rk, wpin = self.att[ref]
        hpos, horient = pose[host]
        pins, _box, body = model.world(host, hpos, horient)
        tip, normal = pins[hpin]
        if abs(normal[0]) > 0.5:
            d = 1 if normal[0] > 0 else -1
            return (body[2] if d > 0 else body[0]) + d * PITCH_MM, tip[1], d
        return tip[0], tip[1] + (PITCH_MM if normal[1] > 0 else -PITCH_MM), 1

    def att_lattice(self, ref, orientation, pose, model):
        xb, yt, d = self._anchor(ref, pose, model)
        wp = self.symbols[ref].geometry[orientation].pins[self.att[ref][3]].point
        steps = int(SPAN_MM / GRID_MM + 1e-9)
        return {_cells(xb + k * GRID_MM - wp[0]) for k in range(-steps, steps + 1)}, {_cells(yt - wp[1])}

    def att_ok(self, ref, orientation, position, pose, model, skip=()):
        wp = self.symbols[ref].geometry[orientation].pins[self.att[ref][3]].point
        tip = (position[0] + wp[0], position[1] + wp[1])
        box = model.world(ref, position, orientation)[1]
        for other in model.symbols:
            if other == ref or other in skip:
                continue
            pins, ob, _body = model.world(other, *pose[other])
            for pt, _n in pins.values():
                if math.hypot(pt[0] - tip[0], pt[1] - tip[1]) < HANG_MM - 1e-6:
                    return False
            if box[0] < ob[2] + HANG_MM and ob[0] < box[2] + HANG_MM and box[1] < ob[3] + HANG_MM and ob[1] < box[3] + HANG_MM:
                return False
        return True

    def _pose_for(self, ref, pose, model, dx):
        xb, yt, d = self._anchor(ref, pose, model)
        o = self.orient[ref]
        wp = self.symbols[ref].geometry[o].pins[self.att[ref][3]].point
        return ((_cells(xb + d * dx - wp[0]) * GRID_MM, _cells(yt - wp[1]) * GRID_MM), o)

    def hang_errors(self, pose, model):
        """Attachments whose body crosses the trunk row line (the wire-net pin's y)."""
        bad = []
        for ref, (_h, _hp, rk, wp) in self.att.items():
            (px, py), o = pose[ref]
            body = model.world(ref, (px, py), o)[2]
            y = py + self.symbols[ref].geometry[o].pins[wp].point[1]
            if not (body[1] >= y - 1e-6 if rk == "ground" else body[3] <= y + 1e-6):
                bad.append(ref)
        return bad

    def settle(self, pose, host, model):
        for ref in self.hosts.get(host, ()):
            pose[ref] = self._pose_for(ref, pose, model, self.dx[ref])

    def sync(self, pose, ref, model):
        xb, _y, d = self._anchor(ref, pose, model)
        wp = self.symbols[ref].geometry[pose[ref][1]].pins[self.att[ref][3]].point
        self.dx[ref] = (pose[ref][0][0] + wp[0] - xb) * d

    def _keep_slid(self, pose, model):
        kept = set()
        for ref in self.att:
            xb, yt, d = self._anchor(ref, pose, model)
            (px, py), o = pose[ref]
            wp = self.symbols[ref].geometry[self.orient[ref]].pins[self.att[ref][3]].point
            if o == self.orient[ref] and abs(py + wp[1] - yt) < 0.7 and abs((px + wp[0] - xb) * d) <= SPAN_MM + 0.7:
                self.dx[ref] = (px + wp[0] - xb) * d
                kept.add(ref)
        return kept

    def _place_all(self, t, model, kept):
        pending = set(self.att) - kept
        for ref in sorted(pending):
            pending.discard(ref)
            first = None
            for dx in (0.0, PITCH_MM, 2 * PITCH_MM, -PITCH_MM, -2 * PITCH_MM):
                p = self._pose_for(ref, t, model, dx)
                first = first or (dx, p)
                if self.att_ok(ref, p[1], p[0], t, model, skip=pending):
                    first = (dx, p)
                    break
            if not self.att_ok(ref, first[1][1], first[1][0], t, model, skip=pending):
                self.noslot.add(ref)
            self.dx[ref], t[ref] = first

    def prepass(self, pose, model):
        kept = self._keep_slid(pose, model)
        ys = sorted({round(pose[r][0][1] / GRID_MM) * GRID_MM for r in self.chain if self.symbols[r].movable})
        best = None
        for y in ys or [None]:
            t = dict(pose)
            self.y = y
            if y is not None:
                prev = None
                for r in self.chain:
                    (x, _y), o = t[r]
                    if self.symbols[r].movable:
                        bl, _br = self._bx(r, t)
                        if prev is not None and x + bl < prev + CHAIN_GAP_MM:
                            x = math.ceil((prev + CHAIN_GAP_MM - bl + 0.01) / GRID_MM) * GRID_MM
                        t[r] = ((x, y), o)
                    prev = t[r][0][0] + self._bx(r, t)[1]
            if kept and len(ys) <= 1:
                for ref in kept:
                    t[ref] = self._pose_for(ref, t, model, self.dx[ref])
                self._place_all(t, model, kept)
            else:
                self._place_all(t, model, set())
            c = model.cost(t).total
            if best is None or c < best[0] - 1e-9:
                best = (c, y, t, dict(self.dx))
        if best is None:
            return pose
        self.y, self.dx = best[1], best[3]
        return best[2]


PLAN = Plan([], [], type("M", (), {"nets": []})())


def refine_placement(
    symbols: Sequence[PlacedSymbol],
    nets: Sequence[Connection],
    *,
    reach: Mapping[tuple[str, str], float] | None = None,
    weights: Weights | None = None,
    half_widths: Mapping[tuple[str, str], float] | None = None,
    radius_cells: int = 24,
    step_cells: int = 2,
    keep: int = 20,
    sweeps: int = 6,
    exhaustive_limit: int = 30000,
    budget: int = 120_000,
    start_conventional: bool = True,
) -> Refinement:
    """Choose position and orientation of the movable symbols; everything else stays where it is.

    A symbol is tried at every orientation it may take, over a lattice of positions around where it stands and at every
    position that lines one of its pins up with a pin it connects to. They are moved one at a time to whatever lowers
    the cost, then the few that interact are tried together. The result is never worse than the start.

    A sheet with many movable symbols is searched nearer to where they stand (each has a lattice of its own), and the
    search stops after ``budget`` poses have been costed, keeping the best found: how long it takes is a count, not a
    clock, so the same design gives the same answer on any machine.
    """
    global PLAN
    model = _Model(symbols, nets, reach or {}, weights or Weights(), half_widths)
    start0: dict[str, tuple[Point, Orientation]] = {s.reference: (s.position, s.orientation) for s in symbols}
    PLAN = Plan(symbols, nets, model)
    start = PLAN.prepass(dict(start0), model)
    before = model.cost(start)
    movable = [s for s in symbols if s.movable]

    def explain(pose: Pose) -> dict[str, list]:
        found: dict[str, list] = {}
        model.cost(pose, None, found)
        return found

    if not movable:
        return Refinement({}, before, before, model.evaluated, {"before": explain(start), "after": explain(start)})

    pose = dict(start)
    if start_conventional:
        # a symbol with learned costs starts at the orientation people draw it in (ties keep where it is)
        for symbol in movable:
            learned = symbol.orientation_cost
            if learned is None or symbol.orientation not in learned:
                continue
            cheapest = min(learned.values())
            if learned[symbol.orientation] > cheapest + 1e-9:
                best = next(o for o in symbol.geometry if o in learned and learned[o] <= cheapest + 1e-9)
                pose[symbol.reference] = (symbol.position, best)
    radius = radius_cells if len(movable) <= 4 else max(10, (radius_cells * 6) // len(movable))
    search = _Search(model, movable, radius, step_cells, budget)
    candidates = search.candidates

    order = sorted(
        movable, key=lambda s: (-len(model.nets_of.get(s.reference, ())), s.reference),
    )
    for _sweep in range(sweeps):
        improved = False
        for symbol in order:
            if model.evaluated >= budget:
                break
            ranked = candidates(symbol.reference, pose)
            best_total, best_position, best_orientation = ranked[0]
            here = model.cost(pose, PLAN.active(symbol.reference)).total
            if best_total < here - 1e-6:
                pose[symbol.reference] = (best_position, best_orientation)
                PLAN.settle(pose, symbol.reference, model)
                if symbol.reference in PLAN.att:
                    PLAN.sync(pose, symbol.reference, model)
                improved = True
        if not improved:
            break

    # symbols that share a net with each other are tried together, a few at a time
    movable_refs = {s.reference for s in movable}
    parent = {ref: ref for ref in movable_refs}

    def find(ref: str) -> str:
        while parent[ref] != ref:
            parent[ref] = parent[parent[ref]]
            ref = parent[ref]
        return ref

    for net in model.nets:
        members = [ref for ref, _pin in net.pins if ref in movable_refs]
        for other in members[1:]:
            parent[find(other)] = find(members[0])
    components: dict[str, list[str]] = {}
    for ref in sorted(movable_refs):
        components.setdefault(find(ref), []).append(ref)
    for members in components.values():
        members = [m for m in members if m not in PLAN.att]
        if len(members) < 2 or len(members) > 3:
            continue
        ranked = {ref: candidates(ref, pose)[:keep] for ref in members}
        combos = 1
        for ref in members:
            combos *= len(ranked[ref])
        if combos > exhaustive_limit or model.evaluated >= budget:
            continue
        active = frozenset(members).union(*(PLAN.active(r) for r in members))
        best_total = model.cost(pose, active).total
        best_pose = {ref: pose[ref] for ref in active}
        for combo in product(*(ranked[ref] for ref in members)):
            trial = dict(pose)
            for ref, (_total, position, orientation) in zip(members, combo):
                trial[ref] = (position, orientation)
                PLAN.settle(trial, ref, model)
            overlapping = any(
                _box_overlap(model.world(a, *trial[a])[1], model.world(b, *trial[b])[1])
                for i, a in enumerate(members) for b in members[i + 1:]
            )
            if overlapping or not PLAN.combo_ok(members, trial):
                continue
            total = model.cost(trial, active).total
            if total < best_total - 1e-6:
                best_total = total
                best_pose = {ref: trial[ref] for ref in active}
        pose.update(best_pose)

    after = model.cost(pose)
    if after.total >= before.total - 1e-6:
        pose = start
        after = before
    placements = {ref: Placement(*pose[ref]) for ref in sorted(pose) if pose[ref] != start0[ref]}
    return Refinement(placements, before, after, model.evaluated, {"before": explain(start), "after": explain(pose)})


# ---------------------------------------------------------------------------------------------------------------------
# bringing symbols in


def seed_placement(
    symbols: Sequence[PlacedSymbol],
    nets: Sequence[Connection],
    *,
    groups: Mapping[str, str] | None = None,
    reach: Mapping[tuple[str, str], float] | None = None,
    weights: Weights | None = None,
    half_widths: Mapping[tuple[str, str], float] | None = None,
    near_cells: int = 16,
    radius_cells: int = 10,
    budget: int = 80_000,
) -> Refinement:
    """Bring the movable symbols that stand far from what they connect to in beside it.

    MCP Pro's auto-layout scatters every symbol over the sheet, a relation puts a support part beside its pin, and a
    part no relation places is left where the scatter dropped it, so a block's members are spread over hundreds of
    millimetres and the floorplan is made of boxes that size. Each movable symbol, the one most connected to what is
    already in place first, goes to the cells around the middle of the pins it connects to (in its own group, when groups
    are given: a block's members stay a block), where it costs least; one already within ``near_cells`` of that stays.
    A symbol with no connection to anything in place is packed beside its group instead of being left to the scatter.
    Nothing here turns a symbol: orientation is chosen after the floorplan.
    """
    model = _Model(symbols, nets, reach or {}, weights or Weights(), half_widths)
    start: dict[str, tuple[Point, Orientation]] = {s.reference: (s.position, s.orientation) for s in symbols}
    before = model.cost(start)
    group_of = dict(groups or {})
    pending = [s.reference for s in symbols if s.movable and s.window is None]
    in_place = {s.reference for s in symbols} - set(pending)
    pose = dict(start)
    search = _Search(model, [s for s in symbols if s.movable], radius_cells, 1, budget)
    placed: list[str] = []

    def partners(reference: str) -> list[tuple[str, str, str]]:
        """(partner reference, partner pin, own pin) of every pin this symbol shares a wire or label net with, among the
        symbols in place and of its group."""
        found = []
        for net in model.nets_of.get(reference, ()):
            if net.style == "power":
                continue
            own_pins = [pin for ref, pin in net.pins if ref == reference]
            for ref, pin in net.pins:
                if ref != reference and ref in in_place and group_of.get(ref) == group_of.get(reference):
                    found.extend((ref, pin, own) for own in own_pins)
        return found

    while pending and model.evaluated < budget:
        pulled = {ref: partners(ref) for ref in pending}
        pick = max(pending, key=lambda ref: (len(pulled[ref]), -pending.index(ref)))
        pending.remove(pick)
        links = pulled[pick]
        if links:
            tips = [model.world(ref, *pose[ref])[0][pin][0] for ref, pin, _own in links]
            target = (sum(t[0] for t in tips) / len(tips), sum(t[1] for t in tips) / len(tips))
            here = pose[pick][0]
            distance_cells = (abs(here[0] - target[0]) + abs(here[1] - target[1])) / GRID_MM
            if distance_cells > near_cells:
                ranked = search.candidates(pick, pose, center=target, ignore=frozenset(pending))
                _total, position, orientation = ranked[0]
                pose[pick] = (position, orientation)
                placed.append(pick)
        else:
            # nothing in place to go to: beside the other symbols of its group that are, or where it stands
            mates = [ref for ref in in_place if group_of.get(ref) == group_of.get(pick) and ref in pose]
            if mates:
                centre = [model.world(ref, *pose[ref])[1] for ref in mates]
                target = (
                    sum((b[0] + b[2]) / 2 for b in centre) / len(centre),
                    sum((b[1] + b[3]) / 2 for b in centre) / len(centre),
                )
                here = pose[pick][0]
                if (abs(here[0] - target[0]) + abs(here[1] - target[1])) / GRID_MM > near_cells:
                    ranked = search.candidates(pick, pose, center=target, ignore=frozenset(pending))
                    _total, position, orientation = ranked[0]
                    pose[pick] = (position, orientation)
                    placed.append(pick)
        in_place.add(pick)

    after = model.cost(pose)
    placements = {ref: Placement(*pose[ref]) for ref in placed if pose[ref] != start[ref]}
    return Refinement(placements, before, after, model.evaluated)


# ---------------------------------------------------------------------------------------------------------------------
# lining blocks up


def _align_groups_engine(
    symbols: Sequence[PlacedSymbol],
    nets: Sequence[Connection],
    *,
    groups: Mapping[str, str],
    order: Sequence[tuple[str, str]] = (),
    reach: Mapping[tuple[str, str], float] | None = None,
    weights: Weights | None = None,
    half_widths: Mapping[tuple[str, str], float] | None = None,
    window_cells: int = 24,
    sweeps: int = 3,
    budget: int = 60_000,
) -> Refinement:
    """Slide each group of symbols (a block), as one piece, to where its connections to the others run straightest.

    ``order`` lists pairs of groups ``(before, after)`` that a signal flows between: a block that stood before another
    stays before it (the cost knows nothing of direction, and sliding a block to the far side of the one it feeds shortens
    a wire by turning the flow back on itself).

    The floorplan puts blocks in columns by the flow between them and leaves each wherever the layered layout dropped
    it, so the wire from one block to the next steps up or down between two pins that could have shared a row, and a chain
    of blocks reads as a staircase. A block is moved over a window of cells around where it stands, coarse then fine, to
    wherever the estimated routes of its connections cost least: that is where its pins line up with the pins they join.
    Blocks that are already joined by a straight wire slide together along it as well (a row of blocks moves as a row,
    a column as a column): one of them alone would break the wire that keeps the other in line, and no slide of a block
    alone could ever bring a chain of three into line. Nothing inside a block changes, and a block never moves onto
    another. The result is never worse than the start.
    """
    model = _Model(symbols, nets, reach or {}, weights or Weights(), half_widths)
    start: dict[str, tuple[Point, Orientation]] = {s.reference: (s.position, s.orientation) for s in symbols}
    before = model.cost(start)
    members: dict[str, list[str]] = {}
    for symbol in symbols:
        group = groups.get(symbol.reference)
        if group is not None:
            members.setdefault(group, []).append(symbol.reference)
    if len(members) < 2:
        return Refinement({}, before, before, model.evaluated)

    pose = dict(start)
    external = {
        name: sum(1 for net in model.nets if {ref for ref, _pin in net.pins} & set(refs)
                  and {ref for ref, _pin in net.pins} - set(refs))
        for name, refs in members.items()
    }
    sweep_order = sorted(members, key=lambda name: (-external[name], name))

    def span_x(name: str, where: Pose) -> tuple[float, float]:
        boxes = [model.world(ref, *where[ref])[1] for ref in members[name]]
        return min(b[0] for b in boxes), max(b[2] for b in boxes)

    def centre_x(name: str, where: Pose) -> float:
        left, right = span_x(name, where)
        return (left + right) / 2.0

    # a flow that runs from one column to the next keeps running that way: the block that feeds stays on its side of the
    # one it feeds (two blocks one above the other share a column, and may slide either way)
    kept = [
        (a, b) for a, b in order
        if a in members and b in members and span_x(a, pose)[1] <= span_x(b, pose)[0] + 1e-6
    ]

    def slid(refs: Sequence[str], dx: int, dy: int) -> dict[str, tuple[Point, Orientation]] | None:
        trial = dict(pose)
        for ref in refs:
            (x, y), orientation = pose[ref]
            trial[ref] = ((x + dx * GRID_MM, y + dy * GRID_MM), orientation)
        names = {groups[ref] for ref in refs if ref in groups}
        if dx and any(
            centre_x(a, trial) > centre_x(b, trial) + 1e-6 for a, b in kept if (a in names) != (b in names)
        ):
            return None
        # a block never lands on another: the room each symbol keeps (its box) must be free of what is not in the block
        moving = set(refs)
        for ref in refs:
            box = model.world(ref, *trial[ref])[1]
            for other in pose:
                if other not in moving and _box_overlap(box, model.world(other, *pose[other])[1]):
                    return None
        return trial

    def straight_neighbours(name: str) -> tuple[set[str], set[str]]:
        """The blocks joined to ``name`` by a chain of straight wires along a row (they share a y) and along a column."""
        links: dict[str, dict[str, set[str]]] = {"row": {}, "column": {}}
        for net in model.nets:
            if net.style != "wire":
                continue
            tips = []
            for ref, pin in net.pins:
                pins, _box, _body = model.world(ref, *pose[ref])
                tips.append((pins[pin][0], pins[pin][1]))
            for route in model.net_routes(tips):
                if route.bends or len(route.pins) != 2 or len(route.points) != 2:
                    continue
                a, b = (groups.get(net.pins[i][0]) for i in route.pins)
                if a is None or b is None or a == b:
                    continue
                kind = "row" if abs(route.points[0][1] - route.points[1][1]) < _EPS else "column"
                links[kind].setdefault(a, set()).add(b)
                links[kind].setdefault(b, set()).add(a)
        found: list[set[str]] = []
        for kind in ("row", "column"):
            seen, stack = {name}, [name]
            while stack:
                for other in links[kind].get(stack.pop(), ()):
                    if other not in seen:
                        seen.add(other)
                        stack.append(other)
            found.append(seen)
        return found[0], found[1]

    def search(refs: Sequence[str], active: frozenset[str], along: str | None) -> None:
        nonlocal pose
        best_total = model.cost(pose, active).total
        best = (0, 0)
        for step in (4, 2, 1):
            centre = best
            reach_cells = window_cells if step == 4 else step * 2
            dxs = [0] if along == "row" else range(centre[0] - reach_cells, centre[0] + reach_cells + 1, step)
            dys = [0] if along == "column" else range(centre[1] - reach_cells, centre[1] + reach_cells + 1, step)
            for dx in dxs:
                for dy in dys:
                    if (dx, dy) == (0, 0) or abs(dx) > window_cells or abs(dy) > window_cells:
                        continue
                    if model.evaluated >= budget:
                        break
                    trial = slid(refs, dx, dy)
                    if trial is None:
                        continue
                    total = model.cost(trial, active).total
                    if total < best_total - 1e-6:
                        best_total, best = total, (dx, dy)
        if best != (0, 0):
            moved = slid(refs, *best)
            assert moved is not None
            pose = moved

    for _sweep in range(sweeps):
        before_sweep = dict(pose)
        for name in sweep_order:
            refs = members[name]
            search(refs, frozenset(refs), None)
            row, column = straight_neighbours(name)
            for along, chain in (("row", row), ("column", column)):
                if len(chain) > 1:
                    chain_refs = [ref for member in sorted(chain) for ref in members[member]]
                    search(chain_refs, frozenset(chain_refs), along)
        if pose == before_sweep or model.evaluated >= budget:
            break

    after = model.cost(pose)
    if after.total >= before.total - 1e-6:
        return Refinement({}, before, before, model.evaluated)
    placements = {ref: Placement(*pose[ref]) for ref in sorted(pose) if pose[ref] != start[ref]}
    return Refinement(placements, before, after, model.evaluated)


def align_groups(symbols, nets, **kwargs):
    model = _Model(symbols, nets, kwargs.get("reach") or {}, kwargs.get("weights") or Weights(), kwargs.get("half_widths"))
    plan = Plan(symbols, nets, model)
    start = {s.reference: (s.position, s.orientation) for s in symbols}
    pose = plan.prepass(dict(start), model)
    shifted = [replace(s, position=pose[s.reference][0], orientation=pose[s.reference][1]) for s in symbols]
    out = _align_groups_engine(shifted, nets, **kwargs)
    pose2 = dict(pose)
    for ref, p in out.placements.items():
        pose2[ref] = (p.position, p.orientation)
    for host in plan.hosts:
        plan.settle(pose2, host, model)
    if not plan.keeps(pose2):
        pose2 = pose
    placements = {ref: Placement(*pose2[ref]) for ref in sorted(pose2) if pose2[ref] != start[ref]}
    return Refinement(placements, out.before, out.after, out.evaluated)
