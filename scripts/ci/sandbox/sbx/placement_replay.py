"""The placement solvers of a recorded run, run again offline: other weights, more rounds, more orientations.

Overlay ``e11_placement_inputs`` records the arguments of every call to ``seed_placement``, ``align_groups`` and
``refine_placement`` (``dumps/placement-<n>-<solver>-<ns>.pkl``). The calls of one circuit come in threes, in the order
the engine makes them. This module reads them back and runs the stages again in seconds, with no MCP Pro, no KiCad and no
libavoid: the way the buck converter's bends were traced to a block aligned before its inductor was turned, a search window
too small to reach the row, and a cost that did not know a 1.27 mm leg for what it is (`FINDINGS.md` section 15).

It judges by the placement's own estimate of the routes (bends, jogs, wire, extent), which is what the solvers optimise and
not what the router draws: the picture of a layout is for looking at, and a change is only kept after a real run.
"""
from __future__ import annotations

import glob
import pickle
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import env

if str(env.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(env.REPO_ROOT))
from backend.schematic import placement_refinement as solvers  # noqa: E402
from backend.schematic.orientation import MIRRORS, ROTATIONS, Orientation  # noqa: E402

SOLVERS = ("seed", "align", "refine")


@dataclass
class Row:
    label: str
    total: float
    length: float
    bends: float
    jogs: int
    crossings: int
    extent: tuple[float, float]


@dataclass
class Replay:
    rows: list[Row] = field(default_factory=list)
    symbols: list[Any] = field(default_factory=list)
    nets: list[Any] = field(default_factory=list)
    model: Any = None


def _stamp(path: str) -> int:
    return int(Path(path).stem.rsplit("-", 1)[1])


def load_circuits(run_dir: Path) -> list[dict[str, dict[str, Any]]]:
    """The recorded calls of each circuit of a run: ``{"seed": call, "align": call, "refine": call}``, in run order."""
    files = sorted(glob.glob(str(Path(run_dir) / "dumps" / "placement-*.pkl")), key=_stamp)
    if not files:
        raise FileNotFoundError(f"no placement-*.pkl dumps in {run_dir}/dumps; run with the e11_placement_inputs overlay")
    calls = [pickle.loads(Path(name).read_bytes()) for name in files]
    # A run with `--jobs N` builds fixtures side by side and their dumps interleave in time, so the calls are grouped by
    # the circuit they are about (its references) before the order within each circuit is read.
    by_circuit: dict[frozenset[str], list[dict[str, Any]]] = {}
    for call in calls:
        by_circuit.setdefault(_references(call), []).append(call)
    circuits: list[dict[str, dict[str, Any]]] = []
    for group in by_circuit.values():
        current: dict[str, dict[str, Any]] | None = None
        for call in group:
            if current is None or call["solver"] == "seed" or call["solver"] in current:
                current = {}
                circuits.append(current)
            current[call["solver"]] = call
    return circuits


def _references(call: dict[str, Any]) -> frozenset[str]:
    """The circuit a recorded call is about: the references of the symbols it was given."""
    symbols = call["args"][0] if call.get("args") else ()
    return frozenset(getattr(s, "reference", "") for s in symbols if not getattr(s, "reference", "#").startswith("#"))


def widen_orientations(symbols: list[Any], maximum_pins: int) -> list[Any]:
    """Give each movable symbol of three to ``maximum_pins`` pins every orientation, from its upright geometry."""
    widened = []
    for symbol in symbols:
        upright = symbol.geometry.get(Orientation(0, ""))
        if symbol.movable and upright is not None and 3 <= len(upright.pins) <= maximum_pins:
            geometry = dict(symbol.geometry)
            for rotation in ROTATIONS:
                for mirror in MIRRORS:
                    geometry.setdefault(Orientation(rotation, mirror), solvers.rotate_geometry(upright, rotation, mirror))
            symbol = replace(symbol, geometry=geometry)
        widened.append(symbol)
    return widened


def parse_weights(text: str | None) -> dict[str, float]:
    changes: dict[str, float] = {}
    for item in (text or "").split(","):
        if item.strip():
            name, _, value = item.partition("=")
            changes[name.strip()] = float(value)
    return changes


def _describe(label: str, symbols: list[Any], nets: list[Any], reach: Any, half_widths: Any, weights: Any) -> tuple[Row, Any]:
    model = solvers._Model(symbols, nets, reach, weights, half_widths)
    cost = model.cost({s.reference: (s.position, s.orientation) for s in symbols})
    boxes = [model.world(s.reference, s.position, s.orientation)[1] for s in symbols]
    extent = (max(b[2] for b in boxes) - min(b[0] for b in boxes), max(b[3] for b in boxes) - min(b[1] for b in boxes))
    return Row(label, cost.total, cost.length, cost.bends, cost.jogs, cost.crossings, extent), model


def _apply(symbols: list[Any], result: Any) -> list[Any]:
    moved = []
    for symbol in symbols:
        place = result.placements.get(symbol.reference)
        moved.append(replace(symbol, position=place.position, orientation=place.orientation) if place else symbol)
    return moved


def replay(
    circuit: dict[str, dict[str, Any]], *, weights: dict[str, float] | None = None, rounds: int = 0,
    stages: tuple[str, ...] = ("refine",), orient: int = 0,
) -> Replay:
    """Run ``stages`` of one circuit from the state the refinement was given, then ``rounds`` of align + refine."""
    refine = circuit["refine"]
    weighting = replace(solvers.Weights(), **weights) if weights else solvers.Weights()
    symbols = list(refine["args"][0])
    if orient:
        symbols = widen_orientations(symbols, orient)
    nets = refine["args"][1]
    reach = refine["kwargs"].get("reach") or {}
    half_widths = refine["kwargs"].get("half_widths")
    result = Replay(nets=nets)
    row, model = _describe("start", symbols, nets, reach, half_widths, weighting)
    result.rows.append(row)
    plan = [list(stages)] + [["align", "refine"]] * rounds
    for number, names in enumerate(plan):
        for name in names:
            if name not in circuit:                # the engine did not make that call (one block, nothing free to seed)
                continue
            kwargs = {**circuit[name]["kwargs"], "weights": weighting}
            if name == "align":
                kwargs.update(reach=reach, half_widths=half_widths)
                outcome = solvers.align_groups(symbols, nets, **kwargs)
            elif name == "seed":
                outcome = solvers.seed_placement(symbols, nets, **kwargs)
            else:
                outcome = solvers.refine_placement(symbols, nets, **kwargs)
            symbols = _apply(symbols, outcome)
            row, model = _describe(f"round {number} {name}", symbols, nets, reach, half_widths, weighting)
            result.rows.append(row)
    result.symbols, result.model = symbols, model
    return result


def format_replay(result: Replay) -> str:
    lines = [f"{'stage':18} {'extent':>12} {'wire mm':>8} {'bends':>6} {'jogs':>5} {'crossings':>9} {'cost':>9}"]
    for row in result.rows:
        lines.append(
            f"{row.label:18} {row.extent[0]:5.1f}x{row.extent[1]:5.1f} {row.length:8.1f} {row.bends:6.1f} {row.jogs:5d} "
            f"{row.crossings:9d} {row.total:9.2f}"
        )
    for symbol in sorted(result.symbols, key=lambda s: s.reference):
        lines.append(
            f"  {symbol.reference:6} ({symbol.position[0]:7.2f},{symbol.position[1]:7.2f}) rotation {symbol.orientation.rotation:3d}"
            f" mirror {symbol.orientation.mirror or '-'}"
        )
    return "\n".join(lines)


def svg(result: Replay, label: str = "") -> str:
    """The layout as the placement sees it: each symbol's body and pin tips, and the routes it estimates."""
    scale = 6.0
    worlds = {s.reference: result.model.world(s.reference, s.position, s.orientation) for s in result.symbols}
    xs = [v for _p, _b, body in worlds.values() for v in (body[0], body[2])]
    ys = [v for _p, _b, body in worlds.values() for v in (body[1], body[3])]
    margin = 8.0
    left, top = min(xs) - margin, min(ys) - margin
    width, height = (max(xs) - min(xs) + 2 * margin) * scale, (max(ys) - min(ys) + 2 * margin) * scale + 24
    colors = ("#c0392b", "#2980b9", "#27ae60", "#8e44ad", "#d35400", "#16a085", "#7f8c8d", "#2c3e50")

    def at(x: float, y: float) -> tuple[float, float]:
        return (x - left) * scale, (y - top) * scale + 24

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" font-family="sans-serif">',
        '<rect width="100%" height="100%" fill="white"/>', f'<text x="6" y="14" font-size="12">{label}</text>',
    ]
    for reference, (pins, _box, body) in sorted(worlds.items()):
        x, y = at(body[0], body[1])
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{(body[2] - body[0]) * scale:.1f}" height="{(body[3] - body[1]) * scale:.1f}"'
            ' fill="#fdf2c8" stroke="#8b0000"/>'
        )
        out.append(f'<text x="{x:.1f}" y="{y - 2:.1f}" font-size="10" fill="#006060">{reference}</text>')
        for tip, normal in pins.values():
            px, py = at(*tip)
            qx, qy = at(tip[0] + normal[0] * 1.5, tip[1] + normal[1] * 1.5)
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2" fill="#8b0000"/>')
            out.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{qx:.1f}" y2="{qy:.1f}" stroke="#8b0000"/>')
    for number, net in enumerate(result.model.nets):
        if net.style != "wire":
            continue
        tips = [(worlds[ref][0][pin][0], worlds[ref][0][pin][1]) for ref, pin in net.pins]
        for route in result.model.net_routes(tips):
            points = " ".join("{:.1f},{:.1f}".format(*at(*p)) for p in route.points)
            out.append(f'<polyline points="{points}" fill="none" stroke="{colors[number % len(colors)]}" stroke-width="2" opacity="0.8"/>')
    out.append("</svg>")
    return "\n".join(out)
