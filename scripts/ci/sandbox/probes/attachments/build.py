"""Builds pr_att.py: a scratch copy of the engine's placement_refinement.py with the attachment (A) and pinned-path (B) rules."""
from pathlib import Path
src = Path("backend/schematic/placement_refinement.py").read_text(encoding="utf-8")
src = src.replace("from .orientation import", "from backend.schematic.orientation import")


def sub(old, new, count=1):
    global src
    assert src.count(old) == count, (old, src.count(old))
    src = src.replace(old, new)


# --- candidates: attachments follow their host, chain parts keep their row and order
sub("""        active = frozenset((reference,))
        for orientation in symbol.geometry:""", """        for orientation in PLAN.orients(reference, symbol):""")
sub("""        others = [
            (ref, model.world(ref, *current[ref])[1]) for ref in model.symbols if ref != reference and ref not in ignore
        ]  # (reference, policy box)""", """        active = PLAN.active(reference)
        is_att = reference in PLAN.att
        BI = 2 if is_att else 1
        row_cell = PLAN.row_cell(reference)
        others = [
            (ref, model.world(ref, *current[ref])[BI]) for ref in model.symbols
            if ref != reference and ref not in ignore and ref not in active and not (is_att and ref == PLAN.att[reference][0])
        ]  # (reference, policy box)""")
sub("""            xs.add(cx)
            ys.add(cy)
            for ix, iy in product(sorted(xs), sorted(ys)):
                key = (ix, iy, orientation)
                if key in seen or abs(ix - cx) > limit or abs(iy - cy) > limit:
                    continue""", """            xs.add(cx)
            ys.add(cy)
            if row_cell is not None:
                ys = {row_cell}
            if is_att:
                xs, ys = PLAN.att_lattice(reference, orientation, current, model)
            for ix, iy in product(sorted(xs), sorted(ys)):
                key = (ix, iy, orientation)
                if key in seen or (not is_att and (abs(ix - cx) > limit or (row_cell is None and abs(iy - cy) > limit))):
                    continue
                if row_cell is not None and not PLAN.x_ok(reference, ix * GRID_MM, current, orientation):
                    continue""")
sub("""                box = (
                    position[0] + geometry.box[0], position[1] + geometry.box[1],
                    position[0] + geometry.box[2], position[1] + geometry.box[3],
                )
                if any(_box_overlap(box, other_box) for _ref, other_box in others):
                    continue
                trial = dict(current)""", """                solid = (geometry.body or geometry.box) if is_att else geometry.box
                box = (position[0] + solid[0], position[1] + solid[1], position[0] + solid[2], position[1] + solid[3])
                if any(_box_overlap(box, other_box) for _ref, other_box in others):
                    continue
                if is_att and not PLAN.att_ok(reference, orientation, position, current, model):
                    continue
                trial = dict(current)""")
sub("""                trial = dict(current)
                trial[reference] = (position, orientation)
                cost = model.cost(trial, active, None, ignore)""", """                trial = dict(current)
                trial[reference] = (position, orientation)
                PLAN.settle(trial, reference, model)
                cost = model.cost(trial, active, None, ignore)""")
sub("""found.append((model.cost(here, active, None, ignore).total, -1,""", """found.append((model.cost(here, active, None, ignore).total, -1,""")

# --- refine_placement
sub("""    model = _Model(symbols, nets, reach or {}, weights or Weights(), half_widths)
    start: dict[str, tuple[Point, Orientation]] = {s.reference: (s.position, s.orientation) for s in symbols}
    before = model.cost(start)
    movable = [s for s in symbols if s.movable]

    def explain""", """    global PLAN
    model = _Model(symbols, nets, reach or {}, weights or Weights(), half_widths)
    start0: dict[str, tuple[Point, Orientation]] = {s.reference: (s.position, s.orientation) for s in symbols}
    PLAN = Plan(symbols, nets, model)
    start = PLAN.prepass(dict(start0), model)
    before = model.cost(start)
    movable = [s for s in symbols if s.movable]

    def explain""")
sub("""            here = model.cost(pose, frozenset((symbol.reference,))).total
            if best_total < here - 1e-6:
                pose[symbol.reference] = (best_position, best_orientation)
                improved = True""", """            here = model.cost(pose, PLAN.active(symbol.reference)).total
            if best_total < here - 1e-6:
                pose[symbol.reference] = (best_position, best_orientation)
                PLAN.settle(pose, symbol.reference, model)
                if symbol.reference in PLAN.att:
                    PLAN.sync(pose, symbol.reference, model)
                improved = True""")
sub("""    movable_refs = {s.reference for s in movable}""", """    movable_refs = {s.reference for s in movable}""")
sub("""        active = frozenset(members)
        best_total = model.cost(pose, active).total
        best_pose = {ref: pose[ref] for ref in members}""", """        active = frozenset(members).union(*(PLAN.active(r) for r in members))
        best_total = model.cost(pose, active).total
        best_pose = {ref: pose[ref] for ref in active}""")
sub("""                trial[ref] = (position, orientation)
            overlapping = any(""", """                trial[ref] = (position, orientation)
                PLAN.settle(trial, ref, model)
            overlapping = any(""")
sub("""            if overlapping:
                continue
            total = model.cost(trial, active).total
            if total < best_total - 1e-6:
                best_total = total
                best_pose = {ref: trial[ref] for ref in members}""", """            if overlapping or not PLAN.combo_ok(members, trial):
                continue
            total = model.cost(trial, active).total
            if total < best_total - 1e-6:
                best_total = total
                best_pose = {ref: trial[ref] for ref in active}""")
sub("""    if after.total >= before.total - 1e-6:
        return Refinement({}, before, before, model.evaluated, {"before": explain(start), "after": explain(start)})
    placements = {
        ref: Placement(*pose[ref]) for ref in sorted(movable_refs) if pose[ref] != start[ref]
    }
    return Refinement(placements, before, after, model.evaluated, {"before": explain(start), "after": explain(pose)})""", """    if after.total >= before.total - 1e-6:
        pose = start
        after = before
    placements = {ref: Placement(*pose[ref]) for ref in sorted(pose) if pose[ref] != start0[ref]}
    return Refinement(placements, before, after, model.evaluated, {"before": explain(start), "after": explain(pose)})""")

sub("""    for members in components.values():
        if len(members) < 2""", """    for members in components.values():
        members = [m for m in members if m not in PLAN.att]
        if len(members) < 2""")

# --- align_groups: the engine's, then keep the rules (discard if it breaks the chain row/order, else re-settle attachments)
sub("def align_groups(", "def _align_groups_engine(")
src += '''

def align_groups(symbols, nets, **kwargs):
    model = _Model(symbols, nets, kwargs.get("reach") or {}, kwargs.get("weights") or Weights(), kwargs.get("half_widths"))
    plan = Plan(symbols, nets, model)
    start = {s.reference: (s.position, s.orientation) for s in symbols}
    pose = plan.prepass(dict(start), model)
    shifted = [replace(s, position=pose[s.reference][0], orientation=pose[s.reference][1]) for s in symbols]
    out = _align_groups_engine(shifted, nets, **kwargs)
    pose2 = dict(pose)
    for ref, p in out.placements.items():
        pose2[ref] = (p.position, p.orientation)
    for host in plan.hosts:
        plan.settle(pose2, host, model)
    if not plan.keeps(pose2):
        pose2 = pose
    placements = {ref: Placement(*pose2[ref]) for ref in sorted(pose2) if pose2[ref] != start[ref]}
    return Refinement(placements, out.before, out.after, out.evaluated)
'''

plan_code = Path('scripts/ci/sandbox/probes/attachments/plan_code.txt').read_text(encoding='utf-8')
sub("def refine_placement(", plan_code + "def refine_placement(")
Path("scripts/ci/sandbox/probes/attachments/pr_att.py").write_text(src, encoding="utf-8")
print("built")
