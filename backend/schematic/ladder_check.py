"""Judge a ladder placement against a fixture: the placement model's counts, a legality verdict and the plan's conformance.

Usage (repository only, no sandbox, no recordings)::

    python -m backend.schematic.ladder_check tests/fixtures/ladder/lm317.json my_placement.json [--json]

``my_placement.json`` holds ``{"placements": {"U1": {"x": 63.5, "y": 50.8, "rotation": 0, "mirror": ""}, ...}}`` (the bare
mapping is accepted): the anchor of every part the plan places, in mm, page y down, and the orientation as a rotation
(0, 90, 180, 270) and a mirror ("", "x", "y") the fixture's symbol offers.  Parts the plan does not place keep the
fixture's positions (the parts the roles leave as ``other`` stand in a row below the placed ones when the fixture says
so, as the sandbox harness does for circuits read from a netlist).

It prints, for the candidate and for the fixture's baseline (the heuristic placer's placement) side by side, the counts
``sandbox.py ladder`` prints, taken from ``placement_refinement._Model.cost`` exactly as the harness does: ``through``
(estimated routes through a body), ``overlaps``, ``clearance`` (a symbol in the room in front of a pin), ``short`` (a
route over the pin of another net), ``bends``, ``jogs``, ``crossings``, ``wire`` (estimated wire length, mm) and
``extent`` (mm).  Then the verdict on the candidate:

legality   hard rules of a drawable placement: every part the plan places is placed in an orientation it has, no two
           bodies overlap (a body is the box less the pin lead on each side a pin points through), no two parts' pin tips
           coincide, and the plan's rows are in order top to bottom.  Exit status 1 when any fails.
conformance what the plan asks beyond legality, reported, not fatal: orientations as planned, the parts of a chain row at
           one height and in chain order, a hung part's tip on its wire with its body beyond the wire on its side, hangs
           ranked by distance to their host pin, tips at least 2.54 mm apart, bodies a gap apart.  A placement that
           differs from the plan in these has chosen to; the legaliser's contract (section 4b of the spec) says it must
           not.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import ladder_fixture, placement_refinement as solvers
from .ladder_plan import Plan, attach_unplanned, body_box, plan_routes
from .placement_refinement import Connection, OrientedGeometry, PlacedSymbol, Placement

_EPS = 1e-6
TIP_SPACING_MM = 2.54


@dataclass
class Counts:
    label: str
    through: int
    overlaps: int
    clearance: int
    short: int
    bends: float
    jogs: int
    crossings: int
    wire_mm: float
    extent: tuple[float, float]
    total: float

    def as_json(self) -> dict[str, Any]:
        return {"through": self.through, "overlaps": self.overlaps, "clearance": self.clearance, "short": self.short,
                "bends": self.bends, "jogs": self.jogs, "crossings": self.crossings, "wire_mm": round(self.wire_mm, 2),
                "extent_mm": [round(self.extent[0], 2), round(self.extent[1], 2)], "total": round(self.total, 2)}


@dataclass
class Verdict:
    legal: list[str] = field(default_factory=list)          # hard failures
    conformance: list[str] = field(default_factory=list)    # departures from the plan

    @property
    def ok(self) -> bool:
        return not self.legal


def counts(label: str, symbols: Sequence[PlacedSymbol], nets: Sequence[Connection],
           reach: Mapping[tuple[str, str], float], half_widths: Mapping[tuple[str, str], float] | None,
           plan: Plan | None = None, wires: Mapping[str, Sequence[Any]] | None = None) -> Counts:
    """The placement model's counts for a pose, as ``sbx.ladder.describe`` takes them (default weights).

    With a ``plan`` the nets it wires are scored on the plan's own wires (``plan_routes``) and not on the estimate of them:
    the plan is the intended wiring and the router draws it, so a through-body or a short on a route the estimator
    invented would be a false count.  The other nets keep the estimate.  ``wires`` (what the legaliser reports having drawn,
    ``Placements.wires``) replace the plan's re-derivation of those nets: a free trunk's height and a lead's length are its
    choice, and the plan has no coordinates to carry them."""
    model = solvers._Model(symbols, nets, reach, solvers.Weights(), half_widths)
    pose = {s.reference: (s.position, s.orientation) for s in symbols}
    found: dict[str, list] = {}
    cost = model.cost(pose, found=found, routes=None if plan is None else
                      attach_unplanned(plan, symbols, nets, {**plan_routes(plan, symbols, nets), **(wires or {})}))
    boxes = [model.world(s.reference, s.position, s.orientation)[1] for s in symbols]
    extent = (max(b[2] for b in boxes) - min(b[0] for b in boxes), max(b[3] for b in boxes) - min(b[1] for b in boxes))
    return Counts(label, len(found.get("through", [])), len(found.get("overlap", [])), len(found.get("clearance", [])),
                  len(found.get("short", [])), cost.bends, cost.jogs, cost.crossings, cost.length, extent, cost.total)


def others_row(symbols: Sequence[PlacedSymbol], others: Sequence[str], gap: float = 5.08,
               nets: Sequence[Connection] = ()) -> list[PlacedSymbol]:
    """The parts the ladder leaves (``others``) stood in a row below everything placed, three gaps under the lowest box, so
    that a wire to one of them reaches it from above along the free space under the last row and passes no neighbour.

    A circuit read from a netlist has no pose for them (they all sit at the origin), so each takes the next place along the
    row, under the placed parts it connects to (their mean x, when ``nets`` says which), left to right, boxes two gaps
    apart.  This is what the sandbox harness does (``sbx.ladder.others_column``, which a sandbox selftest keeps in step)."""
    leftovers = set(others)
    column = [s for s in symbols if s.reference in leftovers]
    fixed = [s for s in symbols if s.reference not in leftovers and not s.reference.startswith("#")]
    if not column:
        return list(symbols)

    def box(symbol: PlacedSymbol) -> tuple[float, float, float, float]:
        left, top, right, bottom = symbol.geometry[symbol.orientation].box
        return (symbol.position[0] + left, symbol.position[1] + top, symbol.position[0] + right, symbol.position[1] + bottom)

    boxes = [box(s) for s in fixed]
    edge = max((b[3] for b in boxes), default=0.0) + 3 * gap
    cursor = min((b[0] for b in boxes), default=0.0)
    centre = {s.reference: (box(s)[0] + box(s)[2]) / 2 for s in fixed}
    wanted: dict[str, float] = {}
    for symbol in column:
        xs = [centre[ref] for net in nets if any(r == symbol.reference for r, _p in net.pins)
              for ref, _pin in net.pins if ref in centre]
        wanted[symbol.reference] = sum(xs) / len(xs) if xs else cursor
    moved = {}
    for symbol in sorted(column, key=lambda s: (wanted[s.reference], s.reference)):
        left, top, right, bottom = symbol.geometry[symbol.orientation].box
        x = max(cursor, wanted[symbol.reference] - (right - left) / 2)
        moved[symbol.reference] = replace(symbol, position=(x - left, edge - top))
        cursor = x + (right - left) + 2 * gap
    return [moved.get(s.reference, s) for s in symbols]


def posed(fixture: ladder_fixture.Fixture, placements: Mapping[str, Placement], leftover: Sequence[str] = ()) -> list[PlacedSymbol]:
    """The fixture's symbols with ``placements`` applied (and the ``other`` parts, with ``leftover``, in a row below the
    placed ones when the fixture says so)."""
    symbols = ladder_fixture.apply_placements(fixture.symbols, placements)
    if fixture.others == "row":
        symbols = others_row(symbols, [*fixture.other, *leftover], nets=fixture.nets)
    return symbols


# ---------------------------------------------------------------------------------------------------------------------
# legality and conformance


class _World:
    """Where each placed part's pins and body are, for the checks below."""

    def __init__(self, symbols: Sequence[PlacedSymbol], nets: Sequence[Connection], refs: Sequence[str]) -> None:
        self.by_ref = {s.reference: s for s in symbols}
        self.refs = sorted(r for r in refs if r in self.by_ref)
        self.net_pin: dict[tuple[str, str], str] = {}
        for net in sorted(nets, key=lambda n: n.net):
            for ref, pin in sorted(net.pins):
                self.net_pin.setdefault((ref, net.net), pin)

    def geometry(self, ref: str) -> OrientedGeometry:
        symbol = self.by_ref[ref]
        return symbol.geometry[symbol.orientation]

    def tip(self, ref: str, pin: str) -> tuple[float, float] | None:
        geometry = self.geometry(ref)
        if pin not in geometry.pins:
            return None
        x, y = self.by_ref[ref].position
        point = geometry.pins[pin].point
        return (x + point[0], y + point[1])

    def tips(self, ref: str) -> list[tuple[str, tuple[float, float]]]:
        return [(pin, self.tip(ref, pin)) for pin in sorted(self.geometry(ref).pins)]

    def body(self, ref: str) -> tuple[float, float, float, float]:
        l, t, r, b = body_box(self.geometry(ref))
        x, y = self.by_ref[ref].position
        return (x + l, y + t, x + r, y + b)

    def box(self, ref: str) -> tuple[float, float, float, float]:
        l, t, r, b = self.geometry(ref).box
        x, y = self.by_ref[ref].position
        return (x + l, y + t, x + r, y + b)

    def net_tip(self, ref: str, net: str) -> tuple[float, float] | None:
        pin = self.net_pin.get((ref, net))
        return self.tip(ref, pin) if pin is not None else None


def _overlap(a, b, grow: float = 0.0) -> bool:
    return (a[0] - grow < b[2] - _EPS and b[0] - grow < a[2] - _EPS
            and a[1] - grow < b[3] - _EPS and b[1] - grow < a[3] - _EPS)


def check(plan: Plan, symbols: Sequence[PlacedSymbol], nets: Sequence[Connection], placed: Sequence[str] | None = None,
          *, gap_mm: float = 2.54) -> Verdict:
    """Legality and conformance of the posed ``symbols`` (every part the plan places has its anchor and orientation).

    ``placed`` lists the references the candidate placed (default: the plan's parts); only those are judged for overlaps and
    tips, so the parts the ladder leaves where they are do not count against it."""
    verdict = Verdict()
    refs = sorted(placed if placed is not None else plan.parts())
    world = _World(symbols, nets, refs)
    for ref in sorted(plan.parts() - set(world.refs)):
        verdict.legal.append(f"{ref} is in the plan but not placed")
    refs = world.refs

    # legality: bodies, tips, rows
    bodies = {ref: world.body(ref) for ref in refs}
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            if _overlap(bodies[a], bodies[b]):
                verdict.legal.append(f"bodies overlap: {a} and {b}")
            elif _overlap(bodies[a], bodies[b], gap_mm / 2):
                verdict.conformance.append(f"bodies closer than a gap ({gap_mm:g} mm): {a} and {b}")
    tips = [(tip[0], tip[1], ref, pin) for ref in refs for pin, tip in world.tips(ref)]
    tips.sort()
    for i, (ax, ay, a_ref, a_pin) in enumerate(tips):
        for bx, by, b_ref, b_pin in tips[i + 1:]:
            if bx - ax >= TIP_SPACING_MM:
                break
            if a_ref == b_ref:
                continue
            distance = math.hypot(bx - ax, by - ay)
            if distance <= _EPS:
                verdict.legal.append(f"pin tips coincide: {a_ref}.{a_pin} and {b_ref}.{b_pin}")
            elif distance < TIP_SPACING_MM - _EPS:
                verdict.conformance.append(
                    f"pin tips {distance:.2f} mm apart (under {TIP_SPACING_MM:g}): {a_ref}.{a_pin} and {b_ref}.{b_pin}")

    row_y = _row_heights(plan, world)
    last_name = last_y = None
    for row in plan.rows:
        y = row_y.get(row.name)
        if y is None:
            continue
        if last_y is not None and y < last_y - 1e-3:
            verdict.legal.append(f"rows out of order: {row.name} (y {y:g}) lies above {last_name} (y {last_y:g})")
        last_name, last_y = row.name, y

    # conformance: orientations, rows, chain order, hangs
    for ref in refs:
        planned = plan.orientations.get(ref)
        if planned is not None and world.by_ref[ref].orientation != planned:
            verdict.conformance.append(
                f"{ref} turned {world.by_ref[ref].orientation.rotation}{world.by_ref[ref].orientation.mirror}"
                f" where the plan has {planned.rotation}{planned.mirror}")
    for row in plan.rows:
        if row.kind != "chain":
            continue
        heights = {}
        for ref in row.order:
            pin = plan.pins.get(ref, "")
            tip = world.tip(ref, pin) if pin and ref in world.by_ref else None
            if tip is not None:
                heights[ref] = tip[1]
        if heights and max(heights.values()) - min(heights.values()) > 1e-3:
            verdict.conformance.append(f"row {row.name}: parts not at one height {sorted(heights.items())}")
    for group in plan.sequence:
        for before, after in zip(group, group[1:]):
            if before in world.by_ref and after in world.by_ref and world.body(after)[0] < world.body(before)[0] - _EPS:
                verdict.conformance.append(f"chain order: {after} lies left of {before}")
    hang_ranks: dict[tuple[str, str, str], list] = defaultdict(list)
    trunk_tips: dict[tuple[str, bool], list[tuple[float, str]]] = defaultdict(list)
    for hang in plan.hangs:
        if hang.reference not in world.by_ref:
            continue
        tip = world.net_tip(hang.reference, hang.wire)
        if tip is None:
            continue
        body = world.body(hang.reference)
        on_row = hang.wire in row_y and any(r.name == hang.wire for r in plan.rows)
        sideways = hang.side in ("left", "right")
        if on_row:
            wire_y = row_y[hang.wire]
            if abs(tip[1] - wire_y) > 1e-3:
                verdict.conformance.append(
                    f"hang {hang.reference}: tip {tip[1] - wire_y:+.2f} mm off its wire {hang.wire}")
        else:
            # an unnamed wire is a trunk through the hang's own tip with a straight drop along the pin normal from
            # every other chain pin on the net (a free height is fine; the hang itself stands off by nothing)
            trunk_tips[(hang.wire, sideways)].append((tip[0] if sideways else tip[1], hang.reference))
            stuck = _unreached(plan, world, hang, tip, sideways)
            if stuck:
                verdict.conformance.append(
                    f"hang {hang.reference}: tip off the straight drop of {', '.join(stuck)} on {hang.wire}")
            wire_y = None if sideways else tip[1]
        if wire_y is not None:
            # The signal tip is the contact: a symbol's box can reach past it (padding, not body), so the body is
            # clipped at the tip when the tip's pin points toward the wire.
            pin = world.net_pin.get((hang.reference, hang.wire))
            normal = world.geometry(hang.reference).pins[pin].normal if pin is not None else (0.0, 0.0)
            top = max(body[1], tip[1]) if normal[1] < -.5 else body[1]
            bottom = min(body[3], tip[1]) if normal[1] > .5 else body[3]
            if hang.side == "below" and top < wire_y - 1e-3:
                verdict.conformance.append(f"hang {hang.reference}: body above the wire {hang.wire} it hangs below")
            if hang.side == "above" and bottom > wire_y + 1e-3:
                verdict.conformance.append(f"hang {hang.reference}: body below the wire {hang.wire} it hangs above")
        span = _wire_span(plan, world, hang.wire, row_y)
        if span is not None and not span[0] - 1e-3 <= tip[0] <= span[1] + 1e-3:
            verdict.conformance.append(f"hang {hang.reference}: outside the span of its wire {hang.wire}")
        host_tip = _host_tip(world, hang.host)
        if host_tip is not None:
            hang_ranks[(hang.wire, hang.side, hang.host)].append((hang.rank, abs(tip[0] - host_tip[0]), hang.reference))
    for (wire, sideways), items in sorted(trunk_tips.items()):
        items.sort()
        if items[-1][0] - items[0][0] > 1e-3:
            verdict.conformance.append(
                f"hangs on {wire} do not share one trunk: " + ", ".join(f"{ref} at {at:g}" for at, ref in items))
    for (wire, side, host), items in sorted(hang_ranks.items()):
        items.sort()
        for (r1, d1, a), (r2, d2, b) in zip(items, items[1:]):
            if d1 > d2 + 1e-3:
                verdict.conformance.append(f"rank: {a} (rank {r1}) lies farther from {host} on {wire} than {b} (rank {r2})")
    return verdict


def _host_tip(world: _World, host: str) -> tuple[float, float] | None:
    ref, _, pin = host.partition(".")
    return world.tip(ref, pin) if ref in world.by_ref and pin else None


def _unreached(plan: Plan, world: _World, hang, tip: tuple[float, float], sideways: bool) -> list[str]:
    """What keeps the trunk through a hang's tip (horizontal, or vertical for a sideways hang) from being reached by a
    straight drop along the pin normal from each other member of the wire: the chain pins on it, the host pin."""
    members: dict[str, tuple[str, tuple[float, float]]] = {}
    for ref in plan.pins:
        if ref != hang.reference and ref in world.by_ref:
            pin = world.net_pin.get((ref, hang.wire))
            point = world.net_tip(ref, hang.wire)
            if pin is not None and point is not None:
                members[ref] = (pin, point)
    host_ref, _, host_pin = hang.host.partition(".")
    if host_ref in world.by_ref and host_pin and host_ref != hang.reference and host_ref not in members:
        point = world.tip(host_ref, host_pin)
        if point is not None:
            members[host_ref] = (host_pin, point)
    axis = 0 if sideways else 1
    out = []
    for ref, (pin, point) in sorted(members.items()):
        normal = world.geometry(ref).pins[pin].normal
        drop = tip[axis] - point[axis]
        if abs(drop) <= 1e-3:
            continue
        step = normal[axis]
        if abs(normal[1 - axis]) > 0.5 or step * drop <= 0:
            out.append(f"{ref}.{pin} ({drop:+.2f} mm, normal {normal[0]:g},{normal[1]:g})")
    return out


def _wire_height(plan: Plan, world: _World, wire: str, host: str, row_y: Mapping[str, float]) -> float | None:
    """The y of the wire a hang lands on: its row's line, else the height of the host pin it serves."""
    if wire in row_y and any(r.name == wire for r in plan.rows):
        return row_y[wire]
    tip = _host_tip(world, host)
    return None if tip is None else tip[1]


def _wire_span(plan: Plan, world: _World, wire: str, row_y: Mapping[str, float]) -> tuple[float, float] | None:
    """Where a wire between chain pins runs along x: from its leftmost to its rightmost chain pin tip (None for a trunk or
    side row, whose wire reaches wherever its parts hang, and for a net with fewer than two chain parts)."""
    if wire in row_y and any(r.name == wire for r in plan.rows):
        return None
    xs = [tip[0] for ref in plan.pins if ref in world.by_ref for tip in [world.net_tip(ref, wire)] if tip is not None]
    return (min(xs), max(xs)) if len(xs) >= 2 else None


def _row_heights(plan: Plan, world: _World) -> dict[str, float]:
    """The y each row lies at in the placement: a chain row at its parts' row pins, a trunk or side row at the tips of
    the parts hung from it (the median when they differ, which conformance reports separately)."""
    heights: dict[str, float] = {}
    on_wire: dict[str, list[float]] = defaultdict(list)
    for hang in plan.hangs:
        if hang.reference in world.by_ref:
            tip = world.net_tip(hang.reference, hang.wire)
            if tip is not None:
                on_wire[hang.wire].append(tip[1])
    for row in plan.rows:
        if row.kind == "chain":
            ys = []
            for ref in row.order:
                pin = plan.pins.get(ref, "")
                if ref in world.by_ref and pin:
                    tip = world.tip(ref, pin)
                    if tip is not None:
                        ys.append(tip[1])
            if ys:
                heights[row.name] = sorted(ys)[len(ys) // 2]
        elif on_wire.get(row.name):
            ys = sorted(on_wire[row.name])
            heights[row.name] = ys[len(ys) // 2]
    return heights


# ---------------------------------------------------------------------------------------------------------------------
# command line


def judge(fixture: ladder_fixture.Fixture, placements: Mapping[str, Placement], *, estimate: bool = False,
          wires: Mapping[str, Sequence[Any]] | None = None) -> dict[str, Any]:
    """Counts of the candidate and of the baseline, both scored on the plan's wires (on the estimate with ``estimate``),
    and the candidate's verdict."""
    plan = None if estimate else fixture.plan
    wires = wires if wires is not None else getattr(placements, "wires", None)
    symbols = posed(fixture, placements)
    baseline_symbols = posed(fixture, fixture.baseline, fixture.unplaced)
    placed = sorted(set(placements) & {s.reference for s in symbols})
    return {
        "candidate": counts("candidate", symbols, fixture.nets, fixture.reach, fixture.half_widths, plan, wires),
        "baseline": counts("baseline", baseline_symbols, fixture.nets, fixture.reach, fixture.half_widths, plan),
        "verdict": check(fixture.plan, symbols, fixture.nets, placed, gap_mm=fixture.gap_mm),
        "baseline_verdict": check(fixture.plan, baseline_symbols, fixture.nets, sorted(fixture.baseline), gap_mm=fixture.gap_mm),
    }


def table(rows: Sequence[Counts]) -> str:
    lines = [f"{'placement':10} {'through':>7} {'overlaps':>8} {'clearance':>9} {'short':>5} {'bends':>6} {'jogs':>4} "
             f"{'crossings':>9} {'wire mm':>8} {'extent mm':>13} {'cost':>8}"]
    for r in rows:
        lines.append(f"{r.label:10} {r.through:7d} {r.overlaps:8d} {r.clearance:9d} {r.short:5d} {r.bends:6.1f} {r.jogs:4d} "
                     f"{r.crossings:9d} {r.wire_mm:8.1f} {r.extent[0]:6.1f} x {r.extent[1]:5.1f} {r.total:8.1f}")
    return "\n".join(lines)


def report(name: str, result: Mapping[str, Any], *, limit: int = 12) -> str:
    verdict: Verdict = result["verdict"]
    lines = [f"fixture {name}", table([result["candidate"], result["baseline"]]), ""]
    lines.append("legality: " + ("OK" if verdict.ok else f"{len(verdict.legal)} problem(s)"))
    lines += [f"  {item}" for item in verdict.legal[:limit]]
    if len(verdict.legal) > limit:
        lines.append(f"  ... {len(verdict.legal) - limit} more")
    baseline: Verdict = result["baseline_verdict"]
    lines.append(f"plan conformance: {len(verdict.conformance)} departure(s) "
                 f"(the baseline has {len(baseline.conformance)})")
    lines += [f"  {item}" for item in verdict.conformance[:limit]]
    if len(verdict.conformance) > limit:
        lines.append(f"  ... {len(verdict.conformance) - limit} more")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.schematic.ladder_check", description=__doc__.split("\n\n")[0])
    parser.add_argument("fixture", help="a ladder fixture JSON file (tests/fixtures/ladder/<name>.json)")
    parser.add_argument("placement", nargs="?", help="a placement JSON file; omitted: judge the fixture's baseline alone")
    parser.add_argument("--json", action="store_true", help="print the counts and the verdict as JSON")
    parser.add_argument("--plan", action="store_true", help="also print the fixture's plan, a line per row, hang, span and column")
    parser.add_argument("--estimate", action="store_true", help="score every net on the router estimate, not the plan's wires")
    args = parser.parse_args(argv)
    fixture = ladder_fixture.load_fixture(args.fixture)
    placements = ladder_fixture.load_placement(args.placement) if args.placement else dict(fixture.baseline)
    wires = ladder_fixture.load_wires(args.placement) if args.placement else None
    result = judge(fixture, placements, estimate=args.estimate, wires=wires)
    if args.json:
        verdict: Verdict = result["verdict"]
        print(json.dumps({
            "fixture": fixture.name, "candidate": result["candidate"].as_json(), "baseline": result["baseline"].as_json(),
            "legal": verdict.ok, "problems": verdict.legal, "conformance": verdict.conformance,
        }, indent=1))
    else:
        print(report(fixture.name, result))
        if args.plan:
            print("\nplan:\n" + fixture.plan.summary())
    return 0 if result["verdict"].ok else 1


if __name__ == "__main__":
    sys.exit(main())
