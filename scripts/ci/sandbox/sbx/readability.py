"""Readability of the engine's sheets, set against how engineers draw.

``measure_readability`` (backend/schematic/readability.py) counts what a reader notices in a sheet. This module applies
it to the shipped and the selected sheet of every fixture in a run, and to KiCad's own demo projects (human-drawn, shipped
with KiCad) to see what a sheet that reads well looks like by the same counts.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import env, kpi, report

# The measuring code is the repository's, not the run snapshot's: kpi.load_intent puts a snapshot ahead on sys.path to
# import a fixture, so the repository's modules are imported first.
if str(env.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(env.REPO_ROOT))
from backend.schematic.kicad_geometry import measured_scene_from_kicad  # noqa: E402
from backend.schematic.readability import measure_readability  # noqa: E402

CORPUS_CACHE = env.WORK_DIR / "out" / "corpus-readability.json"
KICAD_SHARE_DEMOS = ("share", "kicad", "demos")
FIXTURES = ("mcu", "noninverting_amplifier", "rc_filter", "linear_regulator")
# Sheets are judged against sheets of their own size: a four-part circuit is not a baseboard.
STRATA: tuple[tuple[str, int | None], ...] = (("all sheets", None), ("40 symbols or fewer", 40), ("20 symbols or fewer", 20))
# What a sheet is judged on, as rates that can be compared between a three-part circuit and a baseboard.
RATES: tuple[tuple[str, str], ...] = (
    ("field_on_wire_rate", "reference/value text with a wire through it, per text"),
    ("rotated_label_rate", "net labels drawn vertically, per label"),
    ("rotated_field_rate", "reference/value text drawn vertically, per field"),
    ("crossings_per_100", "wire crossings per 100 wire segments"),
    ("jogs_per_100", "tiny sideways steps per 100 wire segments"),
    ("corners_per_connection", "wire corners per pin-to-pin connection"),
    ("bent_wire_rate", "point-to-point wires that are not a single straight run, per wire"),
    ("support_far_rate", "support parts farther from the part they serve than their limit, per support part"),
    ("power_sideways_rate", "power symbols whose wire arrives from the side, per symbol"),
)


def _rates(report_obj: Any) -> dict[str, float | None]:
    m = report_obj.measures
    fields = m["fields"]
    rotated_labels = m["rotated_labels"]
    rotated_fields = m["rotated_text"] - rotated_labels

    def rate(count: float, total: float) -> float | None:
        return round(count / total, 3) if total else None

    return {
        "field_on_wire_rate": rate(m["field_on_wire"], max(1, fields)),
        "rotated_label_rate": rate(rotated_labels, m["labels"]),
        "rotated_field_rate": rate(rotated_fields, max(1, fields)),
        "crossings_per_100": round(100 * m["crossings"] / m["wire_segments"], 2) if m["wire_segments"] else None,
        "jogs_per_100": round(100 * m["jogs"] / m["wire_segments"], 2) if m["wire_segments"] else None,
        "corners_per_connection": (
            round(m["connection_corners"] / m["connections"], 3) if m.get("connections") else None
        ),
        "bent_wire_rate": (
            round(1 - m["straight_wires"] / m["two_pin_wires"], 3) if m.get("two_pin_wires") else None
        ),
        "support_far_rate": rate(m["support_far"], m["support_parts"]),
        "power_sideways_rate": rate(m["power_sideways"], m["power_symbols"]),
    }


def measure_sheet(sheet: Path, *, intent: Any = None) -> Any:
    scene, _ = measured_scene_from_kicad(sheet)
    if intent is None:
        return measure_readability(scene)
    nets = {net.name: [f"{e.reference}.{e.pin}" for e in net.endpoints] for net in intent.all_nets}
    return measure_readability(
        scene, nets=nets,
        rail_nets=[n.name for n in intent.all_nets if n.kind in ("power", "ground")],
        ground_nets=[n.name for n in intent.all_nets if n.kind == "ground"],
    )


def measure_run(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Per fixture: the shipped sheet (the run's verified trial) and the selected sheet, with their readability."""
    result: dict[str, dict[str, Any]] = {}
    present = sorted(
        path.name.removeprefix("schematic-engine-") for path in (run_dir / "artifacts").glob("schematic-engine-*")
    )
    for fixture in [*(f for f in FIXTURES if f in present), *(f for f in present if f not in FIXTURES)]:
        artifact = run_dir / "artifacts" / f"schematic-engine-{fixture}"
        shipped = artifact / "quality-trials" / "01-verified" / "candidate.kicad_sch"
        selected = artifact / "mcu_smoke.kicad_sch"
        if not selected.is_file():
            continue
        intent = kpi.load_intent(fixture, run_dir / "src")
        entry: dict[str, Any] = {"selected": measure_sheet(selected, intent=intent)}
        if shipped.is_file():
            entry["shipped"] = measure_sheet(shipped, intent=intent)
        result[fixture] = entry
    return result


def kicad_demos() -> Path | None:
    cli = env.find_kicad_cli()
    if cli is None:
        return None
    for parent in cli.parents:
        candidate = parent.joinpath(*KICAD_SHARE_DEMOS)
        if candidate.is_dir():
            return candidate
    return None


def corpus(max_kib: int = 700, *, refresh: bool = False, roots: list[Path] | None = None,
           cache: Path | None = None) -> dict[str, Any]:
    """The same counts on KiCad's demo sheets. Cached: the demo projects do not change.

    ``roots`` (default: the demos) lists the folders to read; with roots given the cache is a separate file, so the
    demo cache is never overwritten. Sheets of a non-first root are named ``<root folder name>/<relative path>``.
    """
    if cache is None:
        cache = CORPUS_CACHE if roots is None else CORPUS_CACHE.with_name("corpus-readability-ext.json")
    if cache.is_file() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    if roots is None:
        demos = kicad_demos()
        if demos is None:
            raise FileNotFoundError("KiCad's demo projects were not found next to kicad-cli")
        roots = [demos]
    root = roots[0]
    sheets: list[dict[str, Any]] = []
    skipped = 0
    failures: list[list[str]] = []
    started = time.time()
    paths = [(r, p) for r in roots for p in sorted(r.rglob("*.kicad_sch"))]
    for root_i, path in paths:
        if path.stat().st_size > max_kib * 1024:
            skipped += 1
            failures.append([str(path.relative_to(root_i)), "too large"])
            continue
        try:
            result = measure_sheet(path)
        except Exception as error:  # a sheet the geometry reader cannot take is left out, not guessed at
            skipped += 1
            failures.append([str(path.relative_to(root_i)), f"{type(error).__name__}: {error}"])
            continue
        if result.measures["wire_segments"] < 8:
            continue
        sheets.append({
            "sheet": str(path.relative_to(root_i)) if root_i == root else f"{root_i.name}/{path.relative_to(root_i)}",
            "symbols": result.measures["symbols"],
            "measures": result.measures,
            "rates": _rates(result),
            "support": [[part.kind, part.distance_mm] for part in result.support_parts],
            "label_stubs_mm": list(result.label_stubs_mm),
        })
    pooled_stubs = sorted(d for s in sheets for d in s["label_stubs_mm"])

    def band(values: list[float]) -> dict[str, float] | None:
        values = [v for v in values if v is not None]
        if not values:
            return None
        values.sort()

        def pct(p: float) -> float:
            return round(values[min(len(values) - 1, int(p * (len(values) - 1) + 0.5))], 3)

        return {"p10": pct(0.10), "median": pct(0.5), "p90": pct(0.90), "n": len(values)}

    data = {
        "root": str(root), "roots": [str(r) for r in roots], "failures": failures, "sheets_measured": len(sheets), "sheets_skipped": skipped, "seconds": round(time.time() - started, 1),
        "bands": {
            scope: {
                name: band([s["rates"][name] for s in sheets if limit is None or s["symbols"] <= limit])
                for name, _ in RATES
            }
            for scope, limit in STRATA
        },
        "support_distance_mm": {
            kind: {
                scope: band(sorted(d for s in sheets if limit is None or s["symbols"] <= limit
                                   for k, d in s["support"] if k == kind))
                for scope, limit in (("all sheets", None), ("sheets of 40 symbols or fewer", 40), ("sheets of 20 or fewer", 20))
            }
            for kind in ("decoupling", "shunt", "series")
        },
        "label_stub_mm": band(pooled_stubs),
        "sheets": sheets,
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def format_run_table(label: str, measured: dict[str, dict[str, Any]]) -> str:
    cols = ("field_on_wire", "rotated_labels", "crossings", "wires_through_body", "jogs", "detours", "support_far",
            "support_max_mm", "support_excess_mm")
    heads = ("fld/wire", "vert lbl", "crossing", "thru body", "jogs", "detours", "sup far", "sup max", "sup excess")
    lines = [f"{label}", f"{'fixture':<24} {'sheet':<8} " + " ".join(f"{h:>8}" for h in heads)]
    for fixture, entry in measured.items():
        for name in ("shipped", "selected"):
            if name in entry:
                m = entry[name].measures
                lines.append(f"{fixture:<24} {name:<8} " + " ".join(f"{m[c]:>8.4g}" for c in cols))
    return "\n".join(lines)


def format_corpus(data: dict[str, Any]) -> str:
    lines = [
        f"KiCad demo sheets: {data['sheets_measured']} measured ({data['sheets_skipped']} too large or unreadable), {data['seconds']} s",
        f"{'rate over sheets':<26} {'p10':>8} {'median':>8} {'p90':>8}   meaning",
    ]
    for scope, bands in data["bands"].items():
        lines.append(f"-- {scope}")
        for name, meaning in RATES:
            b = bands[name]
            if b:
                lines.append(f"{name:<26} {b['p10']:>8} {b['median']:>8} {b['p90']:>8}   {meaning} (n={b['n']})")
    for kind, scopes in data["support_distance_mm"].items():
        for scope, b in scopes.items():
            if b:
                lines.append(f"{kind + ' distance (mm)':<26} {b['p10']:>8} {b['median']:>8} {b['p90']:>8}   {scope}, {b['n']} parts")
    b = data["label_stub_mm"]
    if b:
        lines.append(f"{'label stub length (mm)':<26} {b['p10']:>8} {b['median']:>8} {b['p90']:>8}   pooled over {b['n']} labels")
    return "\n".join(lines)
