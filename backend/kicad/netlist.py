from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


Endpoint = tuple[str, str]


@dataclass(frozen=True)
class SchematicNetlist:
    nets: dict[str, frozenset[Endpoint]]
    pins: frozenset[Endpoint]
    references: frozenset[str]
    virtual_references: frozenset[str]


def parse_kicad_xml(document: str) -> SchematicNetlist:
    """Read KiCad's canonical component/pin inventory and electrical net groups."""
    try:
        export = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError("Invalid KiCad XML netlist") from error
    components = export.find("components")
    libparts = export.find("libparts")
    nets_element = export.find("nets")
    if export.tag != "export" or any(
        section is None for section in (components, libparts, nets_element)
    ):
        raise ValueError("KiCad XML netlist is missing component, library, or net data")

    library_pins: dict[tuple[str, str], set[str]] = {}
    for part in libparts.findall("libpart"):
        key = (part.get("lib", ""), part.get("part", ""))
        numbers = {pin.get("num", "") for pin in part.findall("pins/pin")}
        if not all(key) or "" in numbers or key in library_pins:
            raise ValueError("Invalid or duplicate KiCad netlist library part")
        library_pins[key] = numbers

    references: set[str] = set()
    virtual: set[str] = set()
    pins: set[Endpoint] = set()
    for component in components.findall("comp"):
        reference = component.get("ref", "")
        source = component.find("libsource")
        if not reference or reference in references or source is None:
            raise ValueError("Invalid or duplicate KiCad netlist component")
        key = (source.get("lib", ""), source.get("part", ""))
        if key not in library_pins:
            raise ValueError(f"KiCad netlist is missing pin inventory for {reference}")
        references.add(reference)
        if key[0] == "power":
            virtual.add(reference)
        pins.update((reference, pin) for pin in library_pins[key])

    nets: dict[str, frozenset[Endpoint]] = {}
    assigned: set[Endpoint] = set()
    for net in nets_element.findall("net"):
        code = net.get("code", "")
        endpoints = [
            (node.get("ref", ""), node.get("pin", ""))
            for node in net.findall("node")
        ]
        if not code or code in nets or any(not all(endpoint) for endpoint in endpoints):
            raise ValueError("Invalid or duplicate KiCad netlist net")
        members = set(endpoints)
        if len(members) != len(endpoints) or assigned & members:
            raise ValueError("KiCad netlist assigns a pin more than once")
        if members - pins:
            raise ValueError("KiCad netlist references pins absent from its inventory")
        assigned.update(members)
        nets[code] = frozenset(members)
    return SchematicNetlist(nets, frozenset(pins), frozenset(references), frozenset(virtual))


class KiCadCliNetlistExporter:
    """Export read-only electrical evidence using KiCad's supported CLI."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = (
            executable
            or os.environ.get("KICAD_AI_KICAD_CLI")
            or shutil.which("kicad-cli")
            or "kicad-cli"
        )

    def export_xml(self, root: Path, schematic_file: Path) -> str:
        root = root.resolve()
        schematic_file = schematic_file.resolve()
        if not schematic_file.is_file():
            raise ValueError(f"Schematic file does not exist: {schematic_file}")
        try:
            schematic_file.relative_to(root)
        except ValueError as error:
            raise ValueError("Schematic file must be inside the selected project") from error
        if not Path(self.executable).is_file() and shutil.which(self.executable) is None:
            raise RuntimeError("kicad-cli is not available for schematic netlist verification")

        # A fresh temporary output prevents stale evidence from passing a build.
        with tempfile.TemporaryDirectory(prefix="kicad-ai-netlist-") as directory:
            output = Path(directory) / "netlist.xml"
            command = [
                self.executable, "sch", "export", "netlist", "--format", "kicadxml",
                "--output", str(output), str(schematic_file),
            ]
            try:
                completed = subprocess.run(
                    command, cwd=root, capture_output=True, text=True,
                    check=False, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError("KiCad schematic netlist export failed") from error
            if completed.returncode != 0 or not output.is_file():
                detail = completed.stderr.strip() or completed.stdout.strip() or "no XML output"
                raise RuntimeError(f"KiCad schematic netlist export failed: {detail}")
            return output.read_text(encoding="utf-8-sig")
