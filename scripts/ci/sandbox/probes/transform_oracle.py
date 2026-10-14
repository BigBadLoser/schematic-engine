"""KiCad as oracle: which transform puts a pin where KiCad itself says it is?

Places Device:R in all 12 rotation/mirror orientations. At the pin position each
candidate transform predicts (MCP Pro's get_pin_positions, the repo's
kicad_geometry._transform) it drops a labelled wire stub, then exports the
netlist with kicad-cli. The label a pin's node ends up on names the candidate
that was right. Needs kicad-cli, KiCad's symbol library and MCP Pro.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from _common import emit, fail

STUB_MM = 2.54
ORIGIN, PITCH = 50.8, 38.1  # grid-aligned placement lattice


def _blocks(text: str, opener: str):
    """Yield every balanced (...) block that starts with `opener`."""
    index = 0
    while (start := text.find(opener, index)) != -1:
        depth, in_string, i = 0, False, start
        while i < len(text):
            char = text[i]
            if in_string:
                if char == "\\":
                    i += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break
            i += 1
        index = start + len(opener)


def extract_symbol(library_text: str, name: str) -> str:
    for block in _blocks(library_text, f'(symbol "{name}"'):
        if re.match(rf'\(symbol "{re.escape(name)}"[\s)]', block):
            return block
    raise ValueError(f"symbol {name!r} not found in library")


def library_pins(block: str) -> dict[str, tuple[float, float]]:
    pins = {}
    for pin in _blocks(block, "(pin "):
        at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)", pin)
        number = re.search(r'\(number\s+"([^"]+)"', pin)
        if at and number:
            pins[number.group(1)] = (float(at.group(1)), float(at.group(2)))
    return pins


def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _uuid() -> str:
    return str(uuid.uuid4())


def placed_symbol(ref: str, x: float, y: float, rotation: int, mirror: str, root: str) -> str:
    mirror_line = f"\t\t(mirror {mirror})\n" if mirror else ""
    field = lambda name, value, hide="": (  # noqa: E731
        f'\t\t(property "{name}" "{value}"\n\t\t\t(at {_num(x)} {_num(y)} 0)\n'
        f"\t\t\t(effects (font (size 1.27 1.27)){hide})\n\t\t)\n"
    )
    return (
        "\t(symbol\n\t\t(lib_id \"Device:R\")\n"
        f"\t\t(at {_num(x)} {_num(y)} {rotation})\n{mirror_line}"
        "\t\t(unit 1)\n\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
        f'\t\t(uuid "{_uuid()}")\n'
        + field("Reference", ref) + field("Value", "10k") + field("Footprint", "", " (hide yes)")
        + field("Datasheet", "~", " (hide yes)")
        + f'\t\t(instances\n\t\t\t(project "oracle"\n\t\t\t\t(path "/{root}"\n'
        f'\t\t\t\t\t(reference "{ref}") (unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)'
    )


def stub(x: float, y: float, dx: float, dy: float, name: str) -> str:
    x2, y2 = x + dx, y + dy
    return (
        f"\t(wire\n\t\t(pts\n\t\t\t(xy {_num(x)} {_num(y)}) (xy {_num(x2)} {_num(y2)})\n\t\t)\n"
        f'\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "{_uuid()}")\n\t)\n'
        f'\t(label "{name}"\n\t\t(at {_num(x2)} {_num(y2)} 0)\n'
        f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left bottom)\n\t\t)\n"
        f'\t\t(uuid "{_uuid()}")\n\t)'
    )


def main() -> int:
    from sbx import env

    cli = env.find_kicad_cli()
    symbols_dir = env.find_symbol_dir(cli)
    if cli is None or symbols_dir is None or not (symbols_dir / "Device.kicad_sym").is_file():
        return fail("transform_oracle", "kicad-cli or KiCad's Device symbol library was not found")
    try:
        from backend.schematic.kicad_geometry import _transform
        from kicad_mcp.tools.schematic import get_pin_positions
    except (ImportError, AttributeError) as error:
        return fail("transform_oracle", f"imports unavailable: {error}")

    block = extract_symbol((symbols_dir / "Device.kicad_sym").read_text(encoding="utf-8"), "R")
    pins = library_pins(block)
    embedded = block.replace('(symbol "R"', '(symbol "Device:R"', 1)
    combos = [(rot, mirror) for rot in (0, 90, 180, 270) for mirror in ("", "x", "y")]
    root = _uuid()
    body: list[str] = []
    for i, (rotation, mirror) in enumerate(combos):
        cx, cy = ORIGIN + PITCH * (i % 3), ORIGIN + PITCH * (i // 3)
        mcp = get_pin_positions("Device", "R", cx, cy, rotation, 1, mirror)
        repo = {pin: _transform(px, py, cx, cy, float(rotation), mirror or None) for pin, (px, py) in pins.items()}
        claims: dict[tuple[float, float], list[str]] = {}
        for tag, positions in (("MCP", mcp), ("REPO", repo)):
            for pin, (px, py) in positions.items():
                claims.setdefault((round(px, 4), round(py, 4)), []).append(f"{tag}{pin}")
        body.append(placed_symbol(f"R{i}", cx, cy, rotation, mirror, root))
        for (px, py), tags in claims.items():
            vx, vy = px - cx, py - cy
            dx, dy = ((STUB_MM if vx > 0 else -STUB_MM), 0.0) if abs(vx) >= abs(vy) else (0.0, (STUB_MM if vy > 0 else -STUB_MM))
            body.append(stub(px, py, dx, dy, f"X{i}_" + "_".join(sorted(tags))))
    sheet = (
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")\n'
        f'\t(uuid "{root}")\n\t(paper "A4")\n\t(lib_symbols\n\t\t{embedded}\n\t)\n'
        + "\n".join(body)
        + '\n\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
    )

    with tempfile.TemporaryDirectory(prefix="sbx-oracle-") as directory:
        work = Path(directory)
        schematic, netlist = work / "oracle.kicad_sch", work / "oracle.xml"
        schematic.write_text(sheet, encoding="utf-8")
        done = subprocess.run(
            [str(cli), "sch", "export", "netlist", "--format", "kicadxml", "--output", str(netlist), str(schematic)],
            capture_output=True, text=True, cwd=work, check=False, timeout=120,
        )
        if done.returncode != 0 or not netlist.is_file():
            return fail("transform_oracle", f"kicad-cli netlist export failed: {(done.stderr or done.stdout).strip()[:300]}")
        tree = ET.parse(netlist)

    node_net = {
        (node.get("ref"), node.get("pin")): net.get("name", "")
        for net in tree.getroot().iterfind("nets/net")
        for node in net.iterfind("node")
    }
    rows, lines = [], ["rot  mirror  MCP get_pin_positions   repo _transform"]
    for i, (rotation, mirror) in enumerate(combos):
        verdict = {"MCP": True, "REPO": True}
        for pin in pins:
            name = node_net.get((f"R{i}", pin), "")
            tokens = re.findall(r"(MCP|REPO)(\d+)", name.split(f"X{i}_", 1)[1]) if f"X{i}_" in name else []
            right = {tag for tag, number in tokens if number == pin}
            for tag in verdict:
                verdict[tag] &= tag in right
        rows.append({"rotation": rotation, "mirror": mirror or "none", "mcp_matches_kicad": verdict["MCP"],
                     "repo_matches_kicad": verdict["REPO"]})
        word = lambda ok: "matches" if ok else "WRONG"  # noqa: E731
        lines.append(f"{rotation:<4} {mirror or 'none':<7} {word(verdict['MCP']):<22} {word(verdict['REPO'])}")

    controls = [r for r in rows if r["mirror"] == "none" and r["rotation"] in (0, 180)]
    if not any(r["mcp_matches_kicad"] or r["repo_matches_kicad"] for r in controls):
        return fail("transform_oracle", "oracle invalid: neither candidate matched KiCad even for unmirrored 0/180")
    mcp_ok = sum(r["mcp_matches_kicad"] for r in rows)
    repo_ok = sum(r["repo_matches_kicad"] for r in rows)
    lines.append(f"KiCad agrees with MCP Pro in {mcp_ok}/12 orientations and with kicad_geometry._transform in {repo_ok}/12")
    return emit("transform_oracle", lines, {"orientations": rows, "kicad_cli": env.run_capture([str(cli), "--version"])},
                ok=mcp_ok == 12 and repo_ok == 12)


if __name__ == "__main__":
    raise SystemExit(main())
