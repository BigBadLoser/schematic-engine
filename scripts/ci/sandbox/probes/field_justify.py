"""Where does KiCad draw a symbol's field text for a given rotation, mirror, field angle and justification?

The engine moves reference and value text to places a reader can read it, so it has to know what its writes will look
like. This builds one schematic with a resistor for every combination of symbol rotation (0, 90, 180, 270), mirror (none,
x, y), field angle (0, 90) and justification, each with a unique reference text; has kicad-cli draw it; and compares the
rendered text box with the one `backend.schematic.kicad_geometry.text_box` predicts from the absolute text angle (field
angle plus symbol rotation). A combination whose box is off says which rule the estimate has wrong.

Needs a finished sandbox run (for a sheet whose library symbols it can borrow) and kicad-cli.
"""
from __future__ import annotations

import itertools
import os
import re
import subprocess
import tempfile
from pathlib import Path

from _common import REPO, SANDBOX, emit, fail

GROUP = re.compile(r'<g class="stroked-text"><desc>(.*?)</desc>(.*?)</g>', re.S)
NUMBER = re.compile(r"[-+]?\d+\.?\d*")
ROTATIONS = (0, 90, 180, 270)
MIRRORS = (None, "x", "y")
FIELD_ANGLES = (0, 90)
JUSTIFY = ((), ("left",), ("right",), ("top",), ("bottom",), ("left", "top"), ("right", "bottom"))
TOLERANCE_MM = 0.5


def donor_sheet() -> Path | None:
    wanted = os.environ.get("SBX_FIELD_PROBE_RUN")
    runs = sorted((SANDBOX / ".work" / "runs").glob(f"*{wanted}*" if wanted else "*"))
    for run in reversed(runs):
        sheet = run / "artifacts" / "schematic-engine-rc_filter" / "mcu_smoke.kicad_sch"
        if sheet.is_file():
            return sheet
    return None


def find_kicad_cli() -> str | None:
    from sbx import env

    cli = env.find_kicad_cli()
    return str(cli) if cli else None


def sheet_text(donor: str, cases: list[tuple]) -> str:
    start = donor.index("(lib_symbols")
    depth = 0
    for index in range(start, len(donor)):
        if donor[index] == "(":
            depth += 1
        elif donor[index] == ")":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    head, footer = donor[:end], donor[donor.index("(sheet_instances"):]
    symbols = []
    for number, (rotation, mirror, field_angle, justify, x, y) in enumerate(cases, 1):
        text = f"T{number:04d}"
        justify_text = f"(justify {' '.join(justify)})" if justify else ""
        mirror_text = f"(mirror {mirror})" if mirror else ""
        symbols.append(f"""
	(symbol
		(lib_id "Device:R")
		(at {x} {y} {rotation})
		{mirror_text}
		(unit 1)
		(body_style 1)
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(in_pos_files yes)
		(dnp no)
		(uuid "00000000-0000-4000-8000-{number:012d}")
		(property "Reference" "{text}"
			(at {x + 8} {y - 6} {field_angle})
			(effects (font (size 1.27 1.27)) {justify_text})
		)
		(property "Value" "10k" (at {x} {y} 0) (hide yes) (effects (font (size 1.27 1.27))))
		(pin "1" (uuid "00000000-0000-4000-9000-{number:012d}"))
		(pin "2" (uuid "00000000-0000-4000-a000-{number:012d}"))
		(instances (project "" (path "/b78dcb60-2d55-43a6-906a-58fc016fa292" (reference "{text}") (unit 1))))
	)""")
    return head + "".join(symbols) + "\n\t" + footer


def rendered(svg: str) -> dict[str, tuple[float, float, float, float]]:
    boxes = {}
    for text, body in GROUP.findall(svg):
        xs: list[float] = []
        ys: list[float] = []
        for path in re.findall(r'<path d="(.*?)"', body, re.S):
            numbers = [float(n) for n in NUMBER.findall(path)]
            xs += numbers[0::2]
            ys += numbers[1::2]
        if xs and ys:
            boxes[text.strip()] = (min(xs), min(ys), max(xs), max(ys))
    return boxes


def main() -> int:
    donor = donor_sheet()
    cli = find_kicad_cli()
    if donor is None or cli is None:
        return fail("field_justify", "needs a sandbox run (a sheet to borrow library symbols from) and kicad-cli")
    from backend.schematic.kicad_geometry import text_box

    cases: list[tuple] = []
    columns = 8
    for index, (rotation, mirror, field_angle, justify) in enumerate(itertools.product(ROTATIONS, MIRRORS, FIELD_ANGLES, JUSTIFY)):
        cases.append((rotation, mirror, field_angle, justify, 30 + (index % columns) * 40, 30 + (index // columns) * 30))
    with tempfile.TemporaryDirectory() as tmp:
        sheet = Path(tmp) / "probe.kicad_sch"
        sheet.write_text(sheet_text(donor.read_text(encoding="utf-8"), cases), encoding="utf-8")
        done = subprocess.run([cli, "sch", "export", "svg", "--no-background-color", "--exclude-drawing-sheet", "-o", tmp, str(sheet)],
                              capture_output=True, text=True)
        svgs = list(Path(tmp).glob("*.svg"))
        if done.returncode != 0 or not svgs:
            return fail("field_justify", f"kicad-cli could not draw the probe sheet: {done.stderr.strip()[:200]}")
        boxes = rendered(svgs[0].read_text(encoding="utf-8"))

    results: dict[tuple, list[float]] = {}
    for number, (rotation, mirror, field_angle, justify, x, y) in enumerate(cases, 1):
        real = boxes.get(f"T{number:04d}")
        if real is None:
            continue
        guess = text_box(x + 8, y - 6, f"T{number:04d}", 1.27, field_angle + rotation, frozenset(justify), mirror)
        dx = ((guess.left + guess.right) - (real[0] + real[2])) / 2
        dy = ((guess.top + guess.bottom) - (real[1] + real[3])) / 2
        results[(rotation, mirror, field_angle, justify)] = [dx, dy, (guess.right - guess.left) - (real[2] - real[0]),
                                                              (guess.bottom - guess.top) - (real[3] - real[1])]
    bad = {key: value for key, value in results.items() if abs(value[0]) > TOLERANCE_MM or abs(value[1]) > TOLERANCE_MM}
    lines = [f"{len(results)} of {len(cases)} combinations drawn; {len(bad)} with the box centre more than {TOLERANCE_MM} mm off"]
    by_rule: dict[str, int] = {}
    for (rotation, mirror, field_angle, justify), (dx, dy, _dw, _dh) in sorted(bad.items(), key=lambda kv: str(kv[0])):
        by_rule[f"rotation {rotation}, mirror {mirror}"] = by_rule.get(f"rotation {rotation}, mirror {mirror}", 0) + 1
    for rule, count in sorted(by_rule.items()):
        lines.append(f"   {rule}: {count} of {len(FIELD_ANGLES) * len(JUSTIFY)} off")
    for (rotation, mirror, field_angle, justify), (dx, dy, dw, dh) in list(sorted(bad.items(), key=lambda kv: str(kv[0])))[:12]:
        lines.append(f"      rot {rotation:3d} mirror {str(mirror):4} field {field_angle:2d} justify {' '.join(justify) or 'centre':12} "
                     f"-> centre off ({dx:+.2f}, {dy:+.2f}) mm, size off ({dw:+.2f}, {dh:+.2f})")
    data = {"cases": len(cases), "drawn": len(results), "off": len(bad), "repo": str(REPO),
            "off_cases": [{"rotation": k[0], "mirror": k[1], "field_angle": k[2], "justify": list(k[3]), "dx": round(v[0], 2), "dy": round(v[1], 2)}
                          for k, v in bad.items()]}
    return emit("field_justify", lines, data, ok=not bad)


if __name__ == "__main__":
    raise SystemExit(main())
