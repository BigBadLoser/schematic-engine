"""Build the four demos and the ten corpus circuits from scratch with the solver, and compare each sheet with what it was.

    python scripts/ci/check_all.py                 # every sheet, five seeds, the solver cache, three builds at once when cached
    python scripts/ci/check_all.py --seeds 1       # one seed: a pass/fail check that every sheet builds (no comparison of counts)
    python scripts/ci/check_all.py --only demo_buck,corpus_pico_hbridge
    python scripts/ci/check_all.py --update        # write tests/fixtures/check_all/expected.json from this run

Each sheet is built through the sandbox (``sandbox.py run --placement ladder``) with ``KICAD_AI_LADDER_SOLVER=cpsat``. The
solver's answers are cached (``scripts/ci/sandbox/sbx/placement_cache.py``), so a sheet whose plan and solver code have not
changed is not solved again. A first pass builds, ``--jobs`` at a time, the sheets whose answer is cached (a miss stops at
once, ``KICAD_AI_LADDER_CACHE_ONLY=1``); a second pass builds the others one at a time, because a solve of five seeds uses
the machine. The exit status is non-zero when a sheet that built stopped building, its netlist is not PASS, or it has more
wires through a body, shorted nets or crossings than the expected file records; a sheet recorded as a known failure that
builds now is reported as fixed (run ``--update``). Run with the sandbox's Python (``scripts/ci/sandbox/.work/venv``);
in a worktree set ``SBX_WORK_DIR`` to the main clone's ``scripts/ci/sandbox/.work``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SANDBOX = REPO / "scripts" / "ci" / "sandbox"
EXPECTED = REPO / "tests" / "fixtures" / "check_all" / "expected.json"
sys.path.insert(0, str(SANDBOX))

from sbx import corpus, env, placement_cache  # noqa: E402

DEMO_PICKS = {"demo_buck": 7, "demo_lm317": 5, "demo_opamp": 11, "demo_ampac": 5}
DEMO_SOURCE = "rec-1"
FIVE_SEEDS = "7,11,13,17,19"
SHEETS = [*DEMO_PICKS, *(f"corpus_{name}" for name in sorted(corpus.CIRCUITS))]


def sandbox_command(sheet: str, label: str) -> list[str]:
    command = [sys.executable, str(SANDBOX / "sandbox.py"), "run", "--label", label, "--fixtures", sheet, "--placement", "ladder"]
    if sheet in DEMO_PICKS:
        command += ["--placement-from", DEMO_SOURCE, "--placement-pick", f"{sheet}={DEMO_PICKS[sheet]}"]
    return command


def build(sheet: str, seeds: str, *, probe: bool) -> dict:
    """One sheet through the sandbox; what it reported (``probe``: a cache miss ends it at once)."""
    label = f"check-{sheet}"
    child = {**os.environ, "KICAD_AI_LADDER_SOLVER": "cpsat", "KICAD_AI_LADDER_SEEDS": seeds, "PYTHONUTF8": "1",
             "KICAD_AI_ENABLE_SCHEMATIC_WRITES": "1", "SBX_WORK_DIR": str(env.WORK_DIR)}
    child.pop(placement_cache.CACHE_ONLY_ENV, None)
    if probe:
        child[placement_cache.CACHE_ONLY_ENV] = "1"
    before = {path.name for path in env.RUNS_DIR.glob(f"*-{label}")} if env.RUNS_DIR.is_dir() else set()
    started = time.time()
    done = subprocess.run(sandbox_command(sheet, label), env=child, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=REPO)
    wall = time.time() - started
    fresh = sorted(path for path in env.RUNS_DIR.glob(f"*-{label}") if path.name not in before)
    result = {"sheet": sheet, "wall": wall, "run": None, "built": False, "failure": None, "missed": False}
    if not fresh:
        result["failure"] = (done.stderr.strip().splitlines() or done.stdout.strip().splitlines() or ["no run directory"])[-1]
        return result
    run_dir = fresh[-1]
    result["run"] = run_dir
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["failure"] = "no manifest"
        return result
    item = next((r for r in manifest["results"] if r["fixture"] == sheet), None) or {}
    source = next(iter((manifest.get("placement_sources") or {}).values()), {})
    note = source.get("placement_note") or ""
    result.update(built=item.get("returncode") == 0, netlist=item.get("netlist"), build_seconds=item.get("seconds"),
                  solve_seconds=source.get("placement_seconds"), cached="[cached]" in note)
    failure = item.get("failure") or ""
    if item.get("returncode") not in (0, None) or failure:
        result["failure"] = (str(failure).strip().splitlines() or ["failed"])[0][:160]
    if placement_cache.MISS in str(failure) or placement_cache.MISS in done.stdout + done.stderr:
        result["missed"] = True
    try:
        values = json.loads((run_dir / "kpi.json").read_text(encoding="utf-8")).get(sheet) or {}
    except (OSError, ValueError):
        values = {}
    if values and result["built"]:
        result.update(wire=values.get("wire_length_mm"), corners=values.get("corners"), crossings=values.get("crossings"),
                      throughs=values.get("body_hits"), shorts=len(values.get("shorted_nets") or ()))
        fields = next(iter(sorted((run_dir / "artifacts").glob("schematic-engine-*"))), None)
        try:
            collisions = json.loads((fields / "field-placement.json").read_text(encoding="utf-8")).get("collisions_after") or {}
            result["field_collisions"] = sum(collisions.values())
        except (OSError, ValueError, TypeError, AttributeError):
            result["field_collisions"] = None
    return result


def run_all(sheets: list[str], seeds: str, jobs: int) -> tuple[list[dict], float]:
    started = time.time()
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for outcome in pool.map(lambda sheet: build(sheet, seeds, probe=True), sheets):
            results[outcome["sheet"]] = outcome
    for sheet in sheets:
        outcome = results[sheet]
        if outcome["missed"]:
            if outcome["run"] is not None:
                shutil.rmtree(outcome["run"], ignore_errors=True)       # the probe's run holds nothing
            print(f"  solving {sheet} ...", flush=True)
            results[sheet] = build(sheet, seeds, probe=False)
    return [results[sheet] for sheet in sheets], time.time() - started


def row(outcome: dict) -> list[str]:
    def number(value, digits=1):
        return "-" if value is None else f"{value:.{digits}f}"

    solve = "cached" if outcome.get("cached") else number(outcome.get("solve_seconds"), 0)
    return [outcome["sheet"], "yes" if outcome["built"] else "NO", str(outcome.get("netlist") or "-"),
            number(outcome.get("wire")), str(outcome.get("corners", "-")), str(outcome.get("crossings", "-")),
            str(outcome.get("throughs", "-")), str(outcome.get("shorts", "-")), str(outcome.get("field_collisions", "-")),
            solve if outcome["built"] or outcome.get("solve_seconds") else "-", number(outcome.get("build_seconds"), 0),
            "" if outcome["built"] else (outcome.get("failure") or "")]


def table(outcomes: list[dict]) -> str:
    header = ["sheet", "built", "netlist", "wire mm", "corners", "cross", "through", "short", "fields", "solve s", "build s", "failure"]
    rows = [header, *(row(o) for o in outcomes)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header) - 1)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(r, widths)) + "  " + r[-1] for r in rows).rstrip()


def record(outcome: dict) -> dict:
    if not outcome["built"]:
        return {"built": False, "known_failure": outcome.get("failure") or "did not build"}
    return {"built": True, "netlist": outcome.get("netlist"), "throughs": outcome.get("throughs"), "shorts": outcome.get("shorts"),
            "crossings": outcome.get("crossings"), "wire_mm": outcome.get("wire"), "corners": outcome.get("corners"),
            "field_collisions": outcome.get("field_collisions")}


def compare(outcomes: list[dict], expected: dict, *, counts: bool) -> tuple[list[str], list[str]]:
    """(problems, notes): what got worse, and what got better or is not recorded."""
    problems, notes = [], []
    for outcome in outcomes:
        name, was = outcome["sheet"], expected.get(outcome["sheet"])
        if was is None:
            notes.append(f"{name}: not in the expected file (run --update)")
            continue
        if not outcome["built"]:
            if was.get("built"):
                problems.append(f"{name}: stopped building: {outcome.get('failure') or 'failed'}")
            continue
        if not was.get("built"):
            notes.append(f"{name}: builds now (known failure: {was.get('known_failure')}); run --update")
            continue
        if outcome.get("netlist") != "PASS":
            problems.append(f"{name}: netlist {outcome.get('netlist')}")
        if counts:
            for key, label in (("throughs", "through"), ("shorts", "short"), ("crossings", "crossing")):
                if was.get(key) is not None and (outcome.get(key) or 0) > was[key]:
                    problems.append(f"{name}: {label} {was[key]} -> {outcome.get(key)}")
            if was.get("wire_mm") and outcome.get("wire") is not None and outcome["wire"] < was["wire_mm"] - 0.5:
                notes.append(f"{name}: wire {was['wire_mm']} -> {outcome['wire']}")
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, default=5, choices=(1, 5), help="1: one seed (pass/fail); 5: the five-seed portfolio")
    parser.add_argument("--jobs", type=int, default=3, help="builds at once for sheets whose solver answer is cached")
    parser.add_argument("--only", help="comma-separated sheets (default: all 14)")
    parser.add_argument("--update", action="store_true", help="write the expected file from this run")
    args = parser.parse_args(argv)
    sheets = [s for s in (args.only.split(",") if args.only else SHEETS) if s]
    unknown = [s for s in sheets if s not in SHEETS]
    if unknown:
        print(f"unknown sheets {unknown}; one of {SHEETS}", file=sys.stderr)
        return 2
    seeds = FIVE_SEEDS if args.seeds == 5 else FIVE_SEEDS.split(",")[0]
    outcomes, wall = run_all(sheets, seeds, args.jobs)
    print(table(outcomes))
    print(f"\n{len(outcomes)} sheets, {sum(1 for o in outcomes if o['built'])} built, wall {wall:.0f} s "
          f"({sum(1 for o in outcomes if o.get('cached'))} solves cached)")
    expected = json.loads(EXPECTED.read_text(encoding="utf-8")) if EXPECTED.is_file() else {}
    if args.update:
        if args.seeds != 5 or args.only:
            print("--update needs every sheet at five seeds", file=sys.stderr)
            return 2
        EXPECTED.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED.write_text(json.dumps({o["sheet"]: record(o) for o in outcomes}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {EXPECTED.relative_to(REPO)}")
        return 0
    problems, notes = compare(outcomes, expected, counts=args.seeds == 5)
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"FAIL: {problem}")
    if args.seeds != 5:
        print("(one seed: counts are not compared with the five-seed expected file)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
