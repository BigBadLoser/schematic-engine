"""Circuit-independent schematic presentation geometry.

A measured scene is a typed collection of geometry and explicit electrical
attachments. No circuit labels, references, or component library identities
participate in the collision policy. Geometry is read-only in phase 2: future
placement and routing passes should consume the same scene without mutating it.

Coordinates are millimetres in page space (X right, Y down).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Iterable


class GeometryKind(StrEnum):
    BODY = "body"
    PIN_ENDPOINT = "pin_endpoint"
    PIN_STROKE = "pin_stroke"
    PIN_NAME = "pin_name"
    PIN_NUMBER = "pin_number"
    REFERENCE = "reference"
    VALUE = "value"
    POWER_GRAPHIC = "power_graphic"
    POWER_TEXT = "power_text"
    NET_LABEL = "net_label"
    WIRE = "wire"
    JUNCTION = "junction"


TEXT_KINDS = frozenset({
    GeometryKind.PIN_NAME, GeometryKind.PIN_NUMBER,
    GeometryKind.REFERENCE, GeometryKind.VALUE,
    GeometryKind.POWER_TEXT, GeometryKind.NET_LABEL,
})
CONDUCTOR_KINDS = frozenset({
    GeometryKind.PIN_ENDPOINT, GeometryKind.PIN_STROKE,
    GeometryKind.WIRE, GeometryKind.JUNCTION,
})
# Symbol body and its pin construction are intentionally allowed to meet;
# a field crossing its symbol body is never silently excused.
INTERNAL_GRAPHIC_KINDS = frozenset({
    GeometryKind.BODY, GeometryKind.PIN_STROKE,
    GeometryKind.PIN_ENDPOINT, GeometryKind.POWER_GRAPHIC,
})


@dataclass(frozen=True)
class Bounds:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not all(isfinite(n) for n in (self.left, self.top, self.right, self.bottom)):
            raise ValueError("Bounds must contain finite millimetre coordinates")
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("Bounds must be ordered")

    def overlaps(self, other: "Bounds", clearance_mm: float = 0.0) -> bool:
        if clearance_mm < 0 or not isfinite(clearance_mm):
            raise ValueError("Clearance must be finite and non-negative")
        # Zero-sized pin/end-point geometry may lie exactly on a boundary.
        return (
            self.left < other.right + clearance_mm
            and self.right + clearance_mm > other.left
            and self.top < other.bottom + clearance_mm
            and self.bottom + clearance_mm > other.top
        )

    def as_dict(self) -> dict[str, float]:
        return dict(left=self.left, top=self.top, right=self.right, bottom=self.bottom)


# KiCad draws a local label's text centred on its anchor along the wire and above it (to its left when the label
# is rotated 90 or 270 degrees). Measured against KiCad's own rendering of the fixtures: the text rises 0.82 of
# its size above the anchor line and its descenders reach 0.18 below it.
LABEL_RISE = 0.82
LABEL_DROP = 0.18
# A global or hierarchical label carries a flag at its anchor, about this many text heights long and 1.4 high.
LABEL_FLAG = 1.5
LABEL_FLAG_THICKNESS = 1.4


def label_text_bounds(x: float, y: float, text_width: float, size: float, *, vertical: bool) -> Bounds:
    """Where a local label's text is, for a label at (x, y)."""
    if vertical:
        return Bounds(x - LABEL_RISE * size, y - text_width / 2, x + LABEL_DROP * size, y + text_width / 2)
    return Bounds(x - text_width / 2, y - LABEL_RISE * size, x + text_width / 2, y + LABEL_DROP * size)


def flagged_label_bounds(x: float, y: float, text_width: float, size: float, heading: tuple[float, float]) -> Bounds:
    """Where a global label's flag and text are: they run away from the anchor, in the direction ``heading``."""
    run = text_width + LABEL_FLAG * size
    half = LABEL_FLAG_THICKNESS * size / 2
    if abs(heading[0]) >= abs(heading[1]):
        end = x + run if heading[0] >= 0 else x - run
        return Bounds(min(x, end), y - half, max(x, end), y + half)
    end = y + run if heading[1] >= 0 else y - run
    return Bounds(x - half, min(y, end), x + half, max(y, end))


@dataclass(frozen=True)
class GeometryItem:
    id: str
    kind: GeometryKind
    owner: str
    bounds: Bounds
    net: str | None = None
    # IDs of explicit electrical attachment primitives for this conductor.
    attachments: frozenset[str] = frozenset()
    # measured = exact vector geometry; estimated = text metric approximation.
    measurement: str = "measured"
    # Optional exact orthogonal wire endpoints, used to avoid bbox-only crossings.
    segment: tuple[tuple[float, float], tuple[float, float]] | None = None
    # For a text: its anchor, what it says and the angle it is drawn at (absolute, symbol rotation included).
    # For a symbol's graphic: its origin, its rotation, its mirror axis and (as ``text``) its library id.
    anchor: tuple[float, float] | None = None
    text: str | None = None
    angle: float | None = None
    mirror: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.owner:
            raise ValueError("Geometry items require an ID and owner")
        if self.measurement not in {"measured", "estimated"}:
            raise ValueError("Unknown geometry measurement confidence")
        if self.segment is not None:
            if self.kind != GeometryKind.WIRE:
                raise ValueError("Segments belong to wires")
            (ax, ay), (bx, by) = self.segment
            if not all(isfinite(n) for n in (ax, ay, bx, by)):
                raise ValueError("Wire coordinates must be finite")
            if ax != bx and ay != by:
                raise ValueError("Wires must be orthogonal")


@dataclass(frozen=True)
class GeometryConflict:
    first: str
    second: str
    reason: str
    certainty: str

    def as_dict(self) -> dict[str, str]:
        return dict(first=self.first, second=self.second,
                    reason=self.reason, certainty=self.certainty)


def _segments_intersect(a: GeometryItem, b: GeometryItem) -> bool:
    if not (a.segment and b.segment):
        return a.bounds.overlaps(b.bounds)
    (ax, ay), (bx, by) = a.segment
    (cx, cy), (dx, dy) = b.segment
    # Orthogonal horizontal/vertical pair or collinear touching segments.
    if ay == by and cy == dy:
        return ay == cy and max(min(ax, bx), min(cx, dx)) <= min(max(ax, bx), max(cx, dx))
    if ax == bx and cx == dx:
        return ax == cx and max(min(ay, by), min(cy, dy)) <= min(max(ay, by), max(cy, dy))
    if ay == by:
        return min(ax, bx) <= cx <= max(ax, bx) and min(cy, dy) <= ay <= max(cy, dy)
    return min(cx, dx) <= ax <= max(cx, dx) and min(ay, by) <= cy <= max(ay, by)


def _collision_reason(a: GeometryItem, b: GeometryItem) -> str | None:
    if a.id == b.id:
        return None
    if a.owner == b.owner and a.kind in INTERNAL_GRAPHIC_KINDS and b.kind in INTERNAL_GRAPHIC_KINDS:
        return None
    if a.kind == GeometryKind.WIRE and b.kind == GeometryKind.WIRE:
        if not _segments_intersect(a, b):
            return None
        # A common explicitly declared junction, rather than just a matching
        # net name, is required to permit an electrical crossing.
        if a.net and a.net == b.net and a.attachments & b.attachments:
            return None
        return "wire_crossing_without_shared_junction"
    if a.kind == GeometryKind.WIRE or b.kind == GeometryKind.WIRE:
        wire, other = (a, b) if a.kind == GeometryKind.WIRE else (b, a)
        if other.kind in {GeometryKind.PIN_ENDPOINT, GeometryKind.JUNCTION}:
            if wire.net and wire.net == other.net and other.id in wire.attachments:
                return None
        if other.kind == GeometryKind.PIN_STROKE:
            if wire.owner == other.owner and wire.net == other.net:
                return None
        return "wire_intersects_reserved_geometry"
    if a.kind in CONDUCTOR_KINDS and b.kind in CONDUCTOR_KINDS:
        if a.net and a.net == b.net and (a.id in b.attachments or b.id in a.attachments):
            return None
    if a.kind in TEXT_KINDS or b.kind in TEXT_KINDS:
        return "text_clearance"
    return "graphic_clearance"


@dataclass(frozen=True)
class GeometryScene:
    items: tuple[GeometryItem, ...] = ()
    clearance_mm: float = 0.0

    def __post_init__(self) -> None:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Geometry IDs must be unique")
        if self.clearance_mm < 0 or not isfinite(self.clearance_mm):
            raise ValueError("Invalid clearance")
        known = {item.id: item for item in self.items}
        for item in self.items:
            for attachment in item.attachments:
                target = known.get(attachment)
                if target is None:
                    raise ValueError(f"Unknown attachment: {attachment}")
                if not item.net or item.net != target.net:
                    raise ValueError("Attachment must share an explicit electrical net")
                if target.kind not in {GeometryKind.PIN_ENDPOINT, GeometryKind.JUNCTION}:
                    raise ValueError("Only pins and junctions can be electrical attachments")

    @classmethod
    def from_items(cls, items: Iterable[GeometryItem], *, clearance_mm: float = 0.0) -> "GeometryScene":
        return cls(tuple(items), clearance_mm=clearance_mm)

    def conflicts(self) -> tuple[GeometryConflict, ...]:
        found: list[GeometryConflict] = []
        for i, first in enumerate(self.items):
            for second in self.items[i + 1:]:
                if not first.bounds.overlaps(second.bounds, self.clearance_mm):
                    continue
                reason = _collision_reason(first, second)
                if reason is None:
                    continue
                certainty = ("measured" if first.measurement == second.measurement == "measured"
                             else "estimated")
                found.append(GeometryConflict(first.id, second.id, reason, certainty))
        return tuple(found)

    def to_dict(self, *, include_conflicts: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "clearance_mm": self.clearance_mm,
            "items": [
                {
                    "id": item.id, "kind": item.kind.value, "owner": item.owner,
                    "bounds": item.bounds.as_dict(), "net": item.net,
                    "attachments": sorted(item.attachments),
                    "measurement": item.measurement,
                    "segment": item.segment,
                }
                for item in self.items
            ],
        }
        if include_conflicts:
            payload["conflicts"] = [issue.as_dict() for issue in self.conflicts()]
        return payload
