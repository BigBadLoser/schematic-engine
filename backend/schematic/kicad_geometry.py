"""Read-only extraction of presentation geometry from a KiCad schematic.

Uses embedded KiCad symbol primitives; no MCP server, KiCad CLI, or external
parser dependency. Geometric strokes and pin locations are derived from the
saved document; font bounds are explicitly estimates, not authoritative.
Unresolved wire-to-net attribution is recorded rather than guessed.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from .presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene, flagged_label_bounds, label_text_bounds,
)

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')

def _decode(token: str) -> str:
    if token.startswith('"'):
        try:
            return json.loads(token)
        except (ValueError, UnicodeDecodeError):
            return token[1:-1]
    return token


def parse_kicad_sexpr(source: str) -> list[Any]:
    """Parse the subset of KiCad's S-expression document needed for geometry."""
    stack: list[list[Any]] = []
    root: list[Any] | None = None
    for match in _TOKEN.finditer(source):
        token = match.group()
        if token == "(":
            value: list[Any] = []
            if stack:
                stack[-1].append(value)
            stack.append(value)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced KiCad S-expression")
            root = stack.pop()
        else:
            if not stack:
                raise ValueError("S-expression atom outside list")
            stack[-1].append(_decode(token))
    if stack or not root or root[0] != "kicad_sch":
        raise ValueError("Invalid or incomplete KiCad schematic")
    return root


def _children(node: list[Any], tag: str) -> list[list[Any]]:
    return [
        child for child in node
        if isinstance(child, list) and child and child[0] == tag
    ]


def _one(node: list[Any], tag: str) -> list[Any] | None:
    return next(iter(_children(node, tag)), None)


def _float(value: Any) -> float:
    return float(str(value))


def _coord(node: list[Any] | None) -> tuple[float, float, float]:
    if node is None or len(node) < 3:
        raise ValueError("Missing schematic coordinates")
    return _float(node[1]), _float(node[2]), (
        _float(node[3]) if len(node) > 3 else 0.0
    )


def _property(symbol: list[Any], key: str) -> list[Any] | None:
    return next((p for p in _children(symbol, "property") if len(p) > 2 and p[1] == key), None)


def _hidden(node: list[Any]) -> bool:
    effects = _one(node, "effects")
    if _one(node, "hide") is not None:
        return True
    return effects is not None and _one(effects, "hide") is not None


def _symbol_hides(definition: list[Any], tag: str) -> bool:
    """Does the library symbol hide all pin numbers (``pin_numbers``) or names (``pin_names``)?

    KiCad writes ``(pin_numbers (hide yes))`` and, in older files, ``(pin_numbers hide)``. Power
    symbols, resistors and capacitors all use it, so the pins' own nodes say nothing about it.
    """
    node = _one(definition, tag)
    if node is None:
        return False
    if "hide" in node:
        return True
    flag = _one(node, "hide")
    return flag is not None and (len(flag) < 2 or str(flag[1]).lower() in ("yes", "true"))


def _size(node: list[Any] | None, default: float = 1.27) -> float:
    effects = _one(node, "effects") if node else None
    font = _one(effects, "font") if effects else None
    size = _one(font, "size") if font else None
    return _float(size[2]) if size and len(size) > 2 else default


def _text_width(text: str, size: float) -> float:
    # KiCad's stroke font is not a fixed-width font. Calibrated against KiCad's own rendering of the
    # fixtures (FINDINGS section 11): about 0.87 of the text height per character, less a fifth of a
    # character of side bearing. The earlier 0.65 per character made every box about a quarter too narrow.
    return max(0.4, size * (0.87 * len(text) - 0.22))


def text_width_mm(text: str, size: float) -> float:
    """How wide KiCad draws ``text`` at ``size`` mm (calibrated; see _text_width)."""
    return _text_width(text, size)


def _pin_name_offset(definition: list[Any]) -> float:
    node = _one(definition, "pin_names")
    offset = _one(node, "offset") if node else None
    return _float(offset[1]) if offset and len(offset) > 1 else 0.508


def _justify(node: list[Any] | None) -> frozenset[str]:
    """KiCad's justification of a property's text about its anchor: any of left, right, top, bottom."""
    effects = _one(node, "effects") if node else None
    justify = _one(effects, "justify") if effects else None
    return frozenset(str(token) for token in justify[1:]) if justify else frozenset()


# The text's own axes in page space for each absolute angle: along the text, and towards the tops of the letters.
# Measured against KiCad's rendering of every rotation, mirror, field angle and justification (the sandbox's
# ``field_justify`` probe): the justification is applied in the text's frame, the frame turns with the absolute angle
# (the field's angle plus its symbol's rotation), and a mirrored symbol then flips the box across the anchor.
_TEXT_FRAMES = {
    0: ((1.0, 0.0), (0.0, -1.0)),
    90: ((0.0, -1.0), (-1.0, 0.0)),
    180: ((-1.0, 0.0), (0.0, 1.0)),
    270: ((0.0, 1.0), (1.0, 0.0)),
}


def text_box(
    x: float, y: float, text: str, size: float, angle: float, justify: frozenset[str] = frozenset(),
    mirror: str | None = None,
) -> Bounds:
    """The box of ``text`` anchored at (x, y), as KiCad draws it. An estimate, not a measurement.

    ``angle`` is the absolute angle the text is drawn at (the field's angle plus its symbol's rotation) and ``mirror``
    the symbol's mirror axis. ``justify`` says which edge of the text sits on the anchor, in the text's own frame
    (left or right along it, top or bottom across it); an axis without a word is centred on the anchor.
    """
    width = _text_width(text, size)
    height = max(0.4, size)
    along = (0.0, width) if "left" in justify else (-width, 0.0) if "right" in justify else (-width / 2, width / 2)
    across = (-height, 0.0) if "top" in justify else (0.0, height) if "bottom" in justify else (-height / 2, height / 2)
    (ux, uy), (vx, vy) = _TEXT_FRAMES[int(round(angle / 90.0)) % 4 * 90]
    corners = [
        (a * ux + b * vx, a * uy + b * vy) for a in along for b in across
    ]
    if mirror == "x":
        corners = [(cx, -cy) for cx, cy in corners]
    elif mirror == "y":
        corners = [(-cx, cy) for cx, cy in corners]
    return Bounds(
        x + min(c[0] for c in corners), y + min(c[1] for c in corners),
        x + max(c[0] for c in corners), y + max(c[1] for c in corners),
    )


def _estimate_text(x: float, y: float, text: str, size: float, angle: float) -> Bounds:
    # Still an estimate, not a measurement: a box around the text, centred on (x, y).
    return text_box(x, y, text, size, angle)


def _bbox(points: list[tuple[float, float]]) -> Bounds:
    return Bounds(min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points))


def _transform(x: float, y: float, cx: float, cy: float, angle: float,
               mirror: str | None) -> tuple[float, float]:
    # Library coordinates are Y-up; schematic page coordinates are Y-down. KiCad rotates a symbol first and
    # mirrors it afterwards, in page space: the two do not commute for a quarter turn, and mirroring first put every
    # mirrored symbol rotated 90 or 270 degrees on the wrong side (FINDINGS section 10).
    radians = math.radians(angle)
    rotated_x = x * math.cos(radians) - y * math.sin(radians)
    rotated_y = x * math.sin(radians) + y * math.cos(radians)
    if mirror == "x":
        rotated_y = -rotated_y
    elif mirror == "y":
        rotated_x = -rotated_x
    return (cx + rotated_x, cy - rotated_y)


def _graphics(definition: list[Any], unit: int) -> tuple[list[list[Any]], list[list[Any]]]:
    shapes: list[list[Any]] = []
    pins: list[list[Any]] = []
    for group in _children(definition, "symbol"):
        if len(group) < 2:
            continue
        suffix = str(group[1]).rsplit("_", 2)
        if len(suffix) != 3:
            continue
        try:
            group_unit = int(suffix[-2])
        except ValueError:
            continue
        if group_unit not in (0, unit):
            continue
        shapes.extend(
            child for child in group
            if isinstance(child, list) and child and child[0] in {
                "rectangle", "polyline", "circle", "arc",
            }
        )
        pins.extend(_children(group, "pin"))
    return shapes, pins


def _shape_points(shape: list[Any]) -> list[tuple[float, float]]:
    tag = shape[0]
    if tag == "rectangle":
        start, end = _one(shape, "start"), _one(shape, "end")
        if start and end:
            ax, ay, _ = _coord(start)
            bx, by, _ = _coord(end)
            return [(ax, ay), (ax, by), (bx, ay), (bx, by)]
    if tag == "polyline":
        pts = _one(shape, "pts")
        return [_coord(p)[:2] for p in _children(pts, "xy")] if pts else []
    if tag == "circle":
        center, radius = _one(shape, "center"), _one(shape, "radius")
        if center and radius:
            x, y, _ = _coord(center)
            r = _float(radius[1])
            return [(x-r, y-r), (x-r, y+r), (x+r, y-r), (x+r, y+r)]
    if tag == "arc":
        return [
            _coord(p)[:2] for p in (
                _one(shape, "start"), _one(shape, "mid"), _one(shape, "end")
            ) if p
        ]
    return []


_INDEXED_OWNER = re.compile(r"^(wire|junction|label|global_label|hierarchical_label):(\d+)$")


def _canonical_items(items: list[GeometryItem]) -> list[GeometryItem]:
    """Items in an order independent of the sheet's document order (KiCad orders symbols by random UUID).

    Sort by geometry, then renumber the document-index based wire/junction/label ids in that order."""
    def key(item: GeometryItem) -> tuple:
        owner = "" if _INDEXED_OWNER.match(item.owner) else item.owner
        b = item.bounds
        seg = item.segment
        return (
            item.kind.value, owner,
            tuple(round(v, 3) for v in (b.left, b.top, b.right, b.bottom)),
            item.text or "",
            tuple(round(v, 3) for point in seg for v in point) if seg else (),
        )

    ordered = sorted(items, key=key)
    renamed: dict[str, str] = {}
    counters: Counter[str] = Counter()
    for item in ordered:
        match = _INDEXED_OWNER.match(item.owner)
        if match and item.owner not in renamed:
            renamed[item.owner] = f"{match.group(1)}:{counters[match.group(1)]}"
            counters[match.group(1)] += 1
    out: list[GeometryItem] = []
    for item in ordered:
        new_owner = renamed.get(item.owner)
        if new_owner is None:
            out.append(item)
        else:
            tail = item.id[len(item.owner):]  # wires keep their ":segment" suffix
            out.append(replace(item, id=new_owner + tail, owner=new_owner))
    return out


def measured_scene_from_kicad(
    path: Path, *, netlist_xml: str | None = None,
) -> tuple[GeometryScene, dict[str, Any]]:
    doc = parse_kicad_sexpr(path.read_text(encoding="utf-8"))
    libraries = _one(doc, "lib_symbols")
    definitions = {
        str(s[1]): s
        for s in (_children(libraries, "symbol") if libraries else [])
        if len(s) >= 2
    }
    items: list[GeometryItem] = []
    unknown_libraries: set[str] = set()
    missing_symbol_graphics = 0

    for index, symbol in enumerate(_children(doc, "symbol")):
        lib = _one(symbol, "lib_id")
        at = _one(symbol, "at")
        if not lib or not at:
            continue
        library_id = str(lib[1])
        ref_prop = _property(symbol, "Reference")
        ref = str(ref_prop[2]) if ref_prop is not None else f"symbol:{index}"
        is_power = library_id.startswith("power:")
        cx, cy, angle = _coord(at)
        mirror_node = _one(symbol, "mirror")
        mirror = str(mirror_node[1]) if mirror_node and len(mirror_node) > 1 else None
        u = _one(symbol, "unit")
        unit = int(u[1]) if u else 1
        definition = definitions.get(library_id)
        if definition is None:
            unknown_libraries.add(library_id)
        else:
            shapes, pins = _graphics(definition, unit)
            numbers_hidden = _symbol_hides(definition, "pin_numbers")
            names_hidden = _symbol_hides(definition, "pin_names")
            body_points: list[tuple[float, float]] = []
            for primitive in shapes:
                body_points.extend(
                    _transform(x, y, cx, cy, angle, mirror)
                    for x, y in _shape_points(primitive)
                )
            power_net = (
                str(_property(symbol, "Value")[2]) if is_power and _property(symbol, "Value") is not None else None
            )
            if body_points:
                items.append(GeometryItem(
                    f"{ref}:graphic", K.POWER_GRAPHIC if is_power else K.BODY,
                    ref, _bbox(body_points), net=power_net, text=library_id,
                    anchor=(cx, cy), angle=angle, mirror=mirror,
                    measurement=(
                        "estimated"
                        if mirror is not None or any(p[0] == "arc" for p in shapes)
                        else "measured"
                    ),
                ))
            else:
                missing_symbol_graphics += 1
            for pin_index, pin in enumerate(pins):
                pin_at = _one(pin, "at")
                pin_len = _one(pin, "length")
                pin_number = _one(pin, "number")
                pin_name = _one(pin, "name")
                if pin_at is None:
                    continue
                px, py, pin_angle = _coord(pin_at)
                length = _float(pin_len[1]) if pin_len else 0.0
                endx = px + length * math.cos(math.radians(pin_angle))
                endy = py + length * math.sin(math.radians(pin_angle))
                origin = _transform(px, py, cx, cy, angle, mirror)
                inner = _transform(endx, endy, cx, cy, angle, mirror)
                number = str(pin_number[1]) if pin_number else str(pin_index)
                pin_id = f"{ref}.{number}"
                items.append(GeometryItem(
                    f"{pin_id}:terminal", K.PIN_ENDPOINT, ref,
                    Bounds(origin[0], origin[1], origin[0], origin[1]), net=power_net,
                ))
                items.append(GeometryItem(
                    f"{pin_id}:stroke", K.PIN_STROKE, ref, _bbox([origin, inner]),
                ))
                if (
                    pin_name and len(pin_name) > 1 and pin_name[1] not in ("", "~")
                    and not names_hidden and not _hidden(pin_name)
                ):
                    # A pin name is drawn inside the body, starting past the pin's inner end by the
                    # symbol's pin-name offset and running inward, along the pin.
                    name_size = _size(pin_name)
                    run = _text_width(str(pin_name[1]), name_size)
                    ux, uy = inner[0] - origin[0], inner[1] - origin[1]
                    norm = math.hypot(ux, uy)
                    ux, uy = (ux / norm, uy / norm) if norm > 1e-9 else (0.0, 0.0)
                    reach = _pin_name_offset(definition) + run / 2
                    items.append(GeometryItem(
                        f"{pin_id}:name", K.PIN_NAME, ref,
                        _estimate_text(
                            inner[0] + ux * reach, inner[1] + uy * reach,
                            str(pin_name[1]), name_size, angle + pin_angle,
                        ),
                        measurement="estimated",
                    ))
                if pin_number and not numbers_hidden and not _hidden(pin_number):
                    # A pin number is drawn beside the pin's midpoint: above a horizontal pin, to the
                    # left of a vertical one.
                    number_size = _size(pin_number)
                    midx, midy = ((origin[0] + inner[0])/2, (origin[1] + inner[1])/2)
                    shift = number_size * 0.5 + 0.3
                    if abs(inner[1] - origin[1]) <= abs(inner[0] - origin[0]):
                        midy -= shift
                    else:
                        midx -= shift
                    items.append(GeometryItem(
                        f"{pin_id}:number", K.PIN_NUMBER, ref,
                        _estimate_text(midx, midy, number, number_size, angle + pin_angle),
                        measurement="estimated",
                    ))

        value_prop = _property(symbol, "Value")
        power_name = str(value_prop[2]) if is_power and value_prop is not None else None
        for prop in _children(symbol, "property"):
            if len(prop) < 3 or prop[1] not in ("Reference", "Value") or _hidden(prop):
                continue
            if not str(prop[2]):
                continue
            prop_at = _one(prop, "at")
            if prop_at is None:
                continue
            x, y, field_angle = _coord(prop_at)
            field_type = K.POWER_TEXT if is_power else (
                K.REFERENCE if prop[1] == "Reference" else K.VALUE
            )
            # KiCad stores a field's angle relative to its symbol: a connector placed at 90 degrees with a
            # field at 0 draws that text vertically (measured against KiCad's own rendering).
            items.append(GeometryItem(
                f"{ref}:field:{prop[1]}", field_type, ref,
                text_box(x, y, str(prop[2]), _size(prop), field_angle + angle, _justify(prop), mirror),
                net=power_name, measurement="estimated",
                anchor=(x, y), text=str(prop[2]), angle=field_angle + angle,
            ))

    for index, wire in enumerate(_children(doc, "wire")):
        pts = _one(wire, "pts")
        if not pts:
            continue
        coords = [_coord(p)[:2] for p in _children(pts, "xy")]
        for j, (start, end) in enumerate(zip(coords, coords[1:])):
            if start[0] != end[0] and start[1] != end[1]:
                continue  # KiCad wires must be orthogonal for this model.
            items.append(GeometryItem(
                f"wire:{index}:{j}", K.WIRE, f"wire:{index}",
                _bbox([start, end]), segment=(start, end),
            ))

    for index, junction in enumerate(_children(doc, "junction")):
        at = _one(junction, "at")
        if at:
            x, y, _ = _coord(at)
            items.append(GeometryItem(
                f"junction:{index}", K.JUNCTION, f"junction:{index}",
                Bounds(x-0.25, y-0.25, x+0.25, y+0.25),
                measurement="estimated",
            ))

    for tag in ("label", "global_label", "hierarchical_label"):
        for index, label in enumerate(_children(doc, tag)):
            at = _one(label, "at")
            if not at or len(label) < 2:
                continue
            x, y, angle = _coord(at)
            size = _size(label)
            width = _text_width(str(label[1]), size)
            quarter = int(round(angle)) % 360
            if tag == "label":
                box = label_text_bounds(x, y, width, size, vertical=quarter % 180 == 90)
            else:
                # KiCad justifies a global label's text away from its flag: 0 runs right, 90 up, 180 left, 270 down.
                heading = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}.get(quarter, (1.0, 0.0))
                box = flagged_label_bounds(x, y, width, size, heading)
            items.append(GeometryItem(
                f"{tag}:{index}", K.NET_LABEL, f"{tag}:{index}", box,
                net=str(label[1]), measurement="estimated",
                anchor=(x, y), text=str(label[1]), angle=float(quarter),
            ))

    items = _canonical_items(items)

    # A part drawn in several units has one symbol per unit under one reference, so ids repeat; later ones get a suffix.
    seen: Counter[str] = Counter()
    unique: list[GeometryItem] = []
    for item in items:
        seen[item.id] += 1
        unique.append(item if seen[item.id] == 1 else replace(item, id=f"{item.id}~{seen[item.id]}"))
    scene = GeometryScene(tuple(unique))
    counts = Counter(item.kind.value for item in scene.items)
    notes = {
        "status": "observational_only",
        "units": "millimetres",
        "counts_by_kind": dict(sorted(counts.items())),
        "estimated_items": sum(i.measurement == "estimated" for i in scene.items),
        "unknown_libraries": sorted(unknown_libraries),
        "missing_symbol_graphics": missing_symbol_graphics,
        "wire_net_attribution": "not_yet_available",
        "attachment_inference": "not_yet_available",
        "geometry_qa_gate": "not_enabled",
        "font_bounds": "approximate; not a KiCad glyph measurement",
        "note": (
            "Snapshot contains real KiCad coordinates and vector bodies/pins, "
            "but estimated text and unresolved wire-net ownership make raw "
            "collision counts unsuitable as a production quality metric."
        ),
    }
    if netlist_xml is not None:
        # Import lazily to avoid forcing KiCad CLI dependency on geometry-only
        # consumers. Raw wire topology alone is never accepted as net evidence.
        from .net_attribution import attribute_wire_nets
        attributed = attribute_wire_nets(scene, netlist_xml=netlist_xml)
        scene = attributed.scene
        notes.update(attributed.coverage)
        notes["attachment_inference"] = "explicit_pin_and_junction_with_xml"
        notes["note"] = (
            "Exported KiCad XML identities are attached only to unambiguous "
            "measured wire islands; unknown wire segments remain foreign."
        )
    return scene, notes
