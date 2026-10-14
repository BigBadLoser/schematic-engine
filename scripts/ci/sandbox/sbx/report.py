"""Compare sandbox runs and resolve run references."""
from __future__ import annotations

import json
from pathlib import Path

from . import env

# (path into the KPI dict, column title, lower is better)
METRICS = (
    ("wire_length_mm", "wire mm", True),
    ("excess_ratio", "excess", True),
    ("stubs.length_mm", "stub mm", True),
    ("stubs.per_pin_mm", "stub mm/pin", True),
    ("corners", "corners", True),
    ("jogs", "jogs", True),
    ("crossings", "crossings", True),
    ("body_hits_foreign", "foreign hits", True),
    ("body_overlaps", "overlaps", True),
    ("router_clearance.trapped", "trapped portals", True),
    ("passives.nn_center_median_mm", "passive pitch mm", True),
    ("extent_width_mm", "width mm", True),
    ("extent_height_mm", "height mm", True),
)


def resolve_run(reference: str) -> Path:
    """A run directory by path, or the newest run whose name ends with `-reference`."""
    direct = Path(reference)
    if direct.is_dir():
        return direct
    matches = sorted(env.RUNS_DIR.glob(f"*-{reference}")) if env.RUNS_DIR.is_dir() else []
    if not matches:
        raise FileNotFoundError(f"no run named {reference!r} under {env.RUNS_DIR}")
    return matches[-1]


def _get(data: dict, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _cell(before, after) -> str:
    if before is None or after is None:
        return f"{before} -> {after}"
    delta = after - before
    if abs(delta) < 1e-9:
        return f"{after}"
    percent = f" ({delta / before * 100:+.0f}%)" if before else ""
    return f"{before} -> {after}{percent}"


def compare(first: Path, second: Path) -> str:
    kpis = [json.loads((run / "kpi.json").read_text(encoding="utf-8")) for run in (first, second)]
    manifests = [json.loads((run / "manifest.json").read_text(encoding="utf-8")) for run in (first, second)]
    lines = [
        f"A = {first.name}  overlays={[o['name'] for o in manifests[0]['overlays']] or 'none'}  profile={manifests[0].get('profile')}",
        f"B = {second.name}  overlays={[o['name'] for o in manifests[1]['overlays']] or 'none'}  profile={manifests[1].get('profile')}",
        "",
    ]
    for fixture in sorted(set(kpis[0]) | set(kpis[1])):
        a, b = kpis[0].get(fixture), kpis[1].get(fixture)
        lines.append(f"### {fixture}")
        if a is None or b is None:
            lines.append(f"  only present in {'A' if b is None else 'B'}\n")
            continue
        lines.append("| metric | A -> B |")
        lines.append("|---|---|")
        for path, title, _lower_is_better in METRICS:
            lines.append(f"| {title} | {_cell(_get(a, path), _get(b, path))} |")
        shorts = [x.get("shorted_nets") for x in (a, b)]
        if any(shorts):
            lines.append(f"| shorted nets | {shorts[0]} -> {shorts[1]} |")
        lines.append("")
    return "\n".join(lines)


def _fixture_outcomes(run: Path) -> dict[str, dict]:
    """fixture -> {returncode, profile, failure} from the manifest and the engine's own records."""
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    outcomes: dict[str, dict] = {}
    for result in manifest.get("results", []):
        fixture = result["fixture"]
        artifact = run / "artifacts" / f"schematic-engine-{fixture}"
        profile, failure = None, None
        selection = artifact / "quality-selection.json"
        if selection.is_file():
            profile = json.loads(selection.read_text(encoding="utf-8")).get("selected_profile")
        failure_file = artifact / "failure.json"
        if failure_file.is_file():
            failure = str(json.loads(failure_file.read_text(encoding="utf-8")).get("error", ""))[:80]
        outcomes[fixture] = {"returncode": result.get("returncode"), "profile": profile or manifest.get("profile"),
                             "failure": failure}
    return outcomes


def table(runs: list[Path], fixtures: list[str] | None = None) -> str:
    """One row per (fixture, run): outcome and the headline KPIs, so many runs read at a glance."""
    per_run = []
    for run in runs:
        kpis_file = run / "kpi.json"
        kpis = json.loads(kpis_file.read_text(encoding="utf-8")) if kpis_file.is_file() else {}
        per_run.append((run, kpis, _fixture_outcomes(run)))
    names = sorted({fixture for _, kpis, outcomes in per_run for fixture in (*kpis, *outcomes)})
    if fixtures:
        names = [name for name in names if name in fixtures]
    header = (f"{'fixture':<24} {'run':<20} {'result':<9} {'wire':>7} {'excess':>6} {'stub/pin':>8} {'corners':>7} "
              f"{'jogs':>4} {'cross':>5} {'trapped':>7} {'pitch':>6} {'extent':>12}")
    lines = [header, "-" * len(header)]
    for fixture in names:
        for run, kpis, outcomes in per_run:
            label = run.name.split("-", 1)[1]
            outcome = outcomes.get(fixture, {})
            values = kpis.get(fixture)
            ok = outcome.get("returncode") == 0
            result = f"ok/{outcome.get('profile') or '?'}" if ok else "FAIL"
            if values is None:
                lines.append(f"{fixture:<24} {label:<20} {result:<9} {outcome.get('failure') or '-'}")
                continue
            clearance = values.get("router_clearance") or {}
            lines.append(
                f"{fixture:<24} {label:<20} {result:<9} {values['wire_length_mm']:>7.1f} "
                f"{str(values['excess_ratio']):>6} {str(values['stubs']['per_pin_mm']):>8} {values['corners']:>7} "
                f"{values.get('jogs', '-'):>4} {values['crossings']:>5} {str(clearance.get('trapped', '-')):>7} "
                f"{str(values['passives']['nn_center_median_mm']):>6} "
                f"{values['extent_width_mm']:>5.0f}x{values['extent_height_mm']:<5.0f}")
    return "\n".join(lines)
