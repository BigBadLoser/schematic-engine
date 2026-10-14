"""Ladder fixtures: a circuit's placement problem in one JSON file, readable with the repository alone.

A fixture holds everything a legaliser needs for one circuit and nothing that needs the sandbox or a recorded run: the
``Plan`` (see ``ladder_plan``), every symbol with its geometry in every orientation it may take, the nets, the room the
placement model keeps in front of pins, the roles summary, and the heuristic placer's placement as the baseline to beat.
``load`` rebuilds the objects::

    plan, symbols, nets, baseline = ladder_fixture.load("tests/fixtures/ladder/lm317.json")

``python -m backend.schematic.ladder_check FIXTURE PLACEMENT`` judges a placement against a fixture.

The file format (``"format": 1``)::

    {"format": 1, "name": "lm317", "source": "...", "gap_mm": 2.54, "slot_pitch_mm": 7.62,
     "plan": {...Plan.to_json()...},
     "symbols": [{"reference": "U1", "position": [x, y], "orientation": {"rotation": 0, "mirror": ""},
                  "movable": true, "ordered": false, "window": null, "flow_rank": null,
                  "orientation_cost": [[rotation, mirror, cost], ...] | null,
                  "geometry": [{"rotation": 0, "mirror": "", "box": [left, top, right, bottom],
                                "body": [left, top, right, bottom] | null,
                                "pins": {"1": [x, y, nx, ny]}}]}],
     "nets": [{"net": "out", "pins": [["U1", "2"], ["C3", "1"]], "style": "wire", "weight": 1.0, "facing": null}],
     "reach": [["U1", "2", 4.445]], "half_widths": [["U1", "2", 1.27]] | null,
     "text_widths": {"U1": 5.2},
     "roles": {"summary": "...", "rails": {...}, "trunks": [...], "chains": {...}, "attachments": {...},
               "rungs": {...}, "feedback": {...}, "pairs": [...], "other": [...]},
     "others": "row" | "keep", "unplaced": ["R21"],
     "baseline": {"U1": {"x": 63.5, "y": 50.8, "rotation": 0, "mirror": ""}}}

Geometry is relative to a symbol's anchor in millimetres, page y grows down, a pin's normal points out of the symbol.
``box`` is what placement keeps clear around a symbol, ``body`` the symbol without padding.  ``others`` says what to do
with the parts the roles leave as ``other`` (they are not placed by the ladder): ``row`` stands them in a row below the
placed ones, each under the parts it connects to (circuits read from a netlist have no pose for them; ``column``, the name
this had when they stood in a column at the left, is read as ``row``), ``keep`` leaves them where ``position`` says.  ``unplaced`` lists parts the plan places that the heuristic could not (a circuit whose baseline is incomplete: it found
no slot for them); the baseline lacks them and its counts stand them in the ``others`` row.  A placement file is ``{"placements": {"U1": {"x": 63.5, "y": 50.8, "rotation": 0, "mirror": ""}}}`` (the bare
mapping is accepted too).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .ladder_plan import Plan
from .orientation import Orientation
from .placement_refinement import Connection, OrientedGeometry, PinGeometry, PlacedSymbol, Placement, Route

FORMAT = 1


def _round(value: float) -> float:
    """The number as a float, exactly: the placement model breaks ties on float noise, so a rounded geometry scores differently."""
    return float(value)


def _box(box) -> list[float] | None:
    return None if box is None else [_round(v) for v in box]


# ---------------------------------------------------------------------------------------------------------------------
# objects -> JSON


def geometry_to_json(orientation: Orientation, geometry: OrientedGeometry) -> dict[str, Any]:
    return {
        "rotation": orientation.rotation, "mirror": orientation.mirror, "box": _box(geometry.box),
        "body": _box(geometry.body),
        "pins": {pin: [_round(g.point[0]), _round(g.point[1]), _round(g.normal[0]), _round(g.normal[1])]
                 for pin, g in geometry.pins.items()},
    }


def symbol_to_json(symbol: PlacedSymbol) -> dict[str, Any]:
    cost = symbol.orientation_cost
    return {
        "reference": symbol.reference, "position": [_round(symbol.position[0]), _round(symbol.position[1])],
        "orientation": {"rotation": symbol.orientation.rotation, "mirror": symbol.orientation.mirror},
        "movable": symbol.movable, "ordered": symbol.ordered,
        "window": None if symbol.window is None else _round(symbol.window), "flow_rank": symbol.flow_rank,
        "orientation_cost": None if cost is None else [[o.rotation, o.mirror, _round(v)] for o, v in cost.items()],
        "geometry": [geometry_to_json(o, g) for o, g in symbol.geometry.items()],
    }


def net_to_json(net: Connection) -> dict[str, Any]:
    return {
        "net": net.net, "pins": [list(pin) for pin in net.pins], "style": net.style, "weight": _round(net.weight),
        "facing": None if net.facing is None else [_round(net.facing[0]), _round(net.facing[1])],
    }


def wires_to_json(wires: Mapping[str, Sequence[Route]]) -> dict[str, list[dict[str, Any]]]:
    """The routes a legaliser drew (``Placements.wires``), per net, for a placement file's optional ``wires`` key."""
    return {net: [{"points": [[_round(x), _round(y)] for x, y in r.points], "bends": r.bends,
                   "length": _round(r.length), "pins": list(r.pins)} for r in routes]
            for net, routes in sorted(wires.items())}


def wires_from_json(data: Mapping[str, Any]) -> dict[str, list[Route]]:
    """The routes of a placement file's ``wires`` key (empty when it has none)."""
    return {net: [Route(tuple((float(x), float(y)) for x, y in r["points"]), r["bends"], float(r["length"]),
                        tuple(r.get("pins", ()))) for r in routes]
            for net, routes in data.get("wires", {}).items()}


def placements_to_json(placements: Mapping[str, Placement]) -> dict[str, dict[str, Any]]:
    return {ref: {"x": _round(p.position[0]), "y": _round(p.position[1]), "rotation": p.orientation.rotation,
                  "mirror": p.orientation.mirror} for ref, p in sorted(placements.items())}


def roles_to_json(roles: Any, summary: str = "") -> dict[str, Any]:
    """The roles as plain data (``Roles`` is a frozen dataclass of dicts, tuples and small frozen dataclasses)."""
    def item(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {name: item(getattr(value, name)) for name in value.__dataclass_fields__}
        if isinstance(value, Mapping):
            return {str(k): item(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [item(v) for v in value]
        return value

    data = item(roles)
    data["summary"] = summary
    return data


# ---------------------------------------------------------------------------------------------------------------------
# JSON -> objects


def geometry_from_json(entry: Mapping[str, Any]) -> tuple[Orientation, OrientedGeometry]:
    pins = {pin: PinGeometry((v[0], v[1]), (v[2], v[3])) for pin, v in entry["pins"].items()}
    body = entry.get("body")
    return (Orientation(int(entry["rotation"]), entry.get("mirror", "")),
            OrientedGeometry(pins, tuple(entry["box"]), None if body is None else tuple(body)))


def symbol_from_json(entry: Mapping[str, Any]) -> PlacedSymbol:
    geometry = dict(geometry_from_json(g) for g in entry["geometry"])
    cost = entry.get("orientation_cost")
    orientation = entry["orientation"]
    return PlacedSymbol(
        reference=entry["reference"], position=(entry["position"][0], entry["position"][1]),
        orientation=Orientation(int(orientation["rotation"]), orientation.get("mirror", "")), geometry=geometry,
        movable=bool(entry.get("movable", False)), ordered=bool(entry.get("ordered", False)),
        window=entry.get("window"),
        orientation_cost=None if cost is None else {Orientation(int(r), m): v for r, m, v in cost},
        flow_rank=entry.get("flow_rank"),
    )


def net_from_json(entry: Mapping[str, Any]) -> Connection:
    facing = entry.get("facing")
    return Connection(
        net=entry["net"], pins=tuple((ref, pin) for ref, pin in entry["pins"]), style=entry.get("style", "wire"),
        weight=entry.get("weight", 1.0), facing=None if facing is None else (facing[0], facing[1]),
    )


def placements_from_json(data: Mapping[str, Any]) -> dict[str, Placement]:
    """A placement file's mapping: reference -> ``Placement`` (the ``placements`` key is optional)."""
    entries = data["placements"] if "placements" in data else data
    return {
        ref: Placement((float(p["x"]), float(p["y"])), Orientation(int(p.get("rotation", 0)), p.get("mirror", "")))
        for ref, p in entries.items()
    }


def apply_placements(symbols: Sequence[PlacedSymbol], placements: Mapping[str, Placement]) -> list[PlacedSymbol]:
    """The symbols with the placed ones moved and turned; a placement naming an orientation a symbol lacks is an error."""
    moved = []
    for symbol in symbols:
        place = placements.get(symbol.reference)
        if place is None:
            moved.append(symbol)
            continue
        if place.orientation not in symbol.geometry:
            raise ValueError(f"{symbol.reference} has no orientation {place.orientation}")
        moved.append(replace(symbol, position=place.position, orientation=place.orientation))
    unknown = sorted(set(placements) - {s.reference for s in symbols})
    if unknown:
        raise ValueError(f"placement names parts the circuit does not have: {', '.join(unknown)}")
    return moved


# ---------------------------------------------------------------------------------------------------------------------
# the file


@dataclass
class Fixture:
    name: str
    plan: Plan
    symbols: list[PlacedSymbol]
    nets: list[Connection]
    baseline: dict[str, Placement]
    reach: dict[tuple[str, str], float] = field(default_factory=dict)
    half_widths: dict[tuple[str, str], float] | None = None
    text_widths: dict[str, float] = field(default_factory=dict)
    roles: dict[str, Any] = field(default_factory=dict)
    others: str = "keep"
    unplaced: tuple[str, ...] = ()
    gap_mm: float = 2.54
    slot_pitch_mm: float = 7.62
    source: str = ""

    @property
    def other(self) -> tuple[str, ...]:
        """The parts the roles leave to the caller (the ladder places none of them)."""
        return tuple(self.roles.get("other", ()))


def build_document(
    name: str, plan: Plan, symbols: Sequence[PlacedSymbol], nets: Sequence[Connection],
    baseline: Mapping[str, Placement], *, reach: Mapping[tuple[str, str], float] | None = None,
    half_widths: Mapping[tuple[str, str], float] | None = None, text_widths: Mapping[str, float] | None = None,
    roles: Any = None, roles_summary: str = "", others: str = "keep", gap_mm: float = 2.54,
    slot_pitch_mm: float = 7.62, source: str = "",
) -> dict[str, Any]:
    """The JSON-able document of a circuit's placement problem (see the module docstring for its format)."""
    if others not in ("keep", "row"):
        raise ValueError("others is 'keep' or 'row'")
    return {
        "format": FORMAT, "name": name, "source": source, "gap_mm": gap_mm, "slot_pitch_mm": slot_pitch_mm,
        "plan": plan.to_json(),
        "symbols": [symbol_to_json(s) for s in symbols],
        "nets": [net_to_json(n) for n in nets],
        "reach": [[ref, pin, _round(v)] for (ref, pin), v in (reach or {}).items()],
        "half_widths": None if half_widths is None else [[ref, pin, _round(v)] for (ref, pin), v in half_widths.items()],
        "text_widths": {ref: _round(v) for ref, v in sorted((text_widths or {}).items())},
        "roles": roles_to_json(roles, roles_summary) if roles is not None else {"summary": roles_summary},
        "others": others, "unplaced": sorted(plan.parts() - set(baseline)),
        "baseline": placements_to_json(baseline),
    }


def dumps(document: Mapping[str, Any]) -> str:
    """Compact JSON, one top-level key per line so a diff of two fixtures reads."""
    keys = list(document)
    body = ",\n".join(f"{json.dumps(k)}: {json.dumps(document[k], separators=(',', ':'))}" for k in keys)
    return "{\n" + body + "\n}\n"


def fixture_from_document(document: Mapping[str, Any]) -> Fixture:
    if document.get("format") != FORMAT:
        raise ValueError(f"unknown ladder fixture format {document.get('format')!r}")
    half = document.get("half_widths")
    return Fixture(
        name=document["name"], plan=Plan.from_json(document["plan"]),
        symbols=[symbol_from_json(s) for s in document["symbols"]], nets=[net_from_json(n) for n in document["nets"]],
        baseline=placements_from_json(document["baseline"]),
        reach={(ref, pin): v for ref, pin, v in document.get("reach", ())},
        half_widths=None if half is None else {(ref, pin): v for ref, pin, v in half},
        text_widths=dict(document.get("text_widths", {})), roles=dict(document.get("roles", {})),
        others="row" if document.get("others") == "column" else document.get("others", "keep"), unplaced=tuple(document.get("unplaced", ())), gap_mm=document.get("gap_mm", 2.54),
        slot_pitch_mm=document.get("slot_pitch_mm", 7.62), source=document.get("source", ""),
    )


def load_fixture(path: str | Path) -> Fixture:
    return fixture_from_document(json.loads(Path(path).read_text(encoding="utf-8")))


def load(path: str | Path) -> tuple[Plan, list[PlacedSymbol], list[Connection], dict[str, Placement]]:
    """``(plan, symbols, nets, baseline)`` of a fixture file; no sandbox and no recording needed."""
    fixture = load_fixture(path)
    return fixture.plan, fixture.symbols, fixture.nets, fixture.baseline


def load_wires(path: str | Path) -> dict[str, list[Route]]:
    """The wires a placement file records beside the placements, if it does."""
    return wires_from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def load_placement(path: str | Path) -> dict[str, Placement]:
    """A placement file (see the module docstring) as ``Placement`` objects."""
    return placements_from_json(json.loads(Path(path).read_text(encoding="utf-8")))
