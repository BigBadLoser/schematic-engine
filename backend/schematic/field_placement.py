"""Where a symbol's reference and value text goes so that a reader can read it.

MCP Pro writes a symbol's reference above it and its value below it, centred on the symbol's axis. For any part drawn
between two wires (a resistor or capacitor in a vertical chain) that puts both texts on the wire. This chooses a place
for every visible reference, value and power-symbol text from a few candidates round its symbol: beside it, above it,
below it, or a little further out, and inside the body of a large part. A text may not lie on a wire, a symbol, a pin, a
label or another text; among the places that are free it keeps near its symbol and on the conventional side.

It is one small optimisation, solved exactly with CP-SAT, over text boxes measured with the calibrated text model
(``kicad_geometry.text_box``). It knows no circuit. The new anchors are the centres of the boxes, so no justification
is written, and the text is always horizontal.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .kicad_geometry import text_width_mm
from .presentation_geometry import Bounds, GeometryItem, GeometryKind as K, GeometryScene

GAP_MM = 1.05             # between a symbol and the text beside it
OUTER_GAP_MM = 2.7        # the next ring out, for when the first is taken
MARGIN_MM = 1.0           # kept clear all round a text (was 0.35: texts of different parts touched, "GND1K", "R4R6")
INSIDE_MIN_AREA_MM2 = 60.0
# What colliding costs, and what a place costs by itself. Colliding is always worse than any place that is free.
COST_WIRE = 400.0         # text on a wire is nearly hard, like text on text (was 60)
COST_SYMBOL = 400.0       # text on another part's body is nearly hard too (was 80: `flow-1` put values over neighbours)
COST_PIN = 50.0
COST_TEXT = 400.0         # text on other text is nearly hard (was 80: texts of different parts touched)
COST_JUNCTION = 30.0
COST_PER_MM = 0.6         # distance from the symbol
COST_KEEP_BONUS = 0.2     # a text already well placed stays where it is
# A text drawn vertically (a part rotated a quarter turn writes its fields along the part) reads badly: none of the 91
# human-drawn demo sheets' labels is vertical and few fields are. It stays only when every place for it horizontally
# collides with something, which costs more than this.
COST_VERTICAL_TEXT = 25.0
# Which side a reader looks on first: a reference above or to the left of its symbol, a value below or to the right.
PREFERRED_SIDE = {
    "Reference": {"N": 0.0, "W": 0.4, "E": 0.7, "S": 1.4, "IN": 0.9},
    "Value": {"S": 0.0, "E": 0.4, "W": 0.7, "N": 1.4, "IN": 0.5},
}
# Measured on 2,780 two-pin parts of KiCad's demo sheets (probes/field_sides/out/summary.md).
SIDE_PREF = 0.4           # vertical parts: Reference on the right 62%; horizontal: Reference above, Value below (default)
SIDE_PREF_ACROSS = 0.2    # horizontal parts: beside the part is the less usual place
SAME_SIDE_BONUS = 0.5     # Reference and Value share a side on 72% of vertical parts
FIELD_KINDS = (K.REFERENCE, K.VALUE, K.POWER_TEXT)
# The solve stops on CP-SAT's deterministic budget (reproducible on any load or machine with one worker), because a
# wall-clock limit made the chosen positions depend on how busy the machine was; the wall clock is only a safety net.
_SOLVER_DETERMINISTIC_TIME = 25.0
_SOLVER_SECONDS = 60.0
# When the normal solve ends unproven with collisions left, one continuation at a larger budget is tried (hinted from
# the first result) and the result with fewer collisions kept. Measured: sheets that prove at 25 had no overlaps, those
# that stopped at the limit did; a few of those prove by about 100. Its wall-clock net is wider (100 took 74 s).
_CONTINUE_DETERMINISTIC_TIME = 100.0
_CONTINUE_SECONDS = 300.0


@dataclass(frozen=True)
class FieldMove:
    owner: str
    field: str                    # Reference or Value
    x: float                      # the centre of the text: the new anchor
    y: float
    angle: int                    # the field's angle in the file, so that the text is drawn horizontally
    box: Bounds
    was: Bounds


@dataclass(frozen=True)
class FieldPlan:
    moves: tuple[FieldMove, ...]
    fields: int
    collisions_before: dict[str, int] = field(default_factory=dict)
    collisions_after: dict[str, int] = field(default_factory=dict)
    solver: str = ""


@dataclass
class _Owner:
    box: Bounds
    body: Bounds | None
    rotation: int = 0
    mirror: str | None = None
    power: bool = False
    pin_tip: tuple[float, float] | None = None
    pin_tips: list[tuple[float, float]] = field(default_factory=list)


def _union(boxes: list[Bounds]) -> Bounds:
    return Bounds(
        min(b.left for b in boxes), min(b.top for b in boxes), max(b.right for b in boxes), max(b.bottom for b in boxes),
    )


def _overlap(a: Bounds, b: Bounds) -> bool:
    return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top


def _inflate(box: Bounds, margin: float) -> Bounds:
    return Bounds(box.left - margin, box.top - margin, box.right + margin, box.bottom + margin)


def _segment_hits(segment: tuple[tuple[float, float], tuple[float, float]], box: Bounds) -> bool:
    (ax, ay), (bx, by) = segment
    return max(ax, bx) > box.left and min(ax, bx) < box.right and max(ay, by) > box.top and min(ay, by) < box.bottom


def file_angle(rotation: int) -> int:
    """The angle to write on a field so that, added to its symbol's rotation, the text is drawn horizontally."""
    return (180 - (int(rotation) % 180)) % 180


def _owners(scene: GeometryScene) -> dict[str, _Owner]:
    boxes: dict[str, list[Bounds]] = defaultdict(list)
    bodies: dict[str, list[Bounds]] = defaultdict(list)
    info: dict[str, _Owner] = {}
    tips: dict[str, tuple[float, float]] = {}
    ends: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in scene.items:
        if item.kind in (K.BODY, K.POWER_GRAPHIC):
            boxes[item.owner].append(item.bounds)
            bodies[item.owner].append(item.bounds)
            owner = info.setdefault(item.owner, _Owner(item.bounds, item.bounds))
            owner.rotation = int(round(item.angle or 0.0)) % 360
            owner.mirror = item.mirror
            owner.power = item.kind == K.POWER_GRAPHIC
        elif item.kind in (K.PIN_STROKE, K.PIN_ENDPOINT):
            boxes[item.owner].append(item.bounds)
            if item.kind == K.PIN_ENDPOINT:
                tips[item.owner] = (item.bounds.left, item.bounds.top)
                ends[item.owner].append((item.bounds.left, item.bounds.top))
    for name, owner_boxes in boxes.items():
        owner = info.setdefault(name, _Owner(_union(owner_boxes), None))
        owner.box = _union(owner_boxes)
        owner.body = _union(bodies[name]) if bodies.get(name) else None
        owner.pin_tip = tips.get(name)
        owner.pin_tips = ends.get(name, [])
    return info


def _candidates(
    field_name: str, owner: _Owner, width: float, height: float, current: Bounds | None,
) -> list[tuple[str, Bounds, float]]:
    """(side, box, distance from the symbol) for every place the text could go."""
    box = owner.box
    cx, cy = (box.left + box.right) / 2, (box.top + box.bottom) / 2
    out: list[tuple[str, Bounds, float]] = []

    def add(side: str, center_x: float, center_y: float, distance: float) -> None:
        out.append((side, Bounds(center_x - width / 2, center_y - height / 2, center_x + width / 2, center_y + height / 2), distance))

    for gap in (GAP_MM, OUTER_GAP_MM):
        d = gap
        # beside: east and west, level with the symbol, shifted up and down by a line
        for dy in (-1.5 * height, -0.75 * height, 0.0, 0.75 * height, 1.5 * height):
            add("E", box.right + gap + width / 2, cy + dy, d)
            add("W", box.left - gap - width / 2, cy + dy, d)
        # above and below: centred, and shifted so it starts at one end of the symbol
        for dx in (0.0, -(width / 2 + 1.0), width / 2 + 1.0):
            add("N", cx + dx, box.top - gap - height / 2, d)
            add("S", cx + dx, box.bottom + gap + height / 2, d)
    if owner.body is not None and not owner.power:
        body = owner.body
        if (body.right - body.left) * (body.bottom - body.top) >= INSIDE_MIN_AREA_MM2:
            bx, by = (body.left + body.right) / 2, (body.top + body.bottom) / 2
            for dy in (-1.2 * height, 0.0, 1.2 * height):
                if width + 1.0 <= body.right - body.left and 3 * height <= body.bottom - body.top:
                    add("IN", bx, by + dy, 0.0)
    if current is not None:
        out.append(("KEEP", current, 0.0))
    return out


def routing_text_obstacles(scene: GeometryScene, tight: bool) -> GeometryScene:
    """The scene a router should treat as obstacles. In a guarded (tight) layout the reference and value of an ordinary
    symbol are dropped: they stand at library defaults and the field placer moves them after routing, so a wire that
    dodges them only makes a dogleg. Pin names and numbers, net labels, power text and everything else stay."""
    if not tight:
        return scene
    kept = tuple(
        item for item in scene.items
        if not (item.kind in (K.REFERENCE, K.VALUE) and not str(item.owner).startswith("#"))
    )
    if len(kept) == len(scene.items):
        return scene
    return GeometryScene(kept, scene.clearance_mm)


def plan_fields(scene: GeometryScene) -> FieldPlan:
    owners = _owners(scene)
    items = list(scene.items)
    fields = [
        item for item in items
        if item.kind in FIELD_KINDS and item.anchor is not None and item.text and ":field:" in item.id
    ]
    fields.sort(key=lambda item: (item.owner, item.id.rsplit(":field:", 1)[1], item.id))
    wires = [item.segment for item in items if item.kind == K.WIRE and item.segment is not None]
    junctions = [item.bounds for item in items if item.kind == K.JUNCTION]
    solids = [
        (item.owner, item.bounds, "symbol") for item in items if item.kind in (K.BODY, K.POWER_GRAPHIC)
    ] + [
        (item.owner, item.bounds, "pin") for item in items if item.kind == K.PIN_STROKE
    ]
    fixed_texts = [
        (item.owner, item.bounds) for item in items
        if item.kind in (K.PIN_NAME, K.PIN_NUMBER, K.NET_LABEL)
    ]
    moving = {item.id for item in fields}

    def cost_of(box: Bounds, owner: str, inside: bool) -> tuple[float, dict[str, int]]:
        probe = _inflate(box, MARGIN_MM)
        found: dict[str, int] = defaultdict(int)
        total = 0.0
        for segment in wires:
            if _segment_hits(segment, probe):
                found["wire"] += 1
                total += COST_WIRE
        for solid_owner, solid, kind in solids:
            if solid_owner == owner and kind == "symbol" and inside:
                continue
            if _overlap(probe, solid):
                found[kind] += 1
                total += COST_SYMBOL if kind == "symbol" else COST_PIN
        for text_owner, text in fixed_texts:
            if _overlap(probe, text):
                found["text"] += 1
                total += COST_TEXT
        for dot in junctions:
            if _overlap(probe, dot):
                found["junction"] += 1
                total += COST_JUNCTION
        return total, found

    candidates: list[list[tuple[str, Bounds, float, float]]] = []
    partners: dict[str, dict[str, tuple[int, bool]]] = {}
    for item in fields:
        owner = owners.get(item.owner)
        if owner is None:
            candidates.append([("KEEP", item.bounds, 0.0, 0.0)])
            continue
        name = item.id.rsplit(":field:", 1)[1]
        size = min(item.bounds.right - item.bounds.left, item.bounds.bottom - item.bounds.top)
        width = text_width_mm(item.text or "", size)
        height = max(0.4, size)
        horizontal = int(round(item.angle or 0.0)) % 180 == 0
        listed = _candidates(name, owner, width, height, item.bounds)
        preferred = PREFERRED_SIDE.get(name, PREFERRED_SIDE["Value"])
        if owner.power and owner.pin_tip is not None and owner.body is not None:
            # a power symbol's text goes on the side of the graphic away from its pin
            above = owner.pin_tip[1] < (owner.body.top + owner.body.bottom) / 2
            preferred = {"S": 0.0, "E": 0.6, "W": 0.6, "N": 1.5, "IN": 5.0} if above else {
                "N": 0.0, "E": 0.6, "W": 0.6, "S": 1.5, "IN": 5.0,
            }
        pin_side_y = None
        if owner.power and owner.pin_tip is not None and owner.body is not None:
            pin_side_y = owner.pin_tip[1] < (owner.body.top + owner.body.bottom) / 2   # True: the pin is above the graphic
        two_pin = not owner.power and len(owner.pin_tips) == 2 and name in ("Reference", "Value")
        pin_vertical = two_pin and abs(owner.pin_tips[0][1] - owner.pin_tips[1][1]) > abs(owner.pin_tips[0][0] - owner.pin_tips[1][0])
        if two_pin:
            if pin_vertical:
                nudge = {"E": -SIDE_PREF, "W": 0.0, "N": SIDE_PREF, "S": SIDE_PREF}
            elif name == "Reference":
                nudge = {"N": -SIDE_PREF, "E": SIDE_PREF_ACROSS, "W": SIDE_PREF_ACROSS}
            else:
                nudge = {"S": -SIDE_PREF, "E": SIDE_PREF_ACROSS, "W": SIDE_PREF_ACROSS}
            preferred = {side: value + nudge.get(side, 0.0) for side, value in preferred.items()}
            partners.setdefault(item.owner, {})[name] = (len(candidates), pin_vertical)
        entries: list[tuple[str, Bounds, float, float]] = []
        for side, box, distance in listed:
            if side == "KEEP" and not horizontal:
                entries.append((side, box, COST_VERTICAL_TEXT, 0.0))   # it stays only if nothing horizontal is free
                continue
            base = preferred.get(side, 0.0) + COST_PER_MM * distance - (COST_KEEP_BONUS if side == "KEEP" else 0.0)
            if side == "KEEP":
                base = -COST_KEEP_BONUS
                if pin_side_y is not None:
                    # a text kept where it stands must also be on the far side of the graphic from the pin
                    # (the library default does not hold for a symbol turned to hang from its pin)
                    mid_y = (box.top + box.bottom) / 2
                    anchor_y = (owner.body.top + owner.body.bottom) / 2
                    if (mid_y < anchor_y) == pin_side_y:
                        base += 1.5
            entries.append((side, box, base, distance))
        candidates.append(entries)

    # price every candidate against what does not move
    priced: list[list[tuple[str, Bounds, float, dict[str, int]]]] = []
    for item, entries in zip(fields, candidates):
        row = []
        for side, box, base, _distance in entries:
            collision, found = cost_of(box, item.owner, inside=(side == "IN"))
            row.append((side, box, base + collision, found))
        priced.append(row)

    before: dict[str, int] = defaultdict(int)
    for item, row in zip(fields, priced):
        for side, _box, _cost, found in row:
            if side == "KEEP":
                for kind, count in found.items():
                    before[kind] += count
    kept_boxes = [item.bounds for item in fields]
    for index, first in enumerate(kept_boxes):
        for second in kept_boxes[index + 1:]:
            if _overlap(_inflate(first, MARGIN_MM / 2), second):
                before["text"] += 1
    pairs = []
    for roles in partners.values():
        if "Reference" in roles and "Value" in roles:
            combos = [("E", "E"), ("W", "W")] + ([] if roles["Reference"][1] else [("N", "S")])
            pairs.append((roles["Reference"][0], roles["Value"][0], combos))
    chosen, solver_name = _solve_with_continuation(fields, priced, pairs)
    chosen = _clear_of_wires(priced, chosen)
    moves: list[FieldMove] = []
    after: dict[str, int] = defaultdict(int)
    pair_after = 0
    chosen_boxes: list[Bounds] = []
    for item, row, pick in zip(fields, priced, chosen):
        side, box, _cost, found = row[pick]
        for kind, count in found.items():
            after[kind] += count
        chosen_boxes.append(box)
        if side == "KEEP":
            continue
        owner = owners.get(item.owner)
        rotation = owner.rotation if owner else 0
        name = item.id.rsplit(":field:", 1)[1]
        moves.append(FieldMove(
            item.owner, name, round((box.left + box.right) / 2, 4), round((box.top + box.bottom) / 2, 4),
            file_angle(rotation), box, item.bounds,
        ))
    for index, first in enumerate(chosen_boxes):
        for second in chosen_boxes[index + 1:]:
            if _overlap(_inflate(first, MARGIN_MM / 2), second):
                pair_after += 1
    after["text"] += pair_after
    return FieldPlan(tuple(moves), len(fields), dict(before), dict(after), solver_name)


def _collision_count(priced: list[list[tuple[str, Bounds, float, dict[str, int]]]], chosen: list[int]) -> int:
    """Text, symbol and pin collisions of a choice: against what stays put, and between the chosen texts."""
    total = 0
    boxes: list[Bounds] = []
    for row, pick in zip(priced, chosen):
        found = row[pick][3]
        total += found.get("text", 0) + found.get("symbol", 0) + found.get("pin", 0)
        boxes.append(row[pick][1])
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            if _overlap(_inflate(first, MARGIN_MM / 2), second):
                total += 1
    return total


def _solve_with_continuation(
    fields: list[GeometryItem], priced: list[list[tuple[str, Bounds, float, dict[str, int]]]], pairs: list | None = None,
) -> tuple[list[int], str]:
    """Solve at the normal budget. Only when that ends unproven (deterministic limit) and collisions remain, continue
    once at a larger budget, hinted from the first result, and keep the result with fewer collisions (the continuation
    on a tie). A solve that proves optimal takes exactly the normal path."""
    chosen, name = _solve(fields, priced, pairs)
    if name != "cp-sat (deterministic limit)":
        return chosen, name
    first_count = _collision_count(priced, chosen)
    if first_count == 0:
        return chosen, name
    again, again_name = _solve(
        fields, priced, pairs, budget=_CONTINUE_DETERMINISTIC_TIME, seconds=_CONTINUE_SECONDS, hint=chosen,
    )
    if again_name == "greedy" or _collision_count(priced, again) > first_count:
        return chosen, name
    return again, again_name.replace("cp-sat", "cp-sat, continued", 1)


def _solve(
    fields: list[GeometryItem], priced: list[list[tuple[str, Bounds, float, dict[str, int]]]], pairs: list | None = None,
    budget: float = _SOLVER_DETERMINISTIC_TIME, seconds: float = _SOLVER_SECONDS, hint: list[int] | None = None,
) -> tuple[list[int], str]:
    """The index of the chosen candidate for each field, and which solver chose."""
    if not fields:
        return [], "none"
    try:
        from ortools.sat.python import cp_model
    except ImportError:  # pragma: no cover - ortools is a dependency of the engine
        return _greedy(priced, pairs), "greedy"
    model = cp_model.CpModel()
    pick: list[list] = []
    scale = 100
    objective = []
    for f_index, row in enumerate(priced):
        variables = [model.NewBoolVar(f"f{f_index}c{c}") for c in range(len(row))]
        model.AddExactlyOne(variables)
        pick.append(variables)
        for c, (_side, _box, cost, _found) in enumerate(row):
            objective.append(int(round(cost * scale)) * variables[c])
    # candidates of different fields that land on each other: bucket them to find the pairs
    cell = 6.0
    buckets: dict[tuple[int, int], list[tuple[int, int, Bounds]]] = defaultdict(list)
    for f_index, row in enumerate(priced):
        for c, (_side, box, _cost, _found) in enumerate(row):
            padded = _inflate(box, MARGIN_MM / 2)
            for gx in range(int(padded.left // cell), int(padded.right // cell) + 1):
                for gy in range(int(padded.top // cell), int(padded.bottom // cell) + 1):
                    buckets[(gx, gy)].append((f_index, c, padded))
    seen: set[tuple[int, int, int, int]] = set()
    for members in buckets.values():
        for i, (fa, ca, boxa) in enumerate(members):
            for fb, cb, boxb in members[i + 1:]:
                if fa == fb or (fa, ca, fb, cb) in seen or (fb, cb, fa, ca) in seen:
                    continue
                seen.add((fa, ca, fb, cb))
                if _overlap(boxa, boxb):
                    clash = model.NewBoolVar(f"x{fa}_{ca}_{fb}_{cb}")
                    model.Add(clash >= pick[fa][ca] + pick[fb][cb] - 1)
                    objective.append(int(COST_TEXT * scale) * clash)
    for ref_i, val_i, combos in pairs or []:
        for ref_side, val_side in combos:
            both = model.NewBoolVar(f"same{ref_i}_{val_i}_{ref_side}{val_side}")
            ref_vars = [pick[ref_i][c] for c, row in enumerate(priced[ref_i]) if row[0] == ref_side]
            val_vars = [pick[val_i][c] for c, row in enumerate(priced[val_i]) if row[0] == val_side]
            model.Add(both <= sum(ref_vars))
            model.Add(both <= sum(val_vars))
            objective.append(-int(SAME_SIDE_BONUS * scale) * both)
    model.Minimize(sum(objective))
    if hint is not None:
        for variables, hinted in zip(pick, hint):
            for c, var in enumerate(variables):
                model.AddHint(var, 1 if c == hinted else 0)
    solver = cp_model.CpSolver()
    solver.parameters.max_deterministic_time = budget
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 7
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _greedy(priced, pairs), "greedy"
    chosen = [next(c for c, var in enumerate(variables) if solver.Value(var)) for variables in pick]
    if status == cp_model.OPTIMAL:
        return chosen, "cp-sat"
    if solver.WallTime() >= seconds:
        return chosen, "cp-sat (wall-clock limit)"
    return chosen, "cp-sat (deterministic limit)"


def _clear_of_wires(priced: list[list[tuple[str, Bounds, float, dict[str, int]]]], chosen: list[int]) -> list[int]:
    """A text left on a wire moves to the cheapest candidate (outer ring included) that is clear of every wire, if any
    exists and it does not land on another chosen text."""
    chosen = list(chosen)
    for f_index, row in enumerate(priced):
        if not row[chosen[f_index]][3].get("wire"):
            continue
        others = [priced[i][chosen[i]][1] for i in range(len(priced)) if i != f_index]
        free = [
            c for c, (_s, box, _cost, found) in enumerate(row)
            if not found.get("wire") and not any(_overlap(_inflate(box, MARGIN_MM / 2), o) for o in others)
        ]
        if free:
            chosen[f_index] = min(free, key=lambda c: row[c][2])
    return chosen


def _greedy(priced: list[list[tuple[str, Bounds, float, dict[str, int]]]], pairs: list | None = None) -> list[int]:
    order = sorted(range(len(priced)), key=lambda i: min(cost for _s, _b, cost, _f in priced[i]))
    chosen: dict[int, int] = {}
    placed: list[Bounds] = []
    for f_index in reversed(order):
        row = priced[f_index]

        def bonus(c: int) -> float:
            for ref_i, val_i, combos in pairs or []:
                if f_index == ref_i and val_i in chosen:
                    sides = (row[c][0], priced[val_i][chosen[val_i]][0])
                elif f_index == val_i and ref_i in chosen:
                    sides = (priced[ref_i][chosen[ref_i]][0], row[c][0])
                else:
                    continue
                return SAME_SIDE_BONUS if sides in combos else 0.0
            return 0.0
        best = min(
            range(len(row)),
            key=lambda c: row[c][2] - bonus(c) + sum(COST_TEXT for other in placed if _overlap(_inflate(row[c][1], MARGIN_MM / 2), other)),
        )
        chosen[f_index] = best
        placed.append(row[best][1])
    return [chosen[i] for i in range(len(priced))]
