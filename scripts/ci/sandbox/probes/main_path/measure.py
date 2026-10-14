"""Main signal path straightness: KiCad's human demo sheets against the engine's redraws of the same demos.

Parts that carry a signal are the nodes: a part with two or more pins on non-rail nets (each net with 2+ part pins), or
one such pin plus a rail pin (sources and loads). Two parts share an edge when they share a non-rail net. The main path is
the longest shortest path between two degree-1 parts (source and output ends), found by BFS. For that chain:

1. rows: distinct body-centre rows (y rounded to 2.54 mm) divided by chain length; same-row share of consecutive pairs
   with |dy| <= 1.27 mm;
2. left-to-right share: consecutive pairs with x increasing (dx > 0.01 mm) from the end with the smaller x;
3. vertical travel ratio: sum of |dy| along the chain divided by the chain's horizontal extent (max - min centre x).

Body centre = centre of the union of a part's BODY boxes (pin-tip centroid if it has none). Connectivity and net names
come from probes/regularity/measure.py (unique_sheet, connect), so the two probes read nets the same way.
Rail nets: GND, AGND, DGND, "0", names starting with a sign and a digit (+5V, -15V), and VCC/VDD/VEE/VSS.

Run from the repo root:
    PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/main_path/measure.py
Writes out/summary.md and out/rows.csv beside this file.
"""
from __future__ import annotations

import csv
import importlib.util
import re
import statistics
import sys
from collections import defaultdict, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[5]
for path in (REPO / "scripts" / "ci" / "sandbox", REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.presentation_geometry import GeometryKind as K  # noqa: E402
from sbx import demos  # noqa: E402

_spec = importlib.util.spec_from_file_location("regularity_measure", HERE.parent / "regularity" / "measure.py")
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)  # its main() is behind a __main__ guard

RUNS = REPO / "scripts" / "ci" / "sandbox" / ".work" / "runs"
ENGINE_RUNS = (("density-1", "20261012T022455Z-density-1"), ("tidy-1", "20261012T005238Z-tidy-1"))
ENGINE_DEMOS = ("demo_buck", "demo_lm317", "demo_opamp", "demo_ampac")

RAIL = re.compile(r"^(GND|AGND|DGND)|^0$|^[+-]\d|^(VCC|VDD|VEE|VSS)", re.IGNORECASE)
PITCH = 2.54
ROW_TOL = 1.27 + 1e-6
LR_TOL = 0.01


def sheets():
    for demo in demos.DEMOS.values():
        yield "human", demo.key, "KiCad", demos.sheet_path(demo)
    for run_label, run_dir in ENGINE_RUNS:
        for key in ENGINE_DEMOS:
            yield ("engine", key, run_label,
                   RUNS / run_dir / "artifacts" / f"schematic-engine-{key}" / "mcu_smoke.kicad_sch")


def share(flags):
    flags = list(flags)
    return sum(flags) / len(flags) if flags else None


def analyze(path: Path) -> dict:
    text, power, renamed = reg.unique_sheet(path)
    scene, coverage = measured_scene_from_kicad(reg._Text(text))
    items = list(scene.items)
    _segs, pts, dsu = reg.connect(items, power)

    names: dict = defaultdict(set)
    for node in list(dsu.parent):
        if node[0] == "n":
            names[dsu.find(node)].add(node[1])

    def rail(root) -> bool:
        return any(RAIL.search(n) for n in names.get(root, ()))

    pins: dict = defaultdict(list)
    pin_count: dict = defaultdict(int)
    tips: dict = defaultdict(list)
    for k, (xy, role, it) in enumerate(pts):
        if role != "pin":
            continue
        tips[it.owner].append(xy)
        if it.owner.startswith("#"):  # power symbols and flags are not parts
            continue
        root = dsu.find(("p", k))
        pins[it.owner].append(root)
        pin_count[root] += 1

    def signal(root) -> bool:
        return not rail(root) and pin_count[root] >= 2

    qualifying: dict = {}
    for ref, roots in pins.items():
        sig = [r for r in roots if signal(r)]
        if len(sig) >= 2 or (len(sig) == 1 and any(rail(r) for r in roots)):
            qualifying[ref] = sig

    on_net: dict = defaultdict(set)
    for ref, sig in qualifying.items():
        for r in sig:
            on_net[r].add(ref)
    adj: dict = defaultdict(set)
    for refs in on_net.values():
        for a in refs:
            adj[a].update(refs - {a})

    nodes = sorted(qualifying)
    ends = [n for n in nodes if len(adj[n]) == 1]
    fallback = len(ends) < 2  # no two degree-1 parts: search all parts instead
    sources = nodes if fallback else ends
    best = None
    for a in sources:
        dist, parent = {a: 0}, {a: None}
        queue = deque([a])
        while queue:
            u = queue.popleft()
            for v in sorted(adj[u]):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    parent[v] = u
                    queue.append(v)
        for b in sources:
            if b != a and b in dist and (best is None or dist[b] > best[0]):
                best = (dist[b], b, parent)

    chain: list = []
    if best is not None:
        _, node, parent = best
        while node is not None:
            chain.append(node)
            node = parent[node]
        chain.reverse()

    bodies: dict = defaultdict(list)
    for it in items:
        if it.kind == K.BODY:
            bodies[it.owner].append(it.bounds)

    def centre(ref):
        if bodies.get(ref):
            boxes = bodies[ref]
            x0 = min(b.left for b in boxes)
            x1 = max(b.right for b in boxes)
            y0 = min(b.top for b in boxes)
            y1 = max(b.bottom for b in boxes)
            return (x0 + x1) / 2, (y0 + y1) / 2
        xs = [p[0] for p in tips[ref]]
        ys = [p[1] for p in tips[ref]]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    result = {
        "parts_total": len(pins),
        "parts_in_graph": len(qualifying),
        "degrees": " ".join(f"{n}:{len(adj[n])}" for n in nodes),
        "edges": sum(len(v) for v in adj.values()) // 2,
        "ends": len(ends),
        "fallback": fallback,
        "chain_len": len(chain),
        "chain": chain,
        "rows_per_len": None, "same_row": None, "lr": None, "vert_ratio": None,
        "vert_mm": None, "horiz_mm": None,
        "renamed_refs": renamed,
        "unknown_libraries": len(coverage.get("unknown_libraries", [])),
    }
    if len(chain) >= 2:
        cen = {r: centre(r) for r in chain}
        if cen[chain[0]][0] > cen[chain[-1]][0]:  # start from the end with the smaller x
            chain.reverse()
        xs = [cen[r][0] for r in chain]
        ys = [cen[r][1] for r in chain]
        pairs = list(zip(chain, chain[1:]))
        dxs = [cen[b][0] - cen[a][0] for a, b in pairs]
        dys = [cen[b][1] - cen[a][1] for a, b in pairs]
        horiz = max(xs) - min(xs)
        vert = sum(abs(d) for d in dys)
        result.update(
            rows_per_len=len({round(y / PITCH) for y in ys}) / len(chain),
            same_row=share(abs(d) <= ROW_TOL for d in dys),
            lr=share(d > LR_TOL for d in dxs),
            vert_ratio=vert / horiz if horiz > LR_TOL else None,
            vert_mm=vert,
            horiz_mm=horiz,
        )
    return result


def med(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


GROUPS = (
    ("human (KiCad demos)", lambda r: r["group"] == "human"),
    ("engine, all 8", lambda r: r["group"] == "engine"),
    ("engine, density-1", lambda r: r["group"] == "engine" and r["run"] == "density-1"),
    ("engine, tidy-1", lambda r: r["group"] == "engine" and r["run"] == "tidy-1"),
)
METRICS = (
    ("chain length", "chain_len", 1),
    ("rows/length", "rows_per_len", 2),
    ("same-row share", "same_row", 2),
    ("left-to-right share", "lr", 2),
    ("vertical travel ratio", "vert_ratio", 2),
)


def main() -> None:
    rows = []
    for group, key, run, path in sheets():
        if not path.is_file():
            raise FileNotFoundError(path)
        r = analyze(path)
        r.update(sheet=f"{group}:{key}" + ("" if group == "human" else f":{run}"), group=group, key=key, run=run)
        rows.append(r)

    out = HERE / "out"
    out.mkdir(exist_ok=True)
    fields = ["sheet", "group", "key", "run", "parts_total", "parts_in_graph", "edges", "ends", "fallback", "degrees",
              "chain_len", "rows_per_len", "same_row", "lr", "vert_ratio", "vert_mm", "horiz_mm", "chain",
              "renamed_refs", "unknown_libraries"]
    with (out / "rows.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "chain": " ".join(r["chain"])})

    lines = [
        "# Main signal path: human sheets vs engine redraws",
        "",
        "Chain: longest shortest path between two degree-1 signal parts (BFS). Signal parts: 2+ pins on non-rail nets "
        "shared with other parts, or one such pin plus a rail pin. Rows = distinct y rounded to 2.54 mm, divided by chain "
        "length. Same-row = share of consecutive pairs with |dy| <= 1.27 mm. Left-to-right = share of consecutive pairs "
        "with dx > 0.01 mm, oriented from the end with the smaller x. Vertical travel ratio = sum |dy| / (max - min x).",
        "",
        "## Per sheet",
        "",
        "| sheet | chain length | rows/length | same-row share | left-to-right share | vertical travel ratio "
        "| parts | edges | ends | fallback |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['sheet']} | {r['chain_len']} | {fmt(r['rows_per_len'])} | {fmt(r['same_row'])} | {fmt(r['lr'])} "
            f"| {fmt(r['vert_ratio'])} | {r['parts_in_graph']} | {r['edges']} | {r['ends']} | "
            f"{'yes' if r['fallback'] else 'no'} |")
    lines += ["", "## Medians", "", "| group | n | " + " | ".join(m[0] for m in METRICS) + " |",
              "|---|---:|" + "---:|" * len(METRICS)]
    for label, pred in GROUPS:
        sel = [r for r in rows if pred(r)]
        cells = [fmt(med([r[k] for r in sel]), nd) for _, k, nd in METRICS]
        lines.append(f"| {label} | {len(sel)} | " + " | ".join(cells) + " |")
    lines += ["", "## Chains (source end first, after orientation)", ""]
    for r in rows:
        lines.append(f"- {r['sheet']}: " + (" - ".join(r["chain"]) if r["chain"] else "no chain"))
    lines += ["", "## Coverage", ""]
    for r in rows:
        lines.append(f"- {r['sheet']}: parts {r['parts_total']} total, {r['parts_in_graph']} in graph; "
                     f"degrees [{r['degrees']}]; renamed duplicate refs {r['renamed_refs']}, "
                     f"unknown libraries {r['unknown_libraries']}, horizontal extent {fmt(r['horiz_mm'], 1)} mm, "
                     f"vertical travel {fmt(r['vert_mm'], 1)} mm")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines[lines.index("## Per sheet"):lines.index("## Chains (source end first, after orientation)")]))


if __name__ == "__main__":
    main()
