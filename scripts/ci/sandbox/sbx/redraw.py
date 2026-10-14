"""The engine's redraw of a KiCad demo next to the human original, by the same counts and as drawings.

``demos`` gives the engine a demo's netlist and blocks; a run produces the engine's sheet (the shipped baseline and the one
the readability-aware selection chose). This module measures all three with the code that measures every other sheet in
the sandbox, and draws them without annotation, in a shuffled order and with the key kept apart, for someone to judge
without knowing which is which.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import demos, env, kpi, readability

MARGIN_MM = 5.0
SHEETS = ("human", "shipped", "engine")
COLUMNS: tuple[tuple[str, str], ...] = (
    ("wire_mm", "wire mm"), ("area_mm2", "area mm2"), ("corners_per_connection", "corner/conn"),
    ("bent_wire_rate", "bent wire"), ("crossings", "crossings"), ("field_on_wire", "txt on wire"),
    ("support_far", "far support"), ("power_sideways_rate", "pwr side"), ("text_overlaps", "txt overlap"),
    ("nn_gap_med", "nn gap med"), ("nn_gap_min", "nn gap min"), ("crowded_text", "crowded txt"),
)


def is_corpus_key(key: str) -> bool:
    """A fixture read from a corpus sheet (``corpus_<key>``): its human sheet is the corpus entry's, and a run's sheet for it
    is a direct build at the ladder's placement (there is no engine row)."""
    from . import corpus
    return key.startswith("corpus_") and key.removeprefix("corpus_") in corpus.CIRCUITS


def sheets_of(run_dir: Path, key: str) -> dict[str, Path]:
    """The human original, the shipped (verified) sheet and the selected sheet of a demo in a run. For a corpus circuit:
    the human sheet and the run's own (the ladder's) sheet."""
    artifact = run_dir / "artifacts" / f"schematic-engine-{key}"
    if is_corpus_key(key):
        from . import corpus
        found = {"human": corpus.sheet_file(corpus.CIRCUITS[key.removeprefix("corpus_")]), "engine": artifact / "mcu_smoke.kicad_sch"}
        return {name: path for name, path in found.items() if path.is_file()}
    found = {
        "human": demos.sheet_path(demos.DEMOS[key]),
        "shipped": artifact / "quality-trials" / "01-verified" / "candidate.kicad_sch",
        "engine": artifact / "mcu_smoke.kicad_sch",
    }
    return {name: path for name, path in found.items() if path.is_file()}


def nn_gaps(scene: Any) -> tuple[float | None, float | None]:
    """Median and minimum, over part bodies, of the edge-to-edge distance to the nearest other body (0 if overlapping)."""
    import math
    import statistics
    from backend.schematic.presentation_geometry import GeometryKind
    bodies: dict[str, tuple[float, float, float, float]] = {}
    for it in scene.items:
        if it.kind == GeometryKind.BODY and not it.owner.startswith("#"):
            b = it.bounds
            o = bodies.get(it.owner)
            bodies[it.owner] = (b.left, b.top, b.right, b.bottom) if o is None else (
                min(o[0], b.left), min(o[1], b.top), max(o[2], b.right), max(o[3], b.bottom))
    nearest = []
    for a, p in bodies.items():
        gaps = [math.hypot(max(0.0, p[0] - q[2], q[0] - p[2]), max(0.0, p[1] - q[3], q[1] - p[3]))
                for c, q in bodies.items() if c != a]
        if gaps:
            nearest.append(min(gaps))
    if not nearest:
        return None, None
    return round(statistics.median(nearest), 2), round(min(nearest), 2)


def crowded_text(scene: Any, near_mm: float = 1.0) -> float | None:
    """Share of reference, value and power texts whose box is within ``near_mm`` of another owner's text box or of a wire
    that is not of the text owner's own nets (the nets of the owner's pins; a power symbol's own net)."""
    from backend.schematic.presentation_geometry import GeometryKind
    kinds = (GeometryKind.REFERENCE, GeometryKind.VALUE, GeometryKind.POWER_TEXT)
    texts = [i for i in scene.items if i.kind in kinds]
    others = [i for i in scene.items if i.kind in kinds or i.kind == GeometryKind.NET_LABEL or i.kind == GeometryKind.PIN_NAME
              or i.kind == GeometryKind.PIN_NUMBER]
    wires = [i for i in scene.items if i.kind == GeometryKind.WIRE]
    nets: dict[str, set[str]] = {}
    for i in scene.items:
        if i.net and i.kind != GeometryKind.WIRE:
            nets.setdefault(i.owner, set()).add(i.net)
    if not texts:
        return None

    def near(a: Any, b: Any) -> bool:
        return (a.left < b.right + near_mm and a.right > b.left - near_mm
                and a.top < b.bottom + near_mm and a.bottom > b.top - near_mm)

    crowded = 0
    for t in texts:
        mine = nets.get(t.owner, set()) | ({t.net} if t.net else set())
        if any(o.owner != t.owner and near(t.bounds, o.bounds) for o in others) or any(
                w.net not in mine and near(t.bounds, w.bounds) for w in wires):
            crowded += 1
    return round(crowded / len(texts), 3)


def text_collisions(scene: Any) -> int:
    """Texts that stand on another symbol's text or drawing, and a symbol's reference on its own value.

    ``text_overlap_pairs`` leaves a symbol's own texts alone (its name on its own drawing is by design), which also hides
    the two texts of a part written along the part and one over the other (`auto-demo7`, the buck converter's inductor).
    """
    from backend.schematic.composition_metrics import text_overlap_pairs
    from backend.schematic.presentation_geometry import GeometryKind

    own: dict[str, list[Any]] = {}
    for item in scene.items:
        if item.kind in (GeometryKind.REFERENCE, GeometryKind.VALUE):
            own.setdefault(item.owner, []).append(item.bounds)
    stacked = sum(
        1 for boxes in own.values() for index, a in enumerate(boxes) for b in boxes[index + 1:]
        if a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top
    )
    return len(text_overlap_pairs(scene)) + stacked


def measure(run_dir: Path, key: str) -> dict[str, dict[str, Any]]:
    """Counts of each sheet of a demo: readability rates and measures, wire length, drawing area."""
    src = run_dir / "src"
    intent = kpi.load_intent(key, src)
    result: dict[str, dict[str, Any]] = {}
    for name, path in sheets_of(run_dir, key).items():
        drawn = intent_as_drawn(intent, key) if name == "human" else intent
        report = readability.measure_sheet(path, intent=drawn)
        rates = readability._rates(report)
        scene, _coverage = readability.measured_scene_from_kicad(path)
        sizes = kpi.compute(kpi.geometry_from_scene(scene), kpi.nets_of(drawn))
        nn_med, nn_min = nn_gaps(scene)
        result[name] = {
            "nn_gap_med": nn_med, "nn_gap_min": nn_min,
            "wire_mm": sizes["wire_length_mm"],
            "area_mm2": round(sizes["extent_width_mm"] * sizes["extent_height_mm"]),
            "extent": (sizes["extent_width_mm"], sizes["extent_height_mm"]),
            "corners_per_connection": rates["corners_per_connection"],
            "bent_wire_rate": rates["bent_wire_rate"],
            "crossings": report.measures["crossings"],
            "field_on_wire": report.measures["field_on_wire"],
            "support_far": report.measures["support_far"],
            "power_sideways_rate": rates["power_sideways_rate"],
            "text_overlaps": text_collisions(scene), "crowded_text": crowded_text(scene),
            "rates": rates,
        }
    return result


def intent_as_drawn(intent: Any, key: str) -> Any:
    """The circuit with the references the demo's own sheet has: a part the schematic library would not take under its
    name (Rmeas1) was renamed for the engine, and the human sheet still has the name it had."""
    from types import SimpleNamespace

    if str(env.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(env.REPO_ROOT))
    from scripts import schematic_demo_circuits as data

    back = {new: old for new, old in data.DEMO_RENAMES.get(key, {}).items()}
    if not back:
        return intent
    # what the measures read of a circuit is its nets: each pin by reference and number, and what a net is
    nets = tuple(
        SimpleNamespace(
            name=net.name, kind=net.kind,
            endpoints=tuple(
                SimpleNamespace(
                    reference=back.get(e.reference, e.reference), pin=e.pin, token=f"{back.get(e.reference, e.reference)}.{e.pin}",
                )
                for e in net.endpoints
            ),
        )
        for net in intent.all_nets
    )
    return SimpleNamespace(all_nets=nets)


def format_comparison(key: str, measured: dict[str, dict[str, Any]]) -> str:
    lines = [f"{key}", f"  {'sheet':<8}" + "".join(f"{label:>13}" for _name, label in COLUMNS)]
    for sheet in SHEETS:
        if sheet not in measured:
            continue
        row = measured[sheet]
        cells = []
        for name, _label in COLUMNS:
            value = row[name]
            cells.append(f"{'-' if value is None else format(value, '.4g'):>13}")
        lines.append(f"  {sheet:<8}" + "".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------
# drawings

_TEXT_BLOCKS = re.compile(r"\n\t\((?:text|text_box|polyline|rectangle|rule_area|image)\b")
_PROPERTY = re.compile(r'\(property "([^"]+)" "(?:[^"\\]|\\.)*"')


def _skip_block(text: str, start: int) -> int:
    """The index just past the S-expression that starts at ``start`` (its opening parenthesis)."""
    depth = 0
    in_string = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def without_annotations(text: str) -> str:
    """A sheet with its free text, boxes and pictures removed and every symbol field but reference and value hidden, so a
    human-drawn sheet can be drawn beside the engine's, which has none of them. Wires, symbols and labels stay."""
    out: list[str] = []
    position = 0
    for match in _TEXT_BLOCKS.finditer(text):
        if match.start() < position:
            continue
        out.append(text[position:match.start()])
        position = _skip_block(text, match.start() + 2)
    out.append(text[position:])
    return _hide_fields("".join(out))


def _hide_fields(text: str) -> str:
    """Hide every symbol property but the reference and the value."""
    out: list[str] = []
    position = 0
    for match in _PROPERTY.finditer(text):
        if match.start() < position:
            continue
        end = _skip_block(text, match.start())
        block = text[match.start():end]
        out.append(text[position:match.start()])
        if match.group(1) in ("Reference", "Value") or "(hide yes)" in block:
            out.append(block)
        else:
            cut = match.end() - match.start()
            out.append(block[:cut] + " (hide yes)" + block[cut:])
        position = end
    out.append(text[position:])
    return "".join(out)


_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
_FREE_TEXT = re.compile(r'\(text "((?:[^"\\]|\\.)*)"')
_TEXT_AT = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\)")
_TEXT_SIZE = re.compile(r"\(size\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
TEXT_CHAR_WIDTH = 0.75  # of the font height, per character
TEXT_MARGIN_MM = 2.0


def _top_level_blocks(sheet_text: str) -> list[tuple[int, int]]:
    """(start, end) of each child form of the sheet's root form; strings are skipped so their parentheses do not count."""
    blocks: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for token in _TOKEN.finditer(sheet_text):
        if token.group(0) == "(":
            depth += 1
            if depth == 2:
                start = token.start()
        elif token.group(0) == ")":
            if depth == 2:
                blocks.append((start, token.end()))
            depth -= 1
    return blocks


def free_text_boxes(sheet_text: str) -> list[tuple[float, float, float, float]]:
    """Left, top, right, bottom in millimetres (y down) of each top-level free text, estimated and with a margin: KiCad
    draws it from its anchor up and to the right, a character 0.75 of its font height wide; a quarter turn swaps the
    width and the height. Texts inside library symbols or placed symbols are not free text and are left out."""
    boxes: list[tuple[float, float, float, float]] = []
    for start, end in _top_level_blocks(sheet_text):
        block = sheet_text[start:end]
        text = _FREE_TEXT.match(block)
        at = _TEXT_AT.search(block, text.end()) if text else None
        if text is None or at is None:
            continue
        size = _TEXT_SIZE.search(block, text.end())
        height = float(size.group(2)) if size else 1.27
        run = TEXT_CHAR_WIDTH * height * len(text.group(1))
        width, tall = (height, run) if abs(float(at.group(3) or 0)) % 180 == 90 else (run, height)
        x, y = float(at.group(1)), float(at.group(2))
        boxes.append((x - TEXT_MARGIN_MM, y - tall - TEXT_MARGIN_MM, x + width + TEXT_MARGIN_MM, y + TEXT_MARGIN_MM))
    return boxes


def draw(sheet: Path, *, clean: bool = False, scale: float = 8.0) -> tuple[str, tuple[float, float]]:
    """The sheet as an SVG, cropped to what is on it, ``scale`` pixels to the millimetre; also its size in millimetres."""
    cli = env.find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli was not found")
    from backend.schematic.kicad_geometry import measured_scene_from_kicad

    with tempfile.TemporaryDirectory() as tmp:
        source = sheet
        if clean:
            source = Path(tmp) / sheet.name
            source.write_text(without_annotations(sheet.read_text(encoding="utf-8")), encoding="utf-8")
        scene, _coverage = measured_scene_from_kicad(source)
        xs = [v for item in scene.items for v in (item.bounds.left, item.bounds.right)]
        ys = [v for item in scene.items for v in (item.bounds.top, item.bounds.bottom)]
        boxes = free_text_boxes(source.read_text(encoding="utf-8"))
        xs += [v for box in boxes for v in (box[0], box[2])]
        ys += [v for box in boxes for v in (box[1], box[3])]
        left, top = min(xs) - MARGIN_MM, min(ys) - MARGIN_MM
        width, height = max(xs) + MARGIN_MM - left, max(ys) + MARGIN_MM - top
        out_dir = Path(tmp) / "svg"
        subprocess.run(
            [str(cli), "sch", "export", "svg", "--no-background-color", "--exclude-drawing-sheet", "-o", str(out_dir), str(source)],
            check=True, capture_output=True,
        )
        svg = next(out_dir.glob("*.svg")).read_text(encoding="utf-8")
    svg = re.sub(
        r'width="[^"]*mm" height="[^"]*mm" viewBox="[^"]*"',
        f'width="{width * scale:.0f}" height="{height * scale:.0f}" viewBox="{left:.3f} {top:.3f} {width:.3f} {height:.3f}"',
        svg, count=1,
    )
    return svg, (width, height)


def stacked_page(panels: dict[str, tuple[str, tuple[float, float]]]) -> str:
    """One page with the drawings one under another, each under its letter and all at the one scale."""
    body = "".join(
        f"<section><h2>{letter}</h2>{svg}</section>" for letter, (svg, _size) in sorted(panels.items())
    )
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>body{margin:0;background:#fff;font:600 28px system-ui;color:#222}section{padding:14px 18px;"
        "border-bottom:3px solid #888}h2{margin:0 0 6px}svg{display:block}</style><body>" + body
    )


def blind_page(run_dir: Path, keys: list[str], out_dir: Path, *, seed: int = 20261010) -> dict[str, dict[str, str]]:
    """Per demo, the three sheets drawn unlabelled in a shuffled order as ``out_dir/<key>-<letter>.svg`` and, stacked,
    as ``out_dir/<key>.html``; the key is written to ``out_dir/key.json`` and returned. The human sheet is drawn without
    its annotations."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chooser = random.Random(seed)
    key_map: dict[str, dict[str, str]] = {}
    for key in keys:
        sheets = sheets_of(run_dir, key)
        names = [name for name in SHEETS if name in sheets]
        chooser.shuffle(names)
        key_map[key] = {}
        panels: dict[str, tuple[str, tuple[float, float]]] = {}
        for letter, name in zip("ABC", names):
            panels[letter] = draw(sheets[name], clean=(name == "human"))
            (out_dir / f"{key}-{letter}.svg").write_text(panels[letter][0], encoding="utf-8")
            key_map[key][letter] = name
        (out_dir / f"{key}.html").write_text(stacked_page(panels), encoding="utf-8")
    (out_dir / "key.json").write_text(json.dumps(key_map, indent=2), encoding="utf-8")
    return key_map
