"""How a schematic reads to an engineer, measured from the geometry scene.

Wire length, corners and area say how compact a drawing is, not whether it reads well. An engineer glancing at a sheet
notices text lying on a wire, a support part drawn far from the part it serves, wires crossing, labels hung far from
their pins, long detours round a symbol and tiny jogs. Each of those is counted here, with the places they are, so a
layout can be judged and repaired by what a reader would see.

The measures are circuit-independent: they read the scene (symbols, pins, wires, labels, power symbols) and optionally
the declared nets. Without declared nets, the conductors are found from the wires (``wire_nets``) and joined by label and
power-symbol names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Mapping, Sequence

from .designators import is_source
from .presentation_geometry import Bounds, GeometryKind, GeometryScene
from .wire_nets import WireNetwork, wire_crossings, wire_network

# A support part (a bypass or load capacitor, a pull-up, a series resistor) belongs next to the part it serves. Within
# SUPPORT_NEAR_MM nothing is said; beyond SUPPORT_FAR_MM, by what the part does (a bypass between two rails, a shunt from a
# signal to a rail, a part in series between two signals), it is a failure however short the wire or small the sheet.
# Human-drawn sheets are looser than this (KiCad's demo projects put a bypass capacitor a median 30 mm from the nearest
# supply pin of the part it bypasses, FINDINGS section 13); these are the limits the project holds its own sheets to.
# Connectors and voltage or current sources are not support parts: they are where the circuit meets the outside, and
# people draw them apart from it.
SUPPORT_NEAR_MM = 15.24
SUPPORT_FAR_MM: dict[str, float] = {"decoupling": 38.1, "shunt": 38.1, "series": 50.8}
# A label at the end of a stub longer than this is hung away from its pin.
LABEL_STUB_FAR_MM = 15.0
# A net routed with at least this much wire over its minimum, and at least this ratio, makes a detour.
DETOUR_MIN_MM = 15.0
DETOUR_RATIO = 1.8
JOG_MM = 2.54
_GROUND = re.compile(r"^(?:[ADPSEC]?GND\w*|VSS\w*|0V|GROUND\w*|EARTH\w*)$", re.IGNORECASE)
_TEXT_KINDS = (
    GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT, GeometryKind.NET_LABEL,
    GeometryKind.PIN_NAME, GeometryKind.PIN_NUMBER,
)


@dataclass(frozen=True)
class Issue:
    kind: str
    ids: tuple[str, ...]
    where: Bounds | None
    detail: str
    magnitude: float = 0.0


@dataclass(frozen=True)
class SupportPart:
    owner: str
    kind: str                  # decoupling, shunt or series
    distance_mm: float
    host: str


@dataclass(frozen=True)
class ReadabilityReport:
    measures: dict[str, float]
    issues: tuple[Issue, ...]
    support_parts: tuple[SupportPart, ...] = ()
    support_distances_mm: tuple[float, ...] = ()
    label_stubs_mm: tuple[float, ...] = ()
    net_excess: tuple[tuple[tuple[str, ...], float, float], ...] = field(default_factory=tuple)

    def count(self, kind: str) -> int:
        return sum(1 for issue in self.issues if issue.kind == kind)

    def of(self, kind: str) -> list[Issue]:
        return [issue for issue in self.issues if issue.kind == kind]


def is_ground_name(name: str) -> bool:
    return bool(_GROUND.match(name.strip().lstrip("/")))


def _shrunk(box: Bounds, margin: float) -> Bounds:
    margin = min(margin, (box.right - box.left) / 2 - 1e-6, (box.bottom - box.top) / 2 - 1e-6)
    margin = max(margin, 0.0)
    return Bounds(box.left + margin, box.top + margin, box.right - margin, box.bottom - margin)


def _segment_hits(segment: tuple[tuple[float, float], tuple[float, float]], box: Bounds) -> bool:
    (ax, ay), (bx, by) = segment
    return (
        max(ax, bx) > box.left and min(ax, bx) < box.right
        and max(ay, by) > box.top and min(ay, by) < box.bottom
    )


def _on_segment(point: tuple[float, float], segment: tuple[tuple[float, float], tuple[float, float]]) -> bool:
    (ax, ay), (bx, by) = segment
    x, y = point
    if abs(ax - bx) < 1e-3:
        return abs(x - ax) < 1e-3 and min(ay, by) - 1e-3 <= y <= max(ay, by) + 1e-3
    return abs(y - ay) < 1e-3 and min(ax, bx) - 1e-3 <= x <= max(ax, bx) + 1e-3


def _manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _drawn_vertically(text) -> bool:
    """A text turned a quarter: by its angle when the scene has one, else by the shape of its box."""
    if text.angle is not None:
        return int(round(text.angle)) % 180 == 90
    return (text.bounds.bottom - text.bounds.top) > 1.5 * (text.bounds.right - text.bounds.left)


# A wire has to run this far across the inside of a symbol's drawn body to count as through it: a pin tip may sit a
# little inside the box of its symbol's graphic (an LED's does), and the wire leaving it is not through anything.
WIRE_THROUGH_BODY_MIN_MM = 1.0
_BODY_INSET_MM = 0.1


def _runs_across_body(segment: tuple[tuple[float, float], tuple[float, float]], box: Bounds) -> bool:
    (ax, ay), (bx, by) = segment
    left, top, right, bottom = box.left + _BODY_INSET_MM, box.top + _BODY_INSET_MM, box.right - _BODY_INSET_MM, box.bottom - _BODY_INSET_MM
    if right <= left or bottom <= top:
        return False
    if abs(ax - bx) < 1e-6:
        return left < ax < right and min(max(ay, by), bottom) - max(min(ay, by), top) > WIRE_THROUGH_BODY_MIN_MM
    return top < ay < bottom and min(max(ax, bx), right) - max(min(ax, bx), left) > WIRE_THROUGH_BODY_MIN_MM


def _corners(segments: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> int:
    """Where two wires of one conductor meet at a right angle, end to end: a bend in a wire, not a junction."""
    ends: dict[tuple[float, float], list[bool]] = {}
    for start, end in segments:
        vertical = abs(start[0] - end[0]) < 1e-6
        for point in (start, end):
            ends.setdefault((round(point[0], 3), round(point[1], 3)), []).append(vertical)
    return sum(1 for flags in ends.values() if len(flags) == 2 and flags[0] != flags[1])


def rotated_label_owners(scene: GeometryScene) -> frozenset[str]:
    """The symbols whose pins have a vertical net label on their stub.

    A label that stands on end had no room to lie down beside its pin, so these are the symbols a retry should give
    more room. The label sits on the stub of its own pin, so the conductor it is on names them.
    """
    network = wire_network(scene)
    wires = [item for item in scene.items if item.kind == GeometryKind.WIRE and item.segment is not None]
    owners: set[str] = set()
    for label in scene.items:
        if label.kind != GeometryKind.NET_LABEL or label.anchor is None or not _drawn_vertically(label):
            continue
        for wire in wires:
            if _on_segment(label.anchor, wire.segment):  # type: ignore[arg-type]
                component = network.components[network.by_segment[wire.id]]
                owners.update(token.rsplit(".", 1)[0] for token in component.pins)
                break
    return frozenset(owners)


def measure_readability(
    scene: GeometryScene,
    *,
    nets: Mapping[str, Sequence[str]] | None = None,
    rail_nets: Iterable[str] = (),
    ground_nets: Iterable[str] = (),
    support_far_mm: Mapping[str, float] | float | None = None,
) -> ReadabilityReport:
    """Count what a reader notices. ``nets`` maps a declared net to its pins ("REF.PIN"); without it the nets are read
    from the wires. ``rail_nets`` and ``ground_nets`` name the supply and ground nets among them."""
    far_limit = (
        dict(SUPPORT_FAR_MM) if support_far_mm is None
        else {kind: float(support_far_mm) for kind in SUPPORT_FAR_MM} if isinstance(support_far_mm, (int, float))
        else {**SUPPORT_FAR_MM, **dict(support_far_mm)}
    )
    issues: list[Issue] = []
    items = list(scene.items)
    network = wire_network(scene)
    wires = {item.id: item for item in items if item.kind == GeometryKind.WIRE and item.segment is not None}
    texts = [item for item in items if item.kind in _TEXT_KINDS]

    # --- text that something runs through -------------------------------------------------------------------------
    own_wire: dict[str, set[str]] = {}
    for label in (item for item in texts if item.kind == GeometryKind.NET_LABEL and item.anchor is not None):
        own_wire[label.id] = {
            wire_id for wire_id, wire in wires.items() if _on_segment(label.anchor, wire.segment)  # type: ignore[arg-type]
        }
    text_on_wire = 0
    field_on_wire = 0
    for text in texts:
        box = _shrunk(text.bounds, 0.15)
        for wire_id, wire in wires.items():
            if wire_id in own_wire.get(text.id, ()):
                continue
            if _segment_hits(wire.segment, box):  # type: ignore[arg-type]
                text_on_wire += 1
                if text.kind in (GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT):
                    field_on_wire += 1
                issues.append(Issue(
                    "text_on_wire", (text.id, wire_id), text.bounds,
                    f"{text.text or text.id} has a wire through it",
                ))
                break

    # --- text drawn vertically --------------------------------------------------------------------------------------
    # Engineers draw labels horizontally (none of KiCad's 91 measured demo sheets turns one); a symbol's own
    # reference and value are turned often (KiCad's default for a resistor), so those are counted but not held against it.
    rotated = [
        text for text in texts
        if text.kind in (GeometryKind.NET_LABEL, GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT)
        and _drawn_vertically(text)
    ]
    rotated_labels = [text for text in rotated if text.kind == GeometryKind.NET_LABEL]
    for text in rotated_labels:
        issues.append(Issue("rotated_label", (text.id,), text.bounds, f"label {text.text or text.id} is drawn vertically"))

    # --- wires across a symbol -----------------------------------------------------------------------------------------
    # A wire through the inside of a symbol reads as part of it (a connector's own pin wire drawn back across the body).
    through_body = 0
    for body_item in (item for item in items if item.kind == GeometryKind.BODY):
        for wire_id, wire in wires.items():
            if _runs_across_body(wire.segment, body_item.bounds):  # type: ignore[arg-type]
                through_body += 1
                issues.append(Issue(
                    "wire_through_body", (wire_id, body_item.id), body_item.bounds,
                    f"a wire runs through {body_item.owner}",
                ))

    # --- crossings ----------------------------------------------------------------------------------------------------
    crossings = wire_crossings(scene, network)
    for x, y, first, second in crossings:
        issues.append(Issue("crossing", (first, second), Bounds(x - 0.8, y - 0.8, x + 0.8, y + 0.8), "two wires cross"))

    # --- pins, symbols and the nets they are on --------------------------------------------------------------------
    pin_points: dict[str, tuple[float, float]] = {}
    power_pins: dict[str, tuple[str, tuple[float, float]]] = {}
    for item in items:
        if item.kind == GeometryKind.PIN_ENDPOINT and item.id.endswith(":terminal"):
            token = item.id[: -len(":terminal")]
            point = (item.bounds.left, item.bounds.top)
            if item.net is None:
                pin_points[token] = point
            else:
                power_pins[token] = (item.net, point)
    pins_of: dict[str, list[str]] = {}
    for token in pin_points:
        pins_of.setdefault(token.rsplit(".", 1)[0], []).append(token)

    net_of_pin, net_members, rails, grounds = _nets(
        network, pin_points, nets, set(rail_nets), set(ground_nets),
    )

    # --- support parts far from the part they serve ----------------------------------------------------------------
    library_of = {
        item.owner: item.text for item in items if item.kind == GeometryKind.BODY and item.text
    }
    hubs, parts = _hubs_and_parts(pins_of, library_of)
    hub_pins = {token for owner in hubs for token in pins_of[owner]}
    supports: list[SupportPart] = []
    for owner in parts:
        first, second = pins_of[owner]
        net_a, net_b = net_of_pin.get(first), net_of_pin.get(second)
        best, host, kind = _nearest_host(
            owner, ((first, net_a), (second, net_b)), net_members, rails, grounds, hub_pins, pin_points, pins_of,
        )
        if best is None or host is None:
            continue
        supports.append(SupportPart(owner, kind, round(best, 2), host))
        if best > far_limit[kind]:
            box = _bounds_of_points([pin_points[first], pin_points[second]])
            issues.append(Issue(
                "support_far", (owner, host), box,
                f"{owner} is {best:.0f} mm from {host}, the part it serves", best,
            ))
    distances = [part.distance_mm for part in supports]

    # --- wire that goes further than the connection needs ---------------------------------------------------------
    excess: list[tuple[tuple[str, ...], float, float]] = []
    for component in network.components:
        if len(component.pins) < 2 or component.power:
            continue
        points = [pin_points[token] for token in component.pins if token in pin_points]
        if len(points) < 2:
            continue
        hpwl = (max(p[0] for p in points) - min(p[0] for p in points)) + (max(p[1] for p in points) - min(p[1] for p in points))
        if hpwl < 1e-6:
            continue
        ratio = component.length_mm / hpwl
        excess.append((component.pins, round(ratio, 2), round(component.length_mm - hpwl, 1)))
        if ratio >= DETOUR_RATIO and component.length_mm - hpwl >= DETOUR_MIN_MM:
            issues.append(Issue(
                "detour", component.pins, _bounds_of_points(points),
                f"net {', '.join(component.pins)} takes {component.length_mm:.0f} mm for a {hpwl:.0f} mm connection",
                component.length_mm - hpwl,
            ))

    # --- tiny jogs and long label stubs --------------------------------------------------------------------------------
    jogs = _jogs(network, wires)
    for ids, box in jogs:
        issues.append(Issue("jog", ids, box, "a wire steps sideways by less than a pin spacing"))
    stubs: list[float] = []
    for component in network.components:
        if len(component.pins) == 1 and component.labels and not component.power:
            stubs.append(component.length_mm)
            if component.length_mm > LABEL_STUB_FAR_MM:
                box = _bounds_of_segments([wires[sid].segment for sid in component.segments])  # type: ignore[misc]
                issues.append(Issue(
                    "label_far", component.pins, box,
                    f"{component.pins[0]} reaches its label {component.length_mm:.0f} mm away", component.length_mm,
                ))

    # --- how straight the wires run --------------------------------------------------------------------------------------
    # A person draws a straight wire where two pins can line up. A connection is one pin joined to another through a wire
    # (a net of three pins is two); a stub to a label or a power symbol joins nothing.
    connections = connection_corners = two_pin_wires = straight_wires = 0
    for component in network.components:
        if component.power or len(component.pins) < 2:
            continue
        connections += len(component.pins) - 1
        connection_corners += _corners([wires[sid].segment for sid in component.segments])  # type: ignore[misc]
        if len(component.pins) == 2 and not component.labels:
            two_pin_wires += 1
            straight_wires += 1 if len(component.segments) == 1 else 0

    # --- how power symbols are reached -----------------------------------------------------------------------------------
    sideways = _sideways_power(items, wires, power_pins)

    segments = len(wires)
    measures: dict[str, float] = {
        "texts": len(texts),
        "fields": sum(1 for t in texts if t.kind in (GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT)),
        "text_on_wire": text_on_wire,
        "field_on_wire": field_on_wire,
        "rotated_text": len(rotated),
        "rotated_labels": len(rotated_labels),
        "support_excess_mm": round(sum(max(0.0, part.distance_mm - SUPPORT_NEAR_MM) for part in supports), 1),
        "crossings": len(crossings),
        "wires_through_body": through_body,
        "wire_segments": segments,
        "symbols": len(pins_of),
        "support_parts": len(distances),
        "support_far": sum(1 for part in supports if part.distance_mm > far_limit[part.kind]),
        "support_max_mm": round(max(distances), 1) if distances else 0.0,
        "support_median_mm": round(median(distances), 1) if distances else 0.0,
        "detours": sum(1 for pins, ratio, extra in excess if ratio >= DETOUR_RATIO and extra >= DETOUR_MIN_MM),
        "jogs": len(jogs),
        "connections": connections,
        "connection_corners": connection_corners,
        "two_pin_wires": two_pin_wires,
        "straight_wires": straight_wires,
        "labels": sum(1 for t in texts if t.kind == GeometryKind.NET_LABEL),
        "label_stub_max_mm": round(max(stubs), 1) if stubs else 0.0,
        "label_far": sum(1 for s in stubs if s > LABEL_STUB_FAR_MM),
        "power_symbols": len(power_pins),
        "power_sideways": sideways,
    }
    return ReadabilityReport(
        measures, tuple(issues), tuple(supports), tuple(distances), tuple(stubs), tuple(excess),
    )


# ----------------------------------------------------------------------------------------------------------------------


def _bounds_of_points(points: Sequence[tuple[float, float]]) -> Bounds:
    return Bounds(min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))


def _bounds_of_segments(segments: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> Bounds:
    return _bounds_of_points([point for segment in segments for point in segment])


def _nets(
    network: WireNetwork,
    pin_points: Mapping[str, tuple[float, float]],
    declared: Mapping[str, Sequence[str]] | None,
    rail_names: set[str],
    ground_names: set[str],
) -> tuple[dict[str, str], dict[str, set[str]], set[str], set[str]]:
    """pin -> net, net -> pins, the supply and ground nets. Declared nets win; else conductors joined by name."""
    if declared is not None:
        net_of_pin = {token: name for name, tokens in declared.items() for token in tokens if token in pin_points}
        members = {name: {t for t in tokens if t in pin_points} for name, tokens in declared.items()}
        grounds = set(ground_names) or {name for name in declared if is_ground_name(name)}
        return net_of_pin, members, set(rail_names) | grounds, grounds
    # Conductors that share a label or power-symbol name are one net.
    parent = list(range(len(network.components)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_name: dict[str, int] = {}
    for index, component in enumerate(network.components):
        for name in component.names:
            if name in by_name:
                parent[find(index)] = find(by_name[name])
            else:
                by_name[name] = index
    names_of: dict[int, set[str]] = {}
    for index, component in enumerate(network.components):
        names_of.setdefault(find(index), set()).update(component.names)
    net_of_pin: dict[str, str] = {}
    members: dict[str, set[str]] = {}
    rails: set[str] = set()
    grounds: set[str] = set()
    for index, component in enumerate(network.components):
        root = find(index)
        key = f"net{root}"
        for token in component.pins:
            net_of_pin[token] = key
            members.setdefault(key, set()).add(token)
        if component.power:
            rails.add(key)
        if any(is_ground_name(name) for name in component.power):
            grounds.add(key)
    for root, names in names_of.items():
        key = f"net{root}"
        if any(is_ground_name(name) for name in names):
            grounds.add(key)
    return net_of_pin, members, rails | grounds, grounds


def _is_connector(library: str | None) -> bool:
    """KiCad keeps connectors in libraries whose names start with Connector: they are where a circuit meets the outside,
    not parts that support another."""
    return bool(library) and library.split(":", 1)[0].lower().startswith("connector")  # type: ignore[union-attr]


def _hubs_and_parts(
    pins_of: Mapping[str, list[str]], library_of: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """The parts a circuit is built round (three pins or more, or a connector or source) and the two-pin parts that support
    them; connectors and sources are never support parts."""
    library_of = library_of or {}
    sizes = {owner: len(tokens) for owner, tokens in pins_of.items() if not owner.startswith("#")}
    if not sizes:
        return [], []
    hubs = [
        owner for owner, size in sizes.items()
        if size >= 3 or (size >= 2 and (_is_connector(library_of.get(owner)) or is_source(owner, library_of.get(owner) or "")))
    ]
    parts = [owner for owner, size in sizes.items() if size == 2 and owner not in hubs]
    return hubs, parts


def _nearest_host(
    owner: str,
    pins: tuple[tuple[str, str | None], tuple[str, str | None]],
    net_members: Mapping[str, set[str]],
    rails: set[str],
    grounds: set[str],
    hub_pins: set[str],
    pin_points: Mapping[str, tuple[float, float]],
    pins_of: Mapping[str, list[str]],
) -> tuple[float | None, str | None, str]:
    """How far a two-pin part is from the nearest big-part pin it is wired to, which pin that is, and what the part does."""
    (first, net_a), (second, net_b) = pins
    on_rail = [net in rails if net is not None else False for net in (net_a, net_b)]
    candidates: list[tuple[str, str | None]] = []
    if on_rail[0] and on_rail[1]:
        kind = "decoupling"
        # a bypass between two rails: the supply side is what it serves
        for token, net in ((first, net_a), (second, net_b)):
            if net is not None and net not in grounds:
                candidates.append((token, net))
    elif on_rail[0] != on_rail[1]:
        kind = "shunt"
        token, net = (second, net_b) if on_rail[0] else (first, net_a)
        candidates.append((token, net))
    else:
        kind = "series"
        candidates.extend([(first, net_a), (second, net_b)])
    best: tuple[float | None, str | None] = (None, None)
    for token, net in candidates:
        if net is None:
            continue
        for host in _hub_pins_reachable(net, owner, net_members, hub_pins, pins_of, rails):
            if host in pin_points and token in pin_points:
                distance = _manhattan(pin_points[token], pin_points[host])
                if best[0] is None or distance < best[0]:
                    best = (distance, host)
    return best[0], best[1], kind


def _hub_pins_reachable(
    net: str, owner: str, net_members: Mapping[str, set[str]], hub_pins: set[str],
    pins_of: Mapping[str, list[str]], rails: set[str],
) -> list[str]:
    """Big-part pins on a net, or on the next net through another two-pin part (a load capacitor on a crystal)."""
    direct = [token for token in net_members.get(net, ()) if token in hub_pins]
    if direct:
        return direct
    found: list[str] = []
    for token in net_members.get(net, ()):
        other = token.rsplit(".", 1)[0]
        if other == owner or len(pins_of.get(other, ())) != 2:
            continue
        for neighbour in pins_of[other]:
            if neighbour == token:
                continue
            for net_name, members in net_members.items():
                if neighbour in members and net_name not in rails:
                    found.extend(t for t in members if t in hub_pins)
    return found


def _jogs(network: WireNetwork, wires: Mapping[str, object]) -> list[tuple[tuple[str, ...], Bounds]]:
    found: list[tuple[tuple[str, ...], Bounds]] = []
    for component in network.components:
        ends: dict[tuple[float, float], list[str]] = {}
        for wire_id in component.segments:
            segment = wires[wire_id].segment  # type: ignore[attr-defined]
            for point in segment:
                ends.setdefault((round(point[0], 3), round(point[1], 3)), []).append(wire_id)
        for wire_id in component.segments:
            (ax, ay), (bx, by) = wires[wire_id].segment  # type: ignore[attr-defined]
            length = abs(ax - bx) + abs(ay - by)
            if length > JOG_MM + 1e-6:
                continue
            horizontal = abs(ay - by) < 1e-6
            corner_ends = 0
            for point in ((ax, ay), (bx, by)):
                neighbours = [w for w in ends[(round(point[0], 3), round(point[1], 3))] if w != wire_id]
                if len(neighbours) == 1:
                    other = wires[neighbours[0]].segment  # type: ignore[attr-defined]
                    other_horizontal = abs(other[0][1] - other[1][1]) < 1e-6
                    if other_horizontal != horizontal:
                        corner_ends += 1
            if corner_ends == 2:
                found.append(((wire_id,), _bounds_of_points([(ax, ay), (bx, by)])))
    return found


def _sideways_power(
    items: Sequence, wires: Mapping[str, object], power_pins: Mapping[str, tuple[str, tuple[float, float]]],
) -> int:
    """Power symbols whose wire arrives from the side: a ground symbol hangs from a wire above it, a supply symbol
    stands on a wire below it."""
    count = 0
    for token, (_name, tip) in power_pins.items():
        arrival = None
        for wire in wires.values():
            (ax, ay), (bx, by) = wire.segment  # type: ignore[attr-defined]
            for near, far in (((ax, ay), (bx, by)), ((bx, by), (ax, ay))):
                if abs(near[0] - tip[0]) < 1e-3 and abs(near[1] - tip[1]) < 1e-3:
                    arrival = "horizontal" if abs(near[1] - far[1]) < 1e-6 else "vertical"
        if arrival == "horizontal":
            count += 1
    return count
