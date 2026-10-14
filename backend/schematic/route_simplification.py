"""Conservative dogleg elimination for orthogonal schematic routes.

Shortcuts require a caller-supplied *electrical* safety predicate that rejects
foreign conductors and unrelated pin contacts, in addition to solid graphical
keepouts. This module never assumes two crossing traces belong to one net.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

Point = tuple[float, float]
Segment = tuple[Point, Point]


def router_origin(nodes: Sequence[Mapping[str, object]], grid_mm: float) -> Point:
    """The grid corner just below and left of every node: the origin a routing problem is posed in.

    libavoid adds up lengths, so two routes of one length differ by float noise that depends on where on the sheet the
    problem stands, and a point on a half grid snaps either way. Offline, 66 recorded problems each moved three times by
    whole grids: 31 of 120 were routed differently as they were, none when posed with this origin. A node exactly on a grid
    line must not move the origin by float noise, hence the slack.
    """
    left = min(float(node["x"]) for node in nodes)  # type: ignore[arg-type]
    top = min(float(node["y"]) for node in nodes)  # type: ignore[arg-type]
    return math.floor(left / grid_mm + 1e-6) * grid_mm, math.floor(top / grid_mm + 1e-6) * grid_mm


def leaves_pin_outward(tip: Point, other: Point, normal: Point) -> bool:
    """Does a segment from a pin's tip to ``other`` head away from the symbol, the way the pin points?

    The first and last segment of a route touch their own symbol, so they are let off the body check for it. They are
    only an escape when they leave outward: one that runs back across the part (a shortcut over a connector, from a pin
    on its far side) is not.
    """
    return (other[0] - tip[0]) * normal[0] + (other[1] - tip[1]) * normal[1] > 1e-6


def segments_of(points: Sequence[Point]) -> tuple[Segment, ...]:
    return tuple((a, b) for a, b in zip(points, points[1:]) if a != b)


def _compact(points: Sequence[Point]) -> tuple[Point, ...]:
    unique: list[Point] = []
    for point in points:
        if not unique or point != unique[-1]:
            unique.append(point)
    changed = True
    while changed and len(unique) >= 3:
        changed = False
        next_points = [unique[0]]
        for i in range(1, len(unique) - 1):
            a, b, c = next_points[-1], unique[i], unique[i + 1]
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                changed = True
            else:
                next_points.append(b)
        next_points.append(unique[-1])
        unique = next_points
    return tuple(unique)


def _cost(points: Sequence[Point], grid_mm: float) -> float:
    return (
        sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in segments_of(points))
        + grid_mm * 5.0 * max(0,len(points)-2)
    )


def segments_intersect(a: Segment, b: Segment) -> bool:
    """Closed, axis-aligned segment intersection, including T and collinear."""
    (x1,y1),(x2,y2)=a
    (x3,y3),(x4,y4)=b
    eps=1e-5
    if abs(x1-x2)<eps and abs(x3-x4)<eps:
        return abs(x1-x3)<eps and max(min(y1,y2),min(y3,y4)) <= min(max(y1,y2),max(y3,y4))+eps
    if abs(y1-y2)<eps and abs(y3-y4)<eps:
        return abs(y1-y3)<eps and max(min(x1,x2),min(x3,x4)) <= min(max(x1,x2),max(x3,x4))+eps
    if abs(x1-x2)<eps:
        return min(x3,x4)-eps<=x1<=max(x3,x4)+eps and min(y1,y2)-eps<=y3<=max(y1,y2)+eps
    return min(x1,x2)-eps<=x3<=max(x1,x2)+eps and min(y3,y4)-eps<=y1<=max(y3,y4)+eps


def connects_or_overlaps(a: Segment, b: Segment) -> bool:
    """Would KiCad join or merge these two orthogonal segments?

    A touch (a wire end on another wire, or two ends meeting), a T and a collinear
    overlap all connect or merge the two conductors. Two wires that cross in the
    interior of both, with no junction, do not: KiCad draws them as a crossing and
    connects nothing, so that is not a conflict between different nets.
    """
    if not segments_intersect(a, b):
        return False
    (x1,y1),(x2,y2)=a
    (x3,y3),(x4,y4)=b
    eps=1e-5
    a_vertical,b_vertical=abs(x1-x2)<eps,abs(x3-x4)<eps
    if a_vertical==b_vertical:
        return True                      # collinear overlap, or end-to-end contact
    horizontal,vertical=(b,a) if a_vertical else (a,b)
    (hx1,hy),(hx2,_)=horizontal
    (vx,vy1),(_,vy2)=vertical
    return (
        min(abs(vx-hx1),abs(vx-hx2))<eps       # the vertical lands on an end of the horizontal
        or min(abs(hy-vy1),abs(hy-vy2))<eps    # the horizontal lands on an end of the vertical
    )


def simplify_route(
    points: Sequence[Point],
    *,
    route_is_safe: Callable[[tuple[Point,...]], bool],
    grid_mm: float,
    max_rounds: int = 12,
) -> tuple[Point,...]:
    """Remove unnecessary height changes; never mutate an unchecked route.

    Shortcuts may replace any multi-bend subpath with a straight segment or an
    orthogonal L. The safety predicate checks the *entire* proposed path,
    including every pre-existing conductor and foreign pin obstacle.
    """
    if grid_mm <= 0:
        raise ValueError("grid_mm must be positive")
    best=_compact(points)
    if len(best)<2:
        return best
    for _ in range(max_rounds):
        cost=_cost(best,grid_mm)
        replacement=best
        for i in range(len(best)-2):
            for j in range(i+2,len(best)):
                first,last=best[i],best[j]
                options: list[tuple[Point,...]]=[]
                if first[0]==last[0] or first[1]==last[1]:
                    options.append((first,last))
                else:
                    options.append((first,(first[0],last[1]),last))
                    options.append((first,(last[0],first[1]),last))
                for option in options:
                    candidate=_compact((*best[:i],*option,*best[j+1:]))
                    if candidate==best or _cost(candidate,grid_mm)>=cost-1e-6:
                        continue
                    if any(a[0]!=b[0] and a[1]!=b[1] for a,b in segments_of(candidate)):
                        continue
                    if not route_is_safe(candidate):
                        continue
                    cost=_cost(candidate,grid_mm)
                    replacement=candidate
        if replacement==best:
            break
        best=replacement
    return best


def separate_from_foreign(
    points: Sequence[Point],
    *,
    touches_foreign: Callable[[Segment], bool],
    route_is_safe: Callable[[tuple[Point,...]], bool],
    grid_mm: float,
    max_shift: int = 6,
) -> tuple[Point,...]:
    """Move a middle run of a route off a foreign conductor it lies on.

    libavoid keeps parallel runs of two nets apart by a nudging distance, but the route is snapped to the grid afterwards, and
    a channel too narrow for the full distance lets both runs snap to one line: KiCad then merges them and the nets are shorted.
    A run between two bends is slid sideways, the nearest free line first, its neighbours stretching with it. Nothing else is
    changed: a route that touches no foreign conductor is returned as it came, and so is one no slide can free.
    """
    best=_compact(points)
    for _ in range(len(best)):
        segments=segments_of(best)
        hit=next((index for index in range(1,len(segments)-1) if touches_foreign(segments[index])),None)
        if hit is None:
            return best
        (x1,y1),(x2,y2)=segments[hit]
        vertical=abs(x1-x2)<1e-5
        replacement=None
        for step in range(1,max_shift+1):
            for sign in (1.0,-1.0):
                delta=sign*step*grid_mm
                moved=((x1+delta,y1),(x2+delta,y2)) if vertical else ((x1,y1+delta),(x2,y2+delta))
                candidate=_compact((*best[:hit],*moved,*best[hit+2:]))
                if any(a[0]!=b[0] and a[1]!=b[1] for a,b in segments_of(candidate)):
                    continue
                if any(touches_foreign(segment) for segment in segments_of(candidate)):
                    continue
                if route_is_safe(candidate):
                    replacement=candidate
                    break
            if replacement is not None:
                break
        if replacement is None:
            return best
        best=replacement
    return best


class DiagonalRepairError(ValueError):
    """A non-orthogonal segment for which neither L-shaped replacement is safe."""

    def __init__(self, segment: Segment) -> None:
        super().__init__(f"no safe orthogonal replacement for diagonal {segment[0]}->{segment[1]}")
        self.segment = segment


def repair_diagonals(
    points: Sequence[Point],
    *,
    route_is_safe: Callable[[tuple[Point, ...]], bool],
    grid_mm: float,
) -> tuple[tuple[Point, ...], int]:
    """Replace each non-orthogonal segment (a, b) by an L through (b.x, a.y) or (a.x, b.y).

    The corners are tried in that order and the first whose two segments pass `route_is_safe` is used (the corner
    takes its coordinates from the grid-snapped end points, so it is on the grid too). When neither is safe the
    DiagonalRepairError names the segment. Returns the route and the number of segments repaired; an orthogonal
    route comes back unchanged.
    """
    if grid_mm <= 0:
        raise ValueError("grid_mm must be positive")
    result: list[Point] = [points[0]] if points else []
    repaired = 0
    for a, b in zip(points, points[1:], strict=False):
        if abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6:
            result.append(b)
            continue
        for corner in ((b[0], a[1]), (a[0], b[1])):
            if route_is_safe((a, corner, b)):
                result.extend((corner, b))
                repaired += 1
                break
        else:
            raise DiagonalRepairError((a, b))
    return (tuple(result) if repaired == 0 else _compact(result)), repaired
