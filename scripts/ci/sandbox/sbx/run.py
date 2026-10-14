"""Run fixtures inside the sandbox: snapshot, patch, build each at a placement in-process, collect, score.

Native, no Docker. The repository is only read; every run works on its own patched copy
under .work/runs/.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import corpus, demos, env, kpi, snapshot

FIXTURES = ("rc_filter", "noninverting_amplifier", "linear_regulator")
# Circuits that declare no relations at all; run them by name (`--fixtures i2c_sensor,led_switch`)
EXTRA_FIXTURES = ("i2c_sensor", "led_switch")
# KiCad's own demo circuits, redrawn from their netlists (`sandbox.py demos`): `--fixtures demo_lm317,demo_buck,...`
DEMO_FIXTURES = tuple(demos.DEMOS)
# "<fixture>_bare": the same circuit with none of its relations declared (the engine infers what it can)
# Circuits read from other authors' sheets (`sandbox.py corpus`): direct mode only, `--placement ladder`
CORPUS_FIXTURES = tuple(f"corpus_{key}" for key in corpus.CIRCUITS)
BARE_SUFFIX = "_bare"
def effective_jobs(jobs: int, fixture_count: int) -> int:
    """Requested workers, capped at the number of fixtures (and at least 1)."""
    return max(1, min(jobs, fixture_count))


def run_queue(items: list, ports: list[int], work: Callable[[int, object], dict]) -> list[dict]:
    """Run work(port, item) for every item with one worker thread per port, pulling from a shared queue.

    Results come back in the order of `items`, whatever order they finished in.
    """
    pending: queue.Queue = queue.Queue()
    for index, item in enumerate(items):
        pending.put((index, item))
    results: list[dict | None] = [None] * len(items)
    errors: list[BaseException] = []

    def worker(port: int) -> None:
        while not errors:
            try:
                index, item = pending.get_nowait()
            except queue.Empty:
                return
            try:
                results[index] = work(port, item)
            except BaseException as error:  # noqa: BLE001 - re-raised below
                errors.append(error)
                return

    threads = [threading.Thread(target=worker, args=(port,), daemon=True) for port in ports]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(0.5)  # short joins keep Ctrl-C responsive
    if errors:
        raise errors[0]
    return results  # type: ignore[return-value]


class SandboxError(RuntimeError):
    pass


def readiness() -> list[str]:
    """What is missing before `run` can work; empty when the sandbox is ready."""
    python = env.venv_python()
    if not python.is_file():
        return ["virtual environment missing (run: sandbox.py bootstrap --yes)"]
    problems = []
    for name, version in env.load_pins()["pip"].items():
        found = env.package_version(python, name)
        if found != version:
            problems.append(f"{name}: installed {found or 'nothing'}, pinned {version}")
    if not (env.NODE_MODULES / "@mr_mint" / "elkjs-libavoid").is_dir():
        problems.append("node package @mr_mint/elkjs-libavoid is not installed")
    if not env.ELK_BUNDLE.is_file():
        problems.append("elkjs bundle is missing")
    if env.find_kicad_cli() is None:
        problems.append("kicad-cli not found (set KICAD_AI_KICAD_CLI)")
    return problems


def engine_env(src: Path, *, extra: dict[str, str]) -> dict[str, str]:
    """Environment for the fixture processes."""
    child = dict(os.environ)
    child.update({
        "PYTHONPATH": str(src),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "KICAD_AI_ENABLE_SCHEMATIC_WRITES": "1",
        "KICAD_AI_MCP_TIMEOUT_SECONDS": os.environ.get("KICAD_AI_MCP_TIMEOUT_SECONDS", "60"),
        "KICAD_AI_ELKJS_BUNDLE": str(env.ELK_BUNDLE),
    })
    cli = env.find_kicad_cli()
    if cli is not None:
        child["KICAD_AI_KICAD_CLI"] = str(cli)
        child["PATH"] = str(cli.parent) + os.pathsep + child.get("PATH", "")
    symbols = env.find_symbol_dir(cli)
    if symbols is not None:
        child["KICAD10_SYMBOL_DIR"] = str(symbols)
    child.update(extra)
    return child


def parse_picks(text: str | None) -> dict[str, str]:
    """``demo_buck=3,demo_ampac=last`` -> {fixture: pick}; a fixture not named is picked by ``shipped``."""
    picks: dict[str, str] = {}
    for item in (text or "").split(","):
        if item.strip():
            name, _, pick = item.partition("=")
            picks[name.strip()] = pick.strip() or "shipped"
    return picks


def recorded_placement(from_run: Path, fixture: str, pick: str) -> tuple[dict, dict]:
    """The engine's placement of a fixture from a recorded run: {reference: {position, rotation, mirror}} and where it came from."""
    from . import ladder
    circuit, matched, index, distance = ladder.find_circuit(from_run, fixture, pick=pick)
    poses = {
        s.reference: {"position": [float(s.position[0]), float(s.position[1])],
                      "rotation": s.orientation.rotation, "mirror": s.orientation.mirror}
        for s in ladder.engine_pose(circuit) if not s.reference.startswith("#")
    }
    return poses, {"run": from_run.name, "pick": pick, "index": index, "matches": matched, "distance_mm": distance}


SHEET_MARGIN_MM = 12.7


def ladder_placement(circuit: dict, fixture: str, margin: float = SHEET_MARGIN_MM) -> tuple[dict, dict, str, str]:
    """The ladder placer's placement of a recorded circuit: ({reference: pose}, {reference: layout entry}, roles text, and
    the note on where the positions came from: the heuristic placer, a placement file, or the solver with its clocks).

    Parts the ladder does not place keep the engine's pose. The set is then shifted as a whole so its smallest x and y
    are ``margin``: the ladder's frame has its chain row at y=0 and rows above at negative y.
    """
    from scripts.schematic_validation_fixtures import build_fixture
    from . import ladder
    engine = [s for s in ladder.engine_pose(circuit) if not s.reference.startswith("#")]
    intent = build_fixture(fixture)
    roles = ladder.roles_for(engine, intent)
    moved, note, _wires = ladder.layout_step(engine, circuit["refine"]["args"][1], intent, roles, fixture)
    poses = poses_from_symbols(moved, margin)
    layout = {r: {"position": p["position"], "orientation": [p["rotation"], p["mirror"]]} for r, p in sorted(poses.items())}
    return poses, layout, ladder.roles_summary(roles), note


def is_corpus(fixture: str) -> bool:
    """A fixture read from a corpus sheet (``corpus_<key>``, see ``corpus.py``); it has no recorded engine placement."""
    from . import corpus
    return fixture.startswith("corpus_") and fixture.removeprefix("corpus_") in corpus.CIRCUITS


# The in-process server holds one current project, so two workers reading geometry at once read each other's sheets.
_GEOMETRY_LOCK = threading.Lock()


def corpus_placement(fixture: str, libraries: Path, margin: float = SHEET_MARGIN_MM) -> tuple[dict, dict, str, str]:
    """The ladder placer's placement of a corpus circuit, with symbol geometry read from the human sheet's own libraries.

    ``libraries`` is where the sheet's project is prepared (``corpus.prepare_project``: a ``sym-lib-table``, the embedded
    symbols written as libraries); the build is given the same folder (``SBX_DIRECT_LIBS``). Parts the ladder leaves where
    they are stand in a row below its frame. Returns what ``ladder_placement`` does."""
    from . import corpus, ladder
    entry = corpus.CIRCUITS[fixture.removeprefix("corpus_")]
    built = corpus.build(entry)
    with _GEOMETRY_LOCK:
        symbols, nets, _reach, _half = ladder.corpus_inputs(entry, built, libraries)
    result = ladder.ladder_pose(symbols, nets, built.intent, fixture)
    moved = ladder.others_column(result.symbols, result.roles, nets=nets)
    poses = poses_from_symbols(moved, margin)
    layout = {r: {"position": p["position"], "orientation": [p["rotation"], p["mirror"]]} for r, p in sorted(poses.items())}
    return poses, layout, ladder.roles_summary(result.roles), result.note


def poses_from_symbols(symbols: list, margin: float = SHEET_MARGIN_MM) -> dict:
    """Pose dict for the symbols, shifted so the minimum x and y land at ``margin``."""
    left = min(float(s.position[0]) for s in symbols)
    top = min(float(s.position[1]) for s in symbols)
    dx, dy = margin - left, margin - top
    return {
        s.reference: {"position": [round(float(s.position[0]) + dx, 4), round(float(s.position[1]) + dy, 4)],
                      "rotation": s.orientation.rotation, "mirror": s.orientation.mirror}
        for s in symbols
    }


def run_fixture(
    src: Path, child_env: dict[str, str], fixture: str, artifact: Path, log_path: Path, timeout: float,
    command: list[str],
) -> dict:
    fixture_env = dict(child_env, KICAD_AI_SCHEMATIC_FIXTURE=fixture)
    started = time.monotonic()
    with log_path.open("wb") as log:
        try:
            completed = subprocess.run(
                command,
                cwd=src, env=fixture_env, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
            returncode: int | None = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = None
    return {
        "fixture": fixture,
        "returncode": returncode,
        "seconds": round(time.monotonic() - started, 1),
        "artifact": str(artifact),
        "log": str(log_path),
    }


def read_engine_summary(artifact: Path) -> dict:
    """What the engine itself reported for a fixture (cosmetic score, netlist gate, profile)."""
    summary: dict = {}
    report = artifact / "report.json"
    if report.is_file():
        data = json.loads(report.read_text(encoding="utf-8"))
        workflow = data.get("workflow") or {}
        summary["cosmetic_score"] = (workflow.get("cosmetic_score") or {}).get("cosmetic_score")
        summary["netlist"] = (workflow.get("netlist_verification") or {}).get("status")
    selection = artifact / "quality-selection.json"
    if selection.is_file():
        summary["selected_profile"] = json.loads(selection.read_text(encoding="utf-8")).get("selected_profile")
    failure = artifact / "failure.json"
    if failure.is_file():
        summary["failure"] = str(json.loads(failure.read_text(encoding="utf-8")).get("error", ""))[:300]
    return summary


def score(artifact: Path, fixture: str, src: Path) -> dict:
    for name in ("mcu_smoke.kicad_sch", "candidate.kicad_sch"):
        if (artifact / name).is_file():
            return kpi.kpis_for_schematic(artifact / name, fixture, src)
    raise SandboxError(f"no schematic produced in {artifact}")


def tool_versions() -> dict[str, str | None]:
    python = env.venv_python()
    cli = env.find_kicad_cli()
    return {
        "python": ".".join(map(str, env.interpreter_version(python) or ())) or None,
        "kicad-mcp-pro": env.package_version(python, "kicad-mcp-pro"),
        "ortools": env.package_version(python, "ortools"),
        **env.node_info(),
        "kicad-cli": env.run_capture([str(cli), "--version"]) if cli else None,
    }


def run_experiment(
    *, label: str, fixtures: list[str], overlays: list[str],
    extra_env: dict[str, str], timeout: float, source: Path = env.REPO_ROOT, jobs: int = 1,
    placement_from: str | None = None, placement_pick: str | None = None,
    placement: str = "engine",
) -> Path:
    """Build each fixture once at a placement (the engine's recorded one, or the ladder placer's on that circuit).

    """
    from_run: Path | None = None
    corpus_only = all(is_corpus(f) for f in fixtures) and placement == "ladder"
    if not placement_from and not corpus_only:
        raise SandboxError(
            "a build needs --placement-from RUN (a run with the placement dumps); "
            "only corpus fixtures with --placement ladder go without"
        )
    if placement_from:
        from . import report
        from_run = report.resolve_run(placement_from)
    if placement not in ("engine", "ladder"):
        raise SandboxError(f"unknown placement {placement!r}")
    from . import ladder as ladder_mod
    picks = parse_picks(placement_pick)
    sources: dict[str, dict] = {}
    jobs = effective_jobs(jobs, len(fixtures))
    problems = readiness()
    if problems:
        raise SandboxError("sandbox is not ready:\n  - " + "\n  - ".join(problems))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = env.RUNS_DIR / f"{stamp}-{label}"
    src = run_dir / "src"
    run_dir.mkdir(parents=True)
    snap = snapshot.take_snapshot(src, source)
    applied = snapshot.apply_overlays(overlays, src)
    extra = {**extra_env, "SBX_DUMP_DIR": str(run_dir / "dumps")}
    extra["KICAD_AI_MCP_INPROCESS"] = "1"
    child = engine_env(src, extra=extra)
    manifest = {
        "label": label, "started": stamp, "fixtures": fixtures,
        "git": env.git_state(), "snapshot": snap, "overlays": applied,
        "extra_env": extra_env, "jobs": jobs, "client": "inprocess", "tools": tool_versions(),
        "mode": "direct", "placement": placement, "placement_from": from_run.name if from_run else None,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"run {run_dir.name}: {snap['files']} files snapshotted, overlays: {[o['name'] for o in applied] or 'none'}")

    results: list[dict] = []
    kpis: dict[str, dict] = {}
    (run_dir / "logs").mkdir()
    def one_fixture(fixture: str, fixture_env: dict[str, str]) -> dict:
        artifact = run_dir / "artifacts" / f"schematic-engine-{fixture}"
        if is_corpus(fixture):
            if placement != "ladder":
                raise SandboxError(f"{fixture} has no recorded engine placement: use --placement ladder")
            artifact.mkdir(parents=True, exist_ok=True)
            libraries = artifact / "libs"
            try:
                # The placement (grammar, and the solver when switched on) runs here, before the timed build, so its
                # seconds are recorded beside the build's in the manifest for a like-for-like comparison.
                started = time.perf_counter()
                poses, layout, roles_text, note = corpus_placement(fixture, libraries)
                placement_seconds = round(time.perf_counter() - started, 1)
            except Exception as error:   # the ladder (or a library that cannot be read) failing is a result for that fixture
                (artifact / "failure.json").write_text(json.dumps({"error": f"ladder placement: {error}"}, indent=2), encoding="utf-8")
                return {"fixture": fixture, "returncode": 2, "seconds": 0.0, "artifact": str(artifact),
                        "log": str(run_dir / "logs" / f"{fixture}.log"), "failure": f"ladder placement: {error}"[:300]}
            (artifact / "ladder-roles.json").write_text(json.dumps({"roles": roles_text.splitlines()}, indent=2), encoding="utf-8")
            (artifact / "ladder-layout.json").write_text(json.dumps(layout, indent=2, sort_keys=True), encoding="utf-8")
            (artifact / "placement.json").write_text(json.dumps(poses, indent=2, sort_keys=True), encoding="utf-8")
            sources[fixture] = {"placement": "ladder", "source": "corpus", "libraries": str(libraries),
                                "placement_seconds": placement_seconds, "placement_note": note}
            fixture_env = dict(fixture_env, SBX_DIRECT_LIBS=str(libraries))
            command = [str(env.venv_python()), "scripts/schematic_direct_smoke.py", str(artifact), str(artifact / "placement.json")]
        else:
            artifact.mkdir(parents=True, exist_ok=True)
            poses, origin = recorded_placement(from_run, fixture, picks.get(fixture, "shipped"))
            origin["placement"] = placement
            if placement == "ladder":
                circuit = ladder_mod.find_circuit(from_run, fixture, pick=picks.get(fixture, "shipped"))[0]
                try:
                    started = time.perf_counter()
                    poses, layout, roles_text, note = ladder_placement(circuit, fixture)
                    origin["placement_seconds"] = round(time.perf_counter() - started, 1)
                    origin["placement_note"] = note
                except Exception as error:   # the ladder failing on a circuit is a result for that fixture
                    (artifact / "failure.json").write_text(json.dumps({"error": f"ladder placement: {error}"}, indent=2), encoding="utf-8")
                    return {"fixture": fixture, "returncode": 2, "seconds": 0.0, "artifact": str(artifact),
                            "log": str(run_dir / "logs" / f"{fixture}.log"), "failure": f"ladder placement: {error}"[:300]}
                (artifact / "ladder-roles.json").write_text(json.dumps({"roles": roles_text.splitlines()}, indent=2), encoding="utf-8")
                (artifact / "ladder-layout.json").write_text(json.dumps(layout, indent=2, sort_keys=True), encoding="utf-8")
            sources[fixture] = origin
            (artifact / "placement.json").write_text(json.dumps(poses, indent=2, sort_keys=True), encoding="utf-8")
            command = [str(env.venv_python()), "scripts/schematic_direct_smoke.py", str(artifact), str(artifact / "placement.json")]
        result = run_fixture(src, fixture_env, fixture, artifact, run_dir / "logs" / f"{fixture}.log", timeout, command)
        result.update(read_engine_summary(artifact))
        try:
            kpis[fixture] = score(artifact, fixture, src)
        except Exception as error:  # a failed build still leaves a useful log
            result["kpi_error"] = f"{type(error).__name__}: {error}"
        return result

    def show(result: dict) -> None:
        print(
            f"  {result['fixture']:<24} rc={result['returncode']} {result['seconds']:>6}s "
            f"netlist={result.get('netlist')} profile={result.get('selected_profile')}"
        )

    slots = list(range(max(jobs, 1)))
    print(f"  in-process client, {len(slots)} job(s), no MCP server")
    results = run_queue(fixtures, slots, lambda _slot, fixture: one_fixture(fixture, dict(child)))
    for result in results:
        show(result)
    manifest["results"] = results
    if sources:
        manifest["placement_sources"] = sources
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (run_dir / "kpi.json").write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    return run_dir
