"""Layout KPIs measured from an authored .kicad_sch, independent of the engine's own metrics.

Wires are merged (collinear overlaps counted once), connectivity and net
ownership are derived geometrically from wire/pin/junction contacts, and the
result is scored against the fixture's declared nets:

* wire excess  = routed length of a net's wire tree / half-perimeter wirelength
* crossings    = interior-interior wire crossings with no junction (legal in KiCad)
* t_contacts   = a wire end on another wire's interior (connects in KiCad)
* shorted_nets = a connected wire component holding pins of different nets
* body hits    = wire interiors inside a symbol body; foreign = not that symbol's own wire
* passives     = nearest-neighbour spacing of two-pin parts (the engine's footprint floor)
"""
from __future__ import annotations

import dataclasses
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

Point = tuple[float, float]
Segment = tuple[Point, Point]
Rect = tuple[float, float, float, float]  # left, top, right, bottom
EPS = 1e-4
JUNCTION_RADIUS = 0.3
MIN_PASSIVE_PIN_SPAN_MM = 5.0
JOG_MAX_MM = 2.54   # a segment this short between two corners is a visible one-or-two-grid offset
# The router's own geometry (backend/kicad/mcp_extended_server.py): a 0.02 mm portal shape sits
# 2 x grid beyond the pin tip (line 2559), and libavoid keeps clear of a body by the 0.5 x grid
# obstacle clearance (2505) plus shapeBufferDistance of 1 x grid (2633). SCHEMATIC_GRID_MM = 1.27.
PORTAL_OFFSET_MM = 2.54
OBSTACLE_INFLATION_MM = 0.635 + 1.27


@dataclass
class Geometry:
    wires: list[Segment] = field(default_factory=list)
    junctions: list[Point] = field(default_factory=list)
    pins: dict[str, tuple[Point, str]] = field(default_factory=dict)  # token -> (point, owner)
    bodies: list[tuple[str, Rect]] = field(default_factory=list)  # (owner, bounds)
    pin_dirs: dict[str, Point] = field(default_factory=dict)  # token -> outward unit vector


class _Union:
    def __init__(self) -> None:
        self.parent: dict[object, object] = {}

    def find(self, item: object) -> object:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: object, second: object) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[b] = a


def _norm(a: Point, b: Point) -> Segment:
    a = (round(a[0], 4), round(a[1], 4))
    b = (round(b[0], 4), round(b[1], 4))
    return (a, b) if a <= b else (b, a)


def _merge_spans(spans: list[list[float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return [(low, high) for low, high in merged]


def merge_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Union collinear overlapping or touching segments; non-orthogonal ones are rejected."""
    horizontal: dict[float, list[list[float]]] = {}
    vertical: dict[float, list[list[float]]] = {}
    for first, second in segments:
        a, b = _norm(first, second)
        if abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS:
            continue
        if abs(a[1] - b[1]) < EPS:
            horizontal.setdefault(a[1], []).append([a[0], b[0]])
        elif abs(a[0] - b[0]) < EPS:
            vertical.setdefault(a[0], []).append([a[1], b[1]])
        else:
            raise ValueError(f"non-orthogonal wire segment {a}->{b}")
    merged: list[Segment] = []
    for y, spans in horizontal.items():
        merged.extend(((low, y), (high, y)) for low, high in _merge_spans(spans))
    for x, spans in vertical.items():
        merged.extend(((x, low), (x, high)) for low, high in _merge_spans(spans))
    return sorted(merged)


def _length(segment: Segment) -> float:
    (x1, y1), (x2, y2) = segment
    return abs(x2 - x1) + abs(y2 - y1)


def _is_horizontal(segment: Segment) -> bool:
    return abs(segment[0][1] - segment[1][1]) < EPS


def _on_segment(point: Point, segment: Segment) -> bool:
    (x1, y1), (x2, y2) = segment
    px, py = point
    if _is_horizontal(segment):
        return abs(py - y1) < EPS and min(x1, x2) - EPS <= px <= max(x1, x2) + EPS
    return abs(px - x1) < EPS and min(y1, y2) - EPS <= py <= max(y1, y2) + EPS


def _contact(horizontal: Segment, vertical: Segment) -> tuple[Point, str] | None:
    (hx1, hy), (hx2, _) = horizontal
    (vx, vy1), (_, vy2) = vertical
    if not (hx1 - EPS <= vx <= hx2 + EPS and vy1 - EPS <= hy <= vy2 + EPS):
        return None
    at_h_end = abs(vx - hx1) < EPS or abs(vx - hx2) < EPS
    at_v_end = abs(hy - vy1) < EPS or abs(hy - vy2) < EPS
    kind = "corner" if at_h_end and at_v_end else "tee" if at_h_end or at_v_end else "cross"
    return (vx, hy), kind


def _strictly_inside(segment: Segment, rect: Rect) -> bool:
    (x1, y1), (x2, y2) = segment
    left, top, right, bottom = rect
    if _is_horizontal(segment):
        return top + EPS < y1 < bottom - EPS and min(x2, right) - max(x1, left) > EPS
    return left + EPS < x1 < right - EPS and min(y2, bottom) - max(y1, top) > EPS


def _round(value: float, places: int = 3) -> float:
    return round(float(value), places)


def _stats(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return _round(min(values)), _round(statistics.median(values))


def compute(geometry: Geometry, nets: dict[str, list[str]]) -> dict[str, Any]:
    """KPIs for one authored schematic; `nets` maps net name -> pin tokens ("REF.PIN")."""
    segments = merge_segments(geometry.wires)
    raw_length = sum(_length(segment) for segment in map(lambda s: _norm(*s), geometry.wires))
    total_length = sum(_length(segment) for segment in segments)

    union = _Union()
    for index in range(len(segments)):
        union.find(("s", index))
    crossings: list[tuple[int, int, Point]] = []
    t_contacts = 0
    horizontals = [i for i, s in enumerate(segments) if _is_horizontal(s)]
    verticals = [i for i, s in enumerate(segments) if not _is_horizontal(s)]
    for i in horizontals:
        for j in verticals:
            found = _contact(segments[i], segments[j])
            if found is None:
                continue
            point, kind = found
            junction = any(math.dist(point, j_) <= JUNCTION_RADIUS for j_ in geometry.junctions)
            if kind == "cross" and not junction:
                crossings.append((i, j, point))
                continue
            if kind == "tee":
                t_contacts += 1
            union.union(("s", i), ("s", j))

    attached: set[str] = set()
    for token, (point, _owner) in geometry.pins.items():
        union.find(("p", token))
        for index, segment in enumerate(segments):
            if _on_segment(point, segment):
                union.union(("p", token), ("s", index))
                attached.add(token)

    components: dict[object, dict[str, list]] = {}
    for index in range(len(segments)):
        components.setdefault(union.find(("s", index)), {"segments": [], "pins": []})["segments"].append(index)
    for token in attached:
        components.setdefault(union.find(("p", token)), {"segments": [], "pins": []})["pins"].append(token)

    endpoints: dict[Point, list[str]] = {}
    for segment in segments:
        axis = "h" if _is_horizontal(segment) else "v"
        for end in segment:
            endpoints.setdefault(end, []).append(axis)
    corner_points = {
        point for point, axes in endpoints.items()
        if len(axes) == 2 and len(set(axes)) == 2
        and not any(_on_segment(point, s) and point not in s for s in segments)
    }

    # A jog is a short segment with a corner at both ends, so its two neighbours run parallel to each
    # other and the wire shifts sideways by a grid step or two: the "strange offset".
    jogs = sum(
        1 for segment in segments
        if _length(segment) <= JOG_MAX_MM + EPS and segment[0] in corner_points and segment[1] in corner_points
    )

    net_of = {token: name for name, tokens in nets.items() for token in tokens}
    shorted = sorted({
        tuple(sorted({net_of[t] for t in comp["pins"] if t in net_of}))
        for comp in components.values()
        if len({net_of[t] for t in comp["pins"] if t in net_of}) > 1
    })

    per_net: list[dict[str, Any]] = []
    for name, tokens in nets.items():
        located = [t for t in tokens if t in geometry.pins]
        if len(located) < 2:
            continue
        xs = [geometry.pins[t][0][0] for t in located]
        ys = [geometry.pins[t][0][1] for t in located]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        roots = {t: union.find(("p", t)) for t in located if t in attached}
        if not roots:
            mode = "unrouted"
        elif len(roots) == len(located) and len(set(roots.values())) == 1:
            mode = "wired"
        elif len(roots) == len(located) and len(set(roots.values())) == len(roots):
            mode = "symbolic"
        else:
            mode = "partial"
        entry: dict[str, Any] = {"net": name, "mode": mode, "pins": len(located), "hpwl_mm": _round(hpwl)}
        if mode == "wired":
            comp = components[next(iter(roots.values()))]
            length = sum(_length(segments[i]) for i in comp["segments"])
            ends = {end for i in comp["segments"] for end in segments[i]}
            entry.update(
                length_mm=_round(length),
                excess=_round(length / hpwl, 2) if hpwl > EPS else None,
                corners=len(corner_points & ends),
            )
        per_net.append(entry)

    wired = [n for n in per_net if n["mode"] == "wired" and n["hpwl_mm"] > EPS]
    wired_length = sum(n["length_mm"] for n in wired)
    wired_hpwl = sum(n["hpwl_mm"] for n in wired)

    # A stub is a wire component touching exactly one declared pin: a power or label attachment.
    stub_lengths = [
        sum(_length(segments[i]) for i in comp["segments"])
        for comp in components.values()
        if comp["segments"] and sum(token in net_of for token in comp["pins"]) == 1
    ]

    owners_of_component = {
        root: {geometry.pins[t][1] for t in comp["pins"]} for root, comp in components.items()
    }
    body_hits = foreign_hits = 0
    for root, comp in components.items():
        for index in comp["segments"]:
            for owner, rect in geometry.bodies:
                if _strictly_inside(segments[index], rect):
                    body_hits += 1
                    if owner not in owners_of_component[root]:
                        foreign_hits += 1

    overlaps = 0
    for a in range(len(geometry.bodies)):
        for b in range(a + 1, len(geometry.bodies)):
            (owner_a, ra), (owner_b, rb) = geometry.bodies[a], geometry.bodies[b]
            if owner_a != owner_b and (
                min(ra[2], rb[2]) - max(ra[0], rb[0]) > EPS and min(ra[3], rb[3]) - max(ra[1], rb[1]) > EPS
            ):
                overlaps += 1

    pins_by_owner: dict[str, list[Point]] = {}
    for _token, (point, owner) in geometry.pins.items():
        pins_by_owner.setdefault(owner, []).append(point)
    boxes = dict(geometry.bodies)
    passives = {
        owner: boxes[owner] for owner, points in pins_by_owner.items()
        if owner in boxes and not owner.startswith("#") and len(points) == 2
        and abs(points[0][0] - points[1][0]) + abs(points[0][1] - points[1][1]) >= MIN_PASSIVE_PIN_SPAN_MM
    }
    nearest_center: list[float] = []
    nearest_gap: list[float] = []
    for owner, (l1, t1, r1, b1) in passives.items():
        centers, gaps = [], []
        for other, (l2, t2, r2, b2) in passives.items():
            if other == owner:
                continue
            dx = abs((l1 + r1) / 2 - (l2 + r2) / 2)
            dy = abs((t1 + b1) / 2 - (t2 + b2) / 2)
            centers.append(math.hypot(dx, dy))
            gaps.append(max(0.0, max(dx - ((r1 - l1) + (r2 - l2)) / 2, dy - ((b1 - t1) + (b2 - t2)) / 2)))
        if centers:
            nearest_center.append(min(centers))
            nearest_gap.append(min(gaps))
    center_min, center_median = _stats(nearest_center)
    gap_min, gap_median = _stats(nearest_gap)

    xs_all = [x for s in segments for x, _ in s] + [v for _, r in geometry.bodies for v in (r[0], r[2])]
    ys_all = [y for s in segments for _, y in s] + [v for _, r in geometry.bodies for v in (r[1], r[3])]
    return {
        "wire_length_mm": _round(total_length),
        "wire_length_raw_mm": _round(raw_length),
        "duplicate_wire_mm": _round(raw_length - total_length),
        "segments": len(segments),
        "corners": len(corner_points),
        "jogs": jogs,
        "nets": {mode: sum(n["mode"] == mode for n in per_net) for mode in ("wired", "symbolic", "partial", "unrouted")},
        "wired_length_mm": _round(wired_length),
        "wired_hpwl_mm": _round(wired_hpwl),
        "excess_ratio": _round(wired_length / wired_hpwl, 2) if wired_hpwl > EPS else None,
        "stubs": {
            "count": len(stub_lengths),
            "length_mm": _round(sum(stub_lengths)),
            "per_pin_mm": _round(sum(stub_lengths) / len(stub_lengths), 2) if stub_lengths else None,
            "max_mm": _round(max(stub_lengths)) if stub_lengths else None,
        },
        "crossings": len(crossings),
        "crossings_between_components": sum(
            union.find(("s", i)) != union.find(("s", j)) for i, j, _ in crossings
        ),
        "t_contacts": t_contacts,
        "shorted_nets": [list(group) for group in shorted],
        "body_hits": body_hits,
        "body_hits_foreign": foreign_hits,
        "body_overlaps": overlaps,
        "passives": {
            "count": len(passives),
            "nn_center_min_mm": center_min,
            "nn_center_median_mm": center_median,
            "nn_gap_min_mm": gap_min,
            "nn_gap_median_mm": gap_median,
        },
        "router_clearance": router_clearance(geometry),
        "extent_width_mm": _round(max(xs_all) - min(xs_all)) if xs_all else 0.0,
        "extent_height_mm": _round(max(ys_all) - min(ys_all)) if ys_all else 0.0,
        "per_net": per_net,
    }


def outward_direction(tip: Point, stroke: Rect) -> Point | None:
    """Outward unit vector of a pin from its stroke (tip is one end of the stroke's bounds)."""
    left, top, right, bottom = stroke
    if (right - left) >= (bottom - top):
        if abs(right - left) < EPS:
            return None
        return (-1.0, 0.0) if abs(tip[0] - left) < abs(tip[0] - right) else (1.0, 0.0)
    return (0.0, -1.0) if abs(tip[1] - top) < abs(tip[1] - bottom) else (0.0, 1.0)


def router_clearance(geometry: Geometry) -> dict[str, Any]:
    """Pins whose router portal starts inside a neighbouring body's keep-out.

    libavoid cannot leave a portal that sits inside another obstacle's inflated rectangle, so such a
    pin is unroutable by design, however good the placement otherwise is. A pin needs
    PORTAL_OFFSET + OBSTACLE_INFLATION = 4.445 mm between its tip and any foreign body it points at.
    """
    trapped: list[str] = []
    gaps: list[float] = []
    for token, (tip, owner) in geometry.pins.items():
        direction = geometry.pin_dirs.get(token)
        if direction is None:
            continue
        portal = (tip[0] + direction[0] * PORTAL_OFFSET_MM, tip[1] + direction[1] * PORTAL_OFFSET_MM)
        nearest: float | None = None
        for body_owner, (left, top, right, bottom) in geometry.bodies:
            if body_owner == owner:
                continue
            if (left - OBSTACLE_INFLATION_MM + EPS < portal[0] < right + OBSTACLE_INFLATION_MM - EPS
                    and top - OBSTACLE_INFLATION_MM + EPS < portal[1] < bottom + OBSTACLE_INFLATION_MM - EPS):
                trapped.append(token)
            # free run from the tip to the body edge, if the pin points straight at that body
            if direction[0]:
                inside_lane = top - OBSTACLE_INFLATION_MM < tip[1] < bottom + OBSTACLE_INFLATION_MM
                edge = (left if direction[0] > 0 else right)
                run = (edge - tip[0]) * direction[0]
            else:
                inside_lane = left - OBSTACLE_INFLATION_MM < tip[0] < right + OBSTACLE_INFLATION_MM
                edge = (top if direction[1] > 0 else bottom)
                run = (edge - tip[1]) * direction[1]
            if inside_lane and run >= 0 and (nearest is None or run < nearest):
                nearest = run
        if nearest is not None:
            gaps.append(nearest)
    trapped = sorted(set(trapped))
    return {
        "pins_with_direction": sum(1 for t in geometry.pins if t in geometry.pin_dirs),
        "trapped": len(trapped),
        "trapped_pins": trapped,
        "min_outward_run_mm": _round(min(gaps)) if gaps else None,
        "needed_run_mm": _round(PORTAL_OFFSET_MM + OBSTACLE_INFLATION_MM),
    }


def _assign_pin_directions(geometry: Geometry, strokes: dict[str, Rect]) -> None:
    for token, stroke in strokes.items():
        if token in geometry.pins:
            direction = outward_direction(geometry.pins[token][0], stroke)
            if direction is not None:
                geometry.pin_dirs[token] = direction


def geometry_from_scene(scene: Any) -> Geometry:
    """Adapt the repo's GeometryScene (backend.schematic.presentation_geometry)."""
    geometry = Geometry()
    strokes: dict[str, Rect] = {}
    for item in scene.items:
        kind = getattr(item.kind, "value", str(item.kind))
        bounds = item.bounds
        if kind == "wire" and item.segment is not None:
            geometry.wires.append(item.segment)
        elif kind == "junction":
            geometry.junctions.append(((bounds.left + bounds.right) / 2, (bounds.top + bounds.bottom) / 2))
        elif kind == "pin_endpoint" and item.id.endswith(":terminal"):
            geometry.pins[item.id[: -len(":terminal")]] = ((bounds.left, bounds.top), item.owner)
        elif kind in ("body", "power_graphic"):
            # A power symbol is a symbol: one drawn over another symbol, or a wire through it, is a defect.
            geometry.bodies.append((item.owner, (bounds.left, bounds.top, bounds.right, bounds.bottom)))
        elif kind == "pin_stroke" and item.id.endswith(":stroke"):
            strokes[item.id[: -len(":stroke")]] = (bounds.left, bounds.top, bounds.right, bounds.bottom)
    _assign_pin_directions(geometry, strokes)
    return geometry


def geometry_from_scene_dict(scene: dict) -> Geometry:
    """Adapt a saved scene (the engine's *-presentation-scene.json) without importing the repo."""
    geometry = Geometry()
    strokes: dict[str, Rect] = {}
    for item in scene["items"]:
        kind, bounds = item["kind"], item["bounds"]
        if kind == "wire" and item.get("segment"):
            geometry.wires.append(tuple(map(tuple, item["segment"])))
        elif kind == "pin_endpoint" and item["id"].endswith(":terminal"):
            geometry.pins[item["id"][: -len(":terminal")]] = ((bounds["left"], bounds["top"]), item["owner"])
        elif kind in ("body", "power_graphic"):
            geometry.bodies.append((item["owner"], (bounds["left"], bounds["top"], bounds["right"], bounds["bottom"])))
        elif kind == "pin_stroke" and item["id"].endswith(":stroke"):
            strokes[item["id"][: -len(":stroke")]] = (bounds["left"], bounds["top"], bounds["right"], bounds["bottom"])
    _assign_pin_directions(geometry, strokes)
    return geometry


def placement_kpis(geometry: Geometry, nets: dict[str, list[str]], signal_nets: set[str]) -> dict[str, Any]:
    """Wire-independent placement quality, from pin and body positions alone.

    Usable on a pre-route scene, so it still works when routing later fails.
    """
    base = compute(Geometry(pins=geometry.pins, bodies=geometry.bodies, pin_dirs=geometry.pin_dirs), nets)

    def hpwl(names: Iterable[str]) -> float:
        total = 0.0
        for name in names:
            located = [geometry.pins[t][0] for t in nets[name] if t in geometry.pins]
            if len(located) >= 2:
                total += (max(p[0] for p in located) - min(p[0] for p in located)) + (
                    max(p[1] for p in located) - min(p[1] for p in located)
                )
        return _round(total)

    return {
        "hpwl_signal_mm": hpwl(n for n in nets if n in signal_nets),
        "hpwl_all_mm": hpwl(nets),
        "passives": base["passives"],
        "body_overlaps": base["body_overlaps"],
        "router_clearance": base["router_clearance"],
        "extent_width_mm": base["extent_width_mm"],
        "extent_height_mm": base["extent_height_mm"],
    }


def load_intent(fixture: str, src: Path):
    """The declared nets of a fixture, imported from the engine tree at `src`."""
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from scripts import schematic_validation_fixtures as catalogue
    from scripts.schematic_validation_fixtures import FIXTURES, build_fixture

    # runs made before the relation-free circuits (and the demo circuits) existed have a catalogue without them
    inference_fixtures = {
        **getattr(catalogue, "INFERENCE_FIXTURES", {}), **getattr(catalogue, "DEMO_FIXTURES", {}), **getattr(catalogue, "CORPUS_FIXTURES", {}),
    }

    # "<name>_bare" is the same circuit with none of its relations declared
    bare = fixture.endswith("_bare")
    base = fixture[: -len("_bare")] if bare else fixture
    if base in FIXTURES or base in inference_fixtures:
        intent = build_fixture(base)
    else:
        raise ValueError(f"unknown fixture {fixture!r}")
    return dataclasses.replace(intent, relations=()) if bare else intent


def nets_of(intent: Any) -> dict[str, list[str]]:
    return {net.name: [endpoint.token for endpoint in net.endpoints] for net in intent.all_nets}


def kpis_for_schematic(schematic: Path, fixture: str, src: Path) -> dict[str, Any]:
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from backend.schematic.kicad_geometry import measured_scene_from_kicad

    scene, _coverage = measured_scene_from_kicad(schematic)
    result = compute(geometry_from_scene(scene), nets_of(load_intent(fixture, src)))
    result["fixture"] = fixture
    return result


def _clearance_text(clearance: dict[str, Any] | None) -> str:
    if not clearance:
        return "n/a"
    shortest = clearance["min_outward_run_mm"]
    return (f"{clearance['trapped']} of {clearance['pins_with_direction']} pins "
            f"(shortest free run {shortest} mm, router needs {clearance['needed_run_mm']} mm)")


def format_kpis(kpis: dict[str, Any]) -> str:
    passives = kpis["passives"]
    nets = kpis["nets"]
    rows = [
        ("wire length (merged / raw)", f"{kpis['wire_length_mm']} / {kpis['wire_length_raw_mm']} mm"),
        ("wire excess (wired nets)", f"{kpis['excess_ratio']}  ({kpis['wired_length_mm']} / HPWL {kpis['wired_hpwl_mm']} mm)"),
        ("nets wired/symbolic/partial/unrouted", f"{nets['wired']}/{nets['symbolic']}/{nets['partial']}/{nets['unrouted']}"),
        ("power/label stubs: count, total, per pin", f"{kpis['stubs']['count']}, {kpis['stubs']['length_mm']} mm, {kpis['stubs']['per_pin_mm']} mm (max {kpis['stubs']['max_mm']})"),
        ("corners / jogs (<= 2.54 mm offsets)", f"{kpis['corners']} / {kpis.get('jogs', 'n/a')}"),
        ("crossings (between components)", f"{kpis['crossings']} ({kpis['crossings_between_components']})"),
        ("t-contacts", str(kpis["t_contacts"])),
        ("shorted nets", str(kpis["shorted_nets"] or "none")),
        ("wire/body hits (foreign)", f"{kpis['body_hits']} ({kpis['body_hits_foreign']})"),
        ("body overlaps", str(kpis["body_overlaps"])),
        ("portals trapped in a neighbour's keep-out", _clearance_text(kpis.get("router_clearance"))),
        ("passives: count / nn centre min,median", f"{passives['count']} / {passives['nn_center_min_mm']}, {passives['nn_center_median_mm']} mm"),
        ("passives: nn gap min,median", f"{passives['nn_gap_min_mm']}, {passives['nn_gap_median_mm']} mm"),
        ("extent (w x h)", f"{kpis['extent_width_mm']} x {kpis['extent_height_mm']} mm"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)
