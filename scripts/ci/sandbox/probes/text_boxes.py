"""How good are the estimated text boxes? Compare them with KiCad's own rendering of a run's schematic.

KiCad's SVG export wraps every text in <g class="stroked-text"><desc>TEXT</desc><path .../></g>, so the bounding box
of those paths is the real extent of the text. Each rendered text is paired with the nearest estimated box and the
two are compared: width ratio (estimated over real) and the distance between the box centres.

Needs a finished sandbox run (its artifacts hold the schematic and its SVG). Uses the newest one, or the run named
in the environment variable SBX_TEXT_PROBE_RUN, and its MCU fixture unless SBX_TEXT_PROBE_FIXTURE names another. A
global label's estimated box includes its flag and the SVG text does not, so only fixtures with local labels (the
MCU) compare cleanly on net labels.
"""
from __future__ import annotations

import collections
import os
import re
from pathlib import Path

from _common import REPO, SANDBOX, emit, fail

GROUP = re.compile(r'<g class="stroked-text"><desc>(.*?)</desc>(.*?)</g>', re.S)
NUMBER = re.compile(r"[-+]?\d+\.?\d*")
PAIRING_RADIUS_MM = 6.0
# What the calibrated reader achieves; a drift beyond this is a regression in the estimator.
WIDTH_RATIO_BOUNDS = (0.80, 1.20)
CENTRE_OFFSET_LIMIT_MM = 0.6


def rendered_boxes(svg: str) -> list[tuple[str, float, float, float, float]]:
    boxes = []
    for text, body in GROUP.findall(svg):
        xs: list[float] = []
        ys: list[float] = []
        for path in re.findall(r'<path d="(.*?)"', body, re.S):
            numbers = [float(n) for n in NUMBER.findall(path)]
            xs += numbers[0::2]
            ys += numbers[1::2]
        if xs and ys:
            boxes.append((text.strip(), min(xs), min(ys), max(xs), max(ys)))
    return boxes


def newest_artifact() -> Path | None:
    wanted = os.environ.get("SBX_TEXT_PROBE_RUN")
    fixture = os.environ.get("SBX_TEXT_PROBE_FIXTURE", "mcu")
    runs = sorted((SANDBOX / ".work" / "runs").glob(f"*{wanted}*" if wanted else "*"))
    for run in reversed(runs):
        artifact = run / "artifacts" / f"schematic-engine-{fixture}"
        if (artifact / "mcu_smoke.kicad_sch").is_file() and (artifact / "mcu_smoke.svg").is_file():
            return artifact
    return None


def main() -> int:
    artifact = newest_artifact()
    if artifact is None:
        return fail("text_boxes", "no sandbox run with a schematic and its SVG; run `sandbox.py run` first")
    try:
        from backend.schematic.kicad_geometry import measured_scene_from_kicad
        from backend.schematic.presentation_geometry import TEXT_KINDS
    except ImportError as error:
        return fail("text_boxes", f"repository modules unavailable: {error}")

    scene, _coverage = measured_scene_from_kicad(artifact / "mcu_smoke.kicad_sch")
    estimated = [item for item in scene.items if item.kind in TEXT_KINDS]
    real = rendered_boxes((artifact / "mcu_smoke.svg").read_text(encoding="utf-8"))
    rows: dict[str, list[tuple[float, float, float]]] = collections.defaultdict(list)
    unmatched = 0
    for _text, left, top, right, bottom in real:
        cx, cy = (left + right) / 2, (top + bottom) / 2
        best = min(
            estimated,
            key=lambda item: ((item.bounds.left + item.bounds.right) / 2 - cx) ** 2
            + ((item.bounds.top + item.bounds.bottom) / 2 - cy) ** 2,
            default=None,
        )
        if best is None:
            unmatched += 1
            continue
        dx = (best.bounds.left + best.bounds.right) / 2 - cx
        dy = (best.bounds.top + best.bounds.bottom) / 2 - cy
        if (dx * dx + dy * dy) ** 0.5 > PAIRING_RADIUS_MM:
            unmatched += 1     # drawing-sheet text and the like: nothing in the scene to compare with
            continue
        ratio = (best.bounds.right - best.bounds.left) / max(right - left, 1e-6)
        rows[best.kind.value].append((ratio, abs(dx), abs(dy)))

    lines = [f"{artifact.parent.parent.name} / {artifact.name}: {len(real)} rendered texts, "
             f"{len(estimated)} estimated boxes, {unmatched} rendered texts with no estimate near them"]
    data: dict[str, object] = {}
    ok = True
    for kind, values in sorted(rows.items()):
        values.sort()
        median_ratio = values[len(values) // 2][0]
        mean_dx = sum(v[1] for v in values) / len(values)
        mean_dy = sum(v[2] for v in values) / len(values)
        good = (WIDTH_RATIO_BOUNDS[0] <= median_ratio <= WIDTH_RATIO_BOUNDS[1]
                and mean_dx <= CENTRE_OFFSET_LIMIT_MM and mean_dy <= CENTRE_OFFSET_LIMIT_MM)
        ok = ok and good
        lines.append(f"{kind:12} n={len(values):3d}  width ratio median {median_ratio:4.2f}  "
                     f"centre offset |dx| {mean_dx:4.2f} |dy| {mean_dy:4.2f} mm{'' if good else '   <-- off'}")
        data[kind] = {"n": len(values), "width_ratio_median": round(median_ratio, 3),
                      "mean_abs_dx_mm": round(mean_dx, 3), "mean_abs_dy_mm": round(mean_dy, 3)}
    return emit("text_boxes", lines, {"artifact": str(artifact), "kinds": data, "repo": str(REPO)}, ok=ok)


if __name__ == "__main__":
    raise SystemExit(main())
