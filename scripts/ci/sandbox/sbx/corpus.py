"""Human-drawn sheets from other authors (the corpus clones) as engine inputs, offline.

``demos.py`` does this for KiCad's four demos with hand-typed net kinds and generated data. This module takes any sheet
under the corpus folder: the netlist comes from ``kicad-cli sch export netlist`` (the same ``demos`` functions), the net kinds
are *inferred* from the sheet, and the blocks are written here, per circuit, the way an engineer reads the sheet. Nothing
from a clone is executed: its ``.kicad_sch``/``.kicad_sym``/``sym-lib-table`` files are read as text.

Net kinds. A net is a rail when a power symbol is on it: a symbol of the ``power`` library, or any symbol whose library
entry carries ``(power)`` (its pins are ``power_in``/``power_out``). It is ground when the symbol's name or value matches
``readability.is_ground_name``, negative when the value starts with "-", else a supply. Everything else is a signal. A rail
that is only a label, or a regulator output no power symbol marks, is a signal: the converter does not guess from names.

Symbol libraries (for geometry, see ``prepare_project``). A ``lib_id`` is resolved against (1) KiCad's own libraries, (2) the
project's ``sym-lib-table`` (``${KIPRJMOD}`` = the project folder; the table is searched upward from the sheet), when the
file exists and holds the symbol, else (3) the copy of the symbol embedded in the sheet's ``lib_symbols``, which is what
KiCad drew the sheet with. Altium entries, absolute paths of someone else's machine and missing files fall to (3).
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import demos, env

if str(env.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(env.REPO_ROOT))
from backend.schematic import kicad_geometry as kg  # noqa: E402
from backend.schematic.designators import is_source_reference  # noqa: E402,F401  (classify finds sources by reference)
from backend.schematic.intent import FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent  # noqa: E402
from backend.schematic.readability import is_ground_name  # noqa: E402

CORPUS_ROOT = env.WORK_DIR / "corpus"
NICKNAMES_FILE = "library-nicknames.json"   # in a prepared project: {library in the sheet: its name in the sym-lib-table}, renamed ones only


@dataclass(frozen=True)
class Block:
    name: str
    members: str                    # references, space separated
    inputs: str = ""                # nets (or "REF.PIN" of one), space separated, taken in
    outputs: str = ""               # nets sent on


@dataclass(frozen=True)
class CorpusCircuit:
    key: str
    sheet: str                      # relative to the corpus folder
    blocks: tuple[Block, ...]
    input_connectors: tuple[str, ...] = ()      # connectors where the signal enters: role="input_connector"
    output_connectors: tuple[str, ...] = ()
    summary: str = ""


def B(name: str, members: str, inputs: str = "", outputs: str = "") -> Block:
    return Block(name, members, inputs, outputs)


CIRCUITS: dict[str, CorpusCircuit] = {c.key: c for c in (
    CorpusCircuit(
        "tally_timecode", "tallytime-hardware/sheets/timecode.kicad_sch",
        blocks=(B("trrs_in", "J7 TP9 TP10 TP11", outputs="LTC_IN"), B("coupling", "C21 R20 R22", "LTC_IN", "OPAMP_A_N"),
                B("comparator", "U5 R18 R19 C19 C20 R21 TP6 TP7", "OPAMP_A_N", "LTC_RX"), B("trrs_out", "C22 J8 TP8 TP12", "LTC_TX")),
        input_connectors=("J7",), output_connectors=("J8",), summary="audio jack in, AC coupling, LM358 comparator, jack out"),
    CorpusCircuit(
        "tally_power", "tallytime-hardware/sheets/power.kicad_sch",
        blocks=(B("usb_fuse", "F1 TP16", "VBUS", "+5V"),
                B("charger", "U7 R25 R26 R27 R28 D5 D6 C25 C26 TP13", "+5V", "+BATT"),
                B("power_path", "Q1 SW6 D3 R23 TP14", "+BATT +5V", "+VIN"), B("regulator", "U6 C23 C24 R24 D4 TP15", "+VIN", "+3V3")),
        summary="USB fuse, TP4057 charger, battery/USB power path, 3.3 V linear regulator"),
    CorpusCircuit(
        "pico_hbridge", "pico-with-kicad/eg/ch04/h_bridge.kicad_sch",
        blocks=(B("header", "J1", outputs="A B"), B("gate_resistors", "R1 R2 R3 R4", "A B", "Q1.1 Q2.1 Q3.1 Q4.1"),
                B("half_bridges", "Q1 Q2 Q3 Q4", "Q1.1 Q2.1 Q3.1 Q4.1", "M1.1 M1.2"), B("motor", "M1", "M1.1 M1.2")),
        input_connectors=("J1",), summary="four-MOSFET H-bridge with gate resistors driving a DC motor"),
    CorpusCircuit(
        "pmod_hv", "PMOD_rp2040/HV_Pulser/HVPulser_Source/HV-BOARD.kicad_sch",
        blocks=(B("input_filter", "J7 L1 C10 C11", "+5V", "B1.2"), B("hv_converter", "B1", "B1.2", "+HV -HV"),
                B("hv_output", "C12 C13 J5", "+HV -HV")),
        input_connectors=("J7",), summary="5 V to +/-HV DC-DC module with its input LC filter and output capacitors"),
    CorpusCircuit(
        "gamma_preamp", "OpenGammaKit/preamp.kicad_sch",
        blocks=(B("reference", "U7", outputs="VREF1"), B("bias_network", "R1 R3 R2", "VREF1", "ANODE_IN"),
                B("input_protection", "R5 D1", "ANODE_IN", "U2.3"), B("amplifier", "U2 R4 R6", "U2.3", "SIG"), B("decoupling", "C4 C5")),
        summary="shunt reference, SiPM anode bias network, protected input to an OPA357 transimpedance stage"),
    CorpusCircuit(
        "gamma_sipm", "OpenGammaKit/sipm_board.kicad_sch",
        blocks=(B("pre_regulator", "U9 C17 C19", "PS_IN", "U9.5"), B("bias_transistors", "Q3 Q4 R17 R18 R19 TH1", "PS_IN", "Q4.3"),
                B("bias_filter", "R20 R21 C20 C21 C22", "Q4.3", "+29.6V"), B("sipm", "U10", "+29.6V", "ANODE_OUT")),
        summary="temperature-compensated SiPM bias supply with RC filtering into the detector"),
    CorpusCircuit(
        "bitaxe_power", "bitaxeUltra/power.kicad_sch",
        blocks=(B("power_monitor", "U8 C35", "VIN", "VIN_M"), B("input_filter", "C34 C36 C37 C38 C39 Q4", "VIN_M", "U9.1"),
                B("controller", "U9 C40 C41 C42 C43 R10 R12", "VIN_M", "Q1.4"), B("power_stage", "Q1 Q2 L1", "Q1.4", "VOUT"),
                B("output_network", "C45 C46 C47 R13 R14 R15", "VOUT", "OUT0"), B("voltage_trim", "U10 R16 C44 R4", "3V3", "U10.6")),
        summary="TPS40305 buck converter with INA260 monitor and I2C voltage trim"),
    CorpusCircuit(
        "bms_supply", "bms-c1/kicad/power_supply.kicad_sch",
        blocks=(B("input_protection", "D1 C22 C23 C40", "BAT+", "VIN"), B("buck_controller", "U3 C24 R34 TP5", "VIN", "SW"),
                B("output_filter", "L2 D9 C17 C18", "SW", "+3V3"), B("feedback", "R28 R31", "+3V3", "U3.5")),
        summary="LMR38010 battery to 3.3 V buck converter"),
    CorpusCircuit(
        "piaa_power", "piaa/design/piaa/power.kicad_sch",
        blocks=(B("input", "J8 D14 F7 C15", "+24V", "U9.1"), B("buck", "U9 D15 L1 C16 D17", "U9.1", "+5V"),
                B("usb_diode", "D16", "VBUS", "+5V"), B("ldo", "U10 C17", "+5V", "+3V3"), B("power_led", "R32 D18", "+5V")),
        input_connectors=("J8",), summary="24 V screw terminal, LM2596 buck to 5 V, USB diode, 3.3 V LDO"),
    CorpusCircuit(
        "uno_r4_power", "KiCad-Arduino-Boards/KiCad Projects/Uno/Arduino UNO R4 WiFi/Power.kicad_sch",
        blocks=(B("dc_jack", "J5 D1 TP14 TP15 C13 C19", outputs="VDD"),
                B("buck_controller", "U3 R35 C12 C20 R37 R42 R41 C22 C23 R43 R44", "VDD", "U3.5"),
                B("buck_output", "L3 C14 C15 C16 C17 C18 R36 R38 R40 TP16", "U3.5", "+5V"), B("usb_diode", "D2", "VBUS", "+5V"),
                B("ldo", "U5 TP17", "+5V", "+3.3V")),
        input_connectors=("J5",), summary="barrel jack, ISL854102 buck to 5 V, USB diode, 3.3 V LDO"),
)}


# ---------------------------------------------------------------------------------------------------------------------
# the sheet as text: power symbols and embedded libraries


def _balanced(text: str, start: int) -> int:
    """Index just past the parenthesis group that opens at ``text[start]`` (quoted strings may hold parentheses)."""
    depth, index, quoted = 0, start, False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("unbalanced parentheses")


def embedded_symbols(sheet_text: str) -> dict[str, str]:
    """``Lib:Name`` -> the symbol block the sheet carries in its ``lib_symbols``."""
    head = sheet_text.find("(lib_symbols")
    if head < 0:
        return {}
    end = _balanced(sheet_text, head)
    found: dict[str, str] = {}
    for match in re.finditer(r'\(symbol "([^"]+)"', sheet_text[head:end]):
        if ":" not in match.group(1):
            continue                                    # a unit of a symbol, not a symbol
        start = head + match.start()
        found[match.group(1)] = sheet_text[start:_balanced(sheet_text, start)]
    return found


def power_symbols(sheet_text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """(symbol name, value, pin names) of each placed power symbol: a ``power`` library symbol or one whose entry says
    ``(power)``. KiCad names the net after the value, and in sheets drawn by older versions after the power pin."""
    library = embedded_symbols(sheet_text)
    carries = {lib_id for lib_id, block in library.items() if re.search(r"\(power[\s)]", block)}
    found = []
    doc = kg.parse_kicad_sexpr(sheet_text)
    for symbol in kg._children(doc, "symbol"):
        lib_id = kg._one(symbol, "lib_id")
        value = kg._property(symbol, "Value")
        if lib_id is None or value is None:
            continue
        lib_id = str(lib_id[1])
        if lib_id.startswith("power:") or lib_id in carries:
            names = re.findall(r'\(pin\s+power_\w+.*?\(name\s+"([^"]+)"', library.get(lib_id, ""), re.S)
            pins = tuple(dict.fromkeys(n for n in names if n != "~"))
            found.append((lib_id.split(":", 1)[-1], str(value[2]), pins))
    return found


def infer_rails(sheet_text: str, names: set[str] | None = None) -> dict[str, str]:
    """Net name -> ``ground`` or ``power`` for every net a power symbol is on: its value, or (when ``names``, the nets of the
    netlist, lacks that) its power pin's name."""
    kinds: dict[str, str] = {}
    for symbol, value, pins in power_symbols(sheet_text):
        if symbol == "PWR_FLAG" or value.upper().startswith("PWR_FLAG"):
            continue
        ground = is_ground_name(value) or is_ground_name(symbol)
        candidates = [value] if names is None or value in names else [n for n in pins if n in names] or [value]
        for name in candidates:
            kinds[name] = "ground" if ground or kinds.get(name) == "ground" else "power"
    return kinds


def sheet_labels(sheet_text: str) -> dict[str, str]:
    """Net name -> ``global`` or ``local`` for each name the sheet carries across its edge: a global label, a hierarchical label
    or a sheet pin (a plain local label does not leave the sheet and is not listed). A global label wins over the others."""
    doc = kg.parse_kicad_sexpr(sheet_text)
    found: dict[str, str] = {}
    for tag, scope in (("hierarchical_label", "local"), ("global_label", "global")):
        for node in kg._children(doc, tag):
            if len(node) > 1:
                found[str(node[1])] = scope if found.get(str(node[1])) != "global" else "global"
    for sheet in kg._children(doc, "sheet"):
        for pin in kg._children(sheet, "pin"):
            if len(pin) > 1:
                found.setdefault(str(pin[1]), "local")
    return found


def infer_rails_by_name(rails: dict[str, str], names: set[str]) -> dict[str, str]:
    """Add the nets whose name says what they are: one named like ground is ground, one named like a rail a power symbol names
    (up to case) is that kind of rail, even when no power symbol sits on it on this sheet (a plain label ``GND``)."""
    by_fold = {name.casefold(): kind for name, kind in rails.items()}
    found = dict(rails)
    for name in sorted(names - set(rails)):
        if is_ground_name(name):
            found[name] = "ground"
        elif name.casefold() in by_fold:
            found[name] = by_fold[name.casefold()]
    return found


# ---------------------------------------------------------------------------------------------------------------------
# the circuit


@dataclass
class Built:
    circuit: demos.Circuit
    intent: SchematicIntent
    rails: dict[str, str]
    notes: list[str] = field(default_factory=list)


def _first_pin(netlist: demos.Netlist, token: str) -> str:
    """A block's input or output named by net (without the leading slash) or already as ``REF.PIN``."""
    for net in netlist.nets:
        if token in (net.name, net.name.lstrip("/")):
            reference, pin, _kind = next(node for node in net.nodes if node[0] in netlist.parts)
            return f"{reference}.{pin}"
    reference, _dot, pin = token.rpartition(".")
    if reference in netlist.parts:
        return token
    raise ValueError(f"no net or pin called {token!r}")


def circuit_from(entry: CorpusCircuit, netlist: demos.Netlist, sheet_text: str) -> Built:
    """The entry's circuit from a parsed netlist and the sheet's text (pure: no kicad-cli, no files)."""
    names = {demos._readable(net.name, tuple((r, p) for r, p, _k in net.nodes)) for net in netlist.nets}
    rails = infer_rails_by_name(infer_rails(sheet_text, names), names)
    present = {name: kind for name, kind in rails.items() if name in names}
    notes = [f"power symbol net {name} not in the netlist" for name in sorted(set(rails) - set(present))]
    blocks = tuple(
        demos.DemoBlock(
            block.name, tuple(block.members.split()),
            consumes=tuple(_first_pin(netlist, token) for token in block.inputs.split()),
            produces=tuple(_first_pin(netlist, token) for token in block.outputs.split()),
        )
        for block in entry.blocks
    )
    demo = demos.Demo(
        entry.key, entry.sheet, blocks,
        ground=tuple(name for name, kind in present.items() if kind == "ground"),
        supply=tuple(name for name, kind in present.items() if kind == "power"), summary=entry.summary, lone_nets=True,
    )
    circuit = demos.build_circuit(demo, netlist)
    labels = sheet_labels(sheet_text)
    circuit = replace(circuit, nets=tuple(
        replace(net, label_scope=labels[net.name]) if net.kind == "signal" and len(net.pins) == 1 and net.name in labels else net
        for net in circuit.nets
    ))
    lone = [net.name for net in circuit.nets if net.kind == "signal" and len(net.pins) == 1]
    notes.append(
        f"single-pin nets: {len([n for n in lone if n in labels])} carried by a label or sheet pin (labelled), "
        f"{len([n for n in lone if n not in labels])} with none (no-connect candidates: {[n for n in lone if n not in labels]})"
    )
    return Built(circuit, intent_from(circuit, entry), present, notes)


def intent_from(circuit: demos.Circuit, entry: CorpusCircuit | None = None) -> SchematicIntent:
    """The circuit as the engine's input. No power flags: they declare a net driven for ERC and take no part in placement."""
    connectors = {}
    if entry is not None:
        connectors = {**{r: "input_connector" for r in entry.input_connectors}, **{r: "output_connector" for r in entry.output_connectors}}
    symbols = tuple(
        SymbolIntent(p.reference, p.library or "Device", p.symbol, p.value, role=connectors.get(p.reference)) for p in circuit.parts
    )
    nets = []
    for net in circuit.nets:
        endpoints = tuple(PinEndpoint(reference, pin) for reference, pin in net.pins)
        if net.kind == "signal" and net.label_scope:
            nets.append(NetIntent(net.name, endpoints, routing="label", scope=net.label_scope))   # type: ignore[arg-type]
            continue
        if net.kind == "signal":
            nets.append(NetIntent(net.name, endpoints, routing="auto"))
            continue
        polarity = "negative" if net.kind == "power" and net.name.startswith("-") else None
        nets.append(NetIntent(net.name, endpoints, kind=net.kind, routing="label", scope="global", polarity=polarity))
    flags = {flag for flag, _net in circuit.flags}
    blocks = tuple(
        FunctionalBlockIntent(name, tuple(m for m in members if m not in flags), inputs=inputs, outputs=outputs)
        for name, members, inputs, outputs in circuit.blocks
    )
    return SchematicIntent(symbols=symbols, nets=tuple(nets), blocks=blocks)


def sheet_file(entry: CorpusCircuit) -> Path:
    return CORPUS_ROOT / entry.sheet


def build(entry: CorpusCircuit, cache: Path | None = None) -> Built:
    """Export the netlist with kicad-cli and build the circuit."""
    sheet = sheet_file(entry)
    if not sheet.is_file():
        raise FileNotFoundError(f"{sheet} is not there (corpus clones live under {CORPUS_ROOT})")
    xml = demos.export_netlist(sheet, (cache or env.OUT_DIR / "corpus") / f"{entry.key}.xml")
    return circuit_from(entry, demos.parse_netlist(xml.read_text(encoding="utf-8")), sheet.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------------
# a project for the MCP server to read geometry from


def _symbol_table(sheet: Path) -> dict[str, Path]:
    """KiCad-type entries of the project's ``sym-lib-table`` (searched upward from the sheet), ``${KIPRJMOD}`` resolved."""
    for folder in [sheet.parent, *sheet.parent.parents][:4]:
        table = folder / "sym-lib-table"
        if table.is_file():
            files: dict[str, Path] = {}
            for chunk in re.split(r"\(lib\b", table.read_text(encoding="utf-8", errors="ignore"))[1:]:
                name, kind, uri = (re.search(rf'\({tag}\s+"?([^")]+)"?\)', chunk) for tag in ("name", "type", "uri"))
                if name and uri and (kind is None or kind.group(1).lower() == "kicad"):
                    files[name.group(1)] = Path(uri.group(1).replace("${KIPRJMOD}", str(folder)))
            return files
    return {}


def resolve_libraries(sheet: Path, sheet_text: str, stock_dir: Path | None) -> dict[str, str]:
    """Library -> where its symbols come from: ``stock``, ``project`` (the table's file holds them) or ``embedded``."""
    table = _symbol_table(sheet)
    symbols = embedded_symbols(sheet_text)
    used: dict[str, set[str]] = {}
    for lib_id in symbols:
        library, name = lib_id.split(":", 1)
        used.setdefault(library, set()).add(name)
    where = {}
    for library, names in used.items():
        def holds(path: Path | None) -> bool:
            """The file has each symbol with the pins the sheet's own copy has (a library that has moved on since the sheet
            was drawn does not count), and none placed at 1e-16, an exporter's rounding the server's number pattern misses."""
            if path is None or not path.is_file():
                return False
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"\(at\s+[^)]*\d[eE][-+]?\d", text):
                return False
            for name in names:
                start = text.find(f'(symbol "{name}"')
                if start < 0:
                    return False
                numbers = lambda block: sorted(re.findall(r'\(number\s+"([^"]+)"', block))  # noqa: E731
                if numbers(text[start:_balanced(text, start)]) != numbers(symbols[f"{library}:{name}"]):
                    return False
            return True

        if stock_dir is not None and holds(stock_dir / f"{library}.kicad_sym"):
            where[library] = "stock"
        else:
            where[library] = "project" if holds(table.get(library)) else "embedded"
    return where


def _split_units(sheet_text: str) -> tuple[str, dict[str, list[int]]]:
    """The sheet as it is, and the unit numbers of each part the sheet places in more than one unit (``U5``: 1, 2, 3).

    A part drawn in several units is several instances of one reference; each unit is a placeable of its own (``U5:2``,
    spec 4c) and the server reads the geometry of the unit it is asked for."""
    units: dict[str, set[int]] = {}
    for match in re.finditer(r'\n\t\(symbol\n\t\t\(lib_id "', sheet_text):
        start = match.start() + 1
        block = sheet_text[start:_balanced(sheet_text, start)]
        reference = re.search(r'\(property "Reference" "([^"]*)"', block)
        unit = re.search(r"\(unit (\d+)\)", block)
        if reference and unit and not reference.group(1).startswith("#"):
            units.setdefault(reference.group(1), set()).add(int(unit.group(1)))
    return sheet_text, {ref: sorted(found) for ref, found in units.items() if len(found) > 1}


_PIN_TYPE = re.compile(r"\(pin\s+(input|output|bidirectional)(\s+\w+)")


def passive_two_pin(block: str) -> str:
    """A two-pin symbol's pins as ``passive``. Authors of project libraries type a diode's pins ``input``/``output``; KiCad's
    ERC then calls two of them on one net a conflict (``Output`` to ``Output``) on the human's own sheet as well, which is a
    fault of the data and not of any layout. A two-pin discrete part has no driver."""
    if len(re.findall(r"\(pin\s+\w+\s+\w+", block)) != 2:
        return block
    return _PIN_TYPE.sub(lambda m: f"(pin passive{m.group(2)}", block)


def passive_two_pin_library(text: str) -> str:
    """``passive_two_pin`` on each top-level symbol of a library file's text."""
    out, cursor = [], 0
    for match in re.finditer(r'\s*\(symbol "[^"]+"', text):
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
        if start < cursor:
            continue
        end = _balanced(text, start)
        out.append(text[cursor:start])
        out.append(passive_two_pin(text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def prepare_project(sheet: Path, destination: Path, stock_dir: Path | None) -> tuple[Path, dict[str, list[int]], dict[str, str]]:
    """A project folder the MCP server can read ``sheet``'s symbol geometry from: the sheet, and a
    ``sym-lib-table`` that points each non-stock library at the project's file or at one written from the sheet's embedded
    symbols. Returns the sheet copy, the unit numbers of each multi-unit part, and where each library was resolved."""
    destination.mkdir(parents=True, exist_ok=True)
    text = sheet.read_text(encoding="utf-8")
    where = resolve_libraries(sheet, text, stock_dir)
    table, project, nicknames = [], _symbol_table(sheet), {}
    symbols = embedded_symbols(text)
    for library, source in sorted(where.items()):
        if source == "stock":
            continue
        # the server looks in KiCad's own folder first, so a library of the same name as a stock one (that lacks the
        # symbol) is given another name in the copy
        nickname = re.sub(r"[^A-Za-z0-9_.+-]", "_", library)       # the server reads a nickname up to the first space
        if stock_dir is not None and (stock_dir / f"{library}.kicad_sym").is_file():
            nickname += "_sbx"
        if nickname != library:
            text = text.replace(f'"{library}:', f'"{nickname}:')
            nicknames[library] = nickname
        if source == "project":
            path = destination / f"{nickname}.kicad_sym"
            path.write_text(passive_two_pin_library(project[library].read_text(encoding="utf-8", errors="ignore")), encoding="utf-8")
            uri = str(path).replace("\\", "/")
        else:
            blocks = [
                passive_two_pin(re.sub(r'^\(symbol "[^"]+"', f'(symbol "{lib_id.split(":", 1)[1]}"', block, count=1))
                for lib_id, block in symbols.items() if lib_id.startswith(f"{library}:")
            ]
            path = destination / f"{nickname}.kicad_sym"
            path.write_text(
                '(kicad_symbol_lib (version 20231120) (generator "sbx")'+"\n"+ "\n".join(blocks) + "\n" + ')' + "\n", encoding="utf-8",
            )
            uri = str(path).replace("\\", "/")
        table.append(f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")(options "")(descr ""))')
    header = "(sym_lib_table"+"\n  (version 7)\n"
    (destination / "sym-lib-table").write_text(header + "\n".join(table) + "\n)\n", encoding="utf-8")
    # a build places symbols by the library name the netlist has; where the copy had to rename one, this says to what
    (destination / NICKNAMES_FILE).write_text(json.dumps(nicknames, indent=2, sort_keys=True), encoding="utf-8")
    split, units = _split_units(text)
    copy = destination / sheet.name
    copy.write_text(split, encoding="utf-8")
    return copy, units, where


# ---------------------------------------------------------------------------------------------------------------------
# the data module: the circuits as engine fixtures (``corpus_<key>``), so a direct build can take them by name

CIRCUITS_MODULE = env.REPO_ROOT / "scripts" / "schematic_corpus_circuits.py"
MODULE_HEADER = '''"""Circuits from other authors' KiCad sheets (a corpus of public projects) as engine inputs.

Generated by ``python scripts/ci/sandbox/sandbox.py corpus --write`` (``sbx/corpus.py`` holds the sheets, the blocks and the
way net kinds are inferred). Data, not layout modes: nothing here says where a symbol stands. The symbols come from
libraries that are not KiCad's own for some parts (see ``CORPUS_SHEETS``); the sandbox supplies them to a build. The engine
must not import this module.
"""
from __future__ import annotations

from backend.schematic import FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent


def _p(reference: str, pin: str) -> PinEndpoint:
    return PinEndpoint(reference, pin)


def _flag(reference: str) -> SymbolIntent:
    # Electrical source declaration for KiCad ERC, not a layout exception.
    return SymbolIntent(
        reference, "power", "PWR_FLAG", "PWR_FLAG",
        role="power_source", authoring_auxiliary=True,
    )
'''


def fixture_key(key: str) -> str:
    return f"corpus_{key}"


def render_module(built: dict[str, Built]) -> str:
    chunks = [MODULE_HEADER]
    for key, item in built.items():
        entry = CIRCUITS[key]
        roles = {r: "input_connector" for r in entry.input_connectors} | {r: "output_connector" for r in entry.output_connectors}
        chunks.append("\n" + demos.render_function(item.circuit, entry.summary, roles, fixture_key(key)))
    registry = "\n".join(f"    {fixture_key(key)!r}: {fixture_key(key)}," for key in built)
    chunks.append(f"\n\nCORPUS_CIRCUITS = {{\n{registry}\n}}\n")
    sheets = "\n".join(f"    {fixture_key(key)!r}: {CIRCUITS[key].sheet!r}," for key in built)
    chunks.append(f"\n# the sheet each circuit was read from, relative to the corpus folder\nCORPUS_SHEETS = {{\n{sheets}\n}}\n")
    return "\n".join(chunks)


def write_circuits(destination: Path = CIRCUITS_MODULE) -> Path:
    """Write the data module (LF endings: a new file)."""
    destination.write_bytes(render_module({key: build(entry) for key, entry in CIRCUITS.items()}).encode("utf-8"))
    return destination
