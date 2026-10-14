"""KiCad's own demo circuits, redrawn by the engine from their netlists.

The demo projects shipped with KiCad are human-drawn sheets. To see how the engine's drawing of a circuit compares with an
engineer's, the engine is given the demo's *netlist* (parts, values, which pins join) and the functional blocks an
engineer would describe it with, and nothing about where anything stands: that is the input every fixture in
``scripts/schematic_validation_fixtures.py`` has, and no relation is declared.

``kicad-cli sch export netlist`` is the source of parts and nets. What the netlist does not say, and a design has to, is
written in :data:`DEMOS` as data: which blocks the parts form, which nets flow between them, and which nets are drawn with
power symbols. ``write_circuits`` turns all of it into ``scripts/schematic_demo_circuits.py`` (plain data, like the
fixtures), so a run needs neither KiCad's demo folder nor this module.

Only demos whose symbols are all in KiCad's standard libraries are candidates: a demo that draws with symbols of its own
project cannot be rebuilt by name.
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from . import env

CIRCUITS_MODULE = env.REPO_ROOT / "scripts" / "schematic_demo_circuits.py"
KICAD_DEMOS = ("share", "kicad", "demos")


@dataclass(frozen=True)
class DemoBlock:
    name: str
    members: tuple[str, ...]
    consumes: tuple[str, ...] = ()      # pins (as "D1.4") whose nets this block takes in
    produces: tuple[str, ...] = ()      # pins whose nets this block sends on


@dataclass(frozen=True)
class Demo:
    key: str                            # the fixture name
    sheet: str                          # relative to KiCad's demos folder
    blocks: tuple[DemoBlock, ...]
    ground: tuple[str, ...] = ("GND",)  # nets drawn with ground symbols
    supply: tuple[str, ...] = ()        # nets drawn with supply symbols
    rename: Mapping[str, str] = field(default_factory=dict)     # net name in the netlist -> name to use
    summary: str = ""
    lone_nets: bool = False             # a named net with a single pin (a port, a label) is kept, not read as a no-connect


# Four demos, picked for what they ask of a drawing rather than for how they turned out: a transformer-rectifier-regulator
# chain that runs left to right, a switching converter whose parts sit along one rail, a multi-stage amplifier with support
# parts hanging off every transistor, and an op-amp stage with its feedback next to a one-transistor stage. Every symbol is
# a standard-library symbol, which the rest of the demos (project-local symbols) are not.
DEMOS: dict[str, Demo] = {demo.key: demo for demo in (
    Demo(
        "demo_lm317", "simulation/power_supplies/LM317_power_supply/power_supply.kicad_sch",
        blocks=(
            DemoBlock("source", ("V1", "T1", "R3"), produces=("T1.3", "T1.4")),
            DemoBlock("rectifier", ("D1", "C1"), consumes=("D1.4", "D1.3"), produces=("C1.1",)),
            DemoBlock("regulator", ("U1", "D2", "D3", "R4", "R1", "C2", "C3"), consumes=("U1.3",), produces=("U1.2",)),
            DemoBlock("load", ("Rmeas1", "Rload1"), consumes=("Rmeas1.1",)),
        ),
        summary="AC source, transformer, bridge, reservoir capacitor, LM317 with its adjust network, load",
    ),
    Demo(
        "demo_buck", "simulation/power_supplies/buck_conv/buck_conv.kicad_sch",
        blocks=(
            DemoBlock("supply", ("V102",), produces=("V102.1",)),
            DemoBlock("drive", ("V101", "R101"), produces=("R101.1",)),
            DemoBlock("switch", ("Q1", "D101"), consumes=("Q1.2", "Q1.1"), produces=("Q1.3",)),
            DemoBlock("filter", ("L101", "C101"), consumes=("L101.1",), produces=("L101.2",)),
            DemoBlock("load", ("R103",), consumes=("R103.1",)),
        ),
        summary="a buck converter: a MOSFET switch with its gate drive, a freewheel diode, an LC filter and a load",
    ),
    Demo(
        "demo_ampac", "simulation/amplifier-ac/amplifier-ac.kicad_sch",
        blocks=(
            DemoBlock("source", ("V1", "V3"), produces=("V1.1",)),
            DemoBlock(
                "input_pair", ("C1", "R3", "R9", "Q2", "R4", "R10", "Q3", "R5", "R6", "R11", "C2"),
                consumes=("V1.1",), produces=("Q3.1",),
            ),
            DemoBlock("output_stage", ("Q1", "R8", "R12", "L2", "R7", "C3"), consumes=("Q1.2",), produces=("Q1.1",)),
            DemoBlock("collector_network", ("R1", "R2", "L1", "C4"), consumes=("Q1.1",)),
        ),
        supply=("+12V",),
        summary="three NPN transistors: an emitter-coupled input pair driving an output stage with a tuned collector load",
    ),
    Demo(
        "demo_opamp", "simulation/generic_models/generic_opamp_bip.kicad_sch",
        blocks=(
            DemoBlock("source", ("V3",), produces=("V3.1",)),
            DemoBlock("supplies", ("V1", "V2")),
            DemoBlock("opamp_stage", ("R2", "U1", "R1"), consumes=("R2.2",), produces=("U1.5",)),
            DemoBlock("bjt_stage", ("C1", "R4", "R3", "Q1", "R5", "R6", "C2", "R7"), consumes=("C1.1",), produces=("R7.1",)),
        ),
        ground=("GND",), supply=("+5V", "-5V"), rename={"0": "GND"},
        summary="an op-amp inverting stage with its feedback resistor, beside a common-emitter transistor stage",
    ),
)}


# ---------------------------------------------------------------------------------------------------------------------
# the netlist


@dataclass(frozen=True)
class Part:
    reference: str
    library: str
    symbol: str
    value: str
    pins: tuple[str, ...]


@dataclass(frozen=True)
class Net:
    name: str
    nodes: tuple[tuple[str, str, str], ...]     # (reference, pin, pin type)


@dataclass(frozen=True)
class Netlist:
    parts: dict[str, Part]
    nets: tuple[Net, ...]


def demos_folder() -> Path | None:
    cli = env.find_kicad_cli()
    if cli is None:
        return None
    for parent in cli.parents:
        candidate = parent.joinpath(*KICAD_DEMOS)
        if candidate.is_dir():
            return candidate
    return None


def sheet_path(demo: Demo) -> Path:
    root = demos_folder()
    if root is None:
        raise FileNotFoundError("KiCad's demo projects were not found next to kicad-cli")
    return root / demo.sheet


def export_netlist(sheet: Path, destination: Path) -> Path:
    cli = env.find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli was not found")
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(destination), str(sheet)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(f"kicad-cli could not export {sheet.name}: {(completed.stderr or completed.stdout).strip()}")
    return destination


def parse_netlist(text: str) -> Netlist:
    """Parts and nets from the XML ``kicad-cli sch export netlist --format kicadxml`` writes."""
    root = ET.fromstring(text)
    components, listed = root.find("components"), root.find("nets")
    parts: dict[str, Part] = {}
    for comp in components if components is not None else ():
        source = comp.find("libsource")
        if source is None:
            continue
        pins = tuple(pin.get("num", "") for unit in comp.iterfind("units/unit") for pin in unit.iterfind("pins/pin"))
        parts[comp.get("ref", "")] = Part(
            comp.get("ref", ""), source.get("lib", ""), source.get("part", ""),
            (comp.findtext("value") or "").strip(), pins,
        )
    nets = tuple(
        Net(net.get("name", ""), tuple((n.get("ref", ""), n.get("pin", ""), n.get("pintype", "")) for n in net))
        for net in (listed if listed is not None else ())
    )
    return Netlist(parts, nets)


# ---------------------------------------------------------------------------------------------------------------------
# the design


@dataclass(frozen=True)
class CircuitNet:
    name: str
    pins: tuple[tuple[str, str], ...]
    kind: str = "signal"
    driver: tuple[str, str] | None = None       # a pin that drives the net (a power output)
    power_input: bool = False                   # a power input is on it: ERC wants it driven, by a pin or by a flag
    label_scope: str | None = None              # a lone pin the sheet carries through a label: written as a label of this scope

    @property
    def needs_flag(self) -> bool:
        """A power net, or a net a power input is on, that no power output drives is declared driven with a PWR_FLAG."""
        return self.driver is None and (self.kind != "signal" or self.power_input)


@dataclass(frozen=True)
class Circuit:
    key: str
    parts: tuple[Part, ...]                      # in block order
    nets: tuple[CircuitNet, ...]
    flags: tuple[tuple[str, str], ...]           # (flag reference, net it drives)
    no_connects: tuple[tuple[str, str], ...]
    blocks: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]   # name, members, inputs, outputs
    renamed: tuple[tuple[str, str], ...] = ()    # (reference used here, reference in the demo) for those that had to change


# What the schematic library the engine writes with accepts as a reference (its validator's rule, not KiCad's: a demo may
# call a part Rmeas1 or Rload1, which KiCad takes and the library refuses).
_REFERENCE = re.compile(r"^[A-Z]+[0-9]*[A-Z]?$")


def valid_references(references: list[str]) -> dict[str, str]:
    """Old reference -> the reference to use, for every one the schematic library would refuse: Rmeas1 becomes the
    next free R."""
    taken = set(references)
    renames: dict[str, str] = {}
    for old in references:
        if _REFERENCE.match(old):
            continue
        prefix = re.match(r"[A-Z]+", old) or re.match(r"[A-Za-z]", old)
        letters = prefix.group(0).upper() if prefix else "X"
        number = 1
        while f"{letters}{number}" in taken:
            number += 1
        renames[old] = f"{letters}{number}"
        taken.add(renames[old])
    return renames


def _readable(name: str, pins: tuple[tuple[str, str], ...]) -> str:
    """A label's name without the leading slash KiCad gives a local net; an unnamed net after its first pin."""
    if name.startswith("Net-(") or name.startswith("unconnected-"):
        reference, pin = sorted(pins)[0]
        return f"N_{reference}_{pin}"
    return name[1:] if name.startswith("/") else name


def _renamed(demo: Demo, netlist: Netlist, renames: Mapping[str, str]) -> tuple[Demo, Netlist]:
    """The demo and its netlist with every reference in ``renames`` changed, wherever it is named."""
    def ref(reference: str) -> str:
        return renames.get(reference, reference)

    def pin(token: str) -> str:
        reference, number = token.split(".", 1)
        return f"{ref(reference)}.{number}"

    blocks = tuple(
        replace(
            block, members=tuple(ref(member) for member in block.members),
            consumes=tuple(pin(token) for token in block.consumes), produces=tuple(pin(token) for token in block.produces),
        )
        for block in demo.blocks
    )
    parts = {
        ref(reference): replace(part, reference=ref(reference)) for reference, part in netlist.parts.items()
    }
    nets = tuple(
        replace(net, nodes=tuple((ref(reference), number, kind) for reference, number, kind in net.nodes))
        for net in netlist.nets
    )
    return replace(demo, blocks=blocks), Netlist(parts, nets)


def build_circuit(demo: Demo, netlist: Netlist) -> Circuit:
    # the demo's blocks name its own references; the circuit uses ones the schematic library accepts
    renames = valid_references(sorted(netlist.parts))
    if renames:
        demo, netlist = _renamed(demo, netlist, renames)
    circuit = _build_circuit(demo, netlist)
    return replace(circuit, renamed=tuple(sorted((new, old) for old, new in renames.items())))


def _build_circuit(demo: Demo, netlist: Netlist) -> Circuit:
    ordered = [reference for block in demo.blocks for reference in block.members]
    if len(set(ordered)) != len(ordered):
        raise ValueError(f"{demo.key}: a part is in two blocks")
    missing = sorted(set(netlist.parts) - set(ordered))
    unknown = sorted(set(ordered) - set(netlist.parts))
    if missing or unknown:
        raise ValueError(f"{demo.key}: blocks miss {missing} and name {unknown}")

    net_of_pin: dict[tuple[str, str], str] = {}
    nets: list[CircuitNet] = []
    used: set[str] = set()
    for net in netlist.nets:
        pins = tuple((reference, pin) for reference, pin, _type in net.nodes if reference in netlist.parts)
        name = demo.rename.get(net.name, demo.rename.get(net.name.lstrip("/"), _readable(net.name, pins))) if pins else ""
        kind = "ground" if name in demo.ground else "power" if name in demo.supply else "signal"
        if len(pins) < 2 and kind == "signal" and not (demo.lone_nets and pins and not net.name.startswith("unconnected-")):
            continue                         # a pin nothing joins is a no-connect, below (a lone pin on a rail is drawn)
        if name in used:
            raise ValueError(f"{demo.key}: two nets are called {name}")
        used.add(name)
        driver = next(((reference, pin) for reference, pin, kind_ in net.nodes if kind_ == "power_out"), None)
        power_input = any(kind_ == "power_in" for _reference, _pin, kind_ in net.nodes)
        nets.append(CircuitNet(name, pins, kind, driver, power_input))
        for pin in pins:
            net_of_pin[pin] = name

    wanted = set(demo.ground) | set(demo.supply)
    absent = sorted(wanted - {net.name for net in nets})
    if absent:
        raise ValueError(f"{demo.key}: no net called {absent}")

    flags = tuple(
        (f"#FLG{index}", net.name)
        for index, net in enumerate((n for n in nets if n.needs_flag), start=1)
    )
    flag_of = {net: flag for flag, net in flags}

    def net_named(pin: str) -> str:
        reference, number = pin.split(".", 1)
        try:
            return net_of_pin[(reference, number)]
        except KeyError:
            raise ValueError(f"{demo.key}: pin {pin} is on no net") from None

    owner_of: dict[str, str] = {reference: block.name for block in demo.blocks for reference in block.members}
    blocks = []
    for block in demo.blocks:
        members = list(block.members)
        for flag, net_name in flags:           # a power flag joins the block of the first part on its net
            first = next(reference for net in nets if net.name == net_name for reference, _pin in net.pins)
            if owner_of[first] == block.name:
                members.append(flag)
        blocks.append((
            block.name, tuple(members),
            tuple(dict.fromkeys(net_named(pin) for pin in block.consumes)),
            tuple(dict.fromkeys(net_named(pin) for pin in block.produces)),
        ))

    connected = set(net_of_pin)
    unused = tuple(
        (part.reference, pin) for reference in ordered for part in (netlist.parts[reference],)
        for pin in part.pins if (reference, pin) not in connected
    )
    return Circuit(
        demo.key, tuple(netlist.parts[reference] for reference in ordered), tuple(nets),
        flags, tuple(dict.fromkeys(unused)), tuple(blocks),
    )


# ---------------------------------------------------------------------------------------------------------------------
# the data module

HEADER = '''"""KiCad's own demo circuits as engine inputs: a netlist and the functional blocks an engineer would describe it with.

Generated by ``python scripts/ci/sandbox/sandbox.py demos --write`` from the demo projects shipped with KiCad
(``sbx/demos.py`` holds the blocks and the way each was chosen). Data, not layout modes: no relation is declared and
nothing here says where a symbol stands, so the engine places the sheet from what the netlist implies, as it does for the
fixtures in ``schematic_validation_fixtures``. The engine must not import this module.
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


def render_function(
    circuit: Circuit, summary: str = "", roles: Mapping[str, str] | None = None, name: str | None = None,
) -> str:
    roles = roles or {}
    lines = [f"def {name or circuit.key}() -> SchematicIntent:"]
    if summary:
        lines.append(f'    """{summary[0].upper() + summary[1:]}."""')
    lines.append("    return SchematicIntent(")
    lines.append("        symbols=(")
    for part in circuit.parts:
        role = f", role={roles[part.reference]!r}" if part.reference in roles else ""
        lines.append(f"            SymbolIntent({part.reference!r}, {part.library!r}, {part.symbol!r}, {part.value!r}{role}),")
    for flag, _net in circuit.flags:
        lines.append(f"            _flag({flag!r}),")
    lines.append("        ),")
    lines.append("        nets=(")
    flag_of = {net: flag for flag, net in circuit.flags}
    for net in circuit.nets:
        pins = ", ".join(f"_p({reference!r}, {pin!r})" for reference, pin in net.pins)
        if net.kind == "signal":
            if net.name in flag_of:             # a power input on an ordinary net: a flag declares it driven
                flag = flag_of[net.name]
                lines.append(f"            NetIntent({net.name!r}, ({pins}, _p({flag!r}, '1')), routing=\"auto\",")
                lines.append(f"                      drivers=(_p({flag!r}, '1'),), expected_driver_count=1),")
            elif net.label_scope:
                lines.append(f"            NetIntent({net.name!r}, ({pins},), routing=\"label\", scope={net.label_scope!r}),")
            else:
                lines.append(f"            NetIntent({net.name!r}, ({pins},), routing=\"auto\"),")
            continue
        if net.name in flag_of:
            flag = flag_of[net.name]
            pins += f", _p({flag!r}, '1')"
            driver = f"_p({flag!r}, '1')"
        else:
            assert net.driver is not None
            driver = f"_p({net.driver[0]!r}, {net.driver[1]!r})"
        # the sign in the netlist's name is the data; the engine reads the polarity, never the name
        polarity = ", polarity=\"negative\"" if net.kind == "power" and net.name.startswith("-") else ""
        lines.append(f"            NetIntent({net.name!r}, ({pins},), kind={net.kind!r}, routing=\"label\", scope=\"global\"{polarity},")
        lines.append(f"                      drivers=({driver},), expected_driver_count=1),")
    lines.append("        ),")
    if circuit.no_connects:
        pins = ", ".join(f"_p({reference!r}, {pin!r})" for reference, pin in circuit.no_connects)
        lines.append(f"        no_connects=({pins},),")
    lines.append("        blocks=(")
    for name, members, consumes, produces in circuit.blocks:
        extra = ""
        if consumes:
            extra += f", inputs={consumes!r}"
        if produces:
            extra += f", outputs={produces!r}"
        lines.append(f"            FunctionalBlockIntent({name!r}, {members!r}{extra}),")
    lines.append("        ),")
    lines.append("    )")
    return "\n".join(lines)


def render_module(circuits: Mapping[str, Circuit]) -> str:
    chunks = [HEADER]
    for key, circuit in circuits.items():
        chunks.append("\n" + render_function(circuit, DEMOS[key].summary))
    registry = "\n".join(f"    {key!r}: {key}," for key in circuits)
    chunks.append(f"\n\nDEMO_CIRCUITS = {{\n{registry}\n}}\n")
    renames = "\n".join(
        f"    {key!r}: {dict(circuit.renamed)!r}," for key, circuit in circuits.items() if circuit.renamed
    )
    chunks.append(
        "\n# Parts the demo calls something the schematic library refuses as a reference (Rmeas1), by the reference the\n"
        "# circuit uses here: to read the demo's own sheet against this circuit.\n"
        f"DEMO_RENAMES: dict[str, dict[str, str]] = {{\n{renames}\n}}\n"
    )
    return "\n".join(chunks)


def build_all(cache_dir: Path | None = None) -> dict[str, Circuit]:
    cache = cache_dir or env.OUT_DIR / "demos"
    circuits: dict[str, Circuit] = {}
    for key, demo in DEMOS.items():
        xml = export_netlist(sheet_path(demo), cache / f"{key}.xml")
        circuits[key] = build_circuit(demo, parse_netlist(xml.read_text(encoding="utf-8")))
    return circuits


def write_circuits(destination: Path = CIRCUITS_MODULE) -> Path:
    """Write the data module with the checkout's line endings (CRLF here)."""
    text = render_module(build_all())
    crlf = not destination.exists() or b"\r\n" in destination.read_bytes()
    destination.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return destination


def describe(circuit: Circuit) -> str:
    """The circuit as a person reads it, to check the netlist and the blocks before they become data."""
    lines = [f"{circuit.key}: {len(circuit.parts)} parts, {len(circuit.nets)} nets, {len(circuit.blocks)} blocks"]
    for name, members, consumes, produces in circuit.blocks:
        flow = f"   takes {', '.join(consumes) or '-'}; gives {', '.join(produces) or '-'}"
        lines.append(f"  block {name:<18} {' '.join(members)}{flow}")
    for net in circuit.nets:
        pins = " ".join(f"{reference}.{pin}" for reference, pin in net.pins)
        drawn = {"ground": "ground symbols", "power": "supply symbols"}.get(net.kind, "")
        lines.append(f"  net {net.name:<14} {pins}{('   <- ' + drawn) if drawn else ''}")
    if circuit.renamed:
        lines.append("  renamed: " + ", ".join(f"{old} is {new} here" for new, old in circuit.renamed))
    if circuit.flags:
        lines.append("  power flags: " + ", ".join(f"{flag} on {net}" for flag, net in circuit.flags))
    if circuit.no_connects:
        lines.append("  no-connect: " + ", ".join(f"{reference}.{pin}" for reference, pin in circuit.no_connects))
    return "\n".join(lines)
