"""Regularity measures for KiCad's human demo sheets and for sheets the engine drew.

Measurement only. Reads each sheet with backend.schematic.kicad_geometry.measured_scene_from_kicad,
rebuilds net connectivity from the wire geometry (the geometry module leaves wire nets unset without a
netlist), and writes out/summary.md and out/rows.csv beside this file.

Run from the repo root:
    PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/regularity/measure.py
"""
from __future__ import annotations

import csv
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[5]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.presentation_geometry import GeometryKind as K  # noqa: E402

DEMOS = Path("C:/Program Files/KiCad/10.0/share/kicad/demos")
SIM = DEMOS / "simulation"
RUNS = REPO / "scripts" / "ci" / "sandbox" / ".work" / "runs"

HUMAN = [
    ("human:lm317", SIM / "power_supplies/LM317_power_supply/power_supply.kicad_sch"),
    ("human:buck", SIM / "power_supplies/buck_conv/buck_conv.kicad_sch"),
    ("human:ampac", SIM / "amplifier-ac/amplifier-ac.kicad_sch"),
    ("human:opamp", SIM / "generic_models/generic_opamp_bip.kicad_sch"),
    ("human:smps_boost", SIM / "power_supplies/boost/smps-com.kicad_sch"),
    ("human:sallen_key", SIM / "sallen_key/sallen_key.kicad_sch"),
    ("human:laser_driver", SIM / "laser_driver/laser_driver.kicad_sch"),
    ("human:class_d", SIM / "class-d/Class-D.kicad_sch"),
]
ENGINE = [
    ("engine:lm317", RUNS / "20261012T001511Z-route-fix-1/artifacts/schematic-engine-demo_lm317/mcu_smoke.kicad_sch"),
    ("engine:ampac", RUNS / "20261011T234113Z-trunk-1/artifacts/schematic-engine-demo_ampac/mcu_smoke.kicad_sch"),
    ("engine:buck", RUNS / "20261011T234113Z-trunk-1/artifacts/schematic-engine-demo_buck/mcu_smoke.kicad_sch"),
]

CONNECT = 0.01   # mm: touching, for connectivity
ROW = 0.1        # mm: a tip this close to the trunk row is on it
SHORT = 2.54     # mm: "short drop" threshold
GRID = 1.27      # mm: ground rows are rounded to this
GROUND = re.compile(r"^(GND.*|AGND.*|DGND.*|0)$")
PART_EXCLUDE = ("#PWR", "#FLG")

SYMBOL = re.compile(r'\(symbol\s+\(lib_id')
PROP = re.compile(r'\(property\s+"(Reference|Value)"\s+"((?:\\.|[^"\\])*)"')
UNIT = re.compile(r'\(unit\s+(\d+)\)')


class _Text:
    """Stands in for a Path: measured_scene_from_kicad only calls read_text on it."""

    def __init__(self, text):
        self.text = text

    def read_text(self, encoding=None):
        return self.text


def unique_sheet(path: Path):
    """The sheet text with duplicated reference designators split, and each power symbol's value.

    Unannotated sheets reuse references (e.g. six parts named R2). The geometry module keys owners by
    reference, so such parts would merge. A reference shared by instances whose units are all distinct is one
    multi-unit part and is kept; otherwise each instance gets ``ref@k``. Power values are read from the sheet
    because the geometry module only sets them for symbols from the standard ``power:`` library.
    """
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in SYMBOL.finditer(text)]
    ends = starts[1:] + [len(text)]
    inst = []
    for s, e in zip(starts, ends):
        block = text[s:e]
        props, unit = {}, 1
        for m in PROP.finditer(block):
            props.setdefault(m.group(1), (s + m.start(2), s + m.end(2), m.group(2)))
        um = UNIT.search(block)
        if um:
            unit = int(um.group(1))
        inst.append({"ref": props.get("Reference", (0, 0, ""))[2], "value": props.get("Value", (0, 0, None))[2],
                     "span": props.get("Reference"), "unit": unit})
    by_ref: dict = {}
    for i, d in enumerate(inst):
        by_ref.setdefault(d["ref"], []).append(i)
    renames = {}
    for ref, idxs in by_ref.items():
        if len(idxs) < 2 or len({inst[i]["unit"] for i in idxs}) == len(idxs):
            continue
        for k, i in enumerate(idxs, 1):
            renames[i] = f"{ref}@{k}"
    edits = sorted(((inst[i]["span"], new) for i, new in renames.items() if inst[i]["span"]), reverse=True)
    for (a, b, _old), new in edits:
        text = text[:a] + new + text[b:]
    power = {}
    for i, d in enumerate(inst):
        ref = renames.get(i, d["ref"])
        if ref.startswith("#PWR") and d["value"]:
            power[ref] = d["value"]
    return text, power, len(renames)


def near(a, b, tol=CONNECT):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def on_seg(p, seg, tol=CONNECT):
    """True if p lies on the (orthogonal) segment, endpoints included, within tol."""
    (x1, y1), (x2, y2) = seg
    x, y = p
    if abs(x1 - x2) <= tol:
        return abs(x - x1) <= tol and min(y1, y2) - tol <= y <= max(y1, y2) + tol
    if abs(y1 - y2) <= tol:
        return abs(y - y1) <= tol and min(x1, x2) - tol <= x <= max(x1, x2) + tol
    return False


class DSU:
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            nxt = self.parent[x]
            self.parent[x] = root
            x = nxt
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def connect(items, power):
    """Union-find over wire segments, pin tips, junctions and label anchors, joined by name where named.

    Power symbols (#PWR) join the net named by their value (``power``); PWR_FLAG symbols (#FLG) join geometry only.
    """
    segs = [it.segment for it in items if it.kind == K.WIRE and it.segment is not None]
    pts = []  # (xy, role, item)
    for it in items:
        if it.kind == K.PIN_ENDPOINT and it.id.split("~")[0].endswith(":terminal"):
            pts.append(((it.bounds.left, it.bounds.top), "pin", it))
        elif it.kind == K.JUNCTION:
            b = it.bounds
            pts.append((((b.left + b.right) / 2, (b.top + b.bottom) / 2), "junction", it))
        elif it.kind == K.NET_LABEL and it.anchor is not None:
            pts.append((it.anchor, "label", it))

    dsu = DSU()
    for i in range(len(segs)):
        dsu.add(("s", i))
    for i, (a, b) in enumerate(segs):
        for j in range(i + 1, len(segs)):
            c, d = segs[j]
            if on_seg(a, segs[j]) or on_seg(b, segs[j]) or on_seg(c, segs[i]) or on_seg(d, segs[i]):
                dsu.union(("s", i), ("s", j))
    for k, (xy, role, it) in enumerate(pts):
        dsu.add(("p", k))
        for i, seg in enumerate(segs):
            if on_seg(xy, seg):
                dsu.union(("p", k), ("s", i))
        if role == "pin" and it.owner.startswith("#PWR") and power.get(it.owner):
            dsu.union(("p", k), ("n", power[it.owner]))
        elif role == "label" and it.net:
            dsu.union(("p", k), ("n", it.net))
    for k in range(len(pts)):
        for m in range(k + 1, len(pts)):
            if near(pts[k][0], pts[m][0]):
                dsu.union(("p", k), ("p", m))
    return segs, pts, dsu


def rect_gap(p, q):
    """Edge-to-edge distance between two (left, top, right, bottom) rectangles; 0 when they touch or overlap."""
    dx = max(0.0, p[0] - q[2], q[0] - p[2])
    dy = max(0.0, p[1] - q[3], q[1] - p[3])
    return math.hypot(dx, dy)


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def analyze(label: str, path: Path):
    text, power, renamed = unique_sheet(path)
    scene, _notes = measured_scene_from_kicad(_Text(text))
    items = scene.items
    segs, pts, dsu = connect(items, power)
    seg_root = [dsu.find(("s", i)) for i in range(len(segs))]

    names: dict = {}
    for key in list(dsu.parent):
        if key[0] == "n":
            names.setdefault(dsu.find(key), set()).add(key[1])
    anon_ids: dict = {}

    def net_name(root):
        if root in names:
            return "|".join(sorted(names[root]))
        return f"net{anon_ids.setdefault(root, len(anon_ids))}"

    pin_ks = [k for k, (_, role, _) in enumerate(pts) if role == "pin"]
    part_ks = [k for k in pin_ks if not pts[k][2].owner.startswith(PART_EXCLUDE)]
    part_root = {k: dsu.find(("p", k)) for k in part_ks}
    pins_on = Counter(part_root.values())
    wire_on = Counter()
    for i, ((x1, y1), (x2, y2)) in enumerate(segs):
        wire_on[seg_root[i]] += abs(x2 - x1) + abs(y2 - y1)
    trunks = [r for r, n in pins_on.items() if n >= 3 and wire_on.get(r, 0) > 0]
    nets3_nowire = sum(1 for r, n in pins_on.items() if n >= 3 and wire_on.get(r, 0) == 0)

    by_owner: dict = {}
    for k in part_ks:
        by_owner.setdefault(pts[k][2].owner, []).append(k)

    def trunk_row(root):
        h, v = Counter(), Counter()
        for i, ((x1, y1), (x2, y2)) in enumerate(segs):
            if seg_root[i] != root:
                continue
            length = abs(x2 - x1) + abs(y2 - y1)
            if length == 0:
                continue
            if y1 == y2:
                h[round(y1, 2)] += length
            elif x1 == x2:
                v[round(x1, 2)] += length
        best_h = max(h.items(), key=lambda kv: kv[1], default=(None, 0))
        best_v = max(v.items(), key=lambda kv: kv[1], default=(None, 0))
        if best_v[1] > best_h[1]:
            return "V", best_v[0]
        return "H", best_h[0]

    def hanging(root, orient, row):
        """Two-pin parts with one pin on the trunk whose tip is on the row or joined to it by one perpendicular segment."""
        out = []
        for owner, ks in by_owner.items():
            if len(ks) != 2:
                continue
            on = [k for k in ks if part_root[k] == root]
            if len(on) != 1:
                continue
            xy = pts[on[0]][0]
            perp = (lambda p: p[1]) if orient == "H" else (lambda p: p[0])
            along = xy[0] if orient == "H" else xy[1]
            drop = abs(perp(xy) - row)
            if drop <= ROW:
                out.append((owner, along, drop))
                continue
            inc = [i for i, seg in enumerate(segs) if on_seg(xy, seg)]
            if len(inc) != 1:
                continue
            a, b = segs[inc[0]]
            if near(a, b):
                continue
            if near(a, xy):
                far = b
            elif near(b, xy):
                far = a
            else:
                continue
            perpendicular = abs(a[0] - b[0]) <= CONNECT if orient == "H" else abs(a[1] - b[1]) <= CONNECT
            if perpendicular and abs(perp(far) - row) <= ROW:
                out.append((owner, along, drop))
        return out

    rows = []
    all_drops = []
    cvs = []
    for root in trunks:
        orient, row = trunk_row(root)
        hang = hanging(root, orient, row)
        drops = [d for _, _, d in hang]
        all_drops.extend(drops)
        along = sorted(a for _, a, _ in hang)
        cv, gaps = None, []
        if len(along) >= 3:
            gaps = [b - a for a, b in zip(along, along[1:])]
            mean = sum(gaps) / len(gaps)
            if mean > 0:
                cv = statistics.pstdev(gaps) / mean
                cvs.append(cv)
        rows.append({
            "sheet": label, "net": net_name(root), "orient": orient, "row_mm": round(row, 2),
            "part_pins": pins_on[root], "wire_mm": round(wire_on[root], 2), "hanging": len(hang),
            "on_row": sum(d <= ROW for d in drops), "within_2_54": sum(d <= SHORT for d in drops),
            "drops_mm": " ".join(f"{d:.2f}" for d in sorted(drops)),
            "pitch_cv": "" if cv is None else round(cv, 3),
            "gaps_mm": " ".join(f"{g:.2f}" for g in gaps),
        })

    gnd: dict = {}
    for xy, role, it in pts:
        if role == "pin" and it.owner.startswith("#PWR") and GROUND.match(power.get(it.owner) or ""):
            gnd.setdefault(it.owner, xy)
    grow = [round(xy[1] / GRID) for xy in gnd.values()]

    bodies: dict = {}
    for it in items:
        if it.kind == K.BODY and not it.owner.startswith(PART_EXCLUDE):
            b = it.bounds
            if it.owner in bodies:
                l, t, r, bt = bodies[it.owner]
                bodies[it.owner] = (min(l, b.left), min(t, b.top), max(r, b.right), max(bt, b.bottom))
            else:
                bodies[it.owner] = (b.left, b.top, b.right, b.bottom)
    keys = list(bodies)
    nn = []
    for a in keys:
        others = [rect_gap(bodies[a], bodies[b]) for b in keys if b != a]
        if others:
            nn.append(min(others))

    hang_total = len(all_drops)
    summary = {
        "sheet": label,
        "trunks": len(trunks),
        "nets3_nowire": nets3_nowire,
        "hanging": hang_total,
        "on_row": (sum(d <= ROW for d in all_drops) / hang_total) if hang_total else None,
        "within_2_54": (sum(d <= SHORT for d in all_drops) / hang_total) if hang_total else None,
        "pitch_cv_median": median(cvs),
        "pitch_trunks": len(cvs),
        "ground_symbols": len(grow),
        "ground_rows": len(set(grow)),
        "ground_rows_per_symbol": (len(set(grow)) / len(grow)) if grow else None,
        "top_row_share": (Counter(grow).most_common(1)[0][1] / len(grow)) if grow else None,
        "parts": len(bodies),
        "renamed_refs": renamed,
        "nn_median": median(nn),
        "nn_min": min(nn) if nn else None,
    }
    return summary, rows


def fmt(x, nd=2):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def group_median(results, key):
    return median([r.get(key) for r in results if "missing" not in r])


def main():
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    results, rows, missing = [], [], []
    for label, path in HUMAN + ENGINE:
        if not path.exists():
            missing.append(f"{label}: {path}")
            continue
        summary, trunk_rows = analyze(label, path)
        results.append(summary)
        rows.extend(trunk_rows)
    if rows:
        with (out / "rows.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    cols = [
        ("sheet", "sheet", 0), ("trunks", "trunks", 0), ("hanging", "hanging", 0),
        ("on_row", "on_row share", 3), ("within_2_54", "<=2.54 share", 3),
        ("pitch_cv_median", "pitch CV median (trunks with 3+ hanging)", 3),
        ("ground", "ground rows/symbols", 0), ("top_row_share", "top-row share", 3),
        ("nn_median", "nn gap median mm", 2), ("nn_min", "nn gap min mm", 2),
    ]

    def cells(r):
        ground = f"{r['ground_rows']}/{r['ground_symbols']}" if r["ground_symbols"] else "n/a"
        return [
            r["sheet"], fmt(r["trunks"], 0), fmt(r["hanging"], 0), fmt(r["on_row"], 3),
            fmt(r["within_2_54"], 3), f"{fmt(r['pitch_cv_median'], 3)} (n={r['pitch_trunks']})", ground,
            fmt(r["top_row_share"], 3), fmt(r["nn_median"], 2), fmt(r["nn_min"], 2),
        ]

    header = "| " + " | ".join(c[1] for c in cols) + " |"
    sep = "|" + "---|" * len(cols)
    lines = [header, sep] + ["| " + " | ".join(cells(r)) + " |" for r in results]

    def group_row(name, prefix):
        grp = [r for r in results if r["sheet"].startswith(prefix)]
        if not grp:
            return None
        meds = {k: median([r[k] for r in grp]) for k in (
            "trunks", "hanging", "on_row", "within_2_54", "pitch_cv_median", "ground_rows_per_symbol",
            "top_row_share", "nn_median", "nn_min")}
        ground = f"{fmt(meds['ground_rows_per_symbol'], 3)} (ratio)"
        return "| " + " | ".join([
            f"{name} (median)", fmt(meds["trunks"], 1), fmt(meds["hanging"], 1), fmt(meds["on_row"], 3),
            fmt(meds["within_2_54"], 3), fmt(meds["pitch_cv_median"], 3), ground,
            fmt(meds["top_row_share"], 3), fmt(meds["nn_median"], 2), fmt(meds["nn_min"], 2),
        ]) + " |"

    for name, prefix in (("human", "human:"), ("engine", "engine:")):
        row = group_row(name, prefix)
        if row:
            lines.append(row)

    notes = [
        "",
        "Method (all lengths mm):",
        "- Nets: union-find over orthogonal wire segments (touching endpoints or T-touch), pin tips, junctions, and label anchors. Tips and wires within 0.01 join. #PWR tips join by value, labels by name; #FLG tips join geometry only.",
        "- Trunk: nets with 3+ part pins (non-#PWR/#FLG) and some wire. Row = the H y or V x carrying most of that net's wire length (either orientation).",
        "- Hanging part: owner with exactly 2 pin endpoints and exactly one pin on the trunk net, where that pin's tip is within 0.1 of the row, or its only incident segment is perpendicular and ends on the row. Drop = |tip perpendicular coordinate - row|. Pairs counted per trunk.",
        "- on_row = drop <= 0.1; <=2.54 = drop <= 2.54. Pitch: positions = tip coordinate along the trunk, gaps between sorted positions, CV = population std / mean (trunks with 3+ hanging parts and mean gap > 0); sheet value = median over trunks.",
        "- Ground: #PWR tips with value matching GND*/AGND*/DGND*/0; row = round(tip y / 1.27); ratio = distinct rows / symbols; top-row share = largest row count / symbols.",
        "- NN gap: per part, union bbox of its BODY graphics; edge-to-edge distance to the nearest other part (0 if overlapping); sheet median and min.",
        "- nets with 3+ part pins but no wire (excluded from trunks): " + ", ".join(f"{r['sheet']}={r['nets3_nowire']}" for r in results),
        "- duplicated reference designators split into separate parts (count per sheet): " + ", ".join(f"{r['sheet']}={r['renamed_refs']}" for r in results),
    ]
    if missing:
        notes.append("- missing sheets: " + "; ".join(missing))
    table = "\n".join(lines + notes) + "\n"
    (out / "summary.md").write_text("# Regularity measures\n\n" + table, encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
