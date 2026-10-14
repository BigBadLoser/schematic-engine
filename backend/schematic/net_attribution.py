"""Conservative electrical-net attribution for measured KiCad wire geometry.

The exported KiCad XML netlist is authoritative for net membership; geometric
connectivity is used only to determine which saved wire segments can safely
inherit that identity. Orthogonal crossings without a junction do not join.
Unknown/disputed wire islands retain net=None and remain hard obstacles.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from xml.etree import ElementTree

from backend.kicad.netlist import parse_kicad_xml
from .presentation_geometry import GeometryItem, GeometryKind, GeometryScene

Point = tuple[float, float]
_EPS = 1e-4


def _equal(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS


def _contains(segment: tuple[Point, Point], point: Point) -> bool:
    a, b = segment
    return (
        abs(a[0] - b[0]) < _EPS
        and abs(point[0] - a[0]) < _EPS
        and min(a[1], b[1]) - _EPS <= point[1] <= max(a[1], b[1]) + _EPS
    ) or (
        abs(a[1] - b[1]) < _EPS
        and abs(point[1] - a[1]) < _EPS
        and min(a[0], b[0]) - _EPS <= point[0] <= max(a[0], b[0]) + _EPS
    )


def _collinear_overlap(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    if abs(a[0][0] - a[1][0]) < _EPS and abs(b[0][0] - b[1][0]) < _EPS:
        return abs(a[0][0] - b[0][0]) < _EPS and (
            max(min(a[0][1], a[1][1]), min(b[0][1], b[1][1]))
            <= min(max(a[0][1], a[1][1]), max(b[0][1], b[1][1])) + _EPS
        )
    if abs(a[0][1] - a[1][1]) < _EPS and abs(b[0][1] - b[1][1]) < _EPS:
        return abs(a[0][1] - b[0][1]) < _EPS and (
            max(min(a[0][0], a[1][0]), min(b[0][0], b[1][0]))
            <= min(max(a[0][0], a[1][0]), max(b[0][0], b[1][0])) + _EPS
        )
    return False


def _terminal(point: Point, segment: tuple[Point, Point]) -> bool:
    return _equal(point, segment[0]) or _equal(point, segment[1])


def _electrical_codes(xml: str) -> tuple[dict[str, str], dict[str, str]]:
    parsed = parse_kicad_xml(xml)
    nodes = {
        f"{ref}.{pin}": code
        for code, group in parsed.nets.items()
        for ref, pin in group
    }
    root = ElementTree.fromstring(xml)
    names = {}
    for entry in root.findall("nets/net"):
        code = entry.get("code", "")
        name = entry.get("name", "")
        if code and name:
            names[code] = name
    return nodes, names


@dataclass(frozen=True)
class AttributionResult:
    scene: GeometryScene
    coverage: dict[str, object]


def attribute_wire_nets(scene: GeometryScene, *, netlist_xml: str) -> AttributionResult:
    """Annotate saved conductor geometry from exported KiCad XML evidence.

    Only direct endpoint/collinear joins, explicit KiCad junctions, and
    explicitly attached pin terminals can establish a conductor component.
    T contacts without a junction remain unmerged unless endpoints coincide.
    Conflicting netlist seeds invalidate the *entire* geometric component.
    This is intentionally conservative and read-only.
    """
    codes, names = _electrical_codes(netlist_xml)
    wires = [item for item in scene.items
             if item.kind == GeometryKind.WIRE and item.segment is not None]
    junctions = [item for item in scene.items if item.kind == GeometryKind.JUNCTION]
    pins = [item for item in scene.items if item.kind == GeometryKind.PIN_ENDPOINT]
    parent = list(range(len(wires)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        x, y = find(a), find(b)
        if x != y:
            parent[max(x, y)] = min(x, y)

    def junction_point(item: GeometryItem) -> Point:
        return ((item.bounds.left + item.bounds.right) / 2,
                (item.bounds.top + item.bounds.bottom) / 2)

    # Explicitly joined wires, never merely crossing different wire nets.
    for i, wire in enumerate(wires):
        for j in range(i):
            other = wires[j]
            if _collinear_overlap(wire.segment, other.segment):
                union(i, j)
                continue
            if any(_equal(a, b) for a in wire.segment for b in other.segment):
                union(i, j)
                continue
            if any(_contains(wire.segment, junction_point(dot))
                   and _contains(other.segment, junction_point(dot))
                   for dot in junctions):
                union(i, j)

    memberships: dict[int, set[str]] = {}
    wire_pin_ids: dict[int, set[str]] = {}
    wire_junction_ids: dict[int, set[str]] = {}
    for i, wire in enumerate(wires):
        key = find(i)
        memberships.setdefault(key, set())
        wire_pin_ids.setdefault(i, set())
        wire_junction_ids.setdefault(i, set())
        for pin in pins:
            token = pin.id.removesuffix(":terminal")
            point = (pin.bounds.left, pin.bounds.top)
            if (_terminal(point, wire.segment)
                    or (any(_equal(point, junction_point(dot)) for dot in junctions)
                        and _contains(wire.segment, point))):
                if token in codes:
                    memberships[key].add(codes[token])
                    wire_pin_ids[i].add(pin.id)
        for dot in junctions:
            if _contains(wire.segment, junction_point(dot)):
                wire_junction_ids[i].add(dot.id)

    # No label-bbox inference: text bounds are not the electrical anchor.
    # Unseeded wire islands stay unknown, even if they visually cross labels.

    for i, wire in enumerate(wires):
        parent_key = find(i)
        # Each sub-segment inherits all seeds from its connected component.
        memberships.setdefault(parent_key, set())

    # Prefer human-readable KiCad names when unique; codes remain the
    # collision-free identity for any names repeated in malformed XML exports.
    name_counts: dict[str, int] = {}
    for name in names.values():
        name_counts[name] = name_counts.get(name, 0) + 1

    def identity(code: str) -> str:
        name = names.get(code)
        return name if name and name_counts[name] == 1 else f"code:{code}"

    assigned: dict[int, str] = {
        key: next(iter(values))
        for key, values in memberships.items()
        if len(values) == 1
    }
    items: list[GeometryItem] = []
    changed: set[str] = set()
    wires_by_id = {wire.id: i for i, wire in enumerate(wires)}
    for item in scene.items:
        if item.kind == GeometryKind.WIRE and item.id in wires_by_id:
            key = find(wires_by_id[item.id])
            code = assigned.get(key)
            if code is not None:
                items.append(replace(
                    item, net=identity(code),
                    attachments=frozenset(
                        (*wire_pin_ids.get(wires_by_id[item.id], ()),
                         *wire_junction_ids.get(wires_by_id[item.id], ()))
                    ),
                ))
                changed.add(item.id)
            else:
                items.append(item)
        elif item.kind == GeometryKind.PIN_ENDPOINT:
            code = codes.get(item.id.removesuffix(":terminal"))
            items.append(replace(item, net=names.get(code, f"code:{code}")) if code else item)
        elif item.kind == GeometryKind.JUNCTION:
            containing = {
                assigned.get(find(i))
                for i, wire in enumerate(wires)
                if _contains(wire.segment, junction_point(item))
            }
            containing.discard(None)
            if len(containing) == 1:
                code = next(iter(containing))
                items.append(replace(item, net=names.get(code, f"code:{code}")))
            else:
                items.append(item)
        else:
            items.append(item)

    # Only attach junction/pin items when the target's exported net matches.
    target = {item.id: item for item in items}
    cleaned = []
    for item in items:
        if item.kind == GeometryKind.WIRE and item.net:
            item = replace(item, attachments=frozenset(
                x for x in item.attachments
                if target.get(x) is not None and target[x].net == item.net
            ))
        cleaned.append(item)

    conflicted = sum(len(values) > 1 for values in memberships.values())
    unknown = len(wires) - len(changed)
    return AttributionResult(
        GeometryScene(tuple(cleaned), clearance_mm=scene.clearance_mm),
        {
            "wire_net_attribution": (
                "verified_xml_partial" if unknown else "verified_xml_complete"
            ),
            "source": "kicad_cli_export_xml",
            "wire_segments": len(wires),
            "attributed_segments": len(changed),
            "unattributed_segments": unknown,
            "disputed_wire_islands": conflicted,
            "unattributed_wires_remain_obstacles": True,
            "crossings_without_junction_are_disconnected": True,
        },
    )
