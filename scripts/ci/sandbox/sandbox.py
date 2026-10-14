#!/usr/bin/env python3
"""Experiment sandbox for the schematic engine. See README.md.

    sandbox.py doctor                      what is ready, what is missing
    sandbox.py bootstrap [--yes]           show the setup plan; --yes downloads and installs it
    sandbox.py overlays [--check]          list experiment patches; --check applies them offline
    sandbox.py probes [--only NAME]        offline measurements (no installs needed)
    sandbox.py run --label NAME ...        run fixtures on a patched snapshot, score them
    sandbox.py kpi PATH                    KPIs for an existing artifact directory
    sandbox.py rescore RUN                 recompute a past run's KPIs with the current KPI code
    sandbox.py placement RUN [RUN ...]     placement-only KPIs (HPWL, spacing) even for failed builds
    sandbox.py compare A B                 side-by-side KPIs of two runs
    sandbox.py readability RUN... [--corpus]  what a reader notices (text on wires, far support parts, crossings...)
    sandbox.py demos [--write]             KiCad demo circuits as engine inputs (netlist + blocks); --write regenerates the data
    sandbox.py bench RUN [--against RUN2] [--out DIR]   stacked PNGs + bench.md: engine run(s) vs human, fixtures table
    sandbox.py redraw RUN [--blind DIR]    the engine's redraw of each demo in RUN beside the human original, by the same counts
    sandbox.py table RUN [RUN ...]         headline KPIs of many runs, one row per fixture and run
    sandbox.py analyze-dumps RUN           libavoid dump analysis (needs the e3 overlay)
    sandbox.py terminals RUN               power-terminal strategies and unchecked fallbacks (needs the e5 overlay)
    sandbox.py replay RUN [--variants ...] re-route recorded libavoid calls under router variants (needs the e3 overlay)
    sandbox.py ladder --run RUN --circuit lm317|buck|opamp|amp|all   the ladder placer beside the engine's, offline
    sandbox.py ladder-corpus --circuit NAME|all --out DIR   the ladder placer on human-drawn corpus sheets, beside the human's
    sandbox.py ladder-fixtures [--run RUN] [--demos lm317,buck,...|all|none] [--corpus NAME,...|all|none] [--out DIR]
                                           one JSON fixture per circuit (the ladder plan, symbol geometry, nets, the heuristic
                                           placement as baseline; default DIR tests/fixtures/ladder) for legalisers built
                                           outside the sandbox; judged with `python -m backend.schematic.ladder_check`
    (ladder, ladder-corpus and run --placement ladder take positions from the heuristic placer unless the environment says
     otherwise: KICAD_AI_LADDER_PLACEMENT=FILE|DIR reads a placement file (DIR: <circuit>.json per circuit, the format is in
     backend/schematic/ladder_check.py), KICAD_AI_LADDER_SOLVER=cpsat runs plan -> backend/schematic/ladder_solve.py)
    sandbox.py placement-replay RUN ...    run the placement solvers of a recorded run again, offline (needs the e11 overlay)
    sandbox.py selftest                    the sandbox's own unit tests (offline)
    sandbox.py clean --yes                 delete runs, outputs (and the environment with --all)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sbx import bench, bootstrap, corpus, demos, dumps, env, kpi, ladder, placement_replay, readability, redraw, replay, report, run, snapshot  # noqa: E402

PROBES_DIR = env.SANDBOX_DIR / "probes"


def _minimum_python() -> tuple[int, ...]:
    return tuple(int(part) for part in env.load_pins()["python"]["min"].split("."))


def cmd_doctor(args: argparse.Namespace) -> int:
    pins = env.load_pins()
    minimum = _minimum_python()
    rows: list[tuple[str, str, str]] = []

    def add(state: str, label: str, detail: str) -> None:
        rows.append((state, label, detail))

    git = env.git_state()
    add("ok", "repository", f"{env.REPO_ROOT} @ {str(git['head'])[:7]} ({'dirty' if git['dirty'] else 'clean'})")
    python = env.find_python(minimum)
    add("ok" if python else "!!", "python >= " + ".".join(map(str, minimum)), str(python) if python else "not found; install Python 3.13+")
    cli = env.find_kicad_cli()
    if cli:
        on_path = "on PATH" if shutil.which("kicad-cli") else "not on PATH; the sandbox passes it to child processes"
        add("ok", "kicad-cli", f"{cli} ({env.run_capture([str(cli), '--version'])}; {on_path})")
    else:
        add("!!", "kicad-cli", "not found; install KiCad 10 or set KICAD_AI_KICAD_CLI")
    symbols = env.find_symbol_dir(cli)
    add("ok" if symbols else "!!", "KiCad symbols", str(symbols) if symbols else "not found; set KICAD10_SYMBOL_DIR")
    node = env.node_info()
    node_ok = bool(node["node"]) and env.parse_version(node["node"] or "0") >= (int(pins["node"]["min"]),)
    add("ok" if node_ok else "!!", f"node >= {pins['node']['min']}", f"{node['node']}, npm {node['npm']}" if node["node"] else "not found")

    venv = env.venv_python()
    add("ok" if venv.is_file() else "--", "virtual environment", str(venv) if venv.is_file() else "not created (bootstrap --yes)")
    for name, version in pins["pip"].items():
        found = env.package_version(venv, name) if venv.is_file() else None
        add("ok" if found == version else "--", f"pip {name}=={version}", found or "not installed")
    for package in ("@mr_mint/elkjs-libavoid", "libavoid-js"):
        manifest = env.NODE_MODULES / package / "package.json"
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"] if manifest.is_file() else None
        add("ok" if version else "--", f"npm {package}", version or "not installed")
    bundle = env.ELK_BUNDLE
    verified = bundle.is_file() and bootstrap.git_blob_sha1(bundle.read_bytes()) == pins["elkjs_bundle"]["git_blob_sha1"]
    add("ok" if verified else "--", "elkjs bundle", "present, sha1 verified" if verified else "missing")
    try:
        names = list(snapshot.list_overlays())
        snapshot.check_overlays(names)
        add("ok", "overlays", f"all apply to the current tree: {', '.join(names)}")
    except snapshot.PatchError as error:
        add("!!", "overlays", f"stale: {error}")
    ambient = env.package_version(Path(sys.executable), "kicad-mcp-pro")
    if ambient and ambient != pins["pip"]["kicad-mcp-pro"]:
        add("ok", "ambient kicad-mcp-pro", f"{ambient} (pinned {pins['pip']['kicad-mcp-pro']}; the sandbox venv is isolated from it)")

    for state, label, detail in rows:
        print(f"[{state}] {label:<28} {detail}")
    blocking = {"probes": ("python", "kicad-cli", "overlays"), "run": None}[args.target]
    failing = [r for r in rows if r[0] == "!!" or (r[0] == "--" and args.target == "run")]
    if blocking:
        failing = [r for r in failing if r[1].startswith(blocking)]
    print("\nready for " + args.target if not failing else f"\nnot ready for {args.target}: {len(failing)} item(s) above")
    return 0 if not failing else 1


def cmd_bootstrap(args: argparse.Namespace) -> int:
    pins = env.load_pins()
    python = env.find_python(_minimum_python())
    if python is None:
        print("Python 3.13+ is required to create the sandbox environment.", file=sys.stderr)
        return 1
    steps = bootstrap.plan(pins, python)
    print(f"Sandbox bootstrap plan (everything lands under {env.WORK_DIR}):")
    print(bootstrap.describe(steps))
    if not args.yes:
        print("\nNothing was downloaded or installed. Re-run with --yes to execute this plan.")
        return 0
    print()
    bootstrap.execute(pins, python)
    print("\nBootstrap finished.\n")
    return cmd_doctor(argparse.Namespace(target="run"))


def cmd_overlays(args: argparse.Namespace) -> int:
    available = snapshot.list_overlays()
    for name, description in available.items():
        print(f"{name}\n    {description}")
    if args.check:
        applied = snapshot.check_overlays(list(available))
        print(f"\nall {len(applied)} overlays apply to the current tree and compile")
    return 0


def cmd_probes(args: argparse.Namespace) -> int:
    venv = env.venv_python()
    python = Path(args.python) if args.python else (venv if venv.is_file() else Path(sys.executable))
    print(f"probe interpreter: {python}")
    out = env.OUT_DIR / "probes"
    out.mkdir(parents=True, exist_ok=True)
    child = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    crashed = 0
    scripts = sorted(PROBES_DIR.glob("[!_]*.py"))
    if (env.NODE_MODULES / "libavoid-js").is_dir():
        # Node resolves packages upward from the script, so run it from inside the work dir.
        staged = env.WORK_DIR / "probes"
        staged.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROBES_DIR / "libavoid_surface.mjs", staged / "libavoid_surface.mjs")
        scripts.append(staged / "libavoid_surface.mjs")
    for path in scripts:
        if args.only and path.stem not in args.only:
            continue
        command = ["node", str(path)] if path.suffix == ".mjs" else [str(python), str(path)]
        done = subprocess.run(command, capture_output=True, text=True, env=child, timeout=600, check=False)
        lines = done.stdout.strip().splitlines()
        try:
            result = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            crashed += 1
            print(f"\n== {path.stem}: no result\n{(done.stderr or done.stdout).strip()[-600:]}")
            continue
        version = result.get("kicad_mcp_version") or "none"
        (out / f"{path.stem}-mcp{version}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\n== {path.stem} [{'ok' if result['ok'] else 'FINDING'}] (kicad-mcp-pro {version})")
        for line in result["summary"]:
            print(f"   {line}")
    print(f"\nfull JSON in {out}")
    return 2 if crashed else 0


def _parse_env(items: list[str]) -> dict[str, str]:
    pairs = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit(f"--env expects KEY=VALUE, got {item!r}")
        pairs[key] = value
    return pairs


def cmd_run(args: argparse.Namespace) -> int:
    fixtures = [f for f in args.fixtures.split(",") if f]
    known = (*run.FIXTURES, *run.EXTRA_FIXTURES, *run.DEMO_FIXTURES, *run.CORPUS_FIXTURES)
    unknown = [f for f in fixtures if f.removesuffix(run.BARE_SUFFIX) not in known]
    if unknown:
        raise SystemExit(
            f"unknown fixtures {unknown}; choose from {', '.join(known)} "
            f"(or any of them with {run.BARE_SUFFIX}: the same circuit with no relations declared)"
        )
    missing = [name for name in args.overlay or [] if name not in snapshot.list_overlays()]
    if missing:
        raise SystemExit(f"unknown overlays {missing}; choose from {', '.join(snapshot.list_overlays())}")
    try:
        run_dir = run.run_experiment(
            label=args.label, fixtures=fixtures, overlays=args.overlay or [],
            extra_env=_parse_env(args.env or []), timeout=args.timeout,
            jobs=args.jobs,
            placement_from=args.placement_from, placement_pick=args.placement_pick, placement=args.placement,
        )
    except (run.SandboxError, snapshot.PatchError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    kpis = json.loads((run_dir / "kpi.json").read_text(encoding="utf-8"))
    for fixture, values in kpis.items():
        print(f"\n{fixture}\n{kpi.format_kpis(values)}")
    print(f"\nrun directory: {run_dir}")
    failed = [r for r in json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["results"] if r["returncode"] != 0]
    return 1 if failed else 0


def cmd_kpi(args: argparse.Namespace) -> int:
    root = Path(args.path)
    directories = [root] if (root / "mcu_smoke.kicad_sch").is_file() else sorted(root.glob("schematic-engine-*"))
    if not directories:
        print(f"no artifact directories found under {root}", file=sys.stderr)
        return 1
    for directory in directories:
        schematic = directory / "mcu_smoke.kicad_sch"
        fixture = args.fixture
        report_file = directory / "report.json"
        if fixture is None and report_file.is_file():
            fixture = json.loads(report_file.read_text(encoding="utf-8")).get("fixture")
        if fixture is None:
            fixture = directory.name.removeprefix("schematic-engine-")
        values = kpi.kpis_for_schematic(schematic, fixture, Path(args.src))
        if args.json:
            print(json.dumps(values, indent=2))
        else:
            print(f"{directory.name} ({fixture})\n{kpi.format_kpis(values)}\n")
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Recompute kpi.json for a past run with the current KPI code (no engine needed)."""
    try:
        run_dir = report.resolve_run(args.run)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    scores = {}
    for artifact in sorted((run_dir / "artifacts").glob("schematic-engine-*")):
        fixture = artifact.name.removeprefix("schematic-engine-")
        try:
            scores[fixture] = run.score(artifact, fixture, run_dir / "src")
        except Exception as error:  # fixtures that failed to build have nothing to score
            print(f"  {fixture}: not scored ({type(error).__name__}: {error})")
    (run_dir / "kpi.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
    print(f"rescored {len(scores)} fixture(s) in {run_dir.name}")
    return 0


def cmd_placement(args: argparse.Namespace) -> int:
    """Placement-only KPIs from each fixture's pre-route scene (works even if routing failed)."""
    rows = []
    for reference in args.runs:
        run_dir = report.resolve_run(reference)
        for artifact in sorted((run_dir / "artifacts").glob("schematic-engine-*")):
            scene = artifact / "pre-route-presentation-scene.json"
            if not scene.is_file():
                continue
            fixture = artifact.name.removeprefix("schematic-engine-")
            intent = kpi.load_intent(fixture, run_dir / "src")
            signal = {net.name for net in intent.all_nets if net.kind not in ("power", "ground")}
            values = kpi.placement_kpis(
                kpi.geometry_from_scene_dict(json.loads(scene.read_text(encoding="utf-8"))),
                kpi.nets_of(intent), signal,
            )
            passives = values["passives"]
            clearance = values["router_clearance"]
            rows.append((fixture, run_dir.name.split("-", 1)[1], values["hpwl_signal_mm"], values["hpwl_all_mm"],
                         passives["nn_center_median_mm"], values["body_overlaps"],
                         f"{clearance['trapped']}/{clearance['pins_with_direction']}",
                         f"{values['extent_width_mm']} x {values['extent_height_mm']}"))
    print(f"{'fixture':<24} {'run':<22} {'HPWL signal':>11} {'HPWL all':>9} {'passive pitch':>13} {'overlaps':>8} {'trapped':>8}  extent")
    for fixture, label, signal, total, pitch, overlaps, trapped, extent in sorted(rows):
        print(f"{fixture:<24} {label:<22} {signal:>11} {total:>9} {str(pitch):>13} {overlaps:>8} {trapped:>8}  {extent}")
    return 0


def cmd_readability(args: argparse.Namespace) -> int:
    """What a reader notices in each fixture's shipped and selected sheet, and in human-drawn sheets."""
    if args.corpus or args.refresh:
        try:
            data = readability.corpus(refresh=args.refresh)
        except FileNotFoundError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(readability.format_corpus(data))
        print()
    for reference in args.runs:
        run_dir = report.resolve_run(reference)
        print(readability.format_run_table(run_dir.name, readability.measure_run(run_dir)))
        print()
    return 0


def cmd_demos(args: argparse.Namespace) -> int:
    """KiCad's demo circuits as the netlist and blocks the engine is given."""
    try:
        circuits = demos.build_all()
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for circuit in circuits.values():
        print(demos.describe(circuit))
        print()
    if args.write:
        print(f"wrote {demos.write_circuits()}")
    return 0


def cmd_corpus(args: argparse.Namespace) -> int:
    """The corpus circuits as engine fixtures (``corpus_<key>``); --write regenerates scripts/schematic_corpus_circuits.py."""
    try:
        if args.write:
            print(f"wrote {corpus.write_circuits()}")
        else:
            for key, entry in corpus.CIRCUITS.items():
                print(f"{corpus.fixture_key(key):<24} {entry.sheet}")
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_redraw(args: argparse.Namespace) -> int:
    """Human original, shipped sheet and selected sheet of each demo in a run, by the same counts."""
    run_dir = report.resolve_run(args.run)
    keys = [key for key in demos.DEMOS if (run_dir / "artifacts" / f"schematic-engine-{key}").is_dir()]
    if not keys:
        print(f"error: {run_dir.name} has no demo circuits", file=sys.stderr)
        return 1
    for key in keys:
        print(redraw.format_comparison(key, redraw.measure(run_dir, key)))
        print()
    if args.blind:
        key_map = redraw.blind_page(run_dir, keys, Path(args.blind))
        print(f"drawings written to {args.blind}; the key is in key.json there and is not printed")
        del key_map
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    try:
        out = bench.run_bench(args.run, args.against, Path(args.out) if args.out else None)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"bench written to {out}")
    return 0


def cmd_placement_replay(args: argparse.Namespace) -> int:
    """The placement solvers of one circuit of a recorded run, run again with other weights, rounds or orientations."""
    run_dir = report.resolve_run(args.run)
    try:
        circuits = placement_replay.load_circuits(run_dir)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not 0 <= args.group < len(circuits):
        print(f"error: {run_dir.name} has {len(circuits)} recorded circuits (trials); --group 0..{len(circuits) - 1}", file=sys.stderr)
        return 1
    result = placement_replay.replay(
        circuits[args.group], weights=placement_replay.parse_weights(args.weights), rounds=args.rounds,
        stages=tuple(name for name in args.stages.split(",") if name), orient=args.orient,
    )
    print(placement_replay.format_replay(result))
    if args.svg:
        Path(args.svg).write_text(placement_replay.svg(result, args.label), encoding="utf-8")
        print(f"drawing written to {args.svg}")
    return 0


def cmd_ladder_corpus(args: argparse.Namespace) -> int:
    """The ladder placer on circuits from the corpus clones (no engine run), set beside each human sheet."""
    names = sorted(corpus.CIRCUITS) if args.circuit == "all" else [args.circuit]
    unknown = [n for n in names if n not in corpus.CIRCUITS]
    if unknown:
        print(f"unknown circuit {unknown}; one of {sorted(corpus.CIRCUITS)}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else env.SANDBOX_DIR / "probes" / "ladder_corpus" / "out"
    try:
        ladder.run_corpus(names, out_dir)
    except Exception as error:
        print(f"ladder-corpus failed: {error}", file=sys.stderr)
        return 1
    print(f"written to {out_dir}")
    return 0


def cmd_ladder_fixtures(args: argparse.Namespace) -> int:
    """One JSON fixture per circuit (plan, geometry, nets, baseline placement) for legalisers built outside the sandbox."""
    def names(value: str, every) -> list[str]:
        return sorted(every) if value == "all" else [] if value == "none" else [n for n in value.split(",") if n]

    demo_names, corpus_names = names(args.demos, ladder.CIRCUITS), names(args.corpus, corpus.CIRCUITS)
    unknown = [n for n in corpus_names if n not in corpus.CIRCUITS]
    if unknown:
        print(f"unknown circuit {unknown}; one of {sorted(corpus.CIRCUITS)}", file=sys.stderr)
        return 2
    try:
        written = ladder.write_fixtures(report.resolve_run(args.run), Path(args.out), demo_names, corpus_names)
    except (FileNotFoundError, LookupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for name, size, note in written:
        print(f"{name:16} {size / 1024:7.1f} KiB  {note}")
    return 1 if any(note for _n, _s, note in written) else 0


def cmd_ladder(args: argparse.Namespace) -> int:
    """The ladder placer (a stand-in until its modules exist) and the engine's placement of the demos of a recorded run."""
    names = sorted(ladder.CIRCUITS) if args.circuit == "all" else [args.circuit]
    out_dir = Path(args.out) if args.out else env.SANDBOX_DIR / "probes" / "ladder" / "out"
    try:
        ladder.run(report.resolve_run(args.run), names, engine=args.engine, out_dir=out_dir, pick=str(args.index) if args.index is not None else args.pick)
    except (FileNotFoundError, LookupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"written to {out_dir}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        first, second = report.resolve_run(args.a), report.resolve_run(args.b)
        text = report.compare(first, second)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    (second / f"compare-vs-{first.name}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    try:
        runs = [report.resolve_run(reference) for reference in args.runs]
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    fixtures = [name for name in (args.fixtures or "").split(",") if name]
    print(report.table(runs, fixtures))
    return 0


def cmd_analyze_dumps(args: argparse.Namespace) -> int:
    try:
        target = report.resolve_run(args.run)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    directory = target / "dumps" if (target / "dumps").is_dir() else target
    result = dumps.analyze(directory)
    print(json.dumps(result["summary"], indent=2))
    if result["worst_displacement"]:
        print("worst endpoint displacement:", json.dumps(result["worst_displacement"], indent=2))
    return 0 if result["summary"]["routes"] else 1


def cmd_terminals(args: argparse.Namespace) -> int:
    try:
        target = report.resolve_run(args.run)
        grouped = dumps.terminals(target)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(dumps.format_terminals(grouped))
    return 0 if any(grouped.values()) else 1


def cmd_replay(args: argparse.Namespace) -> int:
    if args.list:
        for variant in replay.VARIANTS.values():
            print(f"  {variant.name:<20} {variant.note}")
        return 0
    if not args.run:
        raise SystemExit("replay needs a RUN (or a directory of libavoid-*.json dumps); see --list for variants")
    if args.calibrate:
        try:
            target = report.resolve_run(args.run)
            directory = target / "dumps" if (target / "dumps").is_dir() else target
            print(replay.format_calibration(replay.calibrate(directory)))
        except (FileNotFoundError, replay.ReplayError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    try:
        target = report.resolve_run(args.run)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    directory = target / "dumps" if (target / "dumps").is_dir() else target
    names = list(replay.VARIANTS) if args.variants == "all" else [n for n in args.variants.split(",") if n]
    if "asis" not in names:
        names.insert(0, "asis")
    try:
        result = replay.replay(directory, names)
    except replay.ReplayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(replay.format_report(result))
    env.OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = env.OUT_DIR / f"replay-{target.name}.json"
    out.write_text(json.dumps({k: v for k, v in result.items()}, indent=1), encoding="utf-8")
    print()
    print(f"full per-edge rows: {out}")
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    command = [sys.executable, "-m", "unittest", "discover", "-s", str(env.SANDBOX_DIR / "selftest")]
    return subprocess.run(command, cwd=env.REPO_ROOT, check=False).returncode


def cmd_clean(args: argparse.Namespace) -> int:
    if not args.yes:
        print("clean deletes sandbox data under " + str(env.WORK_DIR) + "; re-run with --yes")
        return 1
    targets = [env.RUNS_DIR, env.OUT_DIR]
    if args.all:
        targets = [env.WORK_DIR]
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
            print(f"removed {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experiment sandbox for the schematic engine.")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="report what is ready and what is missing")
    doctor.add_argument("--for", dest="target", choices=("run", "probes"), default="run")
    doctor.set_defaults(handler=cmd_doctor)

    boot = commands.add_parser("bootstrap", help="show (and with --yes execute) the one-time setup")
    boot.add_argument("--yes", action="store_true", help="download and install; without it only the plan is shown")
    boot.set_defaults(handler=cmd_bootstrap)

    overlays = commands.add_parser("overlays", help="list experiment patches")
    overlays.add_argument("--check", action="store_true", help="apply all overlays to a throwaway snapshot")
    overlays.set_defaults(handler=cmd_overlays)

    probes = commands.add_parser("probes", help="offline measurements")
    probes.add_argument("--only", nargs="+", help="probe names (file stems)")
    probes.add_argument("--python", help="interpreter to run probes with (default: sandbox venv, else this one)")
    probes.set_defaults(handler=cmd_probes)

    runner = commands.add_parser("run", help="run fixtures on a patched snapshot and score them")
    runner.add_argument("--label", required=True)
    runner.add_argument("--fixtures", default=",".join(run.FIXTURES))
    runner.add_argument("--overlay", action="append", help="overlay name (repeatable)")
    runner.add_argument("--env", action="append", help="KEY=VALUE for the server and fixtures (repeatable)")
    runner.add_argument("--client", choices=("http", "inprocess"), default="inprocess", help="a build reaches the MCP tools in this process; the only client, kept so existing command lines work")
    runner.add_argument("--mode", choices=("direct",), default="direct", help="a run builds each fixture once at a recorded placement (needs --placement-from; not for corpus_* fixtures with --placement ladder); the only mode, kept so existing command lines work")
    runner.add_argument("--placement-from", help="run (label or directory) whose recorded placement calls give each fixture's placement (optional for corpus_* fixtures)")
    runner.add_argument("--placement", choices=("engine", "ladder"), default="engine", help="whose placement to build: the engine's recorded one, or the ladder placer's on that circuit")
    runner.add_argument("--placement-pick", help="FIXTURE=INDEX|shipped|last,... which recorded circuit to use (default shipped: the one nearest the run's shipped sheet)")
    runner.add_argument("--timeout", type=float, default=1800.0, help="seconds per fixture")
    runner.add_argument("--jobs", type=int, default=1, help="parallel builds (capped at the fixture count)")
    runner.set_defaults(handler=cmd_run)

    score = commands.add_parser("kpi", help="KPIs for an existing artifact directory")
    score.add_argument("path")
    score.add_argument("--fixture")
    score.add_argument("--src", default=str(env.REPO_ROOT), help="engine tree providing the fixtures and reader")
    score.add_argument("--json", action="store_true")
    score.set_defaults(handler=cmd_kpi)

    rescore = commands.add_parser("rescore", help="recompute a past run's KPIs with the current KPI code")
    rescore.add_argument("run")
    rescore.set_defaults(handler=cmd_rescore)

    placement = commands.add_parser("placement", help="placement-only KPIs from pre-route scenes (works if routing failed)")
    placement.add_argument("runs", nargs="+", help="run labels or directories")
    placement.set_defaults(handler=cmd_placement)

    readable = commands.add_parser("readability", help="what a reader notices in a run's sheets, against human-drawn ones")
    readable.add_argument("runs", nargs="*", help="run labels or directories")
    readable.add_argument("--corpus", action="store_true", help="also print the band measured on KiCad's demo sheets")
    readable.add_argument("--refresh", action="store_true", help="measure the demo sheets again instead of using the cache")
    readable.set_defaults(handler=cmd_readability)

    demo_inputs = commands.add_parser("demos", help="KiCad demo circuits as engine inputs (netlist and functional blocks)")
    demo_inputs.add_argument("--write", action="store_true", help="regenerate scripts/schematic_demo_circuits.py")
    demo_inputs.set_defaults(handler=cmd_demos)

    corpused_data = commands.add_parser("corpus", help="the corpus circuits as engine fixtures (corpus_<key>)")
    corpused_data.add_argument("--write", action="store_true", help="regenerate scripts/schematic_corpus_circuits.py")
    corpused_data.set_defaults(handler=cmd_corpus)

    redrawn = commands.add_parser("redraw", help="the engine's redraw of KiCad demos beside the human originals")
    redrawn.add_argument("run", help="run label or directory")
    redrawn.add_argument("--blind", help="also write shuffled, unlabelled drawings (and key.json) to this directory")
    redrawn.set_defaults(handler=cmd_redraw)

    benched = commands.add_parser("bench", help="stacked demo drawings and count tables for one run, or two compared")
    benched.add_argument("run", help="run label or directory")
    benched.add_argument("--against", help="a second run to put beside the first")
    benched.add_argument("--out", help="output directory (default: <run>/bench)")
    benched.set_defaults(handler=cmd_bench)

    replayed_placement = commands.add_parser(
        "placement-replay", help="run the placement solvers of a recorded run again, offline (e11_placement_inputs overlay)",
    )
    replayed_placement.add_argument("run", help="run label or directory")
    replayed_placement.add_argument("--group", type=int, default=0, help="which recorded circuit (trial) of the run")
    replayed_placement.add_argument("--weights", help='cost weights to change, e.g. "bend=8,extent=0.1,jog=6"')
    replayed_placement.add_argument("--rounds", type=int, default=0, help="rounds of block alignment and refinement after the first stages")
    replayed_placement.add_argument("--stages", default="refine", help="the first stages: a comma list of seed, align, refine")
    replayed_placement.add_argument("--orient", type=int, default=0, help="give movable symbols of 3..N pins every orientation")
    replayed_placement.add_argument("--svg", help="draw the layout (bodies, pin tips, estimated routes) to this file")
    replayed_placement.add_argument("--label", default="", help="a caption for the drawing")
    replayed_placement.set_defaults(handler=cmd_placement_replay)

    laddered = commands.add_parser("ladder", help="the ladder placer beside the engine's placement for the demos of a recorded run")
    laddered.add_argument("--run", required=True, help="run label or directory (e11_placement_inputs overlay)")
    laddered.add_argument("--circuit", default="all", help="lm317, buck, opamp, amp (or the demo_ fixture name) or all")
    laddered.add_argument("--engine", action="store_true", help="also draw and count the engine's placement")
    laddered.add_argument("--out", help="output directory (default: probes/ladder/out)")
    laddered.add_argument("--pick", default="shipped", help="which matching recorded circuit: shipped (nearest the shipped sheet), last, or N")
    laddered.add_argument("--index", type=int, help="override: same as --pick N")
    laddered.set_defaults(handler=cmd_ladder)
    corpused = commands.add_parser("ladder-corpus", help="the ladder placer on human-drawn corpus sheets, beside the human drawing")
    corpused.add_argument("--circuit", default="all", help="one of the corpus circuits (see corpus.CIRCUITS), or all")
    corpused.add_argument("--out", help="output directory (default: probes/ladder_corpus/out)")
    corpused.set_defaults(handler=cmd_ladder_corpus)
    fixtured = commands.add_parser("ladder-fixtures", help="write one JSON fixture per circuit (plan, geometry, nets, baseline) for outside legalisers")
    fixtured.add_argument("--run", default="rec-1", help="run label or directory with the recorded demo circuits (default: rec-1)")
    fixtured.add_argument("--demos", default="all", help="comma list of lm317,buck,opamp,amp, or all, or none")
    fixtured.add_argument("--corpus", default="all", help="comma list of corpus circuits, or all, or none")
    fixtured.add_argument("--out", default="tests/fixtures/ladder", help="output directory (default: tests/fixtures/ladder)")
    fixtured.set_defaults(handler=cmd_ladder_fixtures)

    compare = commands.add_parser("compare", help="compare two runs (label or directory)")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.set_defaults(handler=cmd_compare)

    tabulate = commands.add_parser("table", help="headline KPIs of many runs, one row per fixture and run")
    tabulate.add_argument("runs", nargs="+")
    tabulate.add_argument("--fixtures", help="comma list to restrict the table")
    tabulate.set_defaults(handler=cmd_table)

    analyze = commands.add_parser("analyze-dumps", help="analyze libavoid dumps from a run")
    analyze.add_argument("run")
    analyze.set_defaults(handler=cmd_analyze_dumps)

    terminals = commands.add_parser("terminals", help="power-terminal decisions of a run (needs the e5_trace overlay)")
    terminals.add_argument("run")
    terminals.set_defaults(handler=cmd_terminals)

    replayer = commands.add_parser("replay", help="re-route recorded libavoid calls under router variants")
    replayer.add_argument("run", nargs="?", help="run label/path, or a directory of libavoid-*.json dumps")
    replayer.add_argument("--variants", default="asis,nudge_off,plain,ports,ports_plain",
                          help="comma list or 'all' (see --list); 'asis' is always included as the control")
    replayer.add_argument("--list", action="store_true", help="list the variants")
    replayer.add_argument("--calibrate", action="store_true",
                          help="score the lab's asis verdict against what the engine recorded (no router needed)")
    replayer.set_defaults(handler=cmd_replay)

    selftest = commands.add_parser("selftest", help="run the sandbox's own unit tests")
    selftest.set_defaults(handler=cmd_selftest)

    clean = commands.add_parser("clean", help="delete sandbox data")
    clean.add_argument("--all", action="store_true", help="also delete the virtual environment and node modules")
    clean.add_argument("--yes", action="store_true")
    clean.set_defaults(handler=cmd_clean)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
