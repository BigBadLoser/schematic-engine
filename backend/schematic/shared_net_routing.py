"""Circuit-neutral rectilinear shared-net topology planner.

Uses electrical net endpoints and *measured* KiCad pin terminals. A Hanan-grid
Steiner candidate is considered alongside the rectilinear terminal MST, with
foreign graphics and pre-existing wires treated as obstacles. The output is a
single grouped routing transaction; it is a topology proposal, not a finished
wire path. Libavoid owns actual obstacle-avoiding segments and KiCad's strict
netlist verifier remains the electrical acceptance gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from typing import Any

from .intent import NetIntent
from .presentation_geometry import GeometryKind, GeometryScene

Point = tuple[float, float]
_GRAPHICS = frozenset({GeometryKind.BODY, GeometryKind.POWER_GRAPHIC})
# Far larger than any wire-length difference between candidate junctions.
_PIN_BLOCKED_PENALTY_MM = 1_000_000.0
_STEINER_TOKEN = "@steiner"
# Coordinates are multiples of the grid read from a file, and the planner compares sums of them with margins that are
# multiples of the grid too. Float noise that depends on where on the sheet the circuit stands must not decide a tie
# (the same opamp, moved 178 mm, had a net cut into other branches: `auto-tr2-b`), so distances are rounded and
# every boundary comparison carries this slack.
_EPS = 1e-6
# The router inflates each symbol box by half a grid and libavoid adds one more
# grid of buffer (backend/kicad/mcp_extended_server.py), so it keeps 1.5 grids away.
_ROUTER_KEEPOUT_GRIDS = 1.5


@dataclass(frozen=True)
class _Node:
    token: str
    point: Point
    reference: str | None = None
    pin: str | None = None

    def endpoint(self, suffix: str) -> dict[str, Any]:
        if self.reference is not None:
            return {f"ref{suffix}": self.reference, f"pin{suffix}": self.pin}
        return {f"point{suffix}": [self.point[0], self.point[1]]}


@dataclass(frozen=True)
class SharedNetPlan:
    net: str
    strategy: str
    junction: Point | None
    edges: tuple[dict[str, Any], ...]
    estimated_length_mm: float
    estimated_bends: int
    terminals: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "net": self.net, "strategy": self.strategy,
            "junction": list(self.junction) if self.junction is not None else None,
            "edges": list(self.edges),
            "estimated_length_mm": self.estimated_length_mm,
            "estimated_bends": self.estimated_bends,
            "terminals": list(self.terminals),
        }


def _distance(a: Point, b: Point) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _mst(nodes: tuple[_Node, ...]) -> tuple[tuple[int, int], ...]:
    """Deterministic Manhattan MST; each edge belongs to one electrical net."""
    if len(nodes) < 2:
        return ()
    edges = sorted(
        (
            (round(_distance(nodes[i].point, nodes[j].point), 6), nodes[i].token,
             nodes[j].token, i, j)
            for i, j in combinations(range(len(nodes)), 2)
        )
    )
    parent = list(range(len(nodes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    selected = []
    for _, _, _, i, j in edges:
        a, b = find(i), find(j)
        if a == b:
            continue
        parent[a] = b
        selected.append((i, j))
        if len(selected) == len(nodes)-1:
            break
    return tuple(selected)


def _symbol_keepouts(
    scene: GeometryScene, margin_mm: float,
) -> tuple[tuple[float, float, float, float], ...]:
    """One box per symbol (body plus every pin tip), grown by the router's keep-out margin."""
    boxes: dict[str, list[float]] = {}
    for item in scene.items:
        b = item.bounds
        if item.kind == GeometryKind.BODY:
            corners = ((b.left, b.top), (b.right, b.bottom))
        elif item.kind == GeometryKind.PIN_ENDPOINT:
            corners = ((b.left, b.top),)
        else:
            continue
        box = boxes.setdefault(item.owner, [b.left, b.top, b.right, b.bottom])
        for x, y in corners:
            box[0], box[1] = min(box[0], x), min(box[1], y)
            box[2], box[3] = max(box[2], x), max(box[3], y)
    return tuple(
        (left-margin_mm, top-margin_mm, right+margin_mm, bottom+margin_mm)
        for left, top, right, bottom in boxes.values()
    )


def _candidate_is_clear(
    point: Point, scene: GeometryScene,
    terminal_ids: frozenset[str], grid_mm: float,
    keepouts: tuple[tuple[float, float, float, float], ...] = (),
) -> bool:
    x, y = point
    # libavoid keeps this far from a symbol's box. A junction inside that margin
    # can only be reached by a route through the symbol, and such a route may pass
    # over the symbol's own pins and join other nets.
    for left, top, right, bottom in keepouts:
        if left + _EPS < x < right - _EPS and top + _EPS < y < bottom - _EPS:
            return False
    for item in scene.items:
        b = item.bounds
        if item.kind in _GRAPHICS:
            if (b.left-grid_mm/2-_EPS <= x <= b.right+grid_mm/2+_EPS
                    and b.top-grid_mm/2-_EPS <= y <= b.bottom+grid_mm/2+_EPS):
                return False
        elif item.kind == GeometryKind.PIN_ENDPOINT and item.id not in terminal_ids:
            if abs(b.left-x) <= grid_mm/2+_EPS and abs(b.top-y) <= grid_mm/2+_EPS:
                return False
        elif item.kind == GeometryKind.WIRE and item.segment is not None:
            (ax,ay),(bx,by) = item.segment
            if abs(ax-bx) < 1e-6:
                if abs(x-ax) < grid_mm/2-_EPS and min(ay,by)-grid_mm/2-_EPS<=y<=max(ay,by)+grid_mm/2+_EPS:
                    return False
            elif abs(ay-by) < 1e-6:
                if abs(y-ay) < grid_mm/2-_EPS and min(ax,bx)-grid_mm/2-_EPS<=x<=max(ax,bx)+grid_mm/2+_EPS:
                    return False
    return True


def _path_hits_pin(
    segments: tuple[tuple[Point, Point], ...], pins: list[Point],
) -> bool:
    """True when an axis-aligned path passes through one of the given pin tips."""
    eps = 1e-6
    for start, end in segments:
        if abs(start[0] - end[0]) < eps and abs(start[1] - end[1]) < eps:
            continue
        for px, py in pins:
            if abs(start[0]-end[0]) < eps:
                if (abs(px-start[0]) < eps
                        and min(start[1],end[1])-eps <= py <= max(start[1],end[1])+eps):
                    return True
            elif abs(start[1]-end[1]) < eps:
                if (abs(py-start[1]) < eps
                        and min(start[0],end[0])-eps <= px <= max(start[0],end[0])+eps):
                    return True
    return False


def _tree_cost(
    nodes: tuple[_Node,...], edges: tuple[tuple[int,int],...],
    scene: GeometryScene, net: NetIntent, grid_mm: float,
) -> tuple[float, float, int]:
    length = sum(_distance(nodes[i].point,nodes[j].point) for i,j in edges)
    bends = sum(
        int(abs(nodes[i].point[0]-nodes[j].point[0]) > _EPS
            and abs(nodes[i].point[1]-nodes[j].point[1]) > _EPS)
        for i,j in edges
    )
    # Penalize connections whose two orthogonal elbow options both encounter
    # component bodies or other existing electrical wires. Libavoid still
    # performs exact feasibility and clearance checks.
    obstacles = [
        item for item in scene.items if item.kind in _GRAPHICS
        or (item.kind == GeometryKind.WIRE and item.segment is not None)
    ]
    # A branch from a pin to the junction that is already axis-aligned is drawn
    # as one straight wire without routing around anything, so one laid through
    # another net's pin tip would join the two nets. Such a junction is
    # infeasible and the planner moves it. Branches with a bend are routed by
    # libavoid, which does keep clear of pins, so they are not penalised here.
    own_terminals = {f"{ep.token}:terminal" for ep in net.endpoints}
    foreign_pins = [
        (item.bounds.left, item.bounds.top) for item in scene.items
        if item.kind == GeometryKind.PIN_ENDPOINT and item.id not in own_terminals
    ]
    penalty = 0.0
    for i,j in edges:
        a,b = nodes[i].point,nodes[j].point
        if (_STEINER_TOKEN in (nodes[i].token, nodes[j].token)
                and (abs(a[0]-b[0]) < 1e-6 or abs(a[1]-b[1]) < 1e-6)
                and _path_hits_pin(((a,b),), foreign_pins)):
            penalty += _PIN_BLOCKED_PENALTY_MM
        elbows = ((a[0],b[1]),(b[0],a[1]))
        blocked_paths = 0
        for elbow in elbows:
            segments = ((a,elbow),(elbow,b))
            hits = False
            for start,end in segments:
                if abs(start[0]-end[0]) < _EPS and abs(start[1]-end[1]) < _EPS:
                    continue
                for item in obstacles:
                    bb=item.bounds
                    x1,x2=sorted((start[0],end[0]))
                    y1,y2=sorted((start[1],end[1]))
                    if (x1<=bb.right+_EPS and x2>=bb.left-_EPS
                            and y1<=bb.bottom+_EPS and y2>=bb.top-_EPS):
                        # Symbol bodies containing the pin attachment itself
                        # must be escaped; they're not grounds to disqualify
                        # every path, so just add a small soft preference.
                        hits=True
                        break
                if hits:
                    break
            if hits:
                blocked_paths+=1
        if blocked_paths==2:
            penalty+=grid_mm*6
    return (round(length+grid_mm*3*bends+penalty,6),length,bends)


def plan_shared_net(
    net: NetIntent,
    scene: GeometryScene,
    *,
    grid_mm: float = 1.27,
) -> SharedNetPlan:
    """Choose a terminal tree with optional one shared Steiner junction.

    Only use when all requested endpoints are measurable. It never invents
    pin coordinates. Reject malformed geometry rather than adding wrong wires.
    """
    if grid_mm<=0 or not isfinite(grid_mm):
        raise ValueError("Invalid KiCad grid")
    endpoints = tuple(net.endpoints)
    if len(endpoints)<3:
        raise ValueError("Shared-net tree needs at least three terminals")
    pin_positions = {
        item.id[:-len(":terminal")]: (item.bounds.left,item.bounds.top)
        for item in scene.items
        if item.kind==GeometryKind.PIN_ENDPOINT
        and item.id.endswith(":terminal")
    }
    missing = sorted(p.token for p in endpoints if p.token not in pin_positions)
    if missing:
        raise ValueError(
            "Missing measured electrical pin coordinates: "+", ".join(missing)
        )
    nodes=tuple(
        _Node(ep.token,pin_positions[ep.token],ep.reference,ep.pin)
        for ep in endpoints
    )
    terminals=frozenset(f"{ep.token}:terminal" for ep in endpoints)
    keepouts=_symbol_keepouts(scene,grid_mm*_ROUTER_KEEPOUT_GRIDS)
    best_nodes=nodes
    best_edges=_mst(nodes)
    best_score,_,_=_tree_cost(nodes,best_edges,scene,net,grid_mm)
    best_junction: Point|None=None
    # One Steiner junction is enough to improve a three-pin shared net;
    # multiple-terminal nets retain the MST option when no junction helps.
    xs=sorted({round(p.point[0],6) for p in nodes})
    ys=sorted({round(p.point[1],6) for p in nodes})
    candidates={(x,y) for x in xs for y in ys}
    # Add nearby grid-aligned alternatives for cluttered annotations/pins.
    for x,y in tuple(candidates):
        for delta in (-grid_mm,grid_mm):
            candidates.add((round(x+delta,6),y))
            candidates.add((x,round(y+delta,6)))
    for x,y in sorted(candidates):
        pt=(round(round(x/grid_mm)*grid_mm,6),
            round(round(y/grid_mm)*grid_mm,6))
        if any(_distance(pt,n.point)<grid_mm/3 for n in nodes):
            continue
        if not _candidate_is_clear(pt,scene,terminals,grid_mm,keepouts):
            continue
        augmented=(*nodes,_Node(_STEINER_TOKEN,pt))
        edges=_mst(augmented)
        degree=sum(i==len(augmented)-1 or j==len(augmented)-1 for i,j in edges)
        if degree<3:
            continue
        score,_,_=_tree_cost(augmented,edges,scene,net,grid_mm)
        if score<best_score-1e-6:
            best_score=score
            best_nodes=augmented
            best_edges=edges
            best_junction=pt

    outgoing=tuple(
        {
            "id":f"{net.name}:net_branch_{index}",
            "net":net.name,
            **best_nodes[i].endpoint("1"),
            **best_nodes[j].endpoint("2"),
        }
        for index,(i,j) in enumerate(best_edges)
    )
    _,length,bends=_tree_cost(best_nodes,best_edges,scene,net,grid_mm)
    return SharedNetPlan(
        net.name,
        "steiner_mst" if best_junction is not None else "terminal_mst",
        best_junction,outgoing,round(length,4),bends,
        tuple(ep.token for ep in endpoints),
    )
