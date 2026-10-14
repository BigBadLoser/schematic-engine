"""Feeding the placement refinement from a design.

``placement_refinement`` chooses where the symbols no relation places go and which way they face. It needs three things
a design does not state directly, and this module derives them without knowing a circuit:

* how every net will be drawn (a wire, stubs with labels, or power symbols), because a wire is a route to estimate, a
  labelled pin needs room for its stub and text, and a power pin room for its stub and symbol;
* how much room each pin needs in front of it, in the units the terminal planner draws with;
* which orientations a symbol may take: a connector any of the twelve, a two-pin part its four turns, anything else the
  one it has.

It also reads the geometry the server reports (``sch_symbol_geometry``) and turns a refinement back into what the
workflow writes. Pure: no I/O, no KiCad.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from .intent import NetIntent, SchematicIntent
from .kicad_geometry import text_width_mm
from .orientation import MIRRORS, ROTATIONS, Orientation
from .placement_refinement import (
    LABEL_STUB_MM, POWER_REACH_MM, WIRE_REACH_MM, ZONE_HALF_WIDTH_MM, Connection, OrientedGeometry, PinGeometry,
    PlacedSymbol, Refinement,
)

LABEL_TEXT_SIZE_MM = 1.524
# The text a power symbol shows, at KiCad's default size: what the terminal planner measures its footprint with.
POWER_TEXT_SIZE_MM = 1.27
# A ground symbol hangs from the wire above it and a supply symbol stands on the wire below: the way the host pin points
# when the symbol is entered along its own axis, in page coordinates (y grows down).
GROUND_FACING = (0.0, 1.0)
SUPPLY_FACING = (0.0, -1.0)
# A global label's flag, beyond its text.
GLOBAL_FLAG_MM = 2.0
# More than this in front of a pin is not worth reserving: a label that does not fit turns or is drawn shorter.
LABEL_REACH_CAP_MM = 15.24


def net_style(net: NetIntent, labelled: Callable[[NetIntent], bool]) -> str:
    """How a net will be drawn: ``power`` (power symbols), ``label`` (stubs and labels) or ``wire``."""
    if net.kind in ("power", "ground"):
        return "power"
    return "label" if labelled(net) else "wire"


def connections(
    intent: SchematicIntent, labelled: Callable[[NetIntent], bool], *, skip: Collection[str] = (),
) -> list[Connection]:
    """Every net of the design as a connection between pins, without the symbols in ``skip`` (power flags)."""
    result: list[Connection] = []
    for net in intent.all_nets:
        pins = tuple(
            (endpoint.reference, endpoint.pin) for endpoint in net.endpoints if endpoint.reference not in skip
        )
        if len(pins) >= 2 or (pins and net.kind in ("power", "ground")):
            style = net_style(net, labelled)
            facing = (GROUND_FACING if net.kind == "ground" or net.hangs else SUPPLY_FACING) if style == "power" else None
            result.append(Connection(net.name, pins, style, facing=facing))
    return result


def pin_reach(
    intent: SchematicIntent, labelled: Callable[[NetIntent], bool], *, skip: Collection[str] = (),
) -> dict[tuple[str, str], float]:
    """The room a pin needs in front of it, by what is drawn from it."""
    reach: dict[tuple[str, str], float] = {}
    for net in intent.all_nets:
        style = net_style(net, labelled)
        pins = [endpoint for endpoint in net.endpoints if endpoint.reference not in skip]
        if style == "wire":
            if len(pins) < 2:
                continue
            depth = WIRE_REACH_MM
        elif style == "power":
            depth = POWER_REACH_MM
        else:
            flag = GLOBAL_FLAG_MM if net.effective_scope == "global" else 0.0
            depth = min(LABEL_STUB_MM + text_width_mm(net.name, LABEL_TEXT_SIZE_MM) + flag, LABEL_REACH_CAP_MM)
        for endpoint in pins:
            key = (endpoint.reference, endpoint.pin)
            reach[key] = max(reach.get(key, 0.0), depth)
    return reach


def pin_half_widths(
    intent: SchematicIntent, labelled: Callable[[NetIntent], bool], *, skip: Collection[str] = (),
) -> dict[tuple[str, str], float]:
    """How wide the room in front of a pin is: a power symbol is as wide as the name it shows, anything else a wire."""
    widths: dict[tuple[str, str], float] = {}
    for net in intent.all_nets:
        if net_style(net, labelled) != "power":
            continue
        half = max(ZONE_HALF_WIDTH_MM, text_width_mm(net.name, POWER_TEXT_SIZE_MM) / 2.0)
        for endpoint in net.endpoints:
            if endpoint.reference not in skip:
                key = (endpoint.reference, endpoint.pin)
                widths[key] = max(widths.get(key, 0.0), half)
    return widths


def candidate_orientations(*, is_connector: bool, pin_count: int, planned: Orientation) -> list[Orientation]:
    """The orientations a movable symbol may take, always including the one it is planned to have.

    A connector may take them all, and so may a part of three or four pins (a transistor, a small regulator): turned, its
    pins face the parts they join, and the wire between is straight where a part drawn upright needs a bend (the buck
    converter's MOSFET, `auto-demo10`: a person turns it so that drain and source face the supply and the inductor).
    A part with more pins keeps the orientation it was planned to have, and so does one of two (it has no front)."""
    if is_connector or 3 <= pin_count <= 4:
        found = [Orientation(rotation, mirror) for rotation in ROTATIONS for mirror in MIRRORS]
    elif pin_count == 2:
        found = [Orientation(rotation) for rotation in ROTATIONS]
    else:
        found = []
    if planned not in found:
        found.append(planned)
    return found


def orientations_to_request(candidates: Mapping[str, Iterable[Orientation]]) -> dict[str, list[dict[str, Any]]]:
    return {
        reference: [{"rotation": o.rotation, "mirror": o.mirror} for o in orientations]
        for reference, orientations in candidates.items()
    }


def describe_symbol(
    x: float,
    y: float,
    orientations: Iterable[Orientation],
    pins_at: Callable[[Orientation], Mapping[str, tuple[float, float]]],
    normals_at: Callable[[Orientation], Mapping[str, tuple[float, float]]],
    box_at: Callable[[Orientation], tuple[float, float, float, float]],
    body_at: Callable[[Orientation], tuple[float, float, float, float]] | None = None,
) -> dict[str, Any]:
    """One symbol as ``sch_symbol_geometry`` reports it and ``symbols_from_payload`` reads it: its anchor, and for every
    orientation asked for each pin's point and outward direction and the box placement keeps clear, relative to the
    anchor."""
    described = []
    for orientation in orientations:
        points = pins_at(orientation)
        normals = normals_at(orientation)
        described.append({
            "rotation": orientation.rotation,
            "mirror": orientation.mirror,
            "pins": {
                str(pin): [
                    round(float(point[0]), 4), round(float(point[1]), 4),
                    round(float(normals.get(pin, (0.0, 0.0))[0]), 4), round(float(normals.get(pin, (0.0, 0.0))[1]), 4),
                ]
                for pin, point in points.items()
            },
            "box": [round(float(value), 4) for value in box_at(orientation)],
            **({"body": [round(float(value), 4) for value in body_at(orientation)]} if body_at is not None else {}),
        })
    return {"x": float(x), "y": float(y), "orientations": described}


def symbols_from_payload(
    payload: Mapping[str, Any],
    *,
    planned: Mapping[str, Orientation],
    movable: Collection[str],
    ordered: Collection[str] = (),
    windows: Mapping[str, float] | None = None,
    ranks: Mapping[str, int] | None = None,
) -> list[PlacedSymbol]:
    """The symbols the server described (``sch_symbol_geometry``), each at the orientation it is planned to have.

    ``windows`` limit how far (mm) a movable symbol may stray from where it stands: the support parts a relation placed."""
    symbols: list[PlacedSymbol] = []
    for reference in sorted(payload["symbols"]):
        entry = payload["symbols"][reference]
        geometry: dict[Orientation, OrientedGeometry] = {}
        for item in entry["orientations"]:
            orientation = Orientation(int(item["rotation"]) % 360, str(item.get("mirror") or ""))
            pins = {
                str(pin): PinGeometry((float(values[0]), float(values[1])), (float(values[2]), float(values[3])))
                for pin, values in item["pins"].items()
            }
            box = tuple(float(value) for value in item["box"])
            body = tuple(float(value) for value in item["body"]) if item.get("body") else None
            geometry[orientation] = OrientedGeometry(pins, box, body)  # type: ignore[arg-type]
        current = planned.get(reference, Orientation(0))
        if current not in geometry:
            raise ValueError(f"{reference} has no geometry at {current}")
        symbols.append(PlacedSymbol(
            reference, (float(entry["x"]), float(entry["y"])), current, geometry,
            movable=reference in movable, ordered=reference in ordered,
            window=(windows or {}).get(reference),
            flow_rank=(ranks or {}).get(reference),
        ))
    return symbols


def writes_for(refinement: Refinement) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, str]]:
    """What a refinement changes: anchor moves, rotations and mirrors ("" clears a mirror), by reference."""
    moves: list[dict[str, Any]] = []
    rotations: dict[str, int] = {}
    mirrors: dict[str, str] = {}
    for reference, placement in refinement.placements.items():
        moves.append({"reference": reference, "x_mm": placement.position[0], "y_mm": placement.position[1]})
        rotations[reference] = placement.orientation.rotation
        mirrors[reference] = placement.orientation.mirror
    return moves, rotations, mirrors
