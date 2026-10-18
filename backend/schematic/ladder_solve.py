"""Turn the ladder grammar's fixed structure into legal grid coordinates.

Rows, orientations and attachment hosts are decisions of the grammar. CP-SAT only
chooses distances: it keeps bodies and tips apart, keeps wires clear of bodies,
and minimises the checker's own cost terms: wire length, 8 mm of it per interior crossing, and
0.08 mm per mm of width plus height. The pairwise exclusions are written lazily (see ``lazy``).
Assumptions name the structural or geometric constraints responsible for an infeasible plan.
No input positions, retries or circuit-specific choices enter the solve.
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from itertools import combinations
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
from ortools.sat.python import cp_model

from .placement_refinement import GRID_MM, ROUTE_MARGIN_MM, Placement, Route

if TYPE_CHECKING:
    from .ladder_plan import Plan

# Use the field placer's deterministic clock and search conventions. The wall clock is a
# safety net; a wall-limited incumbent is not reproducible and is not returned.
_SOLVER_DETERMINISTIC_TIME = 20.0
_SOLVER_SECONDS = 650.0     # a safety net only (three times the slowest of the 14 fixtures at five seeds, 212 s); the deterministic limit ends a solve
_SCALE = 1000
# The model is written small and grown where a solution needs it (see ``lazy``): a pairwise exclusion
# between a conductor or reservation and a body or pin is written up front only if the two stand within
# _NEAR_MM of each other in the first placement found, which may use _START_SHARE of the deterministic
# budget. An incumbent that breaks one left out stops the search; what it breaks is written in and the
# search resumes from it. After _ROUND_LIMIT such rounds everything left out is written in. These choose
# what is written when, never what is legal.
_NEAR_MM = 30.0
_START_SHARE = 0.1
_ROUND_LIMIT = 40
# A crossing is a readability fault, so wire does not buy one cheaply: it costs about a row's width of wire. The solver then
# avoids a crossing whenever an ordinary rearrangement removes it and accepts one only where avoiding it means a long
# excursion, which people never draw. (Not the checker's reporting weight, RANK_CROSSING_MM.)
_CROSSING_MM = 50.0
# A wire through the body of a part it joins, outside the exit corridor of the pin it joins by, is priced the same way: it is
# a readability fault the checker counts as a "through". Three crossings' worth of wire, never infeasibility: a symbol whose
# pins lie inside its own body, or a plan that puts a part between two pins of one net, has no legal drawing at all.
_THROUGH_MM = 150.0
# A short is a connectivity fault, not a readability one: in the choice among placements (``true_cost``) it costs twice a through.
_SHORT_MM = 300.0
_KEEP_EVERY = 16            # incumbents between copies of a clean one, to fall back on if the budget ends on a broken one
_WIRE_SHARE = 0.4           # of the budget, the first search, without the crossings, which the weighted one continues
_MONOTONE = False           # the weighted stage starts from the previous placement completed, and may not end above it (no gain: see 4b)
_FULL_MARGIN = True         # a drop keeps the route margin from a same-net body all along unless it runs over its tip (see 4b)
_SELECT = True              # the placement returned is the cheapest of those kept, by true cost (see ``true_cost``)
_KEPT_PER_STAGE = 4         # the latest clean placements of each stage that are kept for that choice
_FEASIBLE_FIRST = False     # no clean placement when the first stage starts: find one (no objective) before optimising
_GLOBAL_BODY = False        # bodies against bodies as one NoOverlap2D (prototype)
_GLOBAL_WIRE = False        # each conductor against the bodies it keeps the gap from as one NoOverlap2D (prototype)
_SEEDS = (7,)               # random seeds; more than one solves once per seed in separate processes (see ``legalise``)
_WORKERS = 1                # search workers; more than one runs CP-SAT's interleaved (deterministic) portfolio
_VERIFY_PRUNE = False       # test every exclusion left out against every placement kept (a soundness check, slow)
_PRUNE = False              # exclusions between things the plan keeps apart in height are not written (sound; off: it did not help, see 4b)
_STOP_WIRE = False          # the first search ends at _WIRE_SHARE once it holds a placement (off: it did not help, see 4b)
_WIRE_WEIGHT, _EXTENT_WEIGHT, _WARM_EXTENT_WEIGHT = 25, 2, 25   # extent: the checker's 0.08 mm of wire per mm; dearer at first
_GRID = round(GRID_MM * _SCALE)


class _Row(NamedTuple):
    name: str
    kind: str
    above: str | None
    order: tuple


class LadderInfeasible(ValueError):
    """A proven conflict, with constraint groups and involved references."""

    def __init__(self, groups, parts=(), detail=""):
        # A bare message (what a caller that re-raises with a prefix passes) keeps the text and no core.
        self.message = groups if isinstance(groups, str) else ""
        self.groups = () if self.message else tuple(sorted(set(groups)))
        self.parts = tuple(sorted(set(parts)))
        self.detail = detail
        self.status = "cp-sat (infeasible)"
        super().__init__(self.message or f"{', '.join(self.groups)}: {', '.join(self.parts)}" +
                         (f" ({detail})" if detail else ""))

    def __reduce__(self):               # the two-argument constructor must survive copying and pickling
        return (type(self), (self.message or self.groups, self.parts, self.detail))


class LadderSolveError(RuntimeError):
    """The budget expired or the model was invalid; infeasibility is unproven."""

    def __init__(self, status):
        self.status = status
        self.stats = {}
        super().__init__(status)


class Placements(dict[str, Placement]):
    """The usual placement mapping, with the solver's status for reporting.

    ``wires`` is what the solver drew: for each net whose pins the plan places, its routes (trunk, leads, drops, a stack's
    junction) as the placement model's ``Route`` objects, in millimetres. A free trunk's height and a lead's length are the
    solver's choice and the plan, which has no coordinates, cannot carry them, so the result does; the checker scores
    these wires and not a re-derivation of them."""

    def __init__(self, values=(), *, solver="cp-sat", wires=None):
        super().__init__(values)
        self.solver = self.status = solver
        self.wires = wires or {}


def _mm(value):
    if not math.isfinite(value):
        raise ValueError("Geometry and distances must be finite")
    return round(value * _SCALE)


def _body(geometry):
    # The contract's body is the layout box without one grid of pin lead,
    # rather than OrientedGeometry.body (which has a different scorer meaning).
    l, t, r, b = map(_mm, geometry.box)
    normals = {tuple(round(v) for v in p.normal) for p in geometry.pins.values()}
    l += _GRID if (-1, 0) in normals else 0
    r -= _GRID if (1, 0) in normals else 0
    t += _GRID if (0, -1) in normals else 0
    b -= _GRID if (0, 1) in normals else 0
    return l, t, max(l, r), max(t, b)


def evaluator(solver):
    """The solver's value of an expression, or of every expression in a tuple, remembered per object."""
    values = {}

    def value(expression):
        if isinstance(expression, tuple):
            return tuple(value(item) for item in expression)
        if isinstance(expression, int):
            return expression
        key = id(expression)
        if key not in values:
            values[key] = solver.Value(expression)
        return values[key]

    return value


def _close(value, a, b):
    """Whether two boxes (left, top, right, bottom as expressions) stand within ``_NEAR_MM`` of each other."""
    (al, at, ar, ab), (bl, bt, br, bb) = value(a), value(b)
    reach = round(_NEAR_MM * _SCALE)
    return max(al - br, bl - ar, at - bb, bt - ab) < reach


def _affine(model, expression):
    """(variable index or -1, coefficient, offset) of an expression with at most one variable, else None."""
    if isinstance(expression, int):
        return -1, 0, expression
    f = model.parse_linear_expression(expression)
    if not f.vars:
        return -1, 0, f.offset
    return (f.vars[0], f.coeffs[0], f.offset) if len(f.vars) == 1 else None


class _Checker:
    """The exclusions left out of the model, checked against a solution in bulk.

    Every coordinate is a single variable's affine function (or a constant), so a solution's value of all of
    them is one array operation, and the same inequalities as ``_overlap`` and ``_on_segment`` give the
    entries it breaks. A solution is looked at for every incumbent, which in Python, entry by entry, cost more
    than the search itself. ``entries`` are (key, emit, check, boxes, kind) with kind ("overlap",) for two
    rectangles and ("point", horizontal) for a segment (start, end) and a point.
    """

    def __init__(self, entries, flat):
        self.entries, self.slow = entries, []
        rows = {}

        def row(expression):
            triple = flat(expression)
            return None if triple is None else rows.setdefault(triple, len(rows))

        overlap, point = [], []
        for index, entry in enumerate(entries):
            a, b = ([row(e) for e in box] for box in entry[3])
            if None in a or None in b:
                self.slow.append(index)
            elif entry[4][0] == "overlap":
                overlap.append((index, a, b))
            else:
                point.append((index, a, b, entry[4][1]))
        triples = list(rows)
        self.needed = sorted({v for v, _, _ in triples if v >= 0})
        place = {v: k for k, v in enumerate(self.needed)}
        self.place = np.array([place.get(v, len(self.needed)) for v, _, _ in triples], dtype=np.int64)
        self.coefficient = np.array([c for _, c, _ in triples], dtype=np.int64)
        self.offset = np.array([o for _, _, o in triples], dtype=np.int64)
        self.overlap_index = np.array([i for i, _, _ in overlap], dtype=np.int64)
        self.overlap_a = np.array([a for _, a, _ in overlap], dtype=np.int64).reshape(-1, 4)
        self.overlap_b = np.array([b for _, _, b in overlap], dtype=np.int64).reshape(-1, 4)
        self.point_index = np.array([i for i, _, _, _ in point], dtype=np.int64)
        self.segment = np.array([a for _, a, _, _ in point], dtype=np.int64).reshape(-1, 4)
        self.point = np.array([b[:2] for _, _, b, _ in point], dtype=np.int64).reshape(-1, 2)
        self.horizontal = np.array([h for _, _, _, h in point], dtype=bool)

    def broken(self, values, evaluate=None):
        """The entries a solution breaks; ``values`` are its values of ``self.needed``, in order."""
        padded = np.append(np.asarray(values, dtype=np.int64), 0)
        v = self.offset + self.coefficient * padded[self.place]
        found = []
        if len(self.overlap_index):
            al, at, ar, ab = (v[self.overlap_a[:, k]] for k in range(4))
            bl, bt, br, bb = (v[self.overlap_b[:, k]] for k in range(4))
            live = (ar > al) & (ab > at) & (br > bl) & (bb > bt)
            apart = (ar <= bl) | (br <= al) | (ab <= bt) | (bb <= at)
            found += self.overlap_index[live & ~apart].tolist()
        if len(self.point_index):
            sx, sy, ex, ey = (v[self.segment[:, k]] for k in range(4))
            px, py = v[self.point[:, 0]], v[self.point[:, 1]]
            on = np.where(self.horizontal, (sx <= px) & (px <= ex) & (py == sy), (sy <= py) & (py <= ey) & (px == sx))
            found += self.point_index[on].tolist()
        if self.slow and evaluate is not None:
            found += [i for i in self.slow if self.entries[i][2](evaluate)]
        return [self.entries[i] for i in sorted(found)]


def _overlap(a, b):
    """Two (left, top, right, bottom) rectangles that both have area share some of it."""
    return (all(r > l and bottom > t for l, t, r, bottom in (a, b)) and
            not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]))


def _on_segment(point, start, end, horizontal):
    along, across = (0, 1) if horizontal else (1, 0)
    return start[along] <= point[along] <= end[along] and point[across] == start[across]


def _point_off_segment(model, point, start, end, horizontal, assumption, name):
    """Exclude a foreign point from the entire closed conductor centreline."""
    along, across = (0, 1) if horizontal else (1, 0)
    flags = []
    for condition in (point[along] < start[along], point[along] > end[along],
                      point[across] < start[across], point[across] > start[across]):
        flag = model.NewBoolVar(name)
        model.Add(condition).OnlyEnforceIf(flag)
        flags.append(flag)
    model.AddBoolOr(flags).OnlyEnforceIf(assumption)


def _interior_crossing(model, h_start, h_end, v_start, v_end):
    """Reify a strict interior intersection of two sorted orthogonal segments."""
    hx0, hy = h_start
    hx1, _ = h_end
    vx, vy0 = v_start
    _, vy1 = v_end
    flags = []
    for condition, inverse in ((vx > hx0, vx <= hx0), (vx < hx1, vx >= hx1),
                               (hy > vy0, hy <= vy0), (hy < vy1, hy >= vy1)):
        flag = model.NewBoolVar("interior crossing condition")
        model.Add(condition).OnlyEnforceIf(flag)
        model.Add(inverse).OnlyEnforceIf(flag.Not())
        flags.append(flag)
    crossing = model.NewBoolVar("interior crossing")
    model.AddBoolAnd(flags).OnlyEnforceIf(crossing)
    model.AddBoolOr([f.Not() for f in flags]).OnlyEnforceIf(crossing.Not())
    return crossing


def _true_cost(plan, symbols, nets, positions, routes):
    """What a placement costs as drawn: the checker's own counts on the wires it draws, at the prices (``_CROSSING_MM`` a
    crossing, ``_THROUGH_MM`` a wire through a body, ``_SHORT_MM`` a short), plus its wire length and extent. None when it
    draws no net. ``positions`` is each placed part's anchor in millimetres."""
    from dataclasses import replace
    from .placement_refinement import Weights, _Model
    if not routes:
        return None
    by_ref = {s.reference: s for s in symbols}
    posed = [replace(by_ref[ref], position=positions[ref], orientation=plan.orientations[ref]) for ref in sorted(positions)]
    drawn_nets = [net for net in nets if net.net in routes]
    # (A route names pins by their place in the net, so the parts the plan leaves out are in the model, far away.)
    away = [replace(s, position=(1e6, 1e6 + 1e3 * i)) for i, s in enumerate(symbols) if s.reference not in plan.orientations]
    scorer = _Model([*posed, *away], drawn_nets, {}, Weights(), None)
    cost = scorer.cost({s.reference: (s.position, s.orientation) for s in [*posed, *away]}, found={}, routes=routes)
    boxes = [scorer.world(s.reference, s.position, s.orientation)[1] for s in posed]
    extent = (max(b[2] for b in boxes) - min(b[0] for b in boxes)) + (max(b[3] for b in boxes) - min(b[1] for b in boxes))
    return (cost.length + _CROSSING_MM * cost.crossings + _THROUGH_MM * cost.through + _SHORT_MM * cost.shorts +
            _EXTENT_WEIGHT / _WIRE_WEIGHT * extent)


def _seed_price(plan, symbols, nets, placements):
    """What a seed's placement costs as the checker draws the whole sheet: every net, the parts the plan leaves out in the
    row under the placed ones, the nets the plan does not wire attached to them (``ladder_check.counts``), at the prices of
    ``_true_cost``. Parts and nets the plan does not place move with the placement, and so do their wires; pricing only the
    nets the solver drew would pick a placement whose leftovers are far off. Falls back to the drawn nets alone for a plan
    the checker cannot read."""
    from dataclasses import replace
    try:
        from . import ladder_check
        posed = [replace(s, position=placements[s.reference].position, orientation=placements[s.reference].orientation)
                 if s.reference in placements else s for s in symbols]
        others = [s.reference for s in symbols if s.reference not in placements and not s.reference.startswith("#")]
        posed = ladder_check.others_row(posed, others, nets=nets)
        c = ladder_check.counts("seed", posed, nets, {}, None, plan, placements.wires)
        return (c.wire_mm + _CROSSING_MM * c.crossings + _THROUGH_MM * c.through + _SHORT_MM * c.short +
                _EXTENT_WEIGHT / _WIRE_WEIGHT * (c.extent[0] + c.extent[1]))
    except Exception:
        positions = {ref: tuple(place.position) for ref, place in placements.items()}
        return _true_cost(plan, symbols, nets, positions, placements.wires)


def _run_seed(arguments):
    """One seed's solve, in a worker process: ("ok", the placements), or the error as plain data (an exception whose
    constructor takes more than a message does not survive the trip)."""
    plan, symbols, nets, gap_mm, pitch_mm, hint, seed = arguments
    try:
        return "ok", _legalise_one(plan, symbols, nets, gap_mm, pitch_mm, hint, seed)
    except LadderInfeasible as error:
        return "infeasible", (error.message or list(error.groups), list(error.parts), error.detail)
    except LadderSolveError as error:
        return "failed", (str(error), getattr(error, "stats", None))


def legalise(plan: Plan, symbols, nets, *, gap_mm=2.54, pitch_mm=7.62, hint=None, seeds=None) -> dict[str, Placement]:
    """Solve section 4b's constraints; raise rather than weaken an illegal plan.

    With more than one seed (``seeds``, else ``_SEEDS``) the same model is solved once per seed, each in a process of its own
    with the whole budget, and the placement that costs least as drawn (``_true_cost``) is returned, the earlier seed on a
    tie: the answer depends on the list of seeds and nothing else. The one-seed path is the solve itself.
    """
    seeds = tuple(_SEEDS if seeds is None else seeds)
    if len(seeds) <= 1:
        return _legalise_one(plan, symbols, nets, gap_mm, pitch_mm, hint, seeds[0] if seeds else _SEEDS[0])
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=len(seeds)) as pool:
        futures = [pool.submit(_run_seed, (plan, symbols, nets, gap_mm, pitch_mm, hint, seed)) for seed in seeds]
        outcomes = [future.result() for future in futures]
    placed = []
    for seed, (kind, value) in zip(seeds, outcomes):
        if kind == "ok":
            price = _seed_price(plan, symbols, nets, value)
            placed.append((float("inf") if price is None else price, len(placed), seed, value))
    if not placed:
        kind, value = outcomes[0]
        if kind == "infeasible":
            raise LadderInfeasible(*value)
        error = LadderSolveError(value[0])
        error.stats = value[1]
        raise error
    price, _order, seed, best = min(placed, key=lambda item: (item[0], item[1]))
    best.stats = dict(best.stats, seeds=[(s, None if p == float("inf") else round(p, 1)) for p, _o, s, _v in placed],
                      seed=seed)
    return best


def _legalise_one(plan, symbols, nets, gap_mm, pitch_mm, hint, seed):
    """Solve section 4b's constraints; raise rather than weaken an illegal plan.

    The first pass builds the model without assumption literals and optimises at once, which is the
    fast path. Only when that proves the model infeasible does a second, assumption-bearing build
    run, over the exclusions the first one had written by then, to name the conflicting constraint
    groups and parts.

    ``hint`` is a layout (reference to ``Placement``, the heuristic placer's for the same plan) that the first search starts
    from where it is given; it is a hint only, never a constraint, and the solution may differ from it anywhere.
    """
    stats = {}
    soft = False
    while True:
        try:
            placements = _solve(plan, symbols, nets, gap_mm, pitch_mm, diagnose=False, stats=stats, soft_joined=soft,
                                layout=hint, seed=seed)
        except LadderSolveError as error:
            error.stats = {k: v for k, v in stats.items() if k != "emitted"}       # what the search did, for a report
            raise
        if placements is not None:
            placements.joined = "priced" if soft else "excluded"
            return placements
        # What the first pass wrote into the model (its cheap start and everything it later found a solution
        # breaking) was already infeasible, so the diagnosis needs nothing more.
        try:
            _solve(plan, symbols, nets, gap_mm, pitch_mm, diagnose=True, learned=stats["emitted"], soft_joined=soft)
        except LadderInfeasible as infeasible:
            # A wire through a body it joins is a fault to price, not a reason to lose the sheet: a symbol with pins inside
            # its own body, or a plan that puts a part between two pins of one net, has no drawing without one.
            if soft or not any(group.startswith("wire-joined") for group in infeasible.groups):
                raise
            soft, stats = True, {}
            continue
        raise LadderSolveError("cp-sat (infeasibility not reproduced with assumptions)")


def _solve(plan, symbols, nets, gap_mm, pitch_mm, diagnose, anchors=None, learned=None, stats=None, soft_joined=False,
           around=None, forbid=(), relax=(), layout=None, seed=7):
    """One build and solve. ``diagnose`` adds an assumption literal per constraint group and only looks for a core.

    Only references in Plan.orientations are placed. Nets' other endpoints belong
    to the caller's unplanned parts. Integer anchors are grid units; geometry is
    in micrometres so fractional box edges do not get snapped to the grid.
    """
    started = time.perf_counter()
    if not math.isfinite(gap_mm) or gap_mm < 0 or not math.isfinite(pitch_mm) or pitch_mm <= 0:
        raise ValueError("gap_mm must be nonnegative and pitch_mm positive")
    gap, pitch = math.ceil(gap_mm * _SCALE), math.ceil(pitch_mm * _SCALE)
    by_ref = {s.reference: s for s in symbols}
    refs = sorted(plan.orientations)
    geoms = {}
    for ref in refs:
        if ref not in by_ref or plan.orientations[ref] not in by_ref[ref].geometry:
            raise LadderInfeasible(["orientation"], [ref], "missing symbol or chosen geometry")
        geoms[ref] = by_ref[ref].geometry[plan.orientations[ref]]
    # A lane names its series parts, and a series part that the plan also records as a span is placed as
    # that span (its tips on the lane's nets); the lane row supplies only its height and the order along it.
    span_by_ref = {s.reference: s for s in plan.spans}
    lane_order = {row.name: tuple(row.order) for row in plan.rows if row.kind == "lane"}
    lane_spans = {ref: name for name, order in lane_order.items() for ref in order if ref in span_by_ref}
    rows = [_Row(r.name, r.kind, r.above, tuple(ref for ref in r.order if ref not in lane_spans)) for r in plan.rows]
    owners = defaultdict(list)
    for row in rows:
        for ref in row.order:
            owners[ref].append("row")
    for hang in plan.hangs:
        owners[hang.reference].append("hang")
    for span in plan.spans:
        owners[span.reference].append("span")
    for column in plan.columns:
        for ref in column.order:
            owners[ref].append("column")
    invalid = sorted(set(owners) - set(refs) | {r for r in refs if len(owners[r]) > 1})
    if invalid:
        raise LadderInfeasible(["plan membership"], invalid, "part has conflicting or unplanned structures")
    if not refs:
        return Placements(solver="none")
    if len({r.name for r in rows}) != len(rows):
        raise LadderInfeasible(["rows"], refs, "duplicate row name")
    bodies = {r: _body(g) for r, g in geoms.items()}
    # Finite CP domains derived from input size, not a sheet size or a fixture tuning knob: along each axis
    # every part may take its own extent, a gap either side, a slot for each of its pins and a grid, and each
    # row a gap, which is more than a sheet laid out in one line needs. The tighter the domains, the tighter
    # the linear relaxation CP-SAT works from.
    def extent(geometry, axis):
        values = [*(geometry.box[axis::2]), *(p.point[axis] for p in geometry.pins.values())]
        return _mm(max(values) - min(values))

    def reach(axis):
        return (sum(extent(g, axis) + 2 * gap + pitch * len(g.pins) + 2 * _GRID for g in geoms.values()) +
                2 * max(abs(_mm(v)) for g in geoms.values() for v in
                        (*g.box[axis::2], *(p.point[axis] for p in g.pins.values()))) +
                2 * gap * len(rows))

    bound_x, bound_y = (max(1, math.ceil(reach(axis) / _GRID)) for axis in (0, 1))
    bound = max(bound_x, bound_y)
    limit = (bound * 2 + 2) * _GRID
    model = cp_model.CpModel()
    x = {r: model.NewIntVar(-bound_x, bound_x, f"x:{r}") for r in refs}
    y = {r: model.NewIntVar(-bound_y, bound_y, f"y:{r}") for r in refs}
    row_y = {r.name: model.NewIntVar(-limit, limit, f"row:{r.name}") for r in rows}
    # What the plan fixes about heights, for leaving out exclusions that cannot bind (see ``prunable``): a part on a row
    # stands at a constant offset from the row's height, a hung part from its wire's; rows keep their order and at least the
    # distances the row constraints give.
    row_index = {r.name: i for i, r in enumerate(rows)}
    ysym = {}                   # part -> (row index, k): y * grid = the row's height + k
    hang_links = []             # (hung part, its pin's local height, the wire's height expression)
    row_edges = []              # (earlier row, later row, least distance between their heights)
    extremes = {}               # variable index of an extrema variable -> ("lo" or "hi", the expressions)
    assumptions = {}
    always = model.NewBoolVar("always")
    model.Add(always == 1)

    def group(name, parts):
        if name in relax:           # a debugging aid: this kind of constraint is not enforced
            return always.Not()
        if not diagnose:
            return always
        parts = tuple(sorted(set(parts)))
        a = model.NewBoolVar(f"{name}:{','.join(parts)}")
        model.AddAssumption(a)
        assumptions[a.Index()] = (name, parts)
        return a

    def require(condition, name, parts):
        model.Add(condition).OnlyEnforceIf(group(name, parts))

    def pin(endpoint):
        ref, number = endpoint
        if ref not in geoms or number not in geoms[ref].pins:
            raise LadderInfeasible(["pins"], [ref], f"missing {ref}.{number}")
        p = geoms[ref].pins[number].point
        return x[ref] * _GRID + _mm(p[0]), y[ref] * _GRID + _mm(p[1])

    def endpoint(text):
        return tuple(text.rsplit(".", 1))

    def row_endpoint(ref):
        value = plan.pins[ref]
        return endpoint(value) if "." in value else (ref, value)

    def lane_pin(ref, wire_name):
        found = [p for p in net_map.get(wire_name, (None, ()))[1] if p[0] == ref]
        return found[0] if len(found) == 1 else None

    def hang_endpoint(hang):
        ep = endpoint(hang.host)
        if len(ep) != 2:
            raise LadderInfeasible(["hang host"], [hang.reference, hang.host], "host pin missing")
        if ep[0] not in geoms:
            raise LadderInfeasible(["hang host"], [hang.reference, ep[0]], "host is not placed by plan")
        if ep[1] not in geoms[ep[0]].pins:
            raise LadderInfeasible(["hang host"], [hang.reference, ep[0]], f"missing pin {hang.host}")
        return ep

    def absolute(expr, name):
        v = model.NewIntVar(0, limit * 2, name)
        model.AddAbsEquality(v, expr)
        return v

    def extrema(expressions, name):
        low, high = (model.NewIntVar(-limit, limit, f"{name}:{s}") for s in ("lo", "hi"))
        # OR-Tools 9.12's AddMinEquality negates coefficients but leaves affine
        # offsets unchanged. Express the minimum through an explicit maximum.
        model.AddMaxEquality(-low, [-e for e in expressions])
        model.AddMaxEquality(high, expressions)
        extremes[low.Index()] = ("lo", tuple(expressions))
        extremes[high.Index()] = ("hi", tuple(expressions))
        return low, high

    def rect(ref, margin=0):
        l, t, r, b = bodies[ref]
        return (x[ref] * _GRID + l - margin, y[ref] * _GRID + t - margin,
                x[ref] * _GRID + r + margin, y[ref] * _GRID + b + margin)

    def disjoint(a, b, name, parts, waived=None):
        """Two rectangles (left, top, right, bottom) of linear expressions may touch but not overlap.

        Each pair is its own disjunction (left of, right of, above, below), enforced while both
        rectangles have area, so a joined body can be left out pair by pair and an assumption core
        names the pair. A zero-width or zero-height rectangle (a drop of no length) is not a conductor.
        ``waived`` is a literal that lifts the exclusion when true (a priced one).
        """
        present = group(name, parts)
        live = [present] if waived is None else [present, waived.Not()]
        for l, t, r, bottom in (a, b):
            for expression in (r - l, bottom - t):
                flat = model.parse_linear_expression(expression)
                if not flat.vars:
                    if flat.offset <= 0:
                        return
                    continue
                positive = model.NewBoolVar(f"{present.Name()}:positive")
                model.Add(expression >= 1).OnlyEnforceIf(positive)
                model.Add(expression <= 0).OnlyEnforceIf(positive.Not())
                live.append(positive)
        separators, options = [], []
        for i, (condition, sides) in enumerate(((a[2] <= b[0], (a[2], b[0])), (b[2] <= a[0], (b[2], a[0])),
                                                (a[3] <= b[1], (a[3], b[1])), (b[3] <= a[1], (b[3], a[1])))):
            chosen = model.NewBoolVar(f"{present.Name()}:separate:{i}")
            model.Add(condition).OnlyEnforceIf(chosen)
            separators.append(chosen)
            options.append((chosen, *sides))
        model.AddBoolOr(separators).OnlyEnforceIf(live)
        return options

    def full_margin(rectangle, box, across, tip_coordinates, name, parts):
        """``rectangle`` keeps off ``box`` unless it runs along one of the tips (its centre line is a tip's coordinate)."""
        lifted = model.NewBoolVar(f"{name}:aligned")
        options = []
        for k, coordinate in enumerate(tip_coordinates):
            option = model.NewBoolVar(f"{name}:aligned:{k}")
            model.Add(across == coordinate).OnlyEnforceIf(option)
            options.append(option)
        model.AddBoolOr(options).OnlyEnforceIf(lifted)
        disjoint(rectangle, box, name, parts, waived=lifted)

    memberships = {}
    for row in rows:
        for ref in row.order:
            if ref not in geoms:
                raise LadderInfeasible(["rows"], [ref], "unplanned row member")
            if ref not in plan.pins:
                raise LadderInfeasible(["row pins"], [ref], "missing row pin")
            ep = row_endpoint(ref)
            if ep[0] != ref:
                raise LadderInfeasible(["row pins"], [ref, ep[0]])
            require((pin(ep)[1] if ep[1] else y[ref] * _GRID) == row_y[row.name], "row pins", [ref])
            ysym[ref] = (row_index[row.name], -(_mm(geoms[ref].pins[ep[1]].point[1]) if ep[1] else 0))
            memberships[ref] = row.name
        for a, b in zip(row.order, row.order[1:]):
            if row.kind == "lane":
                continue        # a lane runs outward from its hub pin, left or right: ordered below
            require(x[a] * _GRID + bodies[a][2] + gap <= x[b] * _GRID + bodies[b][0],
                    "chain order", [a, b])
    origin = next((r.order[0] for r in rows if r.order), refs[0])
    model.Add(x[origin] == 0)
    model.Add(y[origin] == 0)
    for sequence in getattr(plan, "sequence", ()):
        for a, b in zip(sequence, sequence[1:]):
            if a not in geoms or b not in geoms:
                raise LadderInfeasible(["chain sequence"], [a, b], "unplanned sequence member")
            require(x[a] * _GRID + bodies[a][2] + gap <= x[b] * _GRID + bodies[b][0],
                    "chain sequence", [a, b])

    net_map = {}
    pin_net = {}
    for net in sorted(nets, key=lambda n: n.net):
        eps = tuple(sorted(set(ep for ep in net.pins if ep[0] in geoms)))
        if net.net in net_map:
            raise LadderInfeasible(["wires"], [r for r, _ in eps], "duplicate net name")
        net_map[net.net] = (net, eps)
        for ep in eps:
            pin(ep)
            if ep in pin_net and pin_net[ep] != net.net:
                raise LadderInfeasible(["wires"], [ep[0]], "pin belongs to multiple nets")
            pin_net[ep] = net.net

    # A stacked pair (a half bridge) is a column whose pin reads "net:<junction>": the upper part's down-facing pin and the
    # lower part's up-facing pin on that net, tips at one x, one straight vertical wire between them. The wire is a
    # vertical trunk: parts hung from it sideways land their tips on it, a part across two stacks lands one on each.
    # A stacked part may have several pins on the junction (a power FET's source pins), all facing the other part and at
    # one height: the wire's end is the middle one, and a short run at that height joins the others (none where their tips
    # coincide).
    stacks = {}
    stack_pins = {}             # junction -> (the upper part's pins on it, the lower part's), each by x on the part
    for column in plan.columns:         # in the plan's order, which is left to right
        if not column.pin.startswith("net:"):
            continue
        name = column.pin[4:]
        if len(column.order) != 2 or len(column.orientations) != 2:
            raise LadderInfeasible(["stack"], column.order, "a stacked pair is two parts, upper first")
        ends, sides = [], []
        for ref, orientation, down in zip(column.order, column.orientations, (True, False)):
            if ref not in geoms or orientation != plan.orientations[ref]:
                raise LadderInfeasible(["orientation"], [ref], "stack orientation disagrees")
            found = sorted((p for p in net_map.get(name, (None, ()))[1] if p[0] == ref),
                           key=lambda p: (_mm(geoms[ref].pins[p[1]].point[0]), p[1]))
            if not found:
                raise LadderInfeasible(["stack"], column.order, f"{ref} needs a pin on {name}")
            for p in found:
                if geoms[ref].pins[p[1]].normal[1] * (1 if down else -1) < .5:
                    raise LadderInfeasible(["stack pin"], column.order,
                                           f"{ref}.{p[1]} does not face the other part of its stack")
            if len({_mm(geoms[ref].pins[p[1]].point[1]) for p in found}) != 1:
                raise LadderInfeasible(["stack pin"], column.order, f"{ref}'s pins on {name} are at different heights")
            ends.append(found[(len(found) - 1) // 2])
            sides.append(found)
        stack_pins[name] = tuple(sides)
        stacks[name] = tuple(ends)
        require(pin(ends[0])[0] == pin(ends[1])[0], "stack alignment", column.order)
        require(pin(ends[0])[1] <= pin(ends[1])[1], "stack order", column.order)

    def stack_x(name):
        return pin(stacks[name][0])[0]

    def stack_span(name):
        return pin(stacks[name][0])[1], pin(stacks[name][1])[1]

    # A named net row wins. Otherwise chain nets inherit the outgoing chain pin
    # row, with straight drops for endpoints at another height.
    wire_y = {name: value for name, value in row_y.items() if name in net_map}
    wire_row = {name: name for name in wire_y}
    for row in rows:
        for ref in row.order:
            for number in sorted(geoms[ref].pins):
                ep = (ref, number)
                name = pin_net.get(ep)
                if name is not None and name not in wire_y:
                    wire_y[name] = pin(ep)[1]
                    chosen = row_endpoint(ref)
                    offset = geoms[ref].pins[chosen[1]].point[1] if chosen[1] else 0
                    if geoms[ref].pins[number].point[1] == offset:
                        wire_row[name] = row.name

    for ref, lane_name in sorted(lane_spans.items()):
        ys = {round(p.point[1], 7) for p in geoms[ref].pins.values()}
        if len(ys) == 1:            # a series part: both its nets continue along the lane
            for name in (span_by_ref[ref].from_wire, span_by_ref[ref].to_wire):
                wire_y.setdefault(name, row_y[lane_name])

    # A pin facing its hang feeds a trunk at a free height by a straight drop.
    # The hang itself still lands exactly on the trunk, without any stand-off.
    free_trunks = set()
    hung_free = {}
    for hang in sorted(plan.hangs, key=lambda h: (h.wire, h.host, h.reference)):
        if hang.wire not in row_y and hang.wire not in stacks:
            ep = hang_endpoint(hang)
            normal = geoms[ep[0]].pins[ep[1]].normal
            toward = ((hang.side == "below" and normal[1] > .5) or
                      (hang.side == "above" and normal[1] < -.5))
            direction = 1 if hang.side == "below" else -1 if hang.side == "above" else 0
            feeders = [p for p in net_map.get(hang.wire, (None, ()))[1]
                       if p[0] in memberships and direction * geoms[p[0]].pins[p[1]].normal[1] > .5]
            toward = toward or bool(feeders)
            if toward:
                if hang.wire not in free_trunks:
                    wire_y[hang.wire] = model.NewIntVar(-limit, limit, f"trunk:{hang.wire}")
                    free_trunks.add(hang.wire)
                    wire_row.pop(hang.wire, None)
            elif hang.wire not in free_trunks and hang.wire not in hung_free:
                if ep[0] in memberships and abs(normal[0]) > .5:
                    # A sideways chain pin serving a hang: the trunk is free (the pin arrives by a lead and a
                    # drop when the trunk stands off the pin's height), preferring the pin's own height.
                    hung_free[hang.wire] = pin(ep)[1]
                    wire_y[hang.wire] = model.NewIntVar(-limit, limit, f"trunk:{hang.wire}")
                    wire_row.pop(hang.wire, None)
                else:
                    wire_y[hang.wire] = pin(ep)[1]

    # A wire between chain pins that do not all lie at one row height, and that no hang serves, has no
    # height of its own to inherit: its trunk is free, reached by straight drops (outward along each
    # pin's normal) and arrival leads, and it prefers the height of its first chain pin on a tie.
    hung_wires = {h.wire for h in plan.hangs}
    free_nets = []
    for name, (net, eps) in sorted(net_map.items()):
        chain_eps = [p for p in eps if p[0] in memberships]
        if (net.style != "wire" or name in row_y or name in free_trunks or name in hung_wires
                or name not in wire_y or len(chain_eps) < 2):
            continue
        heights = set()
        for ref, number in chain_eps:
            chosen = row_endpoint(ref)
            offset = geoms[ref].pins[chosen[1]].point[1] if chosen[1] else 0
            heights.add((memberships[ref], round(geoms[ref].pins[number].point[1] - offset, 7)))
        if len(heights) > 1:
            free_nets.append((name, wire_y[name]))
            wire_y[name] = model.NewIntVar(-limit, limit, f"trunk:{name}")
            wire_row.pop(name, None)

    def wire(name, parts):
        if name not in net_map or not net_map[name][1]:
            raise LadderInfeasible(["wires"], parts, f"missing wire {name}")
        if name not in wire_y:
            # A wire without a chain host (column or supply group) has its row
            # at the first endpoint. Column constraints keep it a straight run.
            wire_y[name] = pin(net_map[name][1][0])[1]
        return wire_y[name]

    hang_distances = defaultdict(list)
    alignment = []
    between = defaultdict(list)
    free_nets += sorted(hung_free.items())
    for name, first in free_nets:
        alignment.append(absolute(wire_y[name] - first, f"trunk tie:{name}"))
    for hang in sorted(plan.hangs, key=lambda h: (h.wire, h.side, h.rank, h.reference)):
        ref = hang.reference
        if ref not in geoms or hang.orientation != plan.orientations[ref]:
            raise LadderInfeasible(["orientation"], [ref], "hang orientation disagrees")
        ep = hang_endpoint(hang)
        host_x, host_y = pin(ep)
        candidates = [p for p in net_map.get(hang.wire, (None, ()))[1] if p[0] == ref]
        if len(candidates) != 1:
            raise LadderInfeasible(["hangs"], [ref, ep[0]], "hang must have one pin on wire")
        if pin_net.get(ep) != hang.wire:
            raise LadderInfeasible(["hangs"], [ref, ep[0]], "host not on hang wire")
        # The signal tip is the contact: the symbol's box can reach past it (padding, not body of a
        # lead), so the part is on its declared side when the tip's pin points toward the wire.
        signal_normal = geoms[ref].pins[candidates[0][1]].normal
        if ((hang.side == "below" and signal_normal[1] > -.5) or
                (hang.side == "above" and signal_normal[1] < .5)):
            raise LadderInfeasible(["hang row", "hang side"], [ref, ep[0]],
                                   f"{hang.side} hang's signal pin does not point toward its wire")
        px, py = pin(candidates[0])
        if hang.wire in stacks:
            # Sideways from the junction wire: the tip lands on it, anywhere between the two parts' tips, and the
            # body lies on the declared side; the rank orders the hangs of one side by distance down the wire.
            if hang.side not in ("left", "right") or ep not in stack_pins[hang.wire][0]:
                raise LadderInfeasible(["hang side"], [ref, ep[0]], "a stack's hang is left or right of its upper pin")
            top, bottom = stack_span(hang.wire)
            require(px == stack_x(hang.wire), "stack hang", [ref, ep[0]])
            require(py >= top, "stack hang", [ref, ep[0]])
            require(py <= bottom, "stack hang", [ref, ep[0]])
            l, t, r, b = rect(ref)
            require(r <= host_x if hang.side == "left" else l >= host_x, "hang side", [ref, ep[0]])
            hang_distances[hang.wire, hang.side, hang.host].append((hang.rank, ref, absolute(py - host_y, f"stack hang distance:{ref}")))
            continue
        wy = wire(hang.wire, [ref, ep[0]])
        normal = geoms[ep[0]].pins[ep[1]].normal
        toward = ((hang.side == "below" and normal[1] > .5) or
                  (hang.side == "above" and normal[1] < -.5))
        if toward:
            require(wy >= host_y if hang.side == "below" else wy <= host_y,
                    "trunk drop", [ref, ep[0]])
        elif hang.wire not in row_y and hang.wire not in free_trunks and hang.wire not in hung_free:
            require(wy == host_y, "hang wire", [ref, ep[0]])
        if hang.wire in free_trunks:
            direction = 1 if hang.side == "below" else -1 if hang.side == "above" else 0
            for feeder in net_map[hang.wire][1]:
                if feeder[0] in memberships and direction * geoms[feeder[0]].pins[feeder[1]].normal[1] > .5:
                    require(direction * wy >= direction * pin(feeder)[1], "trunk drop", [ref, feeder[0]])
            vertical = [p for p in net_map[hang.wire][1] if p[0] in memberships and
                        direction * geoms[p[0]].pins[p[1]].normal[1] > .5]
            horizontal = [p for p in net_map[hang.wire][1] if p[0] in memberships and
                          abs(geoms[p[0]].pins[p[1]].normal[0]) > .5]
            if len(vertical) == 1:
                alignment.append(absolute(px - pin(vertical[0])[0], f"column tie:{ref}"))
            if len(horizontal) == 1:
                alignment.append(absolute(wy - pin(horizontal[0])[1], f"row tie:{ref}"))
        require(py == wy, "hang row", [ref, ep[0]])
        if len(geoms[ref].pins) == 1 and hang.side in ("below", "above") and abs(normal[0]) > .5 and abs(normal[1]) < .5:
            # A tap (a one-pin part) on a sideways host pin: the net is that one wire, which the router draws from the pin
            # along its row. A tap whose tip is off that row stands clear of it (a grid each side), not across the corridor.
            reach = 10 * _GRID
            left, right = (host_x, host_x + reach) if normal[0] > 0 else (host_x - reach, host_x)
            level = model.NewBoolVar(f"hang level:{ref}")           # the tip on the host's row: the wire is that row's own
            model.Add(py == host_y).OnlyEnforceIf(level)
            model.Add(py != host_y).OnlyEnforceIf(level.Not())
            disjoint(rect(ref), (left, host_y - _GRID, right, host_y + _GRID), "hang corridor", [ref, ep[0]], waived=level)
        hang_links.append((ref, _mm(geoms[ref].pins[candidates[0][1]].point[1]), wy))
        l, t, r, b = rect(ref)
        if hang.side in ("below", "above"):
            pass        # the signal pin's direction, checked above, puts the part on its side of the wire
        elif hang.side == "left":
            require(r <= host_x, "hang side", [ref, ep[0]])
        elif hang.side == "right":
            require(l >= host_x, "hang side", [ref, ep[0]])
        else:
            raise LadderInfeasible(["hang side"], [ref], hang.side)
        # Bound to structural hosts, not to a span enlarged by the hang itself.
        fixed = [pin(p)[0] for p in net_map[hang.wire][1] if p[0] in memberships]
        if hang.wire not in row_y and len(fixed) >= 2:
            lo, hi = extrema(fixed, f"hang span:{ref}")
            require(px >= lo, "hang span", [ref, ep[0]])
            require(px <= hi, "hang span", [ref, ep[0]])
        distance = absolute(px - host_x, f"hang distance:{ref}")
        hang_distances[hang.wire, hang.side, hang.host].append((hang.rank, ref, distance))
        if hang.wire in wire_row:
            between[wire_row[hang.wire], hang.side].append(ref)
    for items in hang_distances.values():
        for rank_a, a, da in items:
            for rank_b, b, db in items:
                if rank_a < rank_b:
                    require(da <= db, "hang rank", [a, b])

    for span in sorted(plan.spans, key=lambda s: s.reference):
        ref = span.reference
        if ref not in geoms or span.orientation != plan.orientations[ref]:
            raise LadderInfeasible(["orientation"], [ref], "span orientation disagrees")
        ends = []
        for name in (span.from_wire, span.to_wire):
            candidates = [p for p in net_map.get(name, (None, ()))[1] if p[0] == ref]
            if len(candidates) != 1:
                raise LadderInfeasible(["spans"], [ref], "span needs one pin on each wire")
            ends.append(candidates[0])
        a, b = ends
        pa, pb = geoms[ref].pins[a[1]].point, geoms[ref].pins[b[1]].point
        # A part between a junction wire and an ordinary wire (a bootstrap capacitor from the controller to the bridge's
        # output) is placed by its ordinary end alone: its junction pin, like any other part's pin on the junction net that
        # the plan does not land on the wire, is wired to it afterwards (``attach_unplanned``).
        if span.from_wire in stacks and span.to_wire in stacks:
            # A part across two stacks (a load across a bridge): a tip on each junction wire, at the one height its
            # two pins share, within both wires.
            if abs(pa[1] - pb[1]) > 1e-7:
                raise LadderInfeasible(["spans"], [ref], "a part across stacks has both tips on junction wires, level")
            require(pin(a)[0] < pin(b)[0], "stack span order", [ref])       # the from_wire stack is the left one
            for name, end in ((span.from_wire, a), (span.to_wire, b)):
                top, bottom = stack_span(name)
                require(pin(end)[0] == stack_x(name), "stack span", [ref])
                require(pin(end)[1] >= top, "stack span", [ref])
                require(pin(end)[1] <= bottom, "stack span", [ref])
            continue
        wa, wb = wire(span.from_wire, [ref]), wire(span.to_wire, [ref])
        if abs(pa[0] - pb[0]) < 1e-7:
            upper, lower, wu, wl = (a, b, wa, wb) if pa[1] < pb[1] else (b, a, wb, wa)
            require(pin(upper)[1] == wu, "span rows", [ref])
            require(pin(lower)[1] <= wl, "span rows", [ref])
        elif abs(pa[1] - pb[1]) < 1e-7 and ref in lane_spans:
            require(pin(a)[1] == row_y[lane_spans[ref]], "lane series", [ref])
        elif abs(pa[1] - pb[1]) < 1e-7:
            require(rect(ref)[3] + gap <= wa, "feedback row", [ref])
            require(rect(ref)[3] + gap <= wb, "feedback row", [ref])
            chain_hosts = {r for name in (span.from_wire, span.to_wire)
                           for r, _ in net_map[name][1] if r in memberships}
            for host in sorted(chain_hosts):
                for number, p in sorted(geoms[host].pins.items()):
                    name = pin_net.get((host, number))
                    if name and net_map[name][0].style == "power" and p.normal[1] < -.5:
                        require(rect(ref)[3] + gap <= pin((host, number))[1] - pitch,
                                "feedback rail slot", [ref, host])
        else:
            raise LadderInfeasible(["spans"], [ref], "span pins must share an axis")

    stacked = [c for c in plan.columns if c.pin.startswith("net:")]
    for left, right in zip(stacked, stacked[1:]):       # the plan lists the stacks left to right
        for a in left.order:
            for b in right.order:
                require(rect(a)[2] + gap <= rect(b)[0], "stack column order", [a, b])

    for column in sorted(plan.columns, key=lambda c: c.pin):
        if column.pin.startswith("net:"):
            continue            # a stack: its constraints are written with its net
        if not column.pin:
            if len(column.order) != len(column.orientations):
                raise LadderInfeasible(["columns"], column.order, "orientation count")
            for ref, orientation in zip(column.order, column.orientations):
                if ref not in geoms or orientation != plan.orientations[ref]:
                    raise LadderInfeasible(["orientation"], [ref], "column orientation disagrees")
                require(x[ref] == x[column.order[0]], "margin column", column.order)
                for other in memberships:
                    require(rect(ref)[2] + gap <= rect(other)[0], "margin column", [ref, other])
                if rows:
                    require(rect(ref)[3] + gap <= row_y[rows[0].name], "margin column", [ref])
            for a, b in zip(column.order, column.order[1:]):
                require(rect(a)[3] + gap <= rect(b)[1], "column order", [a, b])
            continue
        host = endpoint(column.pin)
        hx, hy = pin(host)
        if len(column.order) != len(column.orientations):
            raise LadderInfeasible(["columns"], column.order, "orientation count")
        members = {host[0], *column.order}
        for ref, orientation in zip(column.order, column.orientations):
            if ref not in geoms or orientation != plan.orientations[ref]:
                raise LadderInfeasible(["orientation"], [ref], "column orientation disagrees")
            connections = [p for _, eps in net_map.values() for p in eps if p[0] == ref
                           and any(other[0] != ref and other[0] in members for other in eps)]
            if not connections:
                raise LadderInfeasible(["columns"], [ref], "column member has no connection to its column")
            for ep in connections:
                require(pin(ep)[0] == hx, "column alignment", [ref, host[0]])
        for a, b in zip(column.order, column.order[1:]):
            require(rect(a)[3] + gap <= rect(b)[1], "column order", [a, b])
        if column.order:
            normal = geoms[host[0]].pins[host[1]].normal
            if normal[1] > 0:
                require(rect(column.order[0])[1] >= hy + gap, "column host", [host[0], column.order[0]])
            elif normal[1] < 0:
                require(rect(column.order[-1])[3] <= hy - gap, "column host", [host[0], column.order[-1]])
            else:
                raise LadderInfeasible(["columns"], [host[0]], "column host must face up or down")

    for previous, current in zip(rows, rows[1:]):
        row_edges.append((row_index[previous.name], row_index[current.name], 0))
        # Parts sitting on a row are not parts between the rows. Their body
        # clearance is already enforced separately; intervening hangs set the
        # row distance, and spans constrain the two wires they actually join.
        if "lane" in (previous.kind, current.kind):
            # A lane lies at its hub pin's height whatever the pin pitch, so it keeps the rows' order and
            # nothing more: body and wire clearance already hold its parts apart from their neighbours.
            require(row_y[current.name] >= row_y[previous.name], "row order", [*previous.order, *current.order])
            continue
        involved = set(between[current.name, "above"]) | set(between[previous.name, "below"])
        height = max((bodies[r][3] - bodies[r][1] for r in involved), default=0)
        require(row_y[current.name] >= row_y[previous.name] + height + 2 * gap,
                "row spacing", set(previous.order) | involved)
        row_edges.append((row_index[previous.name], row_index[current.name], height + 2 * gap))
    for row in rows:
        if row.above in row_y:
            require(row_y[row.name] >= row_y[row.above] + 2 * gap, "row stacking", row.order)
            row_edges.append((row_index[row.above], row_index[row.name], 2 * gap))
        elif row.above is not None:
            # A lane can name the hub pin rather than another row.
            ep = endpoint(row.above)
            order = lane_order[row.name]
            require(row_y[row.name] == pin(ep)[1], "lane order", [ep[0], *order])
            nx = round(geoms[ep[0]].pins[ep[1]].normal[0])
            if not nx:
                raise LadderInfeasible(["lane host"], [ep[0], *order], "lane host must face sideways")
            behind = pin(ep)[0]
            for ref in order:
                if ref in lane_spans:
                    span = span_by_ref[ref]
                    near = lane_pin(ref, span.from_wire)
                    far = lane_pin(ref, span.to_wire) if near is not None and len(
                        {round(p.point[1], 7) for p in geoms[ref].pins.values()}) == 1 else near
                else:
                    near = far = row_endpoint(ref)
                if near is None:
                    raise LadderInfeasible(["lanes"], [ep[0], ref], "lane part has no pin on its lane's net")
                require(nx * (pin(near)[0] - behind) >= 2 * _GRID, "lane exit", [ep[0], ref])
                behind = pin(far)[0]

    # Same-side rail pins reserve one pitch each between a chain part and its
    # neighbour. Count by the pin's outward side; power nets never become buses.
    for row in rows:
        for a, b in zip(row.order, row.order[1:]):
            if row.kind == "lane":
                break
            slots = 0
            for ref, direction in ((a, 1), (b, -1)):
                if len(geoms[ref].pins) < 3:
                    continue
                neighbour = b if ref == a else a
                chain_side = any(
                    p.normal[0] * direction > .5 and
                    (name := pin_net.get((ref, number))) is not None and
                    any(other[0] == neighbour for other in net_map[name][1])
                    for number, p in geoms[ref].pins.items())
                if not chain_side:
                    continue
                for number, p in sorted(geoms[ref].pins.items()):
                    name = pin_net.get((ref, number))
                    if name and net_map[name][0].style == "power" and p.normal[0] * direction > .5:
                        slots += 1
            if slots:
                require(x[b] * _GRID + bodies[b][0] >= x[a] * _GRID + bodies[a][2] + gap + slots * pitch,
                        "rail-pin slots", [a, b])

    feedback_refs = {s.reference for s in plan.spans if s.reference not in lane_spans
                     and len({round(p.point[1], 7) for p in geoms[s.reference].pins.values()}) == 1}
    feedback_inputs = {(s.reference, s.from_wire) for s in plan.spans if s.reference in feedback_refs}
    tip_offsets = {}

    def forbidden_tips(a, b):
        """Grid offsets (x[a] - x[b], y[a] - y[b]) that put two pin tips closer than two grids."""
        if (a, b) not in tip_offsets:
            forbidden = set()
            for ap in geoms[a].pins.values():
                for bp in geoms[b].pins.values():
                    ox, oy = (_mm(ap.point[i]) - _mm(bp.point[i]) for i in (0, 1))
                    for gx in range(math.floor((-2 * _GRID - ox) / _GRID), math.ceil((2 * _GRID - ox) / _GRID) + 1):
                        for gy in range(math.floor((-2 * _GRID - oy) / _GRID), math.ceil((2 * _GRID - oy) / _GRID) + 1):
                            if (gx * _GRID + ox) ** 2 + (gy * _GRID + oy) ** 2 < (2 * _GRID) ** 2:
                                forbidden.add((gx, gy))
            tip_offsets[a, b] = frozenset(forbidden)
        return tip_offsets[a, b]

    # Chain order along a row already separates its parts ('chain order' above is the same inequality),
    # so a pair whose order the plan fixes needs no second statement of it.
    ordered = set()
    for order in [*(r.order for r in rows if r.kind != "lane"), *getattr(plan, "sequence", ())]:
        ordered.update(combinations([r for r in order if r in geoms], 2))
    ordered.update({(b, a) for a, b in ordered})

    implied = defaultdict(int)       # by family, the exclusions another one makes unnecessary
    candidates = []         # (key, boxes, emit, check) of exclusions not written yet
    deferred = []           # (key, emit, check, boxes, kind) of exclusions left out until a solution breaks one
    kept = set(learned or ())         # what a diagnosis writes: what the solve that proved infeasibility had written
    emitted = set()
    cache = {}

    prune_state = {}
    pruned_checks = []          # (key, check) of what was left out, to test against solutions (``_VERIFY_PRUNE``)

    def vertical_structure():
        """The least distance between row heights (longest path over the row constraints) and the heights of hung parts."""
        if prune_state:
            return prune_state
        count = len(rows)
        least = [[None] * count for _ in range(count)]
        for i in range(count):
            least[i][i] = 0
        for j in range(count):
            for i in range(j):
                best = None
                for a, b, w in row_edges:
                    if b == j and a >= i and a < j and least[i][a] is not None:
                        value = least[i][a] + w
                        best = value if best is None or value > best else best
                least[i][j] = best
        prune_state["least"] = least
        prune_state["rowvar"] = {row_y[r.name].Index(): row_index[r.name] for r in rows}
        prune_state["yvar"] = {y[r].Index(): r for r in refs}
        for _ in range(len(hang_links) + 1):
            for ref, local, wy in hang_links:
                if ref not in ysym:
                    s = symbolic(wy)
                    if s is not None:
                        ysym[ref] = (s[0], s[1] - local)
        return prune_state

    def symbolic(expression):
        """(row index, k) when the expression is the height of a row plus k, or of something at a fixed offset from one."""
        if isinstance(expression, int):
            return None
        flat = model.parse_linear_expression(expression)
        if len(flat.vars) != 1:
            return None
        var, coefficient = flat.vars[0], flat.coeffs[0]
        state_ = prune_state
        if var in state_["rowvar"] and coefficient == 1:
            return state_["rowvar"][var], flat.offset
        ref = state_["yvar"].get(var)
        if ref is not None and ref in ysym and coefficient == _GRID:
            return ysym[ref][0], ysym[ref][1] + flat.offset
        return None

    def distance(i, j):
        value = prune_state["least"][i][j]
        return None if value is None else value

    def sym_lower(a, b):
        """A (row, k) no higher than either a or b, or None."""
        if a is None or b is None:
            return None
        (r1, c1), (r2, c2) = (a, b) if a[0] <= b[0] else (b, a)
        if r1 == r2:
            return r1, min(c1, c2)
        d = distance(r1, r2)
        return (r1, min(c1, c2 + d)) if d is not None else (r1, min(c1, c2))      # row order alone: at least r1's height

    def sym_upper(a, b):
        """A (row, k) no lower than either a or b, or None."""
        if a is None or b is None:
            return None
        (r1, c1), (r2, c2) = (a, b) if a[0] <= b[0] else (b, a)
        if r1 == r2:
            return r1, max(c1, c2)
        d = distance(r1, r2)
        return (r2, max(c2, c1 - d)) if d is not None else None

    def bound(expression, upper):
        """A (row, k) the expression cannot exceed (``upper``) or fall below, or None."""
        s = symbolic(expression)
        if s is not None:
            return s
        if isinstance(expression, int):
            return None
        flat = model.parse_linear_expression(expression)
        if len(flat.vars) != 1 or flat.coeffs[0] != 1 or flat.vars[0] not in extremes:
            return None
        kind, items = extremes[flat.vars[0]]
        parts = [bound(item, upper) for item in items]
        if any(p is None for p in parts):
            return None
        if kind == "lo" and not upper:                    # the least of them: no lower than the lowest bound
            folded = parts[0]
            for p in parts[1:]:
                folded = sym_lower(folded, p)
                if folded is None:
                    return None
        elif kind == "hi" and upper:                      # the greatest: no higher than the highest bound
            folded = parts[0]
            for p in parts[1:]:
                folded = sym_upper(folded, p)
                if folded is None:
                    return None
        elif kind == "hi":                                # the greatest is no lower than any one of them
            folded = parts[0]
        elif kind == "lo":                                # the least is no higher than any one of them
            folded = parts[0]
        else:
            return None
        return folded[0], folded[1] + flat.offset

    def interval(box):
        """(lowest, highest) heights a box can stand at, as (row, k) bounds, or None."""
        ends = (box[1], box[3])
        lows = [bound(e, False) for e in ends]
        highs = [bound(e, True) for e in ends]
        if any(v is None for v in lows) or any(v is None for v in highs):
            return None
        return sym_lower(*lows), sym_upper(*highs)

    def prunable(boxes):
        """Whether the plan keeps these two boxes apart in height: the one's highest point is a unit above the other's lowest
        under the row order and the least distances between rows, so that no exclusion between them can ever bind."""
        vertical_structure()
        a, b = (interval(box) for box in boxes)
        if a is None or b is None or None in a or None in b:
            return False

        def above(first, second):          # first's highest point is above second's lowest, by at least a unit
            (r1, c1), (r2, c2) = first[1], second[0]
            if r1 > r2:
                return False
            d = distance(r1, r2)
            return d is not None and c1 + 1 <= c2 + d

        return above(a, b) or above(b, a)

    def lazy(key, boxes, emit, check, kind=("overlap",)):
        """An exclusion between two things whose boxes are ``boxes`` (None: always written).

        It is written when the model is first solved if the pair stands close together in the first
        placement found (``select``), and otherwise when a solution breaks it (``refine``). The check
        decides that from the solution's numbers; it is the exclusion itself restated.

        An exclusion between things the plan keeps apart in height is not written at all (``prunable``).
        """
        if boxes is not None and _PRUNE and not diagnose and prunable(boxes):
            implied["pruned " + str(key[0])] += 1
            if _VERIFY_PRUNE:
                pruned_checks.append((key, check))
            return
        if boxes is None or key in kept or (diagnose and learned is None):
            emit()
            emitted.add(key)
        elif not diagnose:
            candidates.append((key, boxes, emit, check, kind))

    def memo(key, build):
        if key not in cache:
            cache[key] = build()
        return cache[key]

    def body_rect(ref, margin):
        return memo(("rect", ref, margin), lambda: rect(ref, margin))

    global_body = _GLOBAL_BODY and not diagnose and gap % 2 == 0
    if global_body:
        # One constraint over every body grown by half the gap: two of them stay apart exactly when the bodies keep the gap.
        half = gap // 2
        model.AddNoOverlap2D(
            [model.NewFixedSizeIntervalVar(x[ref] * _GRID + bodies[ref][0] - half, bodies[ref][2] - bodies[ref][0] + gap,
                                           f"body x:{ref}") for ref in refs],
            [model.NewFixedSizeIntervalVar(y[ref] * _GRID + bodies[ref][1] - half, bodies[ref][3] - bodies[ref][1] + gap,
                                           f"body y:{ref}") for ref in refs])
        implied["global: body boxes"] = len(refs)
    for a, b in combinations(refs, 2):
        if global_body:
            if (a, b) not in ordered:
                emitted.add(("body", a, b))         # written, for a diagnosis that lists what the search wrote
        elif (a, b) in ordered:
            implied["body"] += 1
        else:
            lazy(("body", a, b), None,
                 lambda a=a, b=b: disjoint(body_rect(a, gap), body_rect(b, 0), "body clearance", [a, b]),
                 lambda v, a=a, b=b: _overlap(v(body_rect(a, gap)), v(body_rect(b, 0))))

        def separate(a=a, b=b):
            dx = model.NewIntVar(-2 * bound_x, 2 * bound_x, f"tip dx:{a}:{b}")
            dy = model.NewIntVar(-2 * bound_y, 2 * bound_y, f"tip dy:{a}:{b}")
            model.Add(dx == x[a] - x[b])
            model.Add(dy == y[a] - y[b])
            model.AddForbiddenAssignments([dx, dy], sorted(forbidden_tips(a, b))).OnlyEnforceIf(
                group("pin separation", [a, b]))

        lazy(("tips", a, b), None, separate,
             lambda v, a=a, b=b: (v(x[a]) - v(x[b]), v(y[a]) - v(y[b])) in forbidden_tips(a, b))

    lengths, wire_rects, segments, power_rects, contacts = [], [], [], [], []
    drawn_wires = {}            # net -> what its routes are made of, read off the solution at the end
    through_pairs = []          # (key, (wire rectangle, joined part's whole body, ref, pieces)): priced, not excluded
    through_terms = []
    through_vars = []           # (term, wire rectangle, pieces, [(separator literal, left side, right side)]), to be hinted

    def add_wire(rectangle, joined, name, start, end, horizontal, stub=False):
        wire_rects.append((rectangle, joined, name, stub))
        segments.append((start, end, horizontal, joined, name))

    for name, (net, eps) in sorted(net_map.items()):
        if net.style != "wire" or len(eps) < 2:
            continue
        if name in stacks:
            # One straight vertical wire between the two tips; everything else lands on it with no wire of its own.
            top, bottom = stack_span(name)
            jx = stack_x(name)
            lengths.append(bottom - top)
            add_wire((jx - 1, top, jx + 1, bottom), {r for r, _ in eps}, name, (jx, top), (jx, bottom), False)
            runs = []
            for end, side in zip(stacks[name], stack_pins[name]):
                owner = end[0]
                xs = sorted({_mm(geoms[owner].pins[p[1]].point[0]) for p in side})
                if len(xs) > 1:         # the tips differ in x: a run at the junction's end joins them
                    level = pin(end)[1]
                    lo, hi = x[owner] * _GRID + xs[0], x[owner] * _GRID + xs[-1]
                    lengths.append(hi - lo)
                    add_wire((lo, level - 1, hi, level + 1), {owner}, name, (lo, level), (hi, level), True)
                    contacts.append(((lo, level), {owner}, name))
                    contacts.append(((hi, level), {owner}, name))
                    runs.append((lo, hi, level, side))
            drawn_wires[name] = ("stack", jx, top, bottom, runs)
            contacts.append(((jx, top), {stacks[name][0][0]}, name))
            contacts.append(((jx, bottom), {stacks[name][1][0]}, name))
            continue
        wy = wire(name, [r for r, _ in eps])
        feet = []
        joined = {r for r, _ in eps}
        legs = []                   # (pin, its tip's x, y, the x it drops from, whether that is a lead's end)
        for ep in eps:
            px, py = pin(ep)
            tip_x = px
            if ep[0] in feedback_refs:
                jx = model.NewIntVar(-limit, limit, f"feedback junction:{name}:{ep}")
                fixed = [p for p in eps if p[0] in memberships]
                if name not in row_y and len(fixed) >= 2:
                    low, high = extrema([pin(p)[0] for p in fixed], f"feedback span:{name}:{ep}")
                    require(jx >= low, "feedback junction", [ep[0], *(p[0] for p in fixed)])
                    require(jx <= high, "feedback junction", [ep[0], *(p[0] for p in fixed)])
                elif len(fixed) == 1:
                    host = fixed[0]
                    direction = round(geoms[host[0]].pins[host[1]].normal[0])
                    if direction:
                        require(direction * (jx - pin(host)[0]) >= gap,
                                "feedback junction", [ep[0], host[0]])
                direction = round(geoms[ep[0]].pins[ep[1]].normal[0])
                if direction:
                    require(direction * (jx - px) >= 0, "feedback lead", [ep[0]])
                    if (ep[0], name) in feedback_inputs:
                        require(jx == px + direction * gap, "feedback return", [ep[0]])
                        for host in fixed:
                            geometry = geoms[host[0]]
                            if len(geometry.pins) < 3:
                                continue
                            normal = round(geometry.pins[host[1]].normal[0])
                            slots = sum(1 for number, p in geometry.pins.items()
                                        if (rail := pin_net.get((host[0], number))) is not None
                                        and net_map[rail][0].style == "power" and p.normal[0] * normal > .5)
                            if normal and slots:
                                require(normal * (px - pin(host)[0]) >= slots * pitch,
                                        "feedback rail slot", [ep[0], host[0]])
                low, high = extrema([px, jx], f"feedback run:{name}:{ep}")
                lengths.append(high - low)
                add_wire((low, py - 1, high, py + 1), {ep[0]}, name, (low, py), (high, py), True)
                contacts.append(((jx, py), {ep[0]}, name))
                px = jx
            else:
                nx, ny = (round(v) for v in geoms[ep[0]].pins[ep[1]].normal)
                if nx:
                    on_row = model.NewBoolVar(f"on row:{name}:{ep}")
                    model.Add(py == wy).OnlyEnforceIf(on_row)
                    above, below = (model.NewBoolVar(f"off row:{name}:{ep}") for _ in range(2))
                    model.Add(py < wy).OnlyEnforceIf(above)        # "not equal" as two bounds, which propagate
                    model.Add(py > wy).OnlyEnforceIf(below)
                    model.AddBoolOr([on_row, above, below])
                    fx = model.NewIntVar(-limit, limit, f"arrival:{name}:{ep}")
                    model.Add(fx == px).OnlyEnforceIf(on_row)
                    # Off the trunk's height the lead leaves the pin at least a gap, and as much further as
                    # it takes to clear what stands in its way (a rail pin's stub reservation, say).
                    model.Add(nx * (fx - px) >= gap).OnlyEnforceIf(on_row.Not())
                    model.Add(nx * (fx - px) <= gap + 3 * pitch).OnlyEnforceIf(on_row.Not())
                    low, high = extrema([px, fx], f"arrival run:{name}:{ep}")
                    lengths.append(high - low)
                    add_wire((low, py - 1, high, py + 1), {ep[0]}, name, (low, py), (high, py), True)
                    contacts.append(((fx, py), {ep[0]}, name))
                    px = fx
                elif ny:
                    require(ny * (wy - py) >= 0, "drop direction", [ep[0]])
            feet.append(px)
            legs.append((ep, tip_x, py, px, px is not tip_x))
            contacts.append(((px, wy), {ep[0]}, name))
            bottom, top = extrema([wy, py], f"drop:{name}:{ep}")
            lengths.append(top - bottom)
            add_wire((px - 1, bottom, px + 1, top), {ep[0]}, name, (px, bottom), (px, top), False)
        lo, hi = extrema(feet, f"wire:{name}")
        lengths.append(hi - lo)
        add_wire((lo, wy - 1, hi, wy + 1), joined, name, (lo, wy), (hi, wy), True)
        drawn_wires[name] = ("trunk", wy, lo, hi, legs)
    # Reserve the full terminal exit, including its width, for every rail pin.
    # Label conductors have no power-symbol reservation.
    terminal_reach = _mm(7.62)
    terminal_half_width = _mm(2.54) // 2
    for name, (net, eps) in sorted(net_map.items()):
        if net.style == "wire":
            continue
        for ep in eps:
            px, py = pin(ep)
            nx, ny = (round(v) for v in geoms[ep[0]].pins[ep[1]].normal)
            reach = terminal_reach if net.style == "power" else _mm(5.08)
            width = terminal_half_width if net.style == "power" else 1
            if nx:
                lo, hi = extrema([px, px + nx * reach], f"terminal x:{name}:{ep}")
                rectangle = (lo, py - width, hi, py + width)
                start, end = (lo, py), (hi, py)
            else:
                lo, hi = extrema([py, py + ny * reach], f"terminal y:{name}:{ep}")
                rectangle = (px - width, lo, px + width, hi)
                start, end = (px, lo), (px, hi)
            add_wire(rectangle, {ep[0]}, name, start, end, bool(nx), stub=net.style == "power")
            if net.style == "power":
                power_rects.append((rectangle, {ep[0]}, name))
            else:
                contacts.append(((px + nx * reach, py + ny * reach), {ep[0]}, name))
    # One gap between a conductor and a body, measured once (the body is grown, the conductor is a line). A
    # conductor is not held a gap from parts of its own net, which it must reach, except that a rail pin's
    # stub reservation is kept a gap from every body that is not its own pin's.
    hung_refs = {hang.reference for hang in plan.hangs}

    def joined_pieces(ref, name):
        """The body of ``ref`` less the exit corridors of its pins on net ``name``, as rectangles relative to its anchor."""
        pieces = [bodies[ref]]
        for number, p in sorted(geoms[ref].pins.items()):
            if pin_net.get((ref, number)) != name:
                continue
            tx, ty = _mm(p.point[0]), _mm(p.point[1])
            nx, ny = round(p.normal[0]), round(p.normal[1])
            half = _GRID // 2
            left, top, right, bottom = bodies[ref]
            # from the tip outward to the body's edge, if the tip is inside the body
            if nx > 0 and left <= tx < right:
                cut = (tx, ty - half, right, ty + half)
            elif nx < 0 and left < tx <= right:
                cut = (left, ty - half, tx, ty + half)
            elif ny > 0 and top <= ty < bottom:
                cut = (tx - half, ty, tx + half, bottom)
            elif ny < 0 and top < ty <= bottom:
                cut = (tx - half, top, tx + half, ty)
            else:
                continue
            kept = []
            for l, t, r, b in pieces:
                cl, ct, cr, cb = max(l, cut[0]), max(t, cut[1]), min(r, cut[2]), min(b, cut[3])
                if cl >= cr or ct >= cb:
                    kept.append((l, t, r, b))
                    continue
                kept += [piece for piece in ((l, t, r, ct), (l, cb, r, b), (l, ct, cl, cb), (cr, ct, r, cb))
                         if piece[0] < piece[2] and piece[1] < piece[3]]
            pieces = kept
        # a conductor is a line drawn 2 micrometres thick: one that runs along a body's edge touches it, and is let
        return [(l + 1, t + 1, r - 1, b - 1) for l, t, r, b in pieces if r - l > 2 and b - t > 2]

    def piece_rect(ref, offsets):
        l, t, r, b = offsets
        return memo(("piece", ref, offsets), lambda: (x[ref] * _GRID + l, y[ref] * _GRID + t,
                                                      x[ref] * _GRID + r, y[ref] * _GRID + b))

    body_boxes = {}

    def body_intervals(ref):
        if ref not in body_boxes:
            l, t, r, b = bodies[ref]
            body_boxes[ref] = (model.NewFixedSizeIntervalVar(x[ref] * _GRID + l, r - l, f"body x:{ref}"),
                               model.NewFixedSizeIntervalVar(y[ref] * _GRID + t, b - t, f"body y:{ref}"))
        return body_boxes[ref]

    def wire_intervals(rectangle, grow, name):
        """The rectangle grown by ``grow`` as a pair of intervals, present when it has length (a drop of none is no
        conductor); None when it never has."""
        l, t, r, b = rectangle
        present = []
        for low, high in ((l, r), (t, b)):
            flat = model.parse_linear_expression(high - low)
            if not flat.vars:
                if flat.offset <= 0:
                    return None
                continue
            literal = model.NewBoolVar(f"{name}:long")
            model.Add(high - low >= 1).OnlyEnforceIf(literal)
            model.Add(high - low <= 0).OnlyEnforceIf(literal.Not())
            present.append(literal)
        if len(present) == 2:
            both = model.NewBoolVar(f"{name}:present")
            model.AddBoolAnd(present).OnlyEnforceIf(both)
            model.AddBoolOr([p.Not() for p in present]).OnlyEnforceIf(both.Not())
            present = [both]
        pair = []
        for low, high, axis in ((l, r, "x"), (t, b, "y")):
            size = model.NewIntVar(0, 2 * limit + 2 * grow, f"{name}:{axis} size")      # an interval's size is a variable
            model.Add(size == high - low + 2 * grow)
            if present:
                pair.append(model.NewOptionalIntervalVar(low - grow, size, high + grow, present[0], f"{name}:{axis}"))
            else:
                pair.append(model.NewIntervalVar(low - grow, size, high + grow, f"{name}:{axis}"))
        return pair

    for index, (rectangle, joined, name, stub) in enumerate(wire_rects):
        on_net = {r for r, _ in net_map[name][1]}
        apart = []              # the bodies this conductor keeps the full gap from, for one constraint
        for ref in refs:
            if ref not in joined:
                if _GLOBAL_WIRE and not diagnose and not (ref in on_net and not stub):
                    apart.append(ref)
                    continue
                if ref in on_net and not stub:
                    # The checker keeps a route 0.5 mm from any body it is not joined to. The conductor may END at
                    # a part of its own net (a drop to the host's tip, which is on the host's body edge), so for
                    # these parts the ends are left out and the length alongside is kept the margin away.
                    margin, m = _mm(ROUTE_MARGIN_MM), _mm(ROUTE_MARGIN_MM)
                    l, t, r, b = rectangle
                    shortened = (l + m, t, r - m, b) if segments[index][2] else (l, t + m, r, b - m)
                else:
                    margin, shortened = gap, rectangle
                lazy(("wire-body", index, ref), (shortened, body_rect(ref, margin)),
                     lambda r=shortened, j=joined, n=name, ref=ref, m=margin:
                         disjoint(r, body_rect(ref, m), f"wire-body:{n}", [ref, *j]),
                     lambda v, r=shortened, ref=ref, m=margin: _overlap(v(r), v(body_rect(ref, m))))
                if shortened is not rectangle and _FULL_MARGIN:
                    # ...but the checker lets a route end at a part's tip only where it passes over one of that part's
                    # pins on the net; elsewhere it keeps the margin all along, ends included.
                    tips = [pin(ep) for ep in net_map[name][1] if ep[0] == ref]
                    horizontal = segments[index][2]
                    across = rectangle[1] + 1 if horizontal else rectangle[0] + 1
                    axis = 1 if horizontal else 0
                    lazy(("wire-body-full", index, ref), (rectangle, body_rect(ref, margin)),
                         lambda r=rectangle, n=name, ref=ref, m=margin, j=joined, tips=tips, across=across, axis=axis:
                             full_margin(r, body_rect(ref, m), across, [t[axis] for t in tips], f"wire-body-full:{n}",
                                         [ref, *j]),
                         lambda v, r=rectangle, ref=ref, m=margin, tips=tips, across=across, axis=axis:
                             _overlap(v(r), v(body_rect(ref, m))) and all(v(across) != v(t[axis]) for t in tips))
            elif net_map[name][0].style == "wire":
                # Through a part it joins too, except where a pin exits (the corridor from its tip to the body's edge):
                # the body less those corridors, as pieces. A rail stub is not a wire here. A hung part's body is priced
                # (_THROUGH_MM), and so is every joined body when the exclusion alone proved the plan infeasible (see
                # ``legalise``); otherwise it is excluded like any other.
                if ref in hung_refs or soft_joined:
                    if not diagnose:
                        through_pairs.append(((index, ref), (rectangle, body_rect(ref, 0), ref,
                                                             [piece_rect(ref, o) for o in joined_pieces(ref, name)])))
                    continue
                for piece, offsets in enumerate(joined_pieces(ref, name)):
                    lazy(("wire-joined", index, ref, piece), (rectangle, piece_rect(ref, offsets)),
                         lambda r=rectangle, n=name, ref=ref, o=offsets, piece=piece:
                             disjoint(r, piece_rect(ref, o), f"wire-joined:{n}:{piece}", [ref]),
                         lambda v, r=rectangle, ref=ref, o=offsets: _overlap(v(r), v(piece_rect(ref, o))))
        if apart:
            # The conductor grown by the gap against the bodies as they are: it stays off each of them, and the bodies, which
            # keep the gap from one another, stay off one another; no other conductor is in the constraint, for those may cross.
            pair = wire_intervals(rectangle, gap, f"wire {index}")
            if pair is not None:
                model.AddNoOverlap2D([pair[0], *(body_intervals(ref)[0] for ref in apart)],
                                     [pair[1], *(body_intervals(ref)[1] for ref in apart)])
                emitted.update(("wire-body", index, ref) for ref in apart)
                implied["global: wire groups"] += 1
                implied["global: wire boxes"] += len(apart) + 1
    for index, (reservation, joined_a, net_a) in enumerate(power_rects):
        for other, (rectangle, joined_b, net_b, _stub) in enumerate(wire_rects):
            if net_a != net_b:
                lazy(("power-exit", index, other), (reservation, rectangle),
                     lambda r=reservation, o=rectangle, a=net_a, b=net_b, j=joined_a | joined_b:
                         disjoint(r, o, f"power exit:{a}/{b}", j),
                     lambda v, r=reservation, o=rectangle: _overlap(v(r), v(o)))

    def point_off_segment(point, start, end, horizontal, name, parts):
        _point_off_segment(model, point, start, end, horizontal, group(name, parts), name)

    def pin_point(ep):
        return memo(("pin", ep), lambda: pin(ep))

    def inside_clearance(ref, number):
        """Whether a pin's tip is within the clearance a foreign conductor keeps from ``ref``'s body."""
        left, top, right, bottom = bodies[ref]
        px, py = (_mm(c) for c in geoms[ref].pins[number].point)
        return left - gap < px < right + gap and top - gap < py < bottom + gap

    for index, (start, end, horizontal, joined, name) in enumerate(segments):
        on_net = {r for r, _ in net_map[name][1]}
        for ref in refs:
            for number in sorted(geoms[ref].pins):
                ep = (ref, number)
                if pin_net.get(ep) != name:
                    if ref not in joined and (wire_rects[index][3] or ref not in on_net) and inside_clearance(ref, number):
                        implied["wire-pin"] += 1    # 'wire-body' keeps the conductor a gap from this body, which holds the tip
                        continue
                    lazy(("wire-pin", index, ep), ((*start, *end), (*pin_point(ep), *pin_point(ep))),
                         lambda ep=ep, st=start, en=end, h=horizontal, n=name, j=joined, ref=ref:
                             point_off_segment(pin_point(ep), st, en, h, f"wire-pin:{n}", [ref, *j]),
                         lambda v, ep=ep, st=start, en=end, h=horizontal:
                             _on_segment(v(pin_point(ep)), v(st), v(en), h),
                         ("point", horizontal))
    # Only an interior crossing of both conductors is permitted. A foreign
    # endpoint on any conductor is a short, including collinear overlap.
    # Every bus end is already one of its drop feet. Pin tips are checked above;
    # the remaining endpoints are feet, lead bends and label-stub ends. Avoid
    # duplicating shorts against min/max variables for the same physical point.
    seen_contacts = set()
    for number, (point, joined_a, net_a) in enumerate(contacts):
        key = (net_a, *(str(coordinate) for coordinate in point))
        if key in seen_contacts:
            continue
        seen_contacts.add(key)
        for index, (b_start, b_end, horizontal, joined_b, net_b) in enumerate(segments):
            if net_a != net_b and net_map[net_b][0].style != "power":
                lazy(("wire-end", number, index), ((*b_start, *b_end), (*point, *point)),
                     lambda p=point, st=b_start, en=b_end, h=horizontal, a=net_a, b=net_b, j=joined_a | joined_b:
                         point_off_segment(p, st, en, h, f"wire-end:{a}/{b}", j),
                     lambda v, p=point, st=b_start, en=b_end, h=horizontal: _on_segment(v(p), v(st), v(en), h),
                     ("point", horizontal))
    # Crossings are soft: a pair is reified into the objective once its segments stand close together in the
    # first placement found or, afterwards, when a solution crosses it. A term added after the objective has
    # been fixed must be zero, for the fixed value was already the minimum over every pair.
    crossing_terms = []
    crossing_state = {"live": False, "fixed": False}
    crossing_pairs = []
    for (i, a), (j, b) in combinations(enumerate(segments), 2):
        if (a[4] == b[4] or a[2] == b[2] or
                net_map[a[4]][0].style == "power" or net_map[b[4]][0].style == "power"):
            continue
        if frozenset((a[4], b[4])) in forbid:       # a debugging aid: these two nets may not cross at all
            h, v = (a, b) if a[2] else (b, a)
            model.Add(_interior_crossing(model, h[0], h[1], v[0], v[1]) == 0)
            continue
        crossing_pairs.append(((i, j), (a, b) if a[2] else (b, a)))

    def add_crossing(h, v):
        term = _interior_crossing(model, h[0], h[1], v[0], v[1])
        crossing_terms.append(term)
        if crossing_state["fixed"]:
            model.Add(term == 0)

    def add_through(rectangle, ref, pieces):
        term = model.NewBoolVar(f"through:{ref}")
        options = []
        for number, piece in enumerate(pieces):
            options += disjoint(rectangle, piece, f"wire-joined:{ref}:{number}", [ref], waived=term) or []
        through_terms.append(term)
        through_vars.append((term, rectangle, pieces, options))
        if crossing_state["fixed"]:
            model.Add(term == 0)

    through_state = {"live": False}

    def start_throughs(solver):
        """Price the wires through a joined body that stand close to it in ``solver``'s placement (all without one)."""
        through_state["live"] = True
        values = None if solver is None else evaluator(solver)
        rest = []
        for key, (rectangle, whole, ref, pieces) in through_pairs:
            if values is None or _close(values, rectangle, whole):
                add_through(rectangle, ref, pieces)
            else:
                rest.append((key, (rectangle, whole, ref, pieces)))
        through_pairs[:] = rest

    def through_check(values):
        added = 0
        if not through_state["live"]:
            return added
        rest = []
        for key, (rectangle, whole, ref, pieces) in through_pairs:
            if any(_overlap(values(rectangle), values(piece)) for piece in pieces):
                add_through(rectangle, ref, pieces)
                added += 1
            else:
                rest.append((key, (rectangle, whole, ref, pieces)))
        through_pairs[:] = rest
        return added

    def start_crossings(solver):
        """Reify the crossings of segments that stand close together in ``solver``'s placement (all without one)."""
        crossing_state["live"] = True
        values = None if solver is None else evaluator(solver)
        rest = []
        for key, (h, v) in crossing_pairs:
            if values is None or _close(values, (*h[0], *h[1]), (*v[0], *v[1])):
                add_crossing(h, v)
            else:
                rest.append((key, (h, v)))
        crossing_pairs[:] = rest

    left, right = extrema([x[ref] * _GRID + _mm(geoms[ref].box[i])
                           for ref in refs for i in (0, 2)], "extent x")
    top, bottom = extrema([y[ref] * _GRID + _mm(geoms[ref].box[i])
                           for ref in refs for i in (1, 3)], "extent y")

    def compact():
        # The first placement is searched with extent as dear as wire, so that a part nothing pulls on (one
        # whose wires end at parts the plan leaves out) is not left wherever the search puts it, a metre away.
        return _WIRE_WEIGHT * sum(lengths) + _WARM_EXTENT_WEIGHT * (right - left + bottom - top)

    def wire_extent():
        return (_WIRE_WEIGHT * (sum(lengths) + _mm(_THROUGH_MM) * sum(through_terms)) +
                _EXTENT_WEIGHT * (right - left + bottom - top))

    def weighted():
        # A crossing costs _CROSSING_MM of wire (soft: never infeasible), then wire, then extent at the checker's
        # 0.08 mm of wire per mm. The tie preference follows as a polish on the optimum found.
        return (_WIRE_WEIGHT * (sum(lengths) + _mm(_CROSSING_MM) * sum(crossing_terms) +
                                _mm(_THROUGH_MM) * sum(through_terms)) +
                _EXTENT_WEIGHT * (right - left + bottom - top))

    def crossing_check(values):
        added = through_check(values)
        if not crossing_state["live"]:
            return added
        rest = []
        for key, (h, v) in crossing_pairs:
            (hx0, hy), (hx1, _), (vx, vy0), (_, vy1) = values(h[0]), values(h[1]), values(v[0]), values(v[1])
            if hx0 < vx < hx1 and vy0 < hy < vy1:
                add_crossing(h, v)
                added += 1
            else:
                rest.append((key, (h, v)))
        crossing_pairs[:] = rest
        return added

    # Solve priorities separately to avoid overflowing a scalar objective.
    objectives = [("wire", wire_extent, _WIRE_SHARE, False), ("weighted", weighted, 1.0, True)]
    # A coordinate-free, conservative hint for the ordered row members helps
    # the feasibility phase avoid exploring arbitrary sheet translations.
    # These are search hints, never constraints or coordinates from the input.
    successors = {ref: set() for ref in memberships}
    for order in [*(r.order for r in rows), *getattr(plan, "sequence", ())]:
        for a, b in zip(order, order[1:]):
            if a in successors and b in successors:
                successors[a].add(b)
    pending = set(successors)
    hint_x = {}
    row_order = {ref: i for i, row in enumerate(rows) for ref in row.order}
    neighbours = {ref: set(successors[ref]) for ref in successors}
    for ref, following in successors.items():
        for other in following:
            neighbours[other].add(ref)
    component = {}
    for ref in sorted(successors):
        if ref in component:
            continue
        todo = [ref]
        while todo:
            other = todo.pop()
            if other in component:
                continue
            component[other] = ref
            todo.extend(sorted(neighbours[other]))
    cursors, previous = defaultdict(int), {}
    while pending:
        choices = sorted(r for r in pending if not any(r in successors[p] for p in pending))
        if not choices:
            raise LadderInfeasible(["chain order"], pending, "cyclic row/sequence order")
        ref = min(choices, key=lambda r: (row_order[r], r))
        key = component[ref]
        before = previous.get(key)
        if before is not None:
            cursors[key] += math.ceil((bodies[before][2] - bodies[ref][0] + 2 * gap +
                                      pitch * (len(geoms[before].pins) + len(geoms[ref].pins))) / _GRID)
        hint_x[ref] = cursors[key]
        previous[key] = ref
        pending.remove(ref)
    cursor = 0
    hint_y, hint_rows = {}, {}
    for row in rows:
        feedback = [ref for ref in feedback_refs if any(
            ep[0] in row.order for name, (_net, eps) in net_map.items() for ep in eps
            if any(p[0] == ref for p in eps))]
        if feedback:
            cursor += math.ceil((max(bodies[r][3] - bodies[r][1] for r in feedback) + 2 * gap + pitch) / _GRID) * _GRID
        hint_rows[row.name] = cursor
        for ref in row.order:
            ep = row_endpoint(ref)
            offset = _mm(geoms[ref].pins[ep[1]].point[1]) if ep[1] else 0
            hint_y[ref] = round((cursor - offset) / _GRID)
        height = max((bodies[r][3] - bodies[r][1] for r in row.order), default=0)
        cursor += math.ceil((height + 2 * gap + pitch) / _GRID) * _GRID
    def hint_pin(ep):
        ref, number = ep
        if ref not in hint_x or ref not in hint_y:
            return None
        point = geoms[ref].pins[number].point
        return hint_x[ref] * _GRID + _mm(point[0]), hint_y[ref] * _GRID + _mm(point[1])

    for column in sorted(plan.columns, key=lambda c: c.pin):
        if not column.order or column.pin.startswith("net:"):
            continue
        if not column.pin:
            left = min((hint_x[r] * _GRID + bodies[r][0] for r in memberships), default=0)
            right = max(bodies[r][2] for r in column.order)
            hx = math.floor((left - right - 2 * gap - pitch) / _GRID)
            bottom = min(hint_rows.values(), default=0) - 2 * gap - pitch
            for ref in reversed(column.order):
                hint_x[ref] = hx
                hint_y[ref] = math.floor((bottom - bodies[ref][3]) / _GRID)
                bottom = hint_y[ref] * _GRID + bodies[ref][1] - 2 * gap - 2 * pitch
        else:
            ep = endpoint(column.pin)
            point = hint_pin(ep)
            if point is None:
                continue
            normal = geoms[ep[0]].pins[ep[1]].normal[1]
            cursor = point[1] + (2 * gap if normal > 0 else -2 * gap)
            order = column.order if normal > 0 else column.order[::-1]
            for ref in order:
                connection = next((p for _net, eps in net_map.values() for p in eps if p[0] == ref
                                   and any(o[0] == ep[0] or o[0] in column.order for o in eps if o[0] != ref)), None)
                offset = _mm(geoms[ref].pins[connection[1]].point[0]) if connection else 0
                hint_x[ref] = round((point[0] - offset) / _GRID)
                edge = bodies[ref][1] if normal > 0 else bodies[ref][3]
                hint_y[ref] = (math.ceil if normal > 0 else math.floor)((cursor - edge) / _GRID)
                cursor = hint_y[ref] * _GRID + (bodies[ref][3] + 2 * gap + 2 * pitch if normal > 0
                                               else bodies[ref][1] - 2 * gap - 2 * pitch)
    hint_wires = dict(hint_rows)
    slot = defaultdict(int)
    wire_stride = {}
    for name in net_map:
        widths = [bodies[h.reference][2] - bodies[h.reference][0] for h in plan.hangs if h.wire == name]
        widths += [bodies[s.reference][2] - bodies[s.reference][0] for s in plan.spans if s.from_wire == name]
        wire_stride[name] = math.ceil(max([pitch, *(w + 2 * gap for w in widths)]) / _GRID) * _GRID
    for hang in sorted(plan.hangs, key=lambda h: (h.wire, h.host, h.side, h.rank, h.reference)):
        ep = hang_endpoint(hang)
        host = hint_pin(ep)
        if host is None:
            continue
        ref = hang.reference
        signal = next(p for p in net_map[hang.wire][1] if p[0] == ref)
        point = geoms[ref].pins[signal[1]].point
        direction = 1 if hang.side == "below" else -1
        feeders = [p for p in net_map[hang.wire][1] if p[0] in memberships
                   and direction * geoms[p[0]].pins[p[1]].normal[1] > .5]
        horizontal = [p for p in net_map[hang.wire][1] if p[0] in memberships
                      and abs(geoms[p[0]].pins[p[1]].normal[0]) > .5]
        wy = hint_wires.get(hang.wire, host[1])
        if hang.wire in free_trunks and hang.wire not in hint_wires:
            ends = [p for p in net_map[hang.wire][1] if p[0] in memberships]
            if horizontal:
                wy = hint_pin(horizontal[0])[1]
            else:
                edge = 3 if direction > 0 else 1
                heights = [hint_y[p[0]] * _GRID + bodies[p[0]][edge] + direction * 2 * gap for p in ends]
                wy = (max if direction > 0 else min)([host[1], *heights])
            if feeders:
                wy = (max if direction > 0 else min)([wy, *(hint_pin(p)[1] for p in feeders)])
            hint_wires[hang.wire] = wy
        if len(feeders) == 1:
            px = hint_pin(feeders[0])[0] + hang.rank * wire_stride[hang.wire]
        else:
            nx = round(geoms[ep[0]].pins[ep[1]].normal[0]) or 1
            slot[hang.wire, hang.host] += 1
            px = host[0] + nx * slot[hang.wire, hang.host] * wire_stride[hang.wire]
        hint_x[ref] = round((px - _mm(point[0])) / _GRID)
        hint_y[ref] = (math.ceil if direction > 0 else math.floor)((wy - _mm(point[1])) / _GRID)
    for span in sorted(plan.spans, key=lambda s: s.reference):
        ref = span.reference
        if ref in lane_spans:
            continue
        ends = [next(p for p in net_map[name][1] if p[0] == ref) for name in (span.from_wire, span.to_wire)]
        if ref in feedback_refs:
            hosts = [p for p in net_map[span.from_wire][1] if p[0] in memberships]
            if not hosts:
                continue
            host = min(hosts, key=lambda p: (geoms[p[0]].pins[p[1]].normal[0], -row_order[p[0]], p))
            px, _py = hint_pin(host)
            nx = round(geoms[host[0]].pins[host[1]].normal[0]) or -1
            px += nx * (pitch + gap)
            hint_x[ref] = round((px - _mm(geoms[ref].pins[ends[0][1]].point[0])) / _GRID)
            all_hosts = [p for name in (span.from_wire, span.to_wire)
                         for p in net_map[name][1] if p[0] in memberships]
            ceilings = [hint_y[p[0]] * _GRID + bodies[p[0]][1] - 2 * gap for p in all_hosts]
            for p in all_hosts:
                for number, geometry in geoms[p[0]].pins.items():
                    name = pin_net.get((p[0], number))
                    if name and net_map[name][0].style == "power" and geometry.normal[1] < -.5:
                        ceilings.append(hint_pin((p[0], number))[1] - pitch - gap)
            hint_y[ref] = math.floor((min(ceilings) - bodies[ref][3]) / _GRID)
        else:
            upper = min(ends, key=lambda p: geoms[ref].pins[p[1]].point[1])
            wire_name = pin_net[upper]
            hosts = [p for p in net_map[wire_name][1] if p[0] in memberships]
            if not hosts:
                continue
            host = max(hosts, key=lambda p: (hint_pin(p)[0], p))
            slot[wire_name, "spans"] += 1
            px = hint_pin(host)[0] + (len([h for h in plan.hangs if h.wire == wire_name]) +
                                     slot[wire_name, "spans"]) * wire_stride[wire_name]
            wy = hint_wires.get(wire_name, hint_pin(host)[1])
            hint_x[ref] = round((px - _mm(geoms[ref].pins[upper[1]].point[0])) / _GRID)
            hint_y[ref] = round((wy - _mm(geoms[ref].pins[upper[1]].point[1])) / _GRID)
    def add_search_hints():
        if layout and origin in layout:
            # The heuristic placer's layout, as grid anchors from the origin part, where it has the part; the coordinate-free
            # hint for the rest.
            ox, oy = layout[origin].position
            bx, by = bound_x, bound_y
            have = {}
            for ref in refs:
                if ref in layout:
                    have[ref] = (round((layout[ref].position[0] - ox) / GRID_MM), round((layout[ref].position[1] - oy) / GRID_MM))
                elif ref in hint_x and origin in hint_x:
                    have[ref] = (hint_x[ref] - hint_x[origin], hint_y[ref] - hint_y[origin])
            for ref, (gx, gy) in sorted(have.items()):
                model.AddHint(x[ref], max(-bx, min(bx, gx)))
                model.AddHint(y[ref], max(-by, min(by, gy)))
            for row in rows:
                if row.kind == "chain" and row.order and row.order[0] in plan.pins:
                    owner, number = row_endpoint(row.order[0])
                    if owner in have and owner in geoms:
                        offset = _mm(geoms[owner].pins[number].point[1]) if number in geoms[owner].pins else 0
                        model.AddHint(row_y[row.name], have[owner][1] * _GRID + offset)
            return
        if origin in hint_x:
            dx, dy = hint_x[origin], hint_y[origin]
            for ref in sorted(hint_x):
                model.AddHint(x[ref], hint_x[ref] - dx)
                model.AddHint(y[ref], hint_y[ref] - dy)
            for name in sorted(hint_rows):
                model.AddHint(row_y[name], hint_rows[name] - dy * _GRID)

    for ref, (gx, gy) in (anchors or {}).items():      # a debugging aid: which constraints reject these anchors?
        model.Add(x[ref] == gx)
        model.Add(y[ref] == gy)
    if around:                  # a debugging aid: the best legal placement within ``radius`` grid steps of these anchors
        for ref, (gx, gy) in around[0].items():
            model.Add(x[ref] >= gx - around[1])
            model.Add(x[ref] <= gx + around[1])
            model.Add(y[ref] >= gy - around[1])
            model.Add(y[ref] <= gy + around[1])
    build_seconds = time.perf_counter() - started
    parameters = cp_model.CpSolver().parameters
    parameters.num_search_workers = _WORKERS
    if _WORKERS > 1:
        parameters.interleave_search = True         # CP-SAT's deterministic parallel mode: the answer does not depend on timing
    parameters.random_seed = seed
    # Probing the Boolean separators took most of the presolve (3 to 5 s a pass, three passes a stage, each
    # of the four stages) and found nothing the search does not: without it the same optimum is proved in a
    # third of the wall time.
    parameters.cp_model_probing_level = 0
    if diagnose:
        solver = cp_model.CpSolver()
        solver.parameters.CopyFrom(parameters)
        solver.parameters.max_deterministic_time = _SOLVER_DETERMINISTIC_TIME
        solver.parameters.max_time_in_seconds = _SOLVER_SECONDS
        status = solver.Solve(model)
        if status == cp_model.INFEASIBLE:
            core = [assumptions[i] for i in solver.SufficientAssumptionsForInfeasibility()]
            if stats is not None:
                stats["core"] = core            # each constraint of the core, with the parts it names
            raise LadderInfeasible([g for g, _ in core] or ["model domains"],
                                   [r for _, parts in core for r in parts] or refs)
        raise LadderSolveError(f"cp-sat (diagnosis {solver.StatusName(status)})")

    timing = {"build": build_seconds, "feasibility": 0.0, "optimisation": 0.0}
    used = {"deterministic": 0.0, "wall": 0.0}
    # "clean" is the latest solution known to break nothing left out; "hint" starts the next search.
    state = {"clean": None, "hint": None, "rounds": 0, "log": [], "callback": 0.0, "incumbents": 0,
             "stage": "start", "history": [], "kept": []}
    if stats is None:
        stats = {}

    class Solution:
        """A solution as a list of values by variable index, evaluating expressions like a solver does."""

        def __init__(self, solution):
            self.solution = solution

        def Value(self, expression):
            flat = model.parse_linear_expression(expression)
            return sum(c * self.solution[i] for i, c in zip(flat.vars, flat.coeffs)) + flat.offset

    flat_cache = {}

    def flat(expression):
        """(variable index or -1, coefficient, offset) of an expression that has at most one variable, else None."""
        key = id(expression)
        if key not in flat_cache:
            flat_cache[key] = _affine(model, expression)
        return flat_cache[key]

    checker = {"current": None}

    def left_out():
        if checker["current"] is None:
            checker["current"] = _Checker(list(deferred), flat)
        return checker["current"]

    class Watch(cp_model.CpSolverSolutionCallback):
        """Checks every incumbent against what was left out; stops the search at the first that breaks any,
        for left to run it would spend its budget improving on a solution that is not one. A clean incumbent
        is kept every ``_KEEP_EVERY``-th time, in case the budget ends on one that breaks something."""

        def __init__(self):
            super().__init__()
            self.clean, self.broken, self.hint, self.final, self.count = None, [], None, None, 0
            self.started, self.best = used["deterministic"], None
            self.improvements, self.last = 0, 0.0       # of the objective, and the deterministic time of the last one

        def on_solution_callback(self):
            began = time.perf_counter()
            entries = left_out()
            values = np.fromiter((self.SolutionIntegerValue(i) for i in entries.needed), dtype=np.int64,
                                 count=len(entries.needed))
            broken = entries.broken(values, evaluator(self) if entries.slow else None)
            self.count += 1
            if broken:
                self.broken, self.hint = broken, list(self.Response().solution)
                self.StopSearch()
            else:
                objective = self.ObjectiveValue()
                if self.best is None or objective < self.best:      # when, in the budget, each stage last improved
                    self.best = objective
                    self.improvements += 1
                    self.last = self.DeterministicTime()
                    state["history"].append((round(self.started + self.DeterministicTime(), 2), state["stage"], objective))
                if self.count % _KEEP_EVERY == 1:
                    self.clean = list(self.Response().solution)
            state["callback"] += time.perf_counter() - began
            state["incumbents"] += 1

    def keep(vector):
        """Remember a placement that breaks nothing, for the final choice (the latest few of each stage)."""
        state["kept"].append((state["stage"], vector))
        same = [i for i, (stage, _v) in enumerate(state["kept"]) if stage == state["stage"]]
        for i in same[:-_KEPT_PER_STAGE][::-1]:
            del state["kept"][i]

    def restore(broken, solution):
        """Write into the model what a solution broke, and what stands close together in it: the search is
        moving this way, and every round costs a presolve."""
        value = evaluator(Solution(solution))
        keys = {entry[0] for entry in broken}
        keys.update(entry[0] for entry in deferred if _close(value, *entry[3]))
        rest = []
        for entry in deferred:
            if entry[0] in keys:
                entry[1]()
                emitted.add(entry[0])
            else:
                rest.append(entry)
        deferred[:] = rest
        checker["current"] = None
        state["rounds"] += 1
        if state["rounds"] >= _ROUND_LIMIT:
            for entry in deferred:      # restoring a few at a time is not converging: write in the rest
                entry[1]()
                emitted.add(entry[0])
            deferred.clear()
            checker["current"] = None

    def select(values):
        """Write the exclusions between things that stand close together in this placement; defer the rest."""
        value = evaluator(values)
        for key, boxes, emit, check, kind in candidates:
            if _close(value, *boxes):
                emit()
                emitted.add(key)
            else:
                deferred.append((key, emit, check, boxes, kind))
        candidates.clear()
        checker["current"] = None

    def run(make, limit):
        """One CP-SAT run hinted with the latest solution: (status, watch). The watch has the last clean incumbent."""
        remaining = _SOLVER_DETERMINISTIC_TIME - used["deterministic"]
        wall_remaining = _SOLVER_SECONDS - used["wall"]
        watch = Watch()
        if remaining <= 0 or wall_remaining <= 0:
            return cp_model.UNKNOWN, watch
        model.ClearHints()
        hint = state["hint"] or state["clean"]
        if hint is not None:
            for index, value in enumerate(hint):
                model.AddHint(model.GetIntVarFromProtoIndex(index), value)
            # what the hint does not reach: a priced term written since, as the hint's placement would have it
            fresh = [item for item in through_vars if item[0].Index() >= len(hint)]
            if fresh:
                value = evaluator(Solution(hint))
                for term, rectangle, pieces, options in fresh:
                    model.AddHint(term, int(any(_overlap(value(rectangle), value(piece)) for piece in pieces)))
                    for chosen, low, high in options:
                        model.AddHint(chosen, int(value(low) <= value(high)))
        else:
            add_search_hints()
        if make is None:
            model.ClearObjective()
        else:
            model.Minimize(make())
        solver = cp_model.CpSolver()
        solver.parameters.CopyFrom(parameters)
        solver.parameters.max_deterministic_time = min(limit, remaining)
        solver.parameters.max_time_in_seconds = wall_remaining
        status = solver.Solve(model, watch)
        used["wall"] += solver.WallTime()
        used["deterministic"] += solver.ResponseProto().deterministic_time
        # (At the wall-clock limit no further run starts; the best placement found stands, marked in the status.)
        timing["feasibility" if make is None else "optimisation"] += solver.WallTime()
        state["log"].append((state["stage"], solver.StatusName(status), round(solver.ResponseProto().deterministic_time, 2),
                             len(watch.broken), round(solver.WallTime(), 1), watch.improvements, round(watch.last, 2)))
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            watch.final = list(solver.ResponseProto().solution)
        return status, watch

    def solve(make, share, stop_share=None):
        """Minimise ``make()`` (or just find a placement for None) within ``share`` of what budget is left.

        What an incumbent breaks joins the model and the search resumes, hinted with it. Only a solution that
        breaks nothing is kept, so a proved optimum of the smaller model is the optimum of the whole."""
        stage_start = _SOLVER_DETERMINISTIC_TIME - used["deterministic"]
        budget, spent = stage_start * share, used["deterministic"]
        stop_at = None if stop_share is None else spent + stage_start * stop_share
        while True:
            left = budget - (used["deterministic"] - spent)
            limit = left if left > 0.05 * stage_start else 0.25 * _SOLVER_DETERMINISTIC_TIME
            if stop_at is not None and used["deterministic"] < stop_at:
                # The search may end at its share if it holds a placement by then; if not, it goes on (see below).
                limit = min(limit, stop_at - used["deterministic"])
            status, watch = run(make, limit)
            if watch.broken:
                state["hint"] = watch.hint
                restore(watch.broken, watch.hint)
                continue
            if watch.final is not None:
                # The incumbent the search ended on is checked whether or not the callback saw it.
                final = Solution(watch.final)
                entries = left_out()
                broken = entries.broken([watch.final[i] for i in entries.needed], evaluator(final) if entries.slow else None)
                if broken:
                    state["hint"] = watch.final
                    restore(broken, watch.final)
                    continue
                state["clean"], state["hint"] = watch.final, None
                keep(watch.final)
                if crossing_check(evaluator(final)):
                    continue                # more crossings are in the objective: it has to be searched again
            elif watch.clean is not None:
                state["clean"], state["hint"] = watch.clean, None
                keep(watch.clean)
            elif stop_at is not None and state["clean"] is None and left > 0.05 * stage_start:
                stop_at = None          # nothing to stop at yet: the search goes on within its whole share
                continue
            return status

    def placement_cost(make, vector):
        """The objective ``make()`` of the placement in ``vector``, with everything else (trunk heights, leads, the priced
        terms) chosen best for it, and that complete solution: (value, solution), or (None, None) if the model has none
        there. Wires through and across what the placement breaks are priced first, as a search ends by doing."""
        value = evaluator(Solution(vector))
        crossing_check(value)
        placed = Solution(vector)
        model.ClearHints()
        model.Minimize(make())
        fixed = model.Clone()
        for ref in refs:
            for var in (x[ref], y[ref]):
                fixed.Add(fixed.GetIntVarFromProtoIndex(var.Index()) == placed.Value(var))
        solver = cp_model.CpSolver()
        solver.parameters.CopyFrom(parameters)
        solver.parameters.max_deterministic_time = 1.0
        status = solver.Solve(fixed)
        used["deterministic"] += solver.ResponseProto().deterministic_time
        used["wall"] += solver.WallTime()
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, None
        return solver.ObjectiveValue(), list(solver.ResponseProto().solution)

    def true_cost(vector):
        """What the placement in ``vector`` costs as drawn (see ``_true_cost``). None when it draws no net."""
        solution = Solution(vector)
        return _true_cost(plan, symbols, nets, {ref: (solution.Value(x[ref]) * GRID_MM, solution.Value(y[ref]) * GRID_MM)
                                                for ref in refs}, drawn_routes(solution))

    def finish():
        proto = model.Proto()
        stats.update(variables=len(proto.variables), constraints=len(proto.constraints),
                     rounds=state["rounds"], emitted=frozenset(emitted), left_out=len(deferred), log=state["log"],
                     callback=round(state["callback"], 1), incumbents=state["incumbents"], history=state["history"][-400:],
                     written=dict(Counter(key[0] for key in emitted)),
                     left=dict(Counter(entry[0][0] for entry in deferred)), implied=dict(implied))

    def drawn_routes(solution):
        """The routes the solution draws, per net, for the nets whose every pin the plan places."""
        found = {}
        for name, drawn in sorted(drawn_wires.items()):
            net, eps = net_map[name]
            order = {ep: i for i, ep in enumerate(net.pins)}
            value = lambda e: solution.Value(e) / _SCALE
            if drawn[0] == "stack":
                _kind, jx, top, bottom, runs = drawn
                joined = [side for *_r, side in runs]
                # the vertical wire reaches each end, and every pin whose tip is that end's (no run); a run reaches its own
                pins = tuple(order[p] for end, side in zip(stacks[name], stack_pins[name])
                             for p in ([end] if side in joined else side))
                found[name] = [Route(((value(jx), value(top)), (value(jx), value(bottom))), 0,
                                     value(bottom) - value(top), pins)]
                for lo, hi, level, side in runs:
                    found[name].append(Route(((value(lo), value(level)), (value(hi), value(level))), 0, value(hi) - value(lo),
                                             tuple(order[e] for e in side)))
                continue
            _kind, wy, lo, hi, legs = drawn
            level, left, right = value(wy), value(lo), value(hi)
            arrivals, ends = [], []
            for ep, tip_x, tip_y, foot, lead in legs:
                tx, ty, fx = value(tip_x), value(tip_y), value(foot)
                dropped = abs(ty - level) > 1e-6
                if lead:
                    points = [(tx, ty), (fx, ty)] + ([(fx, level)] if dropped else [])
                elif dropped:
                    points = [(tx, ty), (tx, level)]
                else:
                    points = None
                if points:
                    length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:]))
                    arrivals.append(Route(tuple(points), len(points) - 2, length, (order[ep],)))
                ends.append((fx, bool(points) and dropped))
            bends = 0
            for end in (left, right):
                at_end = [drop for fx, drop in ends if abs(fx - end) < 1e-6]
                if at_end and all(at_end):
                    bends += 1
            trunk = [Route(((left, level), (right, level)), bends, right - left, ())] if right - left > 1e-6 else []
            found[name] = trunk + arrivals
        return found

    # Wire plus the crossing penalty, extent, then a geometry-derived tie preference. Each proved
    # optimum is fixed before the next priority is considered. A first short minimisation of the wire without
    # the pairwise exclusions between distant things shows which ones stand close together; the rest wait
    # in the wings. The first stage is the optimisation itself: a legal incumbent comes with it. Only when
    # it finds none within most of the budget does a plain feasibility search take the rest.
    state["stage"] = "probe"
    if candidates:
        status, watch = run(compact, _SOLVER_DETERMINISTIC_TIME * _START_SHARE)
        if status == cp_model.INFEASIBLE:
            finish()
            return None
        if watch.final is None:
            for key, _boxes, emit, _check, _kind in candidates:
                emit()
                emitted.add(key)
            candidates.clear()
        else:
            state["hint"] = watch.final
            select(Solution(watch.final))
    status = cp_model.UNKNOWN
    start_throughs(Solution(state["hint"]) if state["hint"] is not None else None)
    for position, (stage, make, share, fix) in enumerate(
            [*objectives, *([("alignment", lambda: sum(alignment), 1.0, True)] if alignment else [])]):
        state["stage"] = stage
        if stage == "weighted":
            # The crossings join the objective once there is a good placement to say which can matter, so the
            # searches for a legal placement and for short wire do not branch on a lower-priority metric.
            start_crossings(Solution(state["clean"]))
        first = state["clean"] is None
        floor = None
        if stage == "weighted" and _MONOTONE and state["clean"] is not None:
            # What the previous stage's placement costs under this objective, as a complete solution to hint with and as the
            # floor this stage may not end above.
            cost, complete = placement_cost(make, state["clean"])
            if complete is not None:
                state["clean"], state["hint"], floor = complete, None, cost
                state["floor_vector"] = complete
                stats["floor"] = cost
        if first and _FEASIBLE_FIRST:
            # A placement that breaks nothing written so far, before any objective is optimised: the probe's placement
            # was found without the exclusions written since, and a large model optimised from it can spend its whole share
            # finding none.
            state["hint"] = None            # the plan's own layout hints guide it, not the probe's compacted placement
            if solve(None, 1.0) == cp_model.INFEASIBLE:
                finish()
                return None
        status = solve(make, 0.75 if first else share, _WIRE_SHARE if _STOP_WIRE and stage == "wire" else None)
        if floor is not None and state["clean"] is not None:
            # The stage ends with the better of what it found and what it started from, under the objective as it ends.
            found_cost, found = placement_cost(make, state["clean"])
            start_cost, start = placement_cost(make, state["floor_vector"])
            stats["weighted_end"] = found_cost
            if found is None or (start is not None and start_cost < found_cost):
                state["clean"] = start
                stats["floor_kept"] = True
        if status == cp_model.INFEASIBLE:
            finish()
            if position == 0 and first:
                return None
            raise LadderSolveError("cp-sat (optimisation infeasible)")
        if status == cp_model.MODEL_INVALID:
            raise LadderSolveError(f"cp-sat (model invalid): {model.Validate()}")
        if first and state["clean"] is None:
            status = solve(None, 1.0)
            if status == cp_model.INFEASIBLE:
                finish()
                return None
            if state["clean"] is None:
                finish()
                raise LadderSolveError("cp-sat (deterministic limit, no placement)")
            status = solve(make, 1.0)
        if state["clean"] is None:
            finish()
            raise LadderSolveError("cp-sat (deterministic limit, no placement)")
        if not fix:
            continue            # a short search that leaves the placement to improve from; the next objective includes it
        if status != cp_model.OPTIMAL:
            status = cp_model.FEASIBLE
            break
        objective = make()
        model.Add(objective == Solution(state["clean"]).Value(objective))
        crossing_state["fixed"] = True
    finish()
    if _VERIFY_PRUNE:
        broken = Counter()
        for vector in [state["clean"], *(v for _s, v in state["kept"])]:
            value = evaluator(Solution(vector))
            for key, check in pruned_checks:
                if check(value):
                    broken[key[0]] += 1
        stats["prune_violations"] = dict(broken)
        stats["pruned_checked"] = len(pruned_checks)
    chosen = state["clean"]
    if _SELECT and len(state["kept"]) > 1:
        # The placement that ends the search may carry faults the objective found too late to search again. Each one kept is
        # scored on what it draws, as the checker counts it, at the objective's prices; the cheapest is returned.
        scored = [(true_cost(vector), index, vector) for index, (_stage, vector) in enumerate(state["kept"])]
        scored = [item for item in scored if item[0] is not None]
        ending = true_cost(state["clean"])
        if scored and ending is not None:
            stats["selection"] = [(state["kept"][i][0], round(c, 1)) for c, i, _v in scored] + [("end", round(ending, 1))]
            best = min(scored, key=lambda item: (item[0], -item[1]))
            if best[0] < ending - 1e-6:
                chosen = best[2]
                stats["selected"] = state["kept"][best[1]][0]
    solution = Solution(chosen)
    label = ("cp-sat" if status == cp_model.OPTIMAL else
             "cp-sat (wall-clock limit)" if used["wall"] >= _SOLVER_SECONDS else "cp-sat (deterministic limit)")
    result = Placements(((ref, Placement((solution.Value(x[ref]) * GRID_MM, solution.Value(y[ref]) * GRID_MM),
                                         plan.orientations[ref])) for ref in refs), solver=label,
                        wires=drawn_routes(solution))
    result.timing = {k: round(v, 2) for k, v in timing.items()}
    result.deterministic_time = round(used["deterministic"], 2)
    result.stats = {k: v for k, v in stats.items() if k != "emitted"}
    return result


class LadderResult(NamedTuple):
    """Status-bearing adapter expected by the offline ladder harness."""

    placements: dict[str, Placement]
    status: str


def legalise_with_status(plan, symbols, nets, *, gap_mm=2.54, pitch_mm=7.62, hint=None, seeds=None):
    placements = legalise(plan, symbols, nets, gap_mm=gap_mm, pitch_mm=pitch_mm, hint=hint, seeds=seeds)
    return LadderResult(placements, placements.status)
