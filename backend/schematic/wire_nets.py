"""Which wires are one conductor, and where do different conductors cross.

The measured scene has wires, pin tips, labels and power-symbol pins but not which belong together. KiCad joins wires
that touch end to end, or where one wire's end lies on another's length (a T), and joins a pin or a label to a wire it
lies on; two wires that merely cross connect nothing. This module groups the scene's wires into the conductors those
rules make, and finds the crossings. It is pure and knows no circuit.
"""
from __future__ import annotations

from dataclasses import dataclass

from .presentation_geometry import GeometryKind, GeometryScene

_EPS = 1e-3


@dataclass(frozen=True)
class WireComponent:
    """One conductor: wires that touch, the pins and labels on them."""

    segments: tuple[str, ...]
    pins: tuple[str, ...]            # "REF.PIN" of the circuit's symbols
    power: tuple[str, ...]           # the nets that power symbols on it put it on
    labels: tuple[str, ...]          # the text of the net labels on it
    length_mm: float

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.power, *self.labels)))


@dataclass(frozen=True)
class WireNetwork:
    components: tuple[WireComponent, ...]
    by_segment: dict[str, int]       # wire item id -> component
    by_pin: dict[str, int]           # pin token -> component


def _point(value: tuple[float, float]) -> tuple[float, float]:
    return (round(value[0], 3), round(value[1], 3))


def _on_segment(point: tuple[float, float], segment: tuple[tuple[float, float], tuple[float, float]]) -> bool:
    (ax, ay), (bx, by) = segment
    x, y = point
    if abs(ax - bx) < _EPS:
        return abs(x - ax) < _EPS and min(ay, by) - _EPS <= y <= max(ay, by) + _EPS
    return abs(y - ay) < _EPS and min(ax, bx) - _EPS <= x <= max(ax, bx) + _EPS


def _length(segment: tuple[tuple[float, float], tuple[float, float]]) -> float:
    (ax, ay), (bx, by) = segment
    return abs(ax - bx) + abs(ay - by)


class _Union:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, first: int, second: int) -> None:
        self.parent[self.find(first)] = self.find(second)


def wire_network(scene: GeometryScene) -> WireNetwork:
    wires = [item for item in scene.items if item.kind == GeometryKind.WIRE and item.segment is not None]
    segments = [
        (_point(item.segment[0]), _point(item.segment[1])) for item in wires  # type: ignore[index]
    ]
    union = _Union(len(wires))

    # Wires that touch end to end, end on length (a T), or overlap along a line.
    by_endpoint: dict[tuple[float, float], list[int]] = {}
    for index, (start, end) in enumerate(segments):
        for point in (start, end):
            by_endpoint.setdefault(point, []).append(index)
    for indices in by_endpoint.values():
        for other in indices[1:]:
            union.join(indices[0], other)
    for index, segment in enumerate(segments):
        for point, indices in by_endpoint.items():
            if indices[0] != index and index not in indices and _on_segment(point, segment):
                union.join(index, indices[0])

    pin_points: dict[str, tuple[float, float]] = {}
    pin_net: dict[str, str | None] = {}
    for item in scene.items:
        if item.kind == GeometryKind.PIN_ENDPOINT and item.id.endswith(":terminal"):
            token = item.id[: -len(":terminal")]
            pin_points[token] = _point((item.bounds.left, item.bounds.top))
            pin_net[token] = item.net
    labels = [item for item in scene.items if item.kind == GeometryKind.NET_LABEL and item.anchor is not None]

    def attach(point: tuple[float, float]) -> int | None:
        for index, segment in enumerate(segments):
            if _on_segment(point, segment):
                return index
        return None

    def attach_end(point: tuple[float, float]) -> int | None:
        """KiCad joins a pin to the end of a wire; a pin on the middle of one is "not connected" in ERC."""
        for index, (start, end) in enumerate(segments):
            if any(abs(point[0] - p[0]) < _EPS and abs(point[1] - p[1]) < _EPS for p in (start, end)):
                return index
        return None

    pin_segment: dict[str, int | None] = {token: attach_end(point) for token, point in pin_points.items()}
    label_segment: dict[str, int | None] = {
        label.id: attach(_point(label.anchor)) for label in labels  # type: ignore[arg-type]
    }
    # A pin that touches another pin directly (no wire between them) is on the same conductor.
    pins_at: dict[tuple[float, float], list[str]] = {}
    for token, point in pin_points.items():
        pins_at.setdefault(point, []).append(token)

    members: dict[int, dict[str, list]] = {}

    def component_of(root: int) -> dict[str, list]:
        return members.setdefault(root, {"segments": [], "pins": [], "power": [], "labels": [], "length": [0.0]})

    for index, item in enumerate(wires):
        entry = component_of(union.find(index))
        entry["segments"].append(item.id)
        entry["length"][0] += _length(segments[index])
    for token, index in pin_segment.items():
        if index is None:
            continue
        entry = component_of(union.find(index))
        if pin_net[token] is not None:
            entry["power"].append(pin_net[token])
        else:
            entry["pins"].append(token)
    for label in labels:
        index = label_segment[label.id]
        if index is not None:
            component_of(union.find(index))["labels"].append(label.text or label.net or "")

    components: list[WireComponent] = []
    by_segment: dict[str, int] = {}
    by_pin: dict[str, int] = {}
    for root in sorted(members):
        entry = members[root]
        number = len(components)
        components.append(WireComponent(
            segments=tuple(entry["segments"]), pins=tuple(entry["pins"]),
            power=tuple(dict.fromkeys(entry["power"])), labels=tuple(dict.fromkeys(entry["labels"])),
            length_mm=round(entry["length"][0], 3),
        ))
        for segment_id in entry["segments"]:
            by_segment[segment_id] = number
        for token in entry["pins"]:
            by_pin[token] = number
    # Pins that touch each other directly, or touch nothing: a component of their own, so a connection without a wire
    # still has a conductor to be counted on.
    for point, tokens in pins_at.items():
        real = [token for token in tokens if pin_net[token] is None and token not in by_pin]
        if len(tokens) >= 2 and real:
            number = len(components)
            power = tuple(dict.fromkeys(pin_net[token] for token in tokens if pin_net[token] is not None))
            components.append(WireComponent(segments=(), pins=tuple(real), power=power, labels=(), length_mm=0.0))
            for token in real:
                by_pin[token] = number
    return WireNetwork(tuple(components), by_segment, by_pin)


def wire_crossings(scene: GeometryScene, network: WireNetwork) -> list[tuple[float, float, str, str]]:
    """Where two wires of different conductors cross in the interior of both: (x, y, first wire, second wire)."""
    horizontal: list[tuple[str, float, float, float]] = []
    vertical: list[tuple[str, float, float, float]] = []
    for item in scene.items:
        if item.kind != GeometryKind.WIRE or item.segment is None:
            continue
        (ax, ay), (bx, by) = item.segment
        if abs(ay - by) < _EPS and abs(ax - bx) > _EPS:
            horizontal.append((item.id, ay, min(ax, bx), max(ax, bx)))
        elif abs(ax - bx) < _EPS and abs(ay - by) > _EPS:
            vertical.append((item.id, ax, min(ay, by), max(ay, by)))
    horizontal.sort(key=lambda entry: entry[1])
    ys = [entry[1] for entry in horizontal]
    from bisect import bisect_left, bisect_right

    found: list[tuple[float, float, str, str]] = []
    for vertical_id, x, top, bottom in vertical:
        lo, hi = bisect_left(ys, top + _EPS), bisect_right(ys, bottom - _EPS)
        for horizontal_id, y, left, right in horizontal[lo:hi]:
            if left + _EPS < x < right - _EPS:
                if network.by_segment.get(vertical_id) != network.by_segment.get(horizontal_id):
                    found.append((round(x, 3), round(y, 3), vertical_id, horizontal_id))
    return found
