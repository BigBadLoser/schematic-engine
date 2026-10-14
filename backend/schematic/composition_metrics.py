"""Circuit-independent composition metrics derived from authored geometry.

These are *descriptive* metrics, not quality scores. They make regressions in
wire length and drawing extent visible even when KiCad's cosmetic score is
unchanged. Net attribution and text glyph metrics remain separate problems.
"""
from __future__ import annotations

import re

from .presentation_geometry import GeometryKind, GeometryScene, TEXT_KINDS

_OVERLAP_GRAPHICS = frozenset({GeometryKind.BODY, GeometryKind.POWER_GRAPHIC})
# KiCad numbers power symbols and power flags at random: "#PWR21696" says nothing about what it is.
_AUTO_REFERENCE = re.compile(r"^(#[A-Za-z]+)\d+$")


def _overlap(a, b, epsilon_mm: float) -> bool:
    return (
        min(a.bounds.right, b.bounds.right) - max(a.bounds.left, b.bounds.left) > epsilon_mm
        and min(a.bounds.bottom, b.bounds.bottom) - max(a.bounds.top, b.bounds.top) > epsilon_mm
    )


def symbol_overlap_pairs(
    scene: GeometryScene, *, epsilon_mm: float = 0.05,
) -> tuple[tuple[str, str], ...]:
    """Symbols whose drawn graphics overlap, measured from the library graphics.

    Each pair names the owners, sorted, one entry per overlapping pair of graphics. A power symbol or power flag
    is named by its prefix alone ("#PWR"): KiCad gives each a random number, so the same overlap in two
    candidates would otherwise never compare equal. Pins and their text are not graphics: MCP Pro's own overlap
    finding widens a symbol by a pin length beyond its pin tips and so reports a power symbol near a pin tip as
    overlapping the symbol it belongs to (a 7 mm gap in the amplifier fixture), and cannot tell candidates apart
    by it.
    """
    graphics = [item for item in scene.items if item.kind in _OVERLAP_GRAPHICS]
    pairs: list[tuple[str, str]] = []
    for index, first in enumerate(graphics):
        for second in graphics[index + 1:]:
            if first.owner != second.owner and _overlap(first, second, epsilon_mm):
                owners = sorted(_AUTO_REFERENCE.sub(r"\1", owner) for owner in (first.owner, second.owner))
                pairs.append((owners[0], owners[1]))
    return tuple(sorted(pairs))


def text_overlap_pairs(
    scene: GeometryScene, *, epsilon_mm: float = 0.05,
) -> tuple[tuple[str, str], ...]:
    """Visible text that sits on something else: another symbol's text, or another symbol's graphics.

    Text boxes are estimates, so the pairs are a comparison between layouts, not an exact count. A symbol's
    own text on its own graphics is by design and is not reported. Pin numbers and names that the library
    symbol hides are not in the scene, so they never appear here.
    """
    texts = [item for item in scene.items if item.kind in TEXT_KINDS]
    graphics = [item for item in scene.items if item.kind in _OVERLAP_GRAPHICS]

    def overlaps(a, b) -> bool:
        return _overlap(a, b, epsilon_mm)

    pairs: list[tuple[str, str]] = []
    for index, first in enumerate(texts):
        for second in texts[index + 1:]:
            if first.owner != second.owner and overlaps(first, second):
                pairs.append((first.id, second.id))
        for graphic in graphics:
            if graphic.owner != first.owner and overlaps(first, graphic):
                pairs.append((first.id, graphic.id))
    return tuple(pairs)


def composition_metrics(scene: GeometryScene) -> dict[str, object]:
    wires = [item for item in scene.items if item.kind == GeometryKind.WIRE]
    routed_length = 0.0
    orthogonal = True
    endpoint_axes: dict[tuple[float,float], list[str]] = {}
    for item in wires:
        if item.segment is None:
            orthogonal = False
            continue
        (ax, ay), (bx, by) = item.segment
        if abs(ax - bx) > 1e-6 and abs(ay - by) > 1e-6:
            orthogonal = False
        routed_length += abs(ax - bx) + abs(ay - by)
        if abs(ax - bx) < 1e-6 and abs(ay - by) > 1e-6:
            axis = "vertical"
        elif abs(ay - by) < 1e-6 and abs(ax - bx) > 1e-6:
            axis = "horizontal"
        else:
            continue
        for px, py in ((ax,ay),(bx,by)):
            key=(round(px,4),round(py,4))
            endpoint_axes.setdefault(key,[]).append(axis)

    # Only degree-two perpendicular wire vertices are bends. Three-way
    # junctions and four-way crossings must not be mistaken for doglegs.
    wire_corners=sum(
        len(axes)==2 and len(set(axes))==2
        for axes in endpoint_axes.values()
    )

    visible = [
        item for item in scene.items
        if item.kind in {
            GeometryKind.BODY, GeometryKind.POWER_GRAPHIC,
            GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT,
            GeometryKind.PIN_NAME, GeometryKind.PIN_NUMBER,
            GeometryKind.NET_LABEL, GeometryKind.WIRE,
        }
    ]
    width = (
        max(item.bounds.right for item in visible)
        - min(item.bounds.left for item in visible)
        if visible else 0.0
    )
    height = (
        max(item.bounds.bottom for item in visible)
        - min(item.bounds.top for item in visible)
        if visible else 0.0
    )
    return {
        "wire_length_mm": round(routed_length, 3),
        "wire_segment_count": len(wires),
        "wire_corner_count": wire_corners,
        "all_wires_measured_and_orthogonal": orthogonal,
        "drawing_width_mm": round(width, 3),
        "drawing_height_mm": round(height, 3),
        "drawing_bounding_area_mm2": round(width * height, 3),
        "estimated_annotation_count": sum(
            item.measurement == "estimated" for item in visible
        ),
        "text_overlap_count": len(text_overlap_pairs(scene)),
        "symbol_overlap_pairs": [list(pair) for pair in symbol_overlap_pairs(scene)],
        "assessment": "diagnostic_only",
    }
