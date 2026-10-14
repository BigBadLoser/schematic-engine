import re, subprocess
from pathlib import Path
from scripts.ci.sandbox.sbx import report, placement_replay as pr
from scripts.ci.sandbox.probes.attachments import pr_att
from backend.schematic import placement_refinement as base

OUT = Path("scripts/ci/sandbox/probes/attachments/out"); OUT.mkdir(exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
cs = pr.load_circuits(report.resolve_run("auto-st3"))
GROUPS = {"lm317": 10, "amp": 58}


def run(circuit, module, attach, chain):
    pr.solvers = module
    pr_att.ATTACH, pr_att.CHAIN = attach, chain
    return pr.replay(circuit, rounds=2, orient=0)


def shot(res, label, name):
    r = pr.Replay(nets=res.nets, symbols=res.symbols, model=res.model)
    text = pr.svg(r, label); f = OUT / f"{name}.svg"; f.write_text(text)
    w, h = map(int, re.search(r'width="(\d+)" height="(\d+)"', text).groups())
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", f"--screenshot={(OUT / (name + '.png')).resolve()}",
                    f"--window-size={w},{h}", f.resolve().as_uri()], capture_output=True, timeout=60)


for name, idx in GROUPS.items():
    circuit = cs[idx]; refine = circuit["refine"]
    reach = refine["kwargs"].get("reach") or {}; hw = refine["kwargs"].get("half_widths")
    print(f"== {name} ({len(refine['args'][0])} symbols)")
    for label, mod, a, c in (("a engine", base, False, False), ("b +A", pr_att, True, False), ("c +A+B", pr_att, True, True)):
        res = run(circuit, mod, a, c)
        pr.solvers = base
        row, model = pr._describe(label, res.symbols, res.nets, reach, hw, base.Weights())
        cost = model.cost({s.reference: (s.position, s.orientation) for s in res.symbols})
        print(f"{label:9} total {cost.total:7.1f} wire {cost.length:6.1f} bends {cost.bends:5.1f} jogs {cost.jogs} cross {cost.crossings} "
              f"through {cost.through} overlaps {cost.overlaps} extent {row.extent[0]:.0f}x{row.extent[1]:.0f}")
        res.model = model
        if label[0] in "ac":
            shot(res, f"{name} {label}", f"{name}_{label[0]}")
        if label[0] == "c":
            pl = pr_att.Plan(list(refine["args"][0]), refine["args"][1], model)
            nf = {}; model.cost({s.reference: (s.position, s.orientation) for s in res.symbols}, None, nf)
            print("  defects:", {k: v for k, v in nf.items() if k in ("through", "overlap")})
            fp = {s.reference: (s.position, s.orientation) for s in res.symbols}
            pl.noslot = set(); bad = pl.hang_errors(fp, model)
            horiz = [x for x in pl.chain if len(pl.symbols[x].geometry[fp[x][1]].pins) == 2 and
                     len({round(model.world(x, *fp[x])[0][p][0][1], 2) for p in model.world(x, *fp[x])[0]}) > 1]
            print("  hang violations:", bad, "| chain 2-pin parts off the row line:", horiz,
                  "| att boxes overlapping boxes:", [(a_, o_) for a_ in pl.att for o_ in fp if o_ != a_ and
                   base._box_overlap(model.world(a_, *fp[a_])[1], model.world(o_, *fp[o_])[1])])
            print("  chain:", " -> ".join(pl.chain), "| y", pl.y)
            print("  attachments:", {r: f"{v[0]}.{v[1]} {v[2]} dx{pl.dx[r]:+.1f}" for r, v in sorted(pl.att.items())})
            print("  final x/y:", " ".join(f"{s.reference}({s.position[0]:.0f},{s.position[1]:.0f})" for s in sorted(res.symbols, key=lambda s: s.reference)))
