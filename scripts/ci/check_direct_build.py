#!/usr/bin/env python3
"""Build the demo circuits from their committed placements and check what was drawn.

    python scripts/ci/check_direct_build.py [OUT_DIR] [FIXTURE ...]

For each fixture (default: every one in ``tests/fixtures/direct/expected.json``) this runs
``scripts/schematic_direct_smoke.py`` on ``tests/fixtures/direct/<fixture>/placement.json`` into ``OUT_DIR/<fixture>`` and
then requires: the build exits 0, the exported netlist matches the intent (``netlist_verification`` is PASS), and the
sheet's merged wire length (within 0.01 mm), corner count and crossing count equal the committed ``expected.json``,
measured from the saved sheet by ``scripts/ci/sandbox/sbx/kpi.py``. Needs the engine's runtime: kicad-mcp-pro, kicad-cli,
KiCad's symbol libraries and Node with ``@mr_mint/elkjs-libavoid`` (the libavoid router).

To change what the build is expected to draw, run it, look at the sheets, and edit ``expected.json`` (and the
placements, if the placer changed) in the same commit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "direct"
WIRE_TOLERANCE_MM = 0.01
BUILD_TIMEOUT_SECONDS = 300


def build(fixture: str, out: Path) -> tuple[int | None, float]:
    out.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        KICAD_AI_SCHEMATIC_FIXTURE=fixture,
        KICAD_AI_ENABLE_SCHEMATIC_WRITES="1",
        KICAD_AI_MCP_INPROCESS="1",
        KICAD_AI_MCP_TIMEOUT_SECONDS=os.environ.get("KICAD_AI_MCP_TIMEOUT_SECONDS", "60"),
    )
    started = time.monotonic()
    with (out / "build.log").open("wb") as log:
        try:
            done = subprocess.run(
                [sys.executable, "scripts/schematic_direct_smoke.py", str(out), str(FIXTURES / fixture / "placement.json")],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=BUILD_TIMEOUT_SECONDS, check=False,
            )
            code: int | None = done.returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, time.monotonic() - started


def check(fixture: str, out: Path, expected: dict) -> list[str]:
    """What is wrong with the sheet built into ``out``; empty when it is right."""
    sys.path.insert(0, str(ROOT / "scripts" / "ci" / "sandbox"))
    from sbx import kpi

    problems: list[str] = []
    report = out / "report.json"
    if not report.is_file():
        failure = out / "failure.json"
        return [f"no report.json ({failure.read_text(encoding='utf-8')[:300] if failure.is_file() else 'no failure.json either'})"]
    status = ((json.loads(report.read_text(encoding="utf-8")).get("workflow") or {}).get("netlist_verification") or {}).get("status")
    if status != "PASS":
        problems.append(f"netlist verification {status!r}, expected 'PASS'")
    measured = kpi.kpis_for_schematic(out / "mcu_smoke.kicad_sch", fixture, ROOT)
    if abs(measured["wire_length_mm"] - expected["wire_length_mm"]) > WIRE_TOLERANCE_MM:
        problems.append(f"wire {measured['wire_length_mm']} mm, expected {expected['wire_length_mm']} +/- {WIRE_TOLERANCE_MM}")
    for name in ("corners", "crossings"):
        if measured[name] != expected[name]:
            problems.append(f"{name} {measured[name]}, expected {expected[name]}")
    return problems


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv else ROOT / ".kicad-ai" / "direct"
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    names = argv[1:] or sorted(expected)
    failed = 0
    for fixture in names:
        out = out_dir / fixture
        code, seconds = build(fixture, out)
        problems = [f"build exited {code}" + (" (timeout)" if code is None else "")] if code != 0 else []
        if not problems:
            problems = check(fixture, out, expected[fixture])
        failed += bool(problems)
        print(f"{fixture:<12} {'FAIL' if problems else 'ok':<5} {seconds:6.1f}s  " + "; ".join(problems), flush=True)
    print(f"{len(names) - failed} of {len(names)} builds as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
