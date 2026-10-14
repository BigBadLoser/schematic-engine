"""Circuit-independent adapters from presentation geometry to solver keepouts.

Both CP-SAT candidate selection and libavoid routing use *these same typed
reservations*. The caller still owns its exact electrical pin escape rules.
Geometry is in KiCad page millimetres; reservation transforms never inspect a
symbol library, reference designator, circuit category or net name.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians, sin
from typing import Iterable

from .presentation_geometry import Bounds, GeometryKind, GeometryScene

# Pin geometry may legally meet a conductor at its own terminal. Treat the
# symbol body as the graphic obstacle and reserve nearby text independently.
# Existing wires have no trustworthy net owner until netlist attribution lands.
RESERVED_KINDS = frozenset({
    GeometryKind.BODY,
    GeometryKind.POWER_GRAPHIC,
    GeometryKind.REFERENCE,
    GeometryKind.VALUE,
    GeometryKind.POWER_TEXT,
    GeometryKind.PIN_NAME,
    GeometryKind.PIN_NUMBER,
    GeometryKind.NET_LABEL,
})


@dataclass(frozen=True)
class Reservation:
    identity: str
    owner: str
    kind: GeometryKind
    bounds: Bounds
    certainty: str

    def as_router_node(self, clearance_mm: float = 0.0) -> dict[str, object]:
        b = self.bounds
        if clearance_mm < 0:
            raise ValueError("clearance_mm must be non-negative")
        return {
            "id": "keepout::" + self.identity,
            "x": b.left - clearance_mm,
            "y": b.top - clearance_mm,
            "width": max(0.01, b.right - b.left + 2 * clearance_mm),
            "height": max(0.01, b.bottom - b.top + 2 * clearance_mm),
        }


def reservations_for_owners(
    scene: GeometryScene,
    owners: Iterable[str],
    *,
    include_net_labels: bool = False,
) -> tuple[Reservation, ...]:
    selected = frozenset(owners)
    return tuple(
        Reservation(item.id, item.owner, item.kind, item.bounds, item.measurement)
        for item in scene.items
        if item.kind in RESERVED_KINDS
        and (item.owner in selected or (
            include_net_labels and item.kind == GeometryKind.NET_LABEL
        ))
        # Hidden labels and any zero-sized degenerate measurements must not
        # become unbounded or fabricated router obstacles.
        and item.bounds.right > item.bounds.left
        and item.bounds.bottom > item.bounds.top
    )


def relocated_reservations(
    reservations: Iterable[Reservation],
    *,
    old_origin: tuple[float, float],
    new_origin: tuple[float, float],
    rotation_delta_degrees: int = 0,
) -> tuple[Reservation, ...]:
    """Predict candidate owner geometry for CP-SAT, including its text fields.

    Exact coordinate transform is rigid for graphics. Text glyph metrics remain
    estimates (until we measure rendered KiCad text), but the solver can still
    reserve their conservative rectangles instead of ignoring them.
    """
    if rotation_delta_degrees % 90:
        raise ValueError("KiCad placement rotations must be multiples of 90")
    theta = radians(rotation_delta_degrees % 360)
    co, si = round(cos(theta)), round(sin(theta))
    result: list[Reservation] = []
    for item in reservations:
        b = item.bounds
        points = (
            (b.left, b.top), (b.right, b.top),
            (b.left, b.bottom), (b.right, b.bottom),
        )
        # In page coordinates, +90 library rotation maps (dx, dy) -> (dy,-dx).
        transformed = [
            (
                new_origin[0] + co * (x - old_origin[0]) + si * (y - old_origin[1]),
                new_origin[1] - si * (x - old_origin[0]) + co * (y - old_origin[1]),
            )
            for x, y in points
        ]
        box = Bounds(
            min(p[0] for p in transformed),
            min(p[1] for p in transformed),
            max(p[0] for p in transformed),
            max(p[1] for p in transformed),
        )
        result.append(Reservation(
            item.identity, item.owner, item.kind, box,
            "estimated" if item.certainty == "estimated" else "measured",
        ))
    return tuple(result)


def overlap_pairs(
    first: Iterable[Reservation],
    second: Iterable[Reservation],
    *,
    clearance_mm: float = 0.0,
) -> tuple[tuple[str, str], ...]:
    """Return every cross-owner collision, independent of component identity."""
    left, right = tuple(first), tuple(second)
    return tuple(
        (a.identity, b.identity)
        for a in left for b in right
        if a.owner != b.owner and a.bounds.overlaps(b.bounds, clearance_mm)
    )


def segment_intersects_reservation(
    start: tuple[float, float],
    end: tuple[float, float],
    reservation: Reservation,
    *,
    clearance_mm: float = 0.0,
) -> bool:
    """Closed segment versus rectangle; contacts count as collisions."""
    b = reservation.bounds
    x1, y1, x2, y2 = (
        b.left - clearance_mm, b.top - clearance_mm,
        b.right + clearance_mm, b.bottom + clearance_mm,
    )
    ax, ay = start
    bx, by = end
    if abs(ax - bx) < 1e-6:
        return x1 <= ax <= x2 and max(min(ay, by), y1) <= min(max(ay, by), y2)
    if abs(ay - by) < 1e-6:
        return y1 <= ay <= y2 and max(min(ax, bx), x1) <= min(max(ax, bx), x2)
    raise ValueError("Only orthogonal schematic segments are supported")


def external_text_reservations(
    scene: GeometryScene,
    owners: Iterable[str],
    body_bounds: dict[str, Bounds],
    *,
    include_net_labels: bool = True,
) -> tuple[Reservation, ...]:
    """Supplement a router's existing symbol body rectangles with visible text.

    Labels *within* the primitive body obstacle need no redundant router shape.
    Avoiding these duplicates also makes libavoid's obstacle graph smaller.
    """
    text_kinds = {
        GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT,
        GeometryKind.PIN_NAME, GeometryKind.PIN_NUMBER, GeometryKind.NET_LABEL,
    }
    return tuple(
        r for r in reservations_for_owners(
            scene, owners, include_net_labels=include_net_labels,
        )
        if r.kind in text_kinds
        and (
            r.owner not in body_bounds
            or (
                r.bounds.left < body_bounds[r.owner].left
                or r.bounds.right > body_bounds[r.owner].right
                or r.bounds.top < body_bounds[r.owner].top
                or r.bounds.bottom > body_bounds[r.owner].bottom
            )
        )
    )
