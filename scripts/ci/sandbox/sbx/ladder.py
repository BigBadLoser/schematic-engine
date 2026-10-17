"""The ladder placer, run offline on the circuits of a recorded run and set beside the engine's own placement.

The recorded run (overlay ``e11_placement_inputs``) holds the arguments the engine gave its refinement for each demo. This
module finds a demo's circuit among them, asks ``ladder_roles.classify`` and ``ladder_layout.ladder_layout`` where the
parts would go (when those modules exist), and judges both layouts by the placement model's own counts and pictures. Where
the ladder modules are absent it runs in stand-in mode: the engine's placement is returned unchanged, so the harness can
be checked before the modules land.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from . import env, placement_replay, redraw

if str(env.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(env.REPO_ROOT))
from backend.schematic import placement_refinement as solvers  # noqa: E402
from backend.schematic import kicad_geometry as kg  # noqa: E402
from backend.schematic.orientation import MIRRORS, ROTATIONS, Orientation  # noqa: E402
from backend.schematic.kicad_geometry import text_width_mm  # noqa: E402
from scripts.schematic_validation_fixtures import build_fixture  # noqa: E402

CIRCUITS = {"lm317": "demo_lm317", "buck": "demo_buck", "opamp": "demo_opamp", "amp": "demo_ampac"}
FIELD_SIZE_MM = 1.27           # KiCad's default text size, which the sheets' reference and value fields use
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"   # where the attachments probe finds it
STAND_IN = "stand-in: ladder modules not present, engine placement used"
# Where the ladder's positions come from. Default: the heuristic placer (``ladder_layout``). ``KICAD_AI_LADDER_PLACEMENT``
# names a placement file (see ``backend.schematic.ladder_check``), or a folder holding ``<circuit>.json`` per circuit, whose
# positions are taken as they are (an outside legaliser's output, judged in this harness and built on real sheets).
# ``KICAD_AI_LADDER_SOLVER=cpsat`` runs grammar -> plan -> ``ladder_solve.legalise`` instead.
PLACEMENT_ENV = "KICAD_AI_LADDER_PLACEMENT"
SOLVER_ENV = "KICAD_AI_LADDER_SOLVER"
SEEDS_ENV = "KICAD_AI_LADDER_SEEDS"          # "7,11,13,17,19": the solver runs once per seed, in processes, and the cheapest placement stands
HINT_ENV = "KICAD_AI_LADDER_HINT"          # "heuristic": the solver starts from the heuristic placer's layout of the same plan (off: worse on amp and uno_r4_power)


@dataclass
class LadderResult:
    symbols: list[Any]
    roles: Any
    note: str
    wires: Any = None       # the wires the solver (or a placement file) reports having drawn, per net; None for the heuristic


@dataclass
class Counts:
    label: str
    through: int
    overlaps: int
    clearance: int
    bends: float
    jogs: int
    crossings: int
    wire_mm: float
    extent: tuple[float, float]
    total: float
    shorts: int = 0
    short_notes: tuple = ()


def fixture_name(circuit: str) -> str:
    """The fixture key for a CLI name in either form (``buck`` or ``demo_buck``)."""
    if circuit in CIRCUITS:
        return CIRCUITS[circuit]
    if circuit in CIRCUITS.values():
        return circuit
    raise ValueError(f"unknown circuit {circuit!r}; one of {sorted(CIRCUITS)} or {sorted(CIRCUITS.values())}")


def _recorded_references(circuit: dict[str, Any]) -> set[str]:
    return {s.reference for s in circuit["refine"]["args"][0] if not s.reference.startswith("#")}


def _intent_references(intent: Any) -> set[str]:
    return {s.reference for s in intent.symbols if not s.authoring_auxiliary and not s.reference.startswith("#")}


def sheet_anchors(path: Path) -> dict[str, tuple[float, float]]:
    """Reference -> anchor (mm) of each placed symbol of a KiCad sheet, without the power flags (``#`` references)."""
    doc = kg.parse_kicad_sexpr(Path(path).read_text(encoding="utf-8"))
    anchors = {}
    for symbol in kg._children(doc, "symbol"):
        at, ref = kg._one(symbol, "at"), kg._property(symbol, "Reference")
        if at and ref is not None and not str(ref[2]).startswith("#"):
            anchors[str(ref[2])] = (float(at[1]), float(at[2]))
    return anchors


def anchor_distance(symbols: list[Any], anchors: dict[str, tuple[float, float]]) -> float:
    """Sum of |dx|+|dy| (mm) between the symbols' anchors and the sheet's, over the references both have."""
    return sum(
        abs(s.position[0] - anchors[s.reference][0]) + abs(s.position[1] - anchors[s.reference][1])
        for s in symbols if s.reference in anchors and not s.reference.startswith("#")
    )


def _distance(job: tuple[dict[str, Any], dict[str, tuple[float, float]]]) -> float:
    circuit, anchors = job
    return anchor_distance(engine_pose(circuit), anchors)


def _key(circuit: dict[str, Any]) -> tuple:
    """What a refinement's result depends on: the symbols' poses and nets as recorded (trials repeat each other)."""
    symbols, nets = circuit["refine"]["args"][:2]
    return (
        tuple((s.reference, s.position, s.orientation) for s in symbols),
        tuple((n.net, tuple(n.pins)) for n in nets), repr(sorted(circuit["refine"]["kwargs"].items(), key=lambda i: i[0]))[:2000],
    )


def _has_main_file() -> bool:
    """Whether worker processes can re-import the main module (on Windows they do): not so under ``python -`` or a REPL."""
    main = sys.argv[0] if sys.argv else ""
    return main.endswith(".py") and Path(main).is_file()


def pick_circuit(matches: list[dict[str, Any]], anchors: dict[str, tuple[float, float]], workers: int = 1) -> tuple[int, float]:
    """The index of the match whose refined placement lies closest to the shipped sheet, and that distance (0: exact).

    The engine builds a demo in many trials and ships the one its quality gate selected, which is not the last built.
    Refinement takes up to minutes on the big circuits, so identical recorded inputs are refined once and, with
    ``workers`` above 1, the rest in parallel processes. Ties go to the lowest index."""
    firsts: dict[tuple, int] = {}
    for index, circuit in enumerate(matches):
        firsts.setdefault(_key(circuit), index)
    unique = sorted(firsts.values())
    jobs = [(matches[i], anchors) for i in unique]
    if workers > 1 and len(jobs) > 1 and _has_main_file():
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            distances = list(pool.map(_distance, jobs))
    else:
        distances = [_distance(job) for job in jobs]
    distance, index = min(zip(distances, unique))
    return index, distance


def find_circuit(run_dir: Path, fixture: str, pick: str | int | None = "shipped") -> tuple[dict[str, Any], int, int, float | None]:
    """The recorded circuit whose references are the fixture's, how many match, the index used and, for ``shipped``, its
    distance (mm) to the run's shipped sheet. ``pick`` is ``shipped``, ``last`` or an index."""
    wanted = _intent_references(build_fixture(fixture))
    circuits = placement_replay.load_circuits(run_dir)
    matches = [c for c in circuits if _recorded_references(c) == wanted]
    if not matches:
        sets = sorted({frozenset(_recorded_references(c)) for c in circuits}, key=lambda s: (len(s ^ wanted), sorted(s)))[:3]
        shown = "; ".join(f"{sorted(s)} (differs by {sorted(s ^ wanted)})" for s in sets)
        raise LookupError(f"no recorded circuit in {run_dir.name} matches {fixture} {sorted(wanted)}; closest: {shown}")
    distance = None
    if pick in (None, "last"):
        index = len(matches) - 1
    elif pick == "shipped":
        sheet = Path(run_dir) / "artifacts" / f"schematic-engine-{fixture}" / "mcu_smoke.kicad_sch"
        if not sheet.is_file():
            raise FileNotFoundError(f"no shipped sheet {sheet} to pick {fixture} by; use --pick last or an index")
        index, distance = pick_circuit(matches, sheet_anchors(sheet), os.cpu_count() or 1)
    else:
        index = int(pick)
    return matches[index], len(matches), index, distance


def pin_counts(symbols: list[Any]) -> dict[str, int]:
    """Pins per symbol from the geometry it currently has (the pin set is the same in every orientation)."""
    return {s.reference: len(s.geometry[s.orientation].pins) for s in symbols}


def text_widths(intent: Any) -> dict[str, float]:
    """Per symbol the wider of its reference and value text at the default field size: the room its labels need."""
    return {
        s.reference: max(text_width_mm(s.reference, FIELD_SIZE_MM), text_width_mm(s.value, FIELD_SIZE_MM)) for s in intent.symbols
    }


def _ladder_functions() -> tuple[Any, Any] | None:
    try:
        roles = importlib.import_module("backend.schematic.ladder_roles")
        layout = importlib.import_module("backend.schematic.ladder_layout")
        return roles.classify, layout.ladder_layout
    except ImportError:
        return None


def all_orientations(symbols: list[Any]) -> list[Any]:
    """Every symbol except the ``#`` flags with every orientation the ladder may choose from.

    A recorded symbol holds only the orientations the engine allowed at that call. The geometry it has now is brought back
    to upright (a rotation is undone by the opposite rotation; a mirrored one is its own inverse), then turned into the four
    rotations unmirrored and, for a part of three or more pins, the mirrored ones too, as ``placement_plan`` offers them."""
    widened = []
    for symbol in symbols:
        if symbol.reference.startswith("#"):
            widened.append(symbol)
            continue
        current = symbol.orientation
        turn = current.rotation if current.mirror else (360 - current.rotation) % 360
        upright = solvers.rotate_geometry(symbol.geometry[current], turn, current.mirror)
        mirrors = MIRRORS if len(upright.pins) >= 3 else ("",)
        geometry = {Orientation(r, m): solvers.rotate_geometry(upright, r, m) for r in ROTATIONS for m in mirrors}
        widened.append(replace(symbol, geometry=geometry))
    return widened


def roles_for(symbols: list[Any], intent: Any) -> Any:
    """The roles ``classify`` finds for the circuit, or None where the ladder modules are absent."""
    functions = _ladder_functions()
    return None if functions is None else functions[0](intent, pin_counts(symbols))


def placement_file(name: str = "") -> Path | None:
    """The placement file ``KICAD_AI_LADDER_PLACEMENT`` names for a circuit: the file itself, or ``<name>.json`` in a folder
    (the name as given, else without its ``demo_``/``corpus_`` prefix). None when the variable is not set."""
    value = os.environ.get(PLACEMENT_ENV)
    if not value:
        return None
    path = Path(value)
    if not path.is_dir():
        if not path.is_file():
            raise FileNotFoundError(f"{PLACEMENT_ENV}={value}: no such file")
        return path
    stems = list(dict.fromkeys(stem for stem in (name, re.sub(r"^(demo|corpus)_", "", name)) if stem))
    for stem in stems:
        if (path / f"{stem}.json").is_file():
            return path / f"{stem}.json"
    raise FileNotFoundError(f"{PLACEMENT_ENV}={value}: no {' or '.join(f'{s}.json' for s in stems) or '<circuit>.json'} in it")


def _placements(widened: list[Any], nets: list[Any], intent: Any, roles: Any,
                name: str) -> tuple[dict[str, Any], str, Any]:
    """Where the ladder puts the (widened) symbols, a note on where that came from (the heuristic placer, a placement file
    (``KICAD_AI_LADDER_PLACEMENT``) or the in-repo solver (``KICAD_AI_LADDER_SOLVER=cpsat``)), and the wires that source
    reports having drawn (``Placements.wires``; a placement file's, if it records them; None for the heuristic)."""
    file = placement_file(name)
    solver = os.environ.get(SOLVER_ENV, "")
    if file is not None and solver:
        raise ValueError(f"{PLACEMENT_ENV} and {SOLVER_ENV} are both set; use one")
    if file is not None:
        from backend.schematic import ladder_fixture
        placements = ladder_fixture.load_placement(file)
        known = {s.reference: s for s in widened}
        for ref, place in sorted(placements.items()):
            if ref not in known:
                raise ValueError(f"{file.name}: places {ref}, which the circuit does not have")
            if place.orientation not in known[ref].geometry:
                raise ValueError(f"{file.name}: {ref} has no orientation {place.orientation.rotation}{place.orientation.mirror or '-'}")
        from backend.schematic import ladder_fixture as fixtures
        return placements, f"placement from {file.name}", fixtures.load_wires(file) or None
    if solver:
        if solver != "cpsat":
            raise ValueError(f"{SOLVER_ENV}={solver!r}: only 'cpsat' is known")
        try:
            ladder_solve = importlib.import_module("backend.schematic.ladder_solve")
        except ImportError as error:
            raise RuntimeError(f"{SOLVER_ENV}=cpsat needs backend/schematic/ladder_solve.py: {error}") from error
        from backend.schematic.ladder_plan import plan_and_layout
        # partial: a hung part the heuristic finds no slot for stays in the plan (the solver places it)
        plan, heuristic = plan_and_layout(widened, nets, roles, text_widths=text_widths(intent), partial=True)
        # the heuristic placer's layout of the same plan can be the solver's starting hint (never a constraint)
        hint = heuristic if os.environ.get(HINT_ENV, "") == "heuristic" and heuristic else None
        seeds = tuple(int(v) for v in os.environ.get(SEEDS_ENV, "").replace(",", " ").split()) or None
        result = ladder_solve.legalise_with_status(plan, widened, nets, hint=hint, seeds=seeds)
        # The solver's own clocks (model build, feasibility, optimisation; its deterministic time), so the note tells
        # the solve's cost even when the harness runs several placements at once in one process.
        timing = getattr(result.placements, "timing", None) or {}
        clocks = ", ".join(f"{k} {v}s" for k, v in timing.items())
        deterministic = getattr(result.placements, "deterministic_time", None)
        if deterministic is not None:
            clocks += f"; deterministic {deterministic}"
        # an ``other`` part the plan lists in a row stands beside the part it serves: the caller leaves it there
        try:
            result.placements.beside = frozenset(ref for row in plan.rows if row.kind == "chain" for ref in row.order) & set(roles.other)
        except AttributeError:
            pass
        return (result.placements, f"solver cpsat: {result.status}" + (f" ({clocks})" if clocks else ""),
                getattr(result.placements, "wires", None) or None)
    layout = _ladder_functions()[1]
    try:
        return layout(widened, nets, roles, text_widths=text_widths(intent)), "ladder", None
    except ValueError as error:
        if "No attachment slot" not in str(error):
            raise
    # a part the heuristic finds no slot for is left out of the placement (the caller leaves it with the ``other`` parts)
    from backend.schematic.ladder_plan import LayoutRecord
    return layout(widened, nets, roles, text_widths=text_widths(intent), record=LayoutRecord(partial=True)), "ladder", None


def _layout(symbols: list[Any], nets: list[Any], intent: Any, roles: Any,
            name: str = "") -> tuple[list[Any], str, Any, list[str], frozenset]:
    """``layout_step`` and the references the placement left out (a part a partial plan found no slot for), which the caller
    leaves where they are like the parts the roles call ``other``."""
    widened = all_orientations(symbols)
    placements, note, wires = _placements(widened, nets, intent, roles, name)
    moved, left_out = [], []
    # The widened symbols are returned, not the recorded ones: the orientation the ladder chose must be in the
    # symbol's geometry for the placement model to score it.
    for symbol in widened:
        place = placements.get(symbol.reference)
        moved.append(replace(symbol, position=place.position, orientation=place.orientation) if place else symbol)
        if place is None and not symbol.reference.startswith("#") and symbol.reference not in (roles.other if roles else ()):
            left_out.append(symbol.reference)
    return moved, note, wires, left_out, frozenset(getattr(placements, "beside", ()))


def layout_step(symbols: list[Any], nets: list[Any], intent: Any, roles: Any,
                name: str = "") -> tuple[list[Any], str, Any]:
    """``ladder_layout_step``, the note on where the positions came from (``ladder`` for the heuristic placer), and the wires
    the source drew (see ``_placements``), which ``describe`` scores in place of the plan's re-derivation of those nets."""
    return _layout(symbols, nets, intent, roles, name)[:3]


def ladder_layout_step(symbols: list[Any], nets: list[Any], intent: Any, roles: Any, name: str = "") -> list[Any]:
    """The symbols as ``ladder_layout`` places them, given every orientation to choose from (or as the placement file or
    solver the environment names does: see ``PLACEMENT_ENV``, ``SOLVER_ENV``)."""
    return layout_step(symbols, nets, intent, roles, name)[0]


def ladder_pose(symbols: list[Any], nets: list[Any], intent: Any, name: str = "") -> LadderResult:
    """The symbols as the ladder placer puts them; unchanged, with a stand-in note, when its modules are absent."""
    if _ladder_functions() is None:
        return LadderResult(list(symbols), None, STAND_IN)
    intent = expand_for(symbols, intent)
    try:
        roles = roles_for(symbols, intent)
        moved, note, wires, left_out, beside = _layout(symbols, nets, intent, roles, name)
        # a part the layout left out stands with the ``other`` parts, below the placed ones; a unit placeable the layout did place
        # (the power unit of an op-amp, in the supply column) stays where the layout put it
        from backend.schematic.designators import split_unit
        placed_units = {r for r in roles.other if (split_unit(r)[1] is not None or r in beside) and r not in left_out}
        if left_out or placed_units:
            other = tuple(sorted({*roles.other, *left_out} - placed_units))
            try:
                roles = replace(roles, other=other)
            except TypeError:          # roles that are not a dataclass (a stand-in): the same fields with the new ``other``
                roles = SimpleNamespace(**{**vars(roles), "other": other})
    except Exception as error:
        raise type(error)(f"{name or 'circuit'}: {error}") from error
    return LadderResult(moved, roles, note, wires)


def others_column(symbols: list[Any], roles: Any, gap: float = 5.08, nets: Any = ()) -> list[Any]:
    """The parts the ladder leaves where they are (``roles.other``) stand in a row below everything placed, three gaps under
    the lowest box, so that a wire to one of them reaches it from above along the free space under the last row and passes no
    neighbour.

    A circuit read from a netlist has no engine pose for them (they all sit at the origin), so each takes the next place
    along the row, under the placed parts it connects to (their mean x, when ``nets`` says which), left to right, boxes two
    gaps apart."""
    leftovers = set(getattr(roles, "other", ()) or ())
    column = [s for s in symbols if s.reference in leftovers]
    fixed = [s for s in symbols if s.reference not in leftovers and not s.reference.startswith("#")]
    if not column:
        return list(symbols)

    def box(symbol: Any) -> tuple[float, float, float, float]:
        left, top, right, bottom = symbol.geometry[symbol.orientation].box
        return (symbol.position[0] + left, symbol.position[1] + top, symbol.position[0] + right, symbol.position[1] + bottom)

    boxes = [box(s) for s in fixed]
    edge = max((b[3] for b in boxes), default=0.0) + 3 * gap
    cursor = min((b[0] for b in boxes), default=0.0)
    centre = {s.reference: (box(s)[0] + box(s)[2]) / 2 for s in fixed}
    wanted: dict[str, float] = {}
    for symbol in column:
        xs = [centre[ref] for net in nets if any(r == symbol.reference for r, _p in net.pins)
              for ref, _pin in net.pins if ref in centre]
        wanted[symbol.reference] = sum(xs) / len(xs) if xs else cursor
    moved = {}
    for symbol in sorted(column, key=lambda s: (wanted[s.reference], s.reference)):
        left, top, right, bottom = symbol.geometry[symbol.orientation].box
        x = max(cursor, wanted[symbol.reference] - (right - left) / 2)
        moved[symbol.reference] = replace(symbol, position=(x - left, edge - top))
        cursor = x + (right - left) + 2 * gap
    return [moved.get(s.reference, s) for s in symbols]


def engine_pose(circuit: dict[str, Any]) -> list[Any]:
    """The engine's placement of the circuit: its recorded refinement run again, which is deterministic."""
    return placement_replay.replay(circuit, stages=("refine",)).symbols


def _model_inputs(circuit: dict[str, Any]) -> tuple[Any, Any]:
    kwargs = circuit["refine"]["kwargs"]
    return kwargs.get("reach") or {}, kwargs.get("half_widths")


SCORING_ENV = "KICAD_AI_LADDER_SCORING"      # "estimate": score a ladder row on the router estimate, not on its plan's wires


def describe(label: str, symbols: list[Any], circuit: dict[str, Any], plan: Any = None, wires: Any = None) -> Counts:
    """The placement model's counts for a pose. Through, overlaps and clearance are the number of notes ``cost`` lists
    for them (``through``, ``overlap``, ``clearance``); a kind it does not list counts as 0.

    With a ``plan`` the nets it wires are scored on the plan's own wires (``ladder_plan.plan_routes``), the intended wiring
    the router draws, and not on an estimate of them; ``KICAD_AI_LADDER_SCORING=estimate`` scores the estimate. ``wires``
    (what the solver reports having drawn, ``Placements.wires``) replace the plan's re-derivation of those nets, and the
    nets the plan leaves out are attached to them, as ``ladder_check.counts`` does: the numbers are the same."""
    reach, half_widths = _model_inputs(circuit)
    nets = circuit["refine"]["args"][1]
    row, model = placement_replay._describe(label, symbols, nets, reach, half_widths, solvers.Weights())
    found: dict[str, list] = {}
    routes = None
    if plan is not None and os.environ.get(SCORING_ENV, "") != "estimate":
        from backend.schematic.ladder_plan import attach_unplanned, plan_routes
        routes = attach_unplanned(plan, symbols, nets, {**plan_routes(plan, symbols, nets), **(wires or {})})
    cost = model.cost({s.reference: (s.position, s.orientation) for s in symbols}, found=found, routes=routes)
    if routes is not None:
        row = replace(row, total=cost.total, length=cost.length, bends=cost.bends, jogs=cost.jogs, crossings=cost.crossings)
    return Counts(
        label, len(found.get("through", [])), len(found.get("overlap", [])), len(found.get("clearance", [])),
        row.bends, row.jogs, row.crossings, row.length, row.extent, row.total,
        len(found.get("short", [])), tuple((k, *n) for k in ("short", "crossing") for n in found.get(k, [])),
    )


def plan_of(symbols: list[Any], nets: list[Any], roles: Any, intent: Any) -> Any:
    """The plan the heuristic placer follows for these symbols, or None when its modules are absent or it finds none."""
    try:
        from backend.schematic.ladder_plan import plan_and_layout
        return plan_and_layout(all_orientations(symbols), nets, roles, text_widths=text_widths(intent), partial=True)[0]
    except Exception:
        return None


def roles_summary(roles: Any) -> str:
    """Chains per block in flow order, then attachments, rungs, feedback, pairs and the rest, a line each."""
    if roles is None:
        return "roles: (stand-in)"
    lines = [f"chain {block}: " + " -> ".join(parts) for block, parts in sorted(roles.chains.items())]
    lines += [f"attachment {part}: -> {a.host}/{a.net}/{a.rail}" for part, a in sorted(roles.attachments.items())]
    lines += [f"rung {part}: {r.net_a} - {r.net_b}" for part, r in sorted(roles.rungs.items())]
    lines += [f"feedback {part}: {f.net_from} -> {f.net_to}" for part, f in sorted(roles.feedback.items())]
    lines += [f"pair: {a} {b}" for a, b in roles.pairs]
    lines.append("other: " + (", ".join(roles.other) or "-"))
    return "\n".join(lines)


def picture(symbols: list[Any], nets: list[Any], circuit: dict[str, Any], label: str) -> str:
    """The pose drawn the way the placement sees it (bodies, pin tips, estimated routes)."""
    reach, half_widths = _model_inputs(circuit)
    _row, model = placement_replay._describe(label, symbols, nets, reach, half_widths, solvers.Weights())
    return placement_replay.svg(placement_replay.Replay(nets=nets, symbols=symbols, model=model), label)


def _size(svg_text: str) -> tuple[int, int]:
    width, height = re.search(r'width="(\d+)" height="(\d+)"', svg_text).groups()
    return int(width), int(height)


def both_page(engine_svg: str, ladder_svg: str) -> str:
    """The engine's drawing over the ladder's, on one page."""
    return redraw.stacked_page({"A engine": (engine_svg, _size(engine_svg)), "B ladder": (ladder_svg, _size(ladder_svg))})


def png_writer() -> str:
    """Which PNG writer ``to_png`` will use here: cairosvg, chrome, or none."""
    try:
        import cairosvg  # noqa: F401
        return "cairosvg"
    except ImportError:
        return "chrome" if Path(CHROME).exists() else "none"


def _svg_in(page_text: str) -> str:
    """The widest ``<svg ...>`` start tag of a stacked page: its width/height size the window; heights are summed."""
    tags = re.findall(r'<svg[^>]*width="(\d+)" height="(\d+)"', page_text)
    width = max(int(w) for w, _h in tags)
    height = sum(int(h) + 90 for _w, h in tags)
    return f'<svg width="{width + 40}" height="{height}">'


def to_png(svg_text: str, path: Path, *, page: bool = False) -> bool:
    """Write the drawing as a PNG with cairosvg, else headless Chrome; False (nothing written) when neither is there.

    ``page`` marks HTML (``both_page``), which only Chrome can render."""
    path = Path(path)
    writer = png_writer()
    if writer == "none" or (page and not Path(CHROME).exists()):
        return False
    if writer == "cairosvg" and not page:
        import cairosvg
        cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), write_to=str(path))
        return True
    source = path.with_suffix(".html" if page else ".svg")
    source.write_text(svg_text, encoding="utf-8")
    width, height = _size(_svg_in(svg_text)) if page else _size(svg_text)
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", f"--screenshot={path.resolve()}", f"--window-size={width},{height}",
         source.resolve().as_uri()], capture_output=True, timeout=60, check=False,
    )
    if page:
        source.unlink()
    return path.exists()


def table(name: str, rows: list[Counts], caption: str = "") -> str:
    lines = [
        f"### {name}" + (f" ({caption})" if caption else ""), "",
        "| placement | through | overlaps | clearance | bends | jogs | crossings | short | wire mm | extent mm | cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.label} | {r.through} | {r.overlaps} | {r.clearance} | {r.bends:.1f} | {r.jogs} | {r.crossings} | {r.shorts} | "
            f"{r.wire_mm:.1f} | {r.extent[0]:.1f} x {r.extent[1]:.1f} | {r.total:.1f} |",
        )
    return "\n".join(lines)


def _write(svg_text: str, out_dir: Path, stem: str) -> None:
    (out_dir / f"{stem}.svg").write_text(svg_text, encoding="utf-8")
    to_png(svg_text, out_dir / f"{stem}.png")


def run(run_dir: Path, circuits: list[str], *, engine: bool, out_dir: Path, pick: str | int | None = "shipped") -> str:
    """Per circuit the ladder drawing (and with ``engine`` the engine's and both stacked), the roles and a counts table."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sections = []
    completed = 0
    for name in circuits:
        fixture = fixture_name(name)
        circuit, matches, chosen, distance = find_circuit(run_dir, fixture, pick)
        intent = build_fixture(fixture)
        nets = circuit["refine"]["args"][1]
        base = engine_pose(circuit)
        engine_svg = None
        if engine:
            engine_svg = picture(base, nets, circuit, f"{name} engine")
            _write(engine_svg, out_dir, f"{name}_engine")
        error = None
        roles = None
        try:
            result = ladder_pose(base, nets, intent, name)
            roles = result.roles
        except Exception as caught:
            error = caught
            result = None
            try:
                roles = roles_for(base, intent)      # the roles are worth seeing even when the layout failed
            except Exception:
                roles = None
        rows = []
        if engine:
            rows.append(describe("engine", base, circuit))
        note = f"ladder: error: {error}" if error else result.note
        if result is not None:
            ladder_svg = picture(result.symbols, nets, circuit, f"{name} ladder ({result.note})")
            _write(ladder_svg, out_dir, f"{name}_ladder")
            rows.append(describe("ladder", result.symbols, circuit, plan_of(base, nets, roles, intent), result.wires))
            if engine:
                to_png(both_page(engine_svg, ladder_svg), out_dir / f"{name}_both.png", page=True)
            completed += 1
        shown = "" if distance is None else f", {distance:.1f} mm from the shipped sheet"
        caption = result.note if result is not None and result.note != "ladder" else ""
        table_text = table(name, rows, caption) if rows else f"### {name}\n"
        if error:
            table_text += f"\n| ladder: error: {error} |"
        for r in rows:
            for n in r.short_notes:
                table_text += f"\n\nshort ({r.label}): {n}"
        text = (
            f"{table_text}\n\n{len(base)} symbols; {matches} matching recorded circuits (used #{chosen}{shown}); "
            f"{note}\n\n```\n{roles_summary(roles)}\n```"
        )
        print(text, flush=True)
        sections.append(text)
    summary = "\n\n".join(sections) + f"\n\nPNG writer: {png_writer()}\n"
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    if not completed:
        raise ValueError("no circuit completed; see summary.md")
    return summary


# ---------------------------------------------------------------------------------------------------------------------
# an intent with no engine run: geometry from the MCP server, the nets from the placement plan

def unit_pins_of(symbols: list[Any]) -> dict[str, dict[int, set[str]]]:
    """The pins each unit placeable (``U5:2``) in ``symbols`` draws, by part and unit."""
    from backend.schematic.designators import split_unit

    found: dict[str, dict[int, set[str]]] = {}
    for symbol in symbols:
        part, unit = split_unit(symbol.reference)
        if unit is not None:
            found.setdefault(part, {})[unit] = set(symbol.geometry[symbol.orientation].pins)
    return found


def expand_for(symbols: list[Any], intent: Any) -> Any:
    """The intent with every multi-unit part the ``symbols`` hold as units (``U5:1``, ``U5:2``) turned into one symbol per
    unit (``intent.expand_units``): what the roles, the plan and the layout work on. An intent that has none is returned as it is."""
    from backend.schematic.intent import expand_units

    return expand_units(intent, unit_pins_of(symbols))


def symbols_and_nets(intent: Any, payload: dict[str, Any]) -> tuple[list[Any], list[Any], dict, dict]:
    """The placed symbols (every orientation the server described, mirrors only for parts of three or more pins, all at the
    origin: where the human put them is not an input), the connections, and the room in front of each pin, as
    ``placement_plan`` derives them from an intent."""
    from backend.schematic import placement_plan as plan

    references = [s.reference for s in intent.symbols if not s.authoring_auxiliary]
    missing = [r for r in references if r not in payload["symbols"]]
    if missing:
        raise LookupError(f"the server described no geometry for {missing}")
    for reference in references:
        entry = payload["symbols"][reference]
        if len(entry["orientations"][0]["pins"]) < 3:
            entry["orientations"] = [o for o in entry["orientations"] if not o["mirror"]]
    described = {"symbols": {r: {**payload["symbols"][r], "x": 0.0, "y": 0.0} for r in references}}
    symbols = plan.symbols_from_payload(described, planned={}, movable=references)
    labelled = lambda net: net.routing == "label"  # noqa: E731
    nets = plan.connections(intent, labelled)
    pins = {(s.reference, pin) for s in symbols for pin in s.geometry[s.orientation].pins}
    absent = sorted({(r, p) for c in nets for r, p in c.pins if (r, p) not in pins})
    if absent:
        raise LookupError(f"pins the netlist joins but the geometry lacks: {absent}")
    return symbols, nets, plan.pin_reach(intent, labelled), plan.pin_half_widths(intent, labelled)


def corpus_inputs(entry: Any, built: Any, work_dir: Path, client: Any = None) -> tuple[list[Any], list[Any], dict, dict]:
    """Geometry for a corpus circuit from the human sheet's own symbols, read by the extended server in this process (a
    temporary project: the sheet, with a ``sym-lib-table`` for the libraries KiCad does not carry; see ``corpus``)."""
    import json
    from . import corpus

    sheet = corpus.sheet_file(entry)
    copy, units, _where = corpus.prepare_project(sheet, Path(work_dir), env.find_symbol_dir(env.find_kicad_cli()))
    references = [s.reference for s in built.intent.symbols]
    # a multi-unit part is asked for unit by unit (``U5:1``, ``U5:2``): each is a placeable of its own (spec 4c)
    asked = {reference: [f"{reference}:{unit}" for unit in units[reference]] if reference in units else [reference]
             for reference in references}
    wanted = {
        name: [{"rotation": r, "mirror": m} for r in ROTATIONS for m in MIRRORS]
        for names in asked.values() for name in names
    }
    if client is None:
        from backend.kicad.mcp_inprocess import McpInProcessClient
        client = McpInProcessClient()
    # The engine measures parts with the tight clearance (body and pin tips plus 1.27 mm); the server's default is its
    # legacy 20.32 x 15.24 mm box, which made every corpus capacitor a body wider than its own pins.
    from backend.schematic.layout_iteration import TIGHT_MARGIN_MM
    client.call_schematic_write_tool(
        str(work_dir), sch_file=str(copy), name="sch_set_layout_clearances",
        arguments={"mode": "tight", "default_mm": TIGHT_MARGIN_MM, "by_reference": {}, "tight_terminals": True},
    )
    result = client.call_schematic_read_tool(
        str(work_dir), sch_file=str(copy), name="sch_symbol_geometry",
        arguments={"references": list(wanted), "orientations": wanted},
    )
    payload = json.loads(result["result"])
    from backend.schematic.intent import expand_units
    unit_pins = {
        reference: {int(name.rpartition(":")[2]): set(payload["symbols"][name]["orientations"][0]["pins"]) for name in names}
        for reference, names in asked.items() if reference in units
    }
    return symbols_and_nets(expand_units(built.intent, unit_pins), payload)


_WIRE = re.compile(r"\(wire\s*\(pts\s*\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s*\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def human_wire_mm(sheet_text: str) -> float:
    """Total length of the wire segments a sheet draws."""
    return sum(abs(float(a) - float(c)) + abs(float(b) - float(d)) for a, b, c, d in _WIRE.findall(sheet_text))


def run_corpus(names: list[str], out_dir: Path) -> str:
    """Per corpus circuit: the ladder drawing, the human sheet and both stacked, and the counts of each."""
    import tempfile
    from . import corpus, readability

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = None
    sections, completed = [], 0
    for name in names:
        entry = corpus.CIRCUITS[name]
        sheet = corpus.sheet_file(entry)
        text = f"### {name}\n\n{entry.summary}\n"
        try:
            built = corpus.build(entry)
            if client is None:
                from backend.kicad.mcp_inprocess import McpInProcessClient
                client = McpInProcessClient()
            with tempfile.TemporaryDirectory() as tmp:
                symbols, nets, reach, half = corpus_inputs(entry, built, Path(tmp), client)
            pseudo = {"refine": {"args": (symbols, nets), "kwargs": {"reach": reach, "half_widths": half}}}
            result = ladder_pose(symbols, nets, built.intent, name)
            result.symbols = others_column(result.symbols, result.roles, nets=nets)
            counts = describe("ladder", result.symbols, pseudo)
            caption = result.note if result.note != "ladder" else ""
            ladder_svg = picture(result.symbols, nets, pseudo, f"{name} ladder")
            _write(ladder_svg, out_dir, f"{name}_ladder")
            human_svg, human_size = redraw.draw(sheet)
            measured = readability.measure_sheet(sheet).measures
            to_png(
                redraw.stacked_page({"A human": (human_svg, human_size), "B ladder": (ladder_svg, _size(ladder_svg))}),
                out_dir / f"{name}_both.png", page=True,
            )
            wire = human_wire_mm(sheet.read_text(encoding="utf-8"))
            corners = measured["connection_corners"] / measured["connections"] if measured.get("connections") else float("nan")
            text += (
                f"\n{table(name, [counts], caption)}\n\nhuman sheet: wire {wire:.1f} mm, {measured['connection_corners']} corners over "
                f"{measured['connections']} connections ({corners:.2f} per connection), {measured['crossings']} crossings; "
                f"ladder: {counts.bends:.1f} bends over {len(nets)} nets\n\n"
                f"{len(symbols)} symbols, {len(built.intent.blocks)} blocks, rails {built.rails or 'none inferred'}\n\n"
                f"```\n{roles_summary(result.roles)}\n```"
            )
            completed += 1
        except Exception as error:
            text += f"\nerror: {type(error).__name__}: {error}\n"
        print(text, flush=True)
        sections.append(text)
    summary = "\n\n".join(sections) + f"\n\nPNG writer: {png_writer()}\n"
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    if not completed:
        raise ValueError("no circuit completed; see summary.md")
    return summary


# ---------------------------------------------------------------------------------------------------------------------
# fixtures: a circuit's placement problem as one JSON file (backend/schematic/ladder_fixture.py), for legalisers built elsewhere

FIXTURE_PICKS = {"lm317": 5, "buck": 7, "opamp": 11, "amp": 5}      # the recorded trials of rec-1 that reproduce its shipped sheets
FIXTURE_LIMIT_BYTES = 300 * 1024


def fixture_document(
    name: str, symbols: list[Any], nets: list[Any], intent: Any, reach: Any, half_widths: Any, *, others: str, source: str,
) -> dict[str, Any]:
    """The fixture document for a circuit: the plan the heuristic placer follows, every symbol in every orientation, the nets,
    and the heuristic's placement as the baseline. Raises what ``ladder_layout`` raises when it cannot place the circuit."""
    from backend.schematic import ladder_fixture
    from backend.schematic.ladder_plan import plan_and_layout

    widened = all_orientations(symbols)
    intent = expand_for(symbols, intent)
    roles = roles_for(symbols, intent)
    widths = text_widths(intent)
    plan, placed = plan_and_layout(widened, nets, roles, text_widths=widths, partial=True)
    return ladder_fixture.build_document(
        name, plan, widened, nets, placed, reach=reach, half_widths=half_widths, text_widths=widths, roles=roles,
        roles_summary=roles_summary(roles), others=others, source=source,
    )


def write_fixtures(run_dir: Path, out_dir: Path, demos: list[str], corpus_names: list[str]) -> list[tuple[str, int, str]]:
    """One ``<name>.json`` per demo (the recorded trial of ``FIXTURE_PICKS``) and per corpus circuit under ``out_dir``.

    Returns ``(name, bytes, note)`` per circuit; a circuit the heuristic cannot place gets no file and its error as the note."""
    import tempfile
    from backend.schematic import ladder_fixture
    from . import corpus

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int, str]] = []

    def save(name: str, document: dict[str, Any]) -> None:
        path = out_dir / f"{name}.json"
        path.write_text(ladder_fixture.dumps(document), encoding="utf-8")
        size = path.stat().st_size
        written.append((name, size, "" if size <= FIXTURE_LIMIT_BYTES else f"over {FIXTURE_LIMIT_BYTES // 1024} KiB"))

    for name in demos:
        fixture = fixture_name(name)
        key = name if name in FIXTURE_PICKS else next(k for k, v in CIRCUITS.items() if v == fixture)
        circuit, _matches, index, _distance = find_circuit(Path(run_dir), fixture, FIXTURE_PICKS[key])
        reach, half_widths = _model_inputs(circuit)
        try:
            save(key, fixture_document(
                key, engine_pose(circuit), circuit["refine"]["args"][1], build_fixture(fixture), reach, half_widths,
                others="keep", source=f"{Path(run_dir).name} recorded circuit #{index} of {fixture}"))
        except Exception as error:
            written.append((key, 0, f"{type(error).__name__}: {error}"))
    client = None
    for name in corpus_names:
        entry = corpus.CIRCUITS[name]
        try:
            built = corpus.build(entry)
            if client is None:
                from backend.kicad.mcp_inprocess import McpInProcessClient
                client = McpInProcessClient()
            with tempfile.TemporaryDirectory() as tmp:
                symbols, nets, reach, half_widths = corpus_inputs(entry, built, Path(tmp), client)
            save(name, fixture_document(name, symbols, nets, built.intent, reach, half_widths, others="row",
                                        source=f"corpus {entry.key}: {entry.sheet}"))
        except Exception as error:
            written.append((name, 0, f"{type(error).__name__}: {error}"))
    return written
