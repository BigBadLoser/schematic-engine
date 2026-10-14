"""One-command comparison bench: demo sheets stacked at one scale (engine RUN, engine RUN2, human) and tables of counts."""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import demos, redraw, report

CAPTION_PX = 34
SCALE = 8.0
DEMO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("wire_mm", "wire mm"), ("area_mm2", "area mm2"), ("corners_per_connection", "corners/conn"),
    ("crossings", "crossings"), ("jogs", "jogs"), ("text_overlaps", "text overlaps"),
    ("support_far", "far support"), ("power_sideways_rate", "power from side"),
    ("nn_gap_med", "nn gap med"), ("nn_gap_min", "nn gap min"), ("crowded_text", "crowded txt"),
)
FIXTURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("wire_length_mm", "wire mm"), ("corners", "corners"), ("jogs", "jogs"), ("crossings", "crossings"), ("extent", "extent mm"),
)


def label_of(run_dir: Path) -> str:
    """The label of a run directory (`<timestamp>-<label>` -> label)."""
    return run_dir.name.split("-", 1)[1] if "-" in run_dir.name else run_dir.name


def resolve_runs(run: str, against: str | None) -> list[Path]:
    runs = [report.resolve_run(run)]
    if against:
        runs.append(report.resolve_run(against))
    return runs


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return format(value, ".4g") if isinstance(value, (int, float)) else str(value)


def markdown_table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def demo_table(key: str, rows: list[tuple[str, dict[str, Any]]]) -> str:
    """rows: (row name, measured dict from redraw.measure plus 'jogs'); human first."""
    ordered = sorted(rows, key=lambda r: r[0] != "human")
    body = [[name] + [m.get(col) for col, _t in DEMO_COLUMNS] for name, m in ordered]
    return f"## {key}\n\n" + markdown_table(["sheet"] + [t for _c, t in DEMO_COLUMNS], body)


def fixtures_table(per_run: list[tuple[str, dict[str, dict[str, Any]]]]) -> str:
    """per_run: (run label, kpi.json dict); one row per (non-demo fixture, run)."""
    names = sorted({f for _l, kpis in per_run for f in kpis if f not in demos.DEMOS and not redraw.is_corpus_key(f)})
    rows = []
    for fixture in names:
        for label, kpis in per_run:
            v = kpis.get(fixture)
            if v is None:
                continue
            extent = f"{v['extent_width_mm']:.0f}x{v['extent_height_mm']:.0f}"
            rows.append([fixture, label] + [v.get(c) if c != "extent" else extent for c, _t in FIXTURE_COLUMNS])
    return "## fixtures\n\n" + markdown_table(["fixture", "run"] + [t for _c, t in FIXTURE_COLUMNS], rows)


def stage_family(path: str) -> str:
    """placement, routing, checks (checks/exports) or other, from the stage path."""
    parts = path.split("/")
    for family, names in (("placement", {"placement"}), ("routing", {"routing", "junctions", "power_flags", "labels"}),
                          ("checks", {"checks", "quality", "export"})):
        if any(part in names for part in parts):
            return family
    return "other"


def time_section(timings: dict[str, dict[str, Any]]) -> str:
    """timings: fixture -> timing.json. Wall and trial seconds, the top stages and MCP tools, and shares by family."""
    out = ["## Time"]
    rows, stages, tools = [], {}, {}
    family: dict[str, float] = {"placement": 0.0, "MCP calls": 0.0, "routing": 0.0, "checks/exports": 0.0, "other": 0.0}
    wall_total = 0.0
    for fixture, data in sorted(timings.items()):
        st = data.get("stages", {})
        trials = {p.split("/")[0]: v["seconds"] for p, v in st.items() if "/" not in p and p.startswith("trial:")}
        rows.append([fixture, data.get("wall_seconds"), "; ".join(f"{k[6:]} {v:.1f}s" for k, v in sorted(trials.items())) or "-"])
        wall_total += data.get("wall_seconds", 0.0)
        for path, v in st.items():
            stages[(fixture, path)] = v["seconds"]
            children = sum(c["seconds"] for q, c in st.items() if q.startswith(path + "/") and "/" not in q[len(path) + 1:])
            own = max(v["seconds"] - children - v.get("mcp_seconds", 0.0), 0.0)
            family["MCP calls"] += v.get("mcp_seconds", 0.0)
            fam = stage_family(path)
            family["checks/exports" if fam == "checks" else fam] += own
        top = sum(v["seconds"] for p, v in st.items() if "/" not in p)
        family["other"] += max(data.get("wall_seconds", 0.0) - top, 0.0)
        for tool, v in data.get("mcp_tools", {}).items():
            row = tools.setdefault(tool, [0.0, 0])
            row[0] += v["seconds"]
            row[1] += v["calls"]
    out.append(markdown_table(["fixture", "wall s", "seconds per trial"], rows))
    wall_total = wall_total or 1.0
    out.append("Top stages by total seconds:\n\n" + markdown_table(
        ["fixture", "stage", "seconds", "share of wall"],
        [[f, p, sec, f"{100 * sec / (timings[f].get('wall_seconds') or 1.0):.1f}%"]
         for (f, p), sec in sorted(stages.items(), key=lambda kv: -kv[1])[:10]]))
    out.append("Top MCP tools by seconds:\n\n" + markdown_table(
        ["tool", "seconds", "calls"], [[t, v[0], v[1]] for t, v in sorted(tools.items(), key=lambda kv: -kv[1][0])[:10]]))
    out.append("Share of wall time by stage family (all fixtures):\n\n" + markdown_table(
        ["family", "seconds", "share"], [[k, v, f"{100 * v / wall_total:.1f}%"] for k, v in family.items()]))
    return "\n\n".join(out)


def load_timings(run_dir: Path) -> dict[str, dict[str, Any]]:
    found = {}
    for f in sorted((run_dir / "artifacts").glob("*/timing.json")):
        try:
            found[f.parent.name.removeprefix("schematic-engine-")] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return found


def demo_keys(run_dir: Path) -> list[str]:
    """Demos and corpus circuits that have an artifact folder in the run."""
    from . import corpus
    keys = [*demos.DEMOS, *(corpus.fixture_key(k) for k in corpus.CIRCUITS)]
    return [k for k in keys if (run_dir / "artifacts" / f"schematic-engine-{k}").is_dir()]


def row_name(key: str, run_dir: Path) -> str:
    """A corpus circuit's sheet is the ladder's; a demo's is the engine's."""
    return f"{'ladder' if redraw.is_corpus_key(key) else 'engine'} {label_of(run_dir)}"


def _chrome() -> str:
    for candidate in (os.environ.get("CHROME"), shutil.which("chrome"), shutil.which("google-chrome"),
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Chrome was not found (set CHROME)")


def stack_png(panels: list[tuple[str, Path]], out: Path) -> None:
    """Sheets one above the other at one scale, each under its caption, as a PNG (kicad-cli SVG + headless Chrome)."""
    drawn = [(caption, *redraw.draw(sheet, scale=SCALE)) for caption, sheet in panels]
    width = int(max(size[0] * SCALE for _c, _s, size in drawn)) + 20
    height = int(sum(size[1] * SCALE + CAPTION_PX + 14 for _c, _s, size in drawn)) + 10
    body = "".join(
        f"<div style='font:600 20px system-ui;color:#123;padding:8px 10px 2px'>{html.escape(c)}</div>{svg}"
        "<hr style='border:0;border-top:1px solid #ccd;margin:6px 0'>" for c, svg, _size in drawn
    )
    page = out.with_suffix(".html")
    page.write_text("<!doctype html><meta charset=utf-8><body style='margin:0;background:#fff'>" + body, encoding="utf-8")
    subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={width},{height}",
                    f"--screenshot={out}", page.as_uri()], check=True, capture_output=True, timeout=180)
    page.unlink()


def run_bench(run: str, against: str | None, out_dir: Path | None) -> Path:
    from . import kpi

    runs = resolve_runs(run, against)
    out = out_dir or runs[0] / "bench"
    out.mkdir(parents=True, exist_ok=True)
    sections = [f"# bench: {' vs '.join(label_of(r) for r in runs)}"]
    notes: list[str] = []
    for key in demo_keys(runs[0]):
        rows: list[tuple[str, dict[str, Any]]] = []
        panels: list[tuple[str, Path]] = []
        human: tuple[dict[str, Any], Path] | None = None
        for run_dir in runs:
            if key not in demo_keys(run_dir):
                continue
            sheets = redraw.sheets_of(run_dir, key)
            try:
                measured = redraw.measure(run_dir, key)
                for name, values in measured.items():
                    values["jogs"] = kpi.kpis_for_schematic(sheets[name], key, run_dir / "src").get("jogs")
            except Exception as error:  # a run's own src snapshot may not load (older engine); draw it anyway
                notes.append(f"- {key}: {label_of(run_dir)} not measured ({type(error).__name__}: {str(error)[:100]})")
                measured = {}
                if "engine" in sheets:
                    panels.append((row_name(key, run_dir), sheets["engine"]))
                if human is None and "human" in sheets:
                    human = ({}, sheets["human"])
                continue
            if "engine" in measured:
                rows.append((row_name(key, run_dir), measured["engine"]))
                panels.append((row_name(key, run_dir), sheets["engine"]))
            if human is None and "human" in measured:
                human = (measured["human"], sheets["human"])
        if human is not None:
            if human[0]:
                rows.append(("human", human[0]))
            panels.append(("human", human[1]))
        sections.append(demo_table(key, rows))
        stack_png(panels, out / f"{key}.png")
    per_run = []
    for run_dir in runs:
        f = run_dir / "kpi.json"
        per_run.append((label_of(run_dir), json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}))
    sections.append(fixtures_table(per_run))
    if notes:
        sections.append("## notes\n\n" + "\n".join(notes))
    (out / "bench.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return out
