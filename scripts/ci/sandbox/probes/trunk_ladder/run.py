import math, subprocess, sys
from dataclasses import replace
from pathlib import Path
from scripts.ci.sandbox.sbx import report, placement_replay as pr
from scripts.ci.sandbox.probes.trunk_ladder import pr_trunk
from backend.schematic import orientation_prior as op, placement_refinement as base

OUT = Path("scripts/ci/sandbox/probes/trunk_ladder/out"); OUT.mkdir(exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SHARES = {"horizontal": 0.66, "vertical": 0.34}     # rung parts, measured by rung_stats.py
cs = pr.load_circuits(report.resolve_run("auto-st3"))
GROUPS = {"lm317": 10, "buck": 4}

def kind(c, pin_ref):
    return None

def with_prior(symbols, nets, rung):
    count = {}
    for n in nets:
        for p in n.pins: count[p] = len(n.pins)
    kinds = {}
    for n in nets:
        k = ("ground" if n.facing == (0, 1) or (n.facing is not None and tuple(n.facing) == (0, 1)) else "supply") if n.style == "power" else "signal"
        for p in n.pins: kinds[p] = k
    out, rungs = [], []
    for s in symbols:
        if not s.movable: out.append(s); continue
        pins = sorted(next(iter(s.geometry.values())).pins)
        pk = {p: kinds.get((s.reference, p), "nc") for p in pins}
        cls = op.part_class("", "", s.reference, len(pins)); ctx = op.part_context(pk)
        rails = [p for p, k in pk.items() if k in ("ground", "supply")]
        costs = None
        if rung and ctx == "series" and len(pins) == 2 and all(count.get((s.reference, p), 0) >= 3 for p in pins):
            best = max(SHARES.values()); rungs.append(s.reference)
            costs = {o: op.SCALE * math.log(best / max(SHARES[op._feature("axis", o, g, rails)], op.FLOOR)) for o, g in s.geometry.items()}
        if costs is None:
            costs = op.orientation_costs(cls, ctx, rails, s.geometry)
        out.append(replace(s, orientation_cost=costs) if costs else s)
    return out, rungs

def run(circuit, module, prior, rung):
    pr.solvers = module
    refine = circuit["refine"]; syms = list(refine["args"][0])
    rungs = []
    if prior: syms, rungs = with_prior(syms, refine["args"][1], rung)
    c2 = dict(circuit); c2["refine"] = {**refine, "args": (syms,) + tuple(refine["args"][1:])}
    return pr.replay(c2, rounds=2, orient=0), rungs

def score(result, ref_syms):
    pr.solvers = pr_trunk
    syms = [replace(s, orientation_cost=r.orientation_cost) for s, r in zip(sorted(result.symbols, key=lambda s: s.reference), sorted(ref_syms, key=lambda s: s.reference))]
    row, model = pr._describe("s", syms, result.nets, REACH, HW, pr_trunk.Weights())
    return row, replace(result, symbols=syms, model=model) if False else (syms, model)

def shot(result, syms, model, label, name):
    r = pr.Replay(nets=result.nets, symbols=syms, model=model)
    svgtxt = pr.svg(r, label); f = (OUT / f"{name}.svg"); f.write_text(svgtxt)
    import re; w, h = map(int, re.search(r'width="(\d+)" height="(\d+)"', svgtxt).groups())
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", f"--screenshot={(OUT/(name+'.png')).resolve()}", f"--window-size={w},{h}", f.resolve().as_uri()], capture_output=True, timeout=60)

for name, idx in GROUPS.items():
    circuit = cs[idx]; REACH = circuit["refine"]["kwargs"].get("reach") or {}; HW = circuit["refine"]["kwargs"].get("half_widths")
    pr_trunk.TRUNK_ENABLED = True
    variants = {"a greedy": (base, False, False), "b +prior": (base, True, False), "c +trunk": (pr_trunk, True, False), "d +rung": (pr_trunk, True, True)}
    _, _ = None, None
    # reference symbols carrying the prior (variant c's), for scoring
    ref_syms, _r = with_prior(list(circuit["refine"]["args"][0]), circuit["refine"]["args"][1], False)
    print(f"== {name}")
    for label, (mod, prior, rung) in variants.items():
        res, rungs = run(circuit, mod, prior, rung)
        row, (syms, model) = score(res, ref_syms)
        print(f"{label:9} total {row.total:7.1f} wire {row.length:6.1f} bends {row.bends:5.1f} jogs {row.jogs} cross {row.crossings} through ?  rungs={rungs}")
        c = model.cost({s.reference: (s.position, s.orientation) for s in syms}); print("          through", c.through, "extent %.0fx%.0f" % row.extent)
        print("          " + " ".join(f"{s.reference}:{s.orientation.rotation}{s.orientation.mirror}" for s in sorted(syms, key=lambda s: s.reference)))
        if label[0] in "bd": shot(res, syms, model, f"{name} {label}", f"{name}_{label[0]}")
