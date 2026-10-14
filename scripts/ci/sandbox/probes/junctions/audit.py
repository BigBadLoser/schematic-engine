"""Audit junctions, near-aligned pin pairs and wires hugging body edges, on the engine's redraws and KiCad's demo sheets.

1. Four-way junctions: four or more wire segment ends meet (within 0.01 mm) at a point that is not a pin tip. Three-way
   (T) joins are counted for context.
2. Near-aligned pin pairs: two pin tips on the same netlist net, 1.27 mm apart on one axis (+-0.01) and more than 2.54 mm
   apart on the other.
3. Wires along bodies: wire segments within 0.6 mm of a BODY edge, parallel to it, overlapping the edge by more than
   2.54 mm, and not on a part the wire's net connects to.

Nets come from `kicad-cli sch export netlist` for every sheet. Body edges are the BODY bounding boxes.

Run: PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/junctions/audit.py
Writes out/summary.md next to this file.
"""
from __future__ import annotations

import collections
import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = HERE.parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.presentation_geometry import GeometryKind as K  # noqa: E402
from sbx import demos  # noqa: E402

ENGINE_RUN = REPO / "scripts" / "ci" / "sandbox" / ".work" / "runs" / "20261011T223823Z-timed-2" / "artifacts"
OUT = HERE / "out" / "summary.md"
SAME_POINT = 0.01   # mm
PITCH = 1.27        # mm
SPAN = 2.54         # mm
EDGE_NEAR = 0.6     # mm
TOL_PITCH = 0.01    # mm


def near(a, b, tol=SAME_POINT):
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def pin_number(tip):
    # Tip ids are "<ref>.<pin>:terminal"; the owner is the ref.
    return tip.id[len(tip.owner) + 1:-len(":terminal")]


def pin_label(tip):
    return f"{tip.owner}.{pin_number(tip)}"


def sheets():
    for folder in sorted(ENGINE_RUN.glob("schematic-engine-*")):
        yield "engine", folder.name.removeprefix("schematic-engine-"), folder / "mcu_smoke.kicad_sch"
    for demo in demos.DEMOS.values():
        yield "human", demo.key, demos.sheet_path(demo)


def junction_counts(ends, tip_points):
    centers, members = [], []
    for p in ends:
        for k, c in enumerate(centers):
            if near(c, p):
                members[k].append(p)
                break
        else:
            centers.append(p)
            members.append([p])
    four, three, sites = 0, 0, []
    for c, m in zip(centers, members):
        if any(near(c, t) for t in tip_points):
            continue
        if len(m) >= 4:
            four += 1
            sites.append(c)
        elif len(m) == 3:
            three += 1
    return four, three, sites


def near_aligned_pairs(tips):
    by_net = collections.defaultdict(list)
    for item, point, net in tips:
        if net is not None:
            by_net[net].append((item, point))
    pairs = []
    for net, rows in by_net.items():
        for i, (a, pa) in enumerate(rows):
            for b, pb in rows[i + 1:]:
                dx, dy = abs(pa[0] - pb[0]), abs(pa[1] - pb[1])
                if (abs(dx - PITCH) <= TOL_PITCH and dy > SPAN) or (abs(dy - PITCH) <= TOL_PITCH and dx > SPAN):
                    pairs.append((net, pin_label(a), pa, pin_label(b), pb, dx, dy))
    return pairs


def along_body(segments, bodies, excluded_for):
    flags = []  # (run, offset, segment, body, edge name)
    for w in segments:
        (ax, ay), (bx, by) = w.segment
        horizontal, vertical = ay == by, ax == bx
        if horizontal == vertical:
            continue  # zero length, or not orthogonal (the scene drops those)
        skip = excluded_for(w)
        if horizontal:
            lo, hi = sorted((ax, bx))
            fixed = ay
        else:
            lo, hi = sorted((ay, by))
            fixed = ax
        for body in bodies:
            if body.owner in skip:
                continue
            B = body.bounds
            if horizontal:
                edges, span = (("top", B.top), ("bottom", B.bottom)), (B.left, B.right)
            else:
                edges, span = (("left", B.left), ("right", B.right)), (B.top, B.bottom)
            for name, edge in edges:
                offset = abs(fixed - edge)
                if offset <= EDGE_NEAR:
                    run = min(hi, span[1]) - max(lo, span[0])
                    if run > SPAN:
                        flags.append((run, offset, w, body, name))
    return flags


def box_gap(segment, B):
    (ax, ay), (bx, by) = segment
    x0, x1 = sorted((ax, bx))
    y0, y1 = sorted((ay, by))
    return math.hypot(max(B.left - x1, x0 - B.right, 0.0), max(B.top - y1, y0 - B.bottom, 0.0))


def measure(group, key, sheet, xml):
    scene, _ = measured_scene_from_kicad(sheet, netlist_xml=xml)
    items = scene.items
    netlist = demos.parse_netlist(xml)
    pin_net = {(ref, pin): net.name for net in netlist.nets for ref, pin, _ in net.nodes}

    tips = [(it, (it.bounds.left, it.bounds.top), pin_net.get((it.owner, pin_number(it))))
            for it in items if it.kind == K.PIN_ENDPOINT]
    tip_points = [p for _, p, _ in tips]
    segments = [it for it in items if it.kind == K.WIRE and it.segment is not None and it.segment[0] != it.segment[1]]
    ends = [p for w in segments for p in w.segment]
    four, three, four_sites = junction_counts(ends, tip_points)
    dots = sum(it.kind == K.JUNCTION for it in items)
    pairs = near_aligned_pairs(tips)

    owners_by_net = collections.defaultdict(set)
    for item, _, net in tips:
        if net is not None:
            owners_by_net[net].add(item.owner)
    bodies = [it for it in items if it.kind == K.BODY]

    fallback = []

    def excluded_for(w):
        if w.net is not None and w.net in owners_by_net:
            return owners_by_net[w.net]
        # No attributed net: fall back to the parts whose pin tips touch either end of the segment.
        fallback.append(w.id)
        a, b = w.segment
        return {item.owner for item, p, _ in tips if near(p, a) or near(p, b)}

    flags = along_body(segments, bodies, excluded_for)
    gaps = [min(box_gap(w.segment, b.bounds) for b in bodies) for w in segments if bodies]
    return dict(
        group=group, key=key, segments=len(segments), min_gap=min(gaps) if gaps else float("nan"),
        segments_with_net=sum(w.net is not None for w in segments),
        tips=len(tips), tips_with_net=sum(net is not None for _, _, net in tips),
        dots=dots, four=four, three=three, four_sites=four_sites, pairs=pairs,
        bodies=len(bodies), flagged=len({f[2].id for f in flags}), fallback=len(fallback),
        worst=sorted(flags, key=lambda f: (-f[0], f[1]))[:3],
    )


def fmt(p):
    return f"({p[0]:.2f}, {p[1]:.2f})"


def write(results):
    engine = [r for r in results if r["group"] == "engine"]
    human = [r for r in results if r["group"] == "human"]

    def total(rows, field):
        return sum(r[field] for r in rows)

    lines = [
        "# Junction, pin-pair and body-edge audit",
        "",
        "- Engine: `mcu_smoke.kicad_sch` from run `20261011T223823Z-timed-2`, one per demo (4 sheets).",
        "- Human: KiCad 10 demo sheets named in `sbx/demos.py` (4 sheets).",
        "- Nets: `kicad-cli sch export netlist` (kicadxml) per sheet, applied to pin tips and to wire segments via "
        "`measured_scene_from_kicad(..., netlist_xml=...)`.",
        "- Four-way: 4+ wire segment ends at one point (0.01 mm), not a pin tip. T: exactly 3 ends, same rule.",
        "- Pin pairs: same net, one axis 1.27 +-0.01 mm, the other > 2.54 mm.",
        "- Body edges: BODY bounding boxes. A segment parallel to an edge, within 0.6 mm, overlapping it by > 2.54 mm. "
        "Parts with a pin tip on the segment's net are excluded. Segments with no attributed net fall back to parts "
        "whose pin tips touch its ends (column `fallback`).",
        "",
        "## Measure 1: junctions",
        "",
        "| group | sheet | 4-way | T (3 ends) | junction dots | segments | segments with net |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in engine + human:
        lines.append(f"| {r['group']} | {r['key']} | {r['four']} | {r['three']} | {r['dots']} | {r['segments']} | "
                     f"{r['segments_with_net']} |")
    lines += ["", "## Measure 2: near-aligned pin pairs", "",
              "| group | sheet | near-aligned pairs | pin tips on a net |", "|---|---|---:|---:|"]
    for r in engine + human:
        lines.append(f"| {r['group']} | {r['key']} | {len(r['pairs'])} | {r['tips_with_net']} / {r['tips']} |")
    lines += ["", "Pairs on engine sheets (net: pin A at point, pin B at point, dx, dy):", ""]
    for r in engine:
        if not r["pairs"]:
            lines.append(f"- {r['key']}: none")
        for net, a, pa, b, pb, dx, dy in r["pairs"]:
            lines.append(f"- {r['key']}, {net}: {a} {fmt(pa)} vs {b} {fmt(pb)}, dx {dx:.2f} dy {dy:.2f}")
    lines += ["", "## Measure 3: wire segments along body edges", "",
              "| group | sheet | segments flagged | bodies | fallback segments | min wire-to-body gap (mm) |",
              "|---|---|---:|---:|---:|---:|"]
    for r in engine + human:
        lines.append(f"| {r['group']} | {r['key']} | {r['flagged']} | {r['bodies']} | {r['fallback']} | "
                     f"{r['min_gap']:.2f} |")
    lines += ["", "Worst three per sheet (longest run along an edge):", ""]
    for r in engine + human:
        if not r["worst"]:
            lines.append(f"- {r['group']} {r['key']}: none")
        for run, offset, w, body, edge in r["worst"]:
            (ax, ay), (bx, by) = w.segment
            lines.append(f"- {r['group']} {r['key']}: {body.owner} {edge} edge, run {run:.2f} mm, {offset:.2f} mm off, "
                         f"segment {w.id} {fmt((ax, ay))}-{fmt((bx, by))}, net {w.net}")
    lines += ["", "## Totals", "",
              "| measure | engine (4 sheets) | human (4 sheets) |", "|---|---:|---:|",
              f"| four-way junctions | {total(engine, 'four')} | {total(human, 'four')} |",
              f"| T junctions (context) | {total(engine, 'three')} | {total(human, 'three')} |",
              f"| near-aligned pin pairs | {sum(len(r['pairs']) for r in engine)} | "
              f"{sum(len(r['pairs']) for r in human)} |",
              f"| segments along body edges | {total(engine, 'flagged')} | {total(human, 'flagged')} |", ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")


def main():
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for group, key, sheet in sheets():
            netlist = demos.export_netlist(sheet, Path(tmp) / f"{group}-{key}.xml")
            results.append(measure(group, key, sheet, netlist.read_text(encoding="utf-8")))
    write(results)
    print(OUT)
    for r in results:
        print(f"{r['group']:6} {r['key']:12} 4way={r['four']} T={r['three']} pairs={len(r['pairs'])} "
              f"bodyflag={r['flagged']} fallback={r['fallback']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
