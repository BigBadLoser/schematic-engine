"""Audit text overlaps on engine-generated and human KiCad schematics.

Read-only: measures each sheet with the engine's own geometry, lists every overlap the engine counts
(text_overlap_pairs), every text-on-wire issue (measure_readability), and same-owner text-on-text overlaps
that the engine does not count (reported separately, flagged engine_counted=no). Writes out/overlaps.csv and out/summary.md.

Run from the repo root: PYTHONPATH=. scripts/ci/sandbox/.work/venv/Scripts/python.exe scripts/ci/sandbox/probes/text_overlap/audit.py
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from backend.schematic.composition_metrics import text_overlap_pairs  # noqa: E402
from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.presentation_geometry import GeometryKind, TEXT_KINDS  # noqa: E402
from backend.schematic.readability import measure_readability  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
RUN = ROOT / "scripts/ci/sandbox/.work/runs/20261011T031335Z-auto-st3/artifacts"
DEMOS = Path(r"C:\Program Files\KiCad\10.0\share\kicad\demos")
HUMAN = {
    "demo_lm317": "simulation/power_supplies/LM317_power_supply/power_supply.kicad_sch",
    "demo_buck": "simulation/power_supplies/buck_conv/buck_conv.kicad_sch",
    "demo_ampac": "simulation/amplifier-ac/amplifier-ac.kicad_sch",
    "demo_opamp": "simulation/generic_models/generic_opamp_bip.kicad_sch",
}
CAUSES = ("own_symbol_texts", "neighbour_text", "text_on_body", "text_on_power_symbol_or_flag", "text_on_wire", "other")
WIRE_HALO_MM = 0.15  # a wire has no area; its stroke band is taken as +/-0.15 mm around its bounds
TEXT_SHRINK_MM = 0.15  # same shrink readability.py uses before testing text against wires


def _area(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1) -> float:
    w = min(ax1, bx1) - max(ax0, bx0)
    h = min(ay1, by1) - max(ay0, by0)
    return round(w * h, 4) if w > 0 and h > 0 else 0.0


def _box(item) -> tuple[float, float, float, float]:
    b = item.bounds
    return b.left, b.top, b.right, b.bottom


def _grow(box, d):
    return box[0] - d, box[1] - d, box[2] + d, box[3] + d


def _cause_for(a, b) -> str:
    """Cause of an overlap between a text (a) and a non-wire item (b), or text vs text."""
    if a.kind in TEXT_KINDS and b.kind in TEXT_KINDS:
        return "own_symbol_texts" if a.owner == b.owner else "neighbour_text"
    other = b if a.kind in TEXT_KINDS else a
    if other.kind == GeometryKind.BODY:
        return "text_on_body"
    if other.kind == GeometryKind.POWER_GRAPHIC:
        return "text_on_power_symbol_or_flag"
    return "other"


def _owner_label(item) -> str:
    return item.owner


def audit_sheet(sheet: str, path: Path, group: str, rows: list[dict]) -> Counter:
    scene, _coverage = measured_scene_from_kicad(path)
    by_id = {item.id: item for item in scene.items}
    counts: Counter = Counter()

    # 1. Overlaps the engine counts.
    for first_id, second_id in text_overlap_pairs(scene):
        a, b = by_id[first_id], by_id[second_id]
        text, other = (a, b) if a.kind in TEXT_KINDS else (b, a)
        cause = _cause_for(text, other)
        area = _area(*_box(a), *_box(b))
        counts[cause] += 1
        rows.append(dict(
            group=group, sheet=sheet, engine_counted="yes", cause=cause, area_mm2=area,
            side_a_kind=a.kind.value, side_a_owner=a.owner, side_a_text=a.text or "",
            side_b_kind=b.kind.value, side_b_owner=b.owner, side_b_text=b.text or "",
            overlap_with=other.kind.value, wire_crossing_mm="",
        ))

    # 2. Same-owner text on text (not counted by the engine; listed for completeness).
    texts = [item for item in scene.items if item.kind in TEXT_KINDS]
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            if a.owner == b.owner and _area(*_box(a), *_box(b)) > 0.0:
                area = _area(*_box(a), *_box(b))
                counts["own_symbol_texts"] += 1
                rows.append(dict(
                    group=group, sheet=sheet, engine_counted="no", cause="own_symbol_texts", area_mm2=area,
                    side_a_kind=a.kind.value, side_a_owner=a.owner, side_a_text=a.text or "",
                    side_b_kind=b.kind.value, side_b_owner=b.owner, side_b_text=b.text or "",
                    overlap_with="text", wire_crossing_mm="",
                ))

    # 3. Text-on-wire issues from the readability report.
    report = measure_readability(scene)
    for issue in report.issues:
        if issue.kind != "text_on_wire":
            continue
        text_id, wire_id = issue.ids
        t, w = by_id[text_id], by_id[wire_id]
        tb = _grow(_box(t), -TEXT_SHRINK_MM)
        wb = _grow(_box(w), WIRE_HALO_MM)
        area = _area(*tb, *wb)
        # Length of the wire segment inside the text box, for axis-aligned segments.
        crossing = 0.0
        if w.segment is not None:
            (x0, y0), (x1, y1) = w.segment
            if abs(x0 - x1) < 1e-6 or abs(y0 - y1) < 1e-6:
                lo_x, hi_x = max(min(x0, x1), tb[0]), min(max(x0, x1), tb[2])
                lo_y, hi_y = max(min(y0, y1), tb[1]), min(max(y0, y1), tb[3])
                if hi_x >= lo_x and hi_y >= lo_y:
                    crossing = round((hi_x - lo_x) + (hi_y - lo_y), 3)
        counts["text_on_wire"] += 1
        rows.append(dict(
            group=group, sheet=sheet, engine_counted="yes", cause="text_on_wire", area_mm2=area,
            side_a_kind=t.kind.value, side_a_owner=t.owner, side_a_text=t.text or "",
            side_b_kind=w.kind.value, side_b_owner=w.owner, side_b_text=w.text or "",
            overlap_with="wire", wire_crossing_mm=crossing,
        ))
    return counts


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    per_sheet: dict[tuple[str, str], Counter] = {}
    skipped: list[str] = []

    for path in sorted(RUN.glob("schematic-engine-*/mcu_smoke.kicad_sch")):
        sheet = path.parent.name.removeprefix("schematic-engine-")
        per_sheet[("engine", sheet)] = audit_sheet(sheet, path, "engine", rows)
    for d in sorted(RUN.glob("schematic-engine-*")):
        if not (d / "mcu_smoke.kicad_sch").exists():
            skipped.append(d.name)
    for key, rel in HUMAN.items():
        path = DEMOS / rel
        if not path.exists():
            skipped.append(f"human:{key} ({path})")
            continue
        per_sheet[("human", key)] = audit_sheet(key, path, "human", rows)

    fields = list(rows[0].keys()) if rows else ["group"]
    with (OUT / "overlaps.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    lines = ["# Text overlap audit", "", f"Run: `20261011T031335Z-auto-st3` (final mcu_smoke.kicad_sch per sheet).", ""]
    if skipped:
        lines += ["Skipped (no mcu_smoke.kicad_sch or missing file): " + "; ".join(skipped), ""]
    lines += ["Counts include engine-counted overlaps (text_overlap_pairs) and text_on_wire issues. "
              "own_symbol_texts also includes same-owner text-on-text overlaps the engine does not count "
              "(see engine_counted in overlaps.csv).", ""]
    for group, title in (("engine", "Engine sheets"), ("human", "Human (KiCad demo) sheets")):
        lines += [f"## {title}", "", "| sheet | " + " | ".join(CAUSES) + " | total |", "|" + "---|" * (len(CAUSES) + 2)]
        totals: Counter = Counter()
        for (g, sheet), counts in per_sheet.items():
            if g != group:
                continue
            totals.update(counts)
            lines.append(f"| {sheet} | " + " | ".join(str(counts[c]) for c in CAUSES) + f" | {sum(counts.values())} |")
        lines.append("| **total** | " + " | ".join(f"**{totals[c]}**" for c in CAUSES) + f" | **{sum(totals.values())}** |")
        lines.append("")

    lines += ["## 10 largest overlaps (by area_mm2, all sheets)", "",
              "| # | sheet | group | cause | area_mm2 | side A (kind, owner, text) | side B (kind, owner, text) |",
              "|---|---|---|---|---|---|---|"]
    top = sorted(rows, key=lambda r: r["area_mm2"], reverse=True)[:10]
    for n, r in enumerate(top, 1):
        a = f"{r['side_a_kind']}, {r['side_a_owner']}, {r['side_a_text']}"
        b = f"{r['side_b_kind']}, {r['side_b_owner']}, {r['side_b_text']}"
        lines.append(f"| {n} | {r['sheet']} | {r['group']} | {r['cause']} | {r['area_mm2']} | {a} | {b} |")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"rows={len(rows)} sheets={len(per_sheet)} skipped={skipped}")
    for key, counts in per_sheet.items():
        print(key, dict(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
