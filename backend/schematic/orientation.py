"""Which way a symbol faces: a rotation and a mirror, from where its pins end up and which way they point.

A symbol's pins should face the parts they connect to, and a connector's pins should read in order: pin 1 above pin 2
down the side of a connector that faces left or right, and left to right along one that faces up or down. A rotation
of 180 degrees puts a connector's pins on the other side but reverses their order; mirroring it instead keeps both.
People draw connectors that way, so a drawing whose input connector has pin 2 above pin 1 reads as a mistake.

This module scores the twelve orientations (four rotations, each with no mirror or one of the two axes) of one symbol
and is pure: positions and directions in, an orientation out. The server supplies the pin geometry from the symbol
library.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Point = tuple[float, float]
Box = tuple[float, float, float, float]

ROTATIONS = (0, 90, 180, 270)
MIRRORS = ("", "x", "y")

# What a pin that faces the wrong way, or sits far from the pin it serves, costs, in millimetres of wire.
FACING_COST_PER_GRID = 6.0
# One pair of pins out of order on a connector.
PIN_ORDER_COST_MM = 8.0


@dataclass(frozen=True)
class Orientation:
    rotation: int
    mirror: str = ""

    def __post_init__(self) -> None:
        if self.rotation % 90 or self.mirror not in MIRRORS:
            raise ValueError(f"Not an orientation: {self.rotation}, {self.mirror!r}")


@dataclass(frozen=True)
class PinTarget:
    """A pin of the symbol and the pin it should face: where that pin is and which way it points out of its symbol.

    When the target pin is not known (no point), the symbol's own origin distance to ``fallback`` stands in.
    """

    pin: str
    point: Point | None = None
    normal: Point | None = None
    fallback: Point | None = None


# What a part pays for turning a quarter off how its library draws it (a resistor stands, a diode lies), per grid step,
# in the placement of a hung part, and what it pays per grid step for each unit of its far pin pointing the wrong way.
QUARTER_TURN_GRIDS = 3.0
EXTENSION_WEIGHT = 2.0
# A weight above 1.5 stands a diode up when its far pin is asked to go up the page: the quarter turn costs 3 grid
# steps, a far pin 90 degrees off costs ``weight`` grid steps and one pointing back costs twice that.
ENGINE_EXTENSION_WEIGHT = 4.0


def extension_penalty(node: Point, far: Point, wanted: Point, *, grid_mm: float, weight: float = EXTENSION_WEIGHT) -> float:
    """What a part's far pin pointing off the direction it was asked to extend costs: nothing when it points that way."""
    dx, dy = far[0] - node[0], far[1] - node[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 1e-9:
        return grid_mm * 20.0
    alignment = (dx / length) * wanted[0] + (dy / length) * wanted[1]
    return (1.0 - alignment) * grid_mm * weight


def mirror_box(box: Box, anchor: Point, axis: str) -> Box:
    """A box of a symbol drawn without mirror, as it lies when the symbol is mirrored about its anchor.

    ``axis`` is KiCad's ``(mirror x|y)``: ``x`` flips top and bottom about the anchor's row, ``y`` flips left and right
    about the anchor's column.
    """
    left, top, right, bottom = box
    ax, ay = anchor
    if axis == "x":
        return (left, 2.0 * ay - bottom, right, 2.0 * ay - top)
    if axis == "y":
        return (2.0 * ax - right, top, 2.0 * ax - left, bottom)
    return box


def _pin_number(pin: str) -> int | None:
    return int(pin) if re.fullmatch(r"\d+", pin) else None


def order_inversions(pins: Mapping[str, Point], normals: Mapping[str, Point]) -> int:
    """How many pairs of numbered pins read out of order along the side they sit on.

    Down a left or right side the pins should run top to bottom in increasing number; along a top or bottom side, left
    to right. A symbol whose pins face both ways (a part with pins on four sides) has no one reading order and counts
    nothing.
    """
    numbered = sorted((n, pin) for pin in pins if (n := _pin_number(pin)) is not None)
    if len(numbered) < 2:
        return 0
    horizontal = [abs(normals.get(pin, (0.0, 0.0))[0]) > abs(normals.get(pin, (0.0, 0.0))[1]) for _n, pin in numbered]
    if any(horizontal) and not all(horizontal):
        return 0
    along = 1 if all(horizontal) else 0
    coordinates = [pins[pin][along] for _n, pin in numbered]
    return sum(
        1 for i in range(len(coordinates)) for j in range(i + 1, len(coordinates)) if coordinates[i] > coordinates[j] + 1e-6
    )


def score_orientation(
    pins: Mapping[str, Point],
    normals: Mapping[str, Point],
    targets: Sequence[PinTarget],
    *,
    ordered: bool = False,
    grid_mm: float = 2.54,
) -> float | None:
    """How well an orientation serves: lower is better, None when a wanted pin does not exist at this orientation."""
    score = 0.0
    for target in targets:
        if target.pin not in pins:
            return None
        px, py = pins[target.pin]
        if target.point is None:
            if target.fallback is not None:
                dx, dy = px - target.fallback[0], py - target.fallback[1]
                score += dx * dx + dy * dy
            continue
        vx, vy = target.point[0] - px, target.point[1] - py
        distance = (vx * vx + vy * vy) ** 0.5
        nx, ny = normals.get(target.pin, (0.0, 0.0))
        if distance > 1e-9:
            subject_align = (nx * vx + ny * vy) / distance
            tnx, tny = target.normal if target.normal is not None else (0.0, 0.0)
            target_align = (tnx * -vx + tny * -vy) / distance
        else:
            subject_align = target_align = 1.0
        score += distance
        score += (1.0 - subject_align) * grid_mm * FACING_COST_PER_GRID
        score += (1.0 - target_align) * grid_mm * FACING_COST_PER_GRID
    if ordered:
        score += PIN_ORDER_COST_MM * order_inversions(pins, normals)
    return score


def choose_orientation(
    candidates: Mapping[Orientation, tuple[Mapping[str, Point], Mapping[str, Point]]],
    targets: Sequence[PinTarget],
    *,
    ordered: bool = False,
    mirrors: bool = False,
    grid_mm: float = 2.54,
) -> tuple[Orientation, dict[Orientation, float]]:
    """The best orientation among ``candidates`` (orientation to its pin positions and outward directions).

    A mirror is only considered when ``mirrors`` is set, and a tie goes to no mirror and then the smaller rotation:
    a symbol is never mirrored for nothing.
    """
    scores: dict[Orientation, float] = {}
    for orientation, (pins, normals) in candidates.items():
        if orientation.mirror and not mirrors:
            continue
        score = score_orientation(pins, normals, targets, ordered=ordered, grid_mm=grid_mm)
        if score is not None:
            scores[orientation] = score
    if not scores:
        raise ValueError("No valid orientation candidate; check the symbol's pin numbers")
    best = min(scores, key=lambda o: (round(scores[o], 6), bool(o.mirror), o.rotation, o.mirror))
    return best, scores
