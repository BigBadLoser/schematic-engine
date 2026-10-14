import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.kicad.netlist import KiCadCliNetlistExporter, parse_kicad_xml
from backend.schematic.connectivity import verify_intent_netlist
from backend.schematic import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent


def netlist_xml(groups: tuple[tuple[str, ...], ...], *, pin_count: int = 2) -> str:
    """Independent test circuit containing three physical two-pin symbols."""
    components = "".join(
        f'<comp ref="{ref}"><libsource lib="Device" part="R"/></comp>'
        for ref in ("R1", "R2", "R3")
    )
    pins = "".join(f'<pin num="{pin}"/>' for pin in range(1, pin_count + 1))
    nets = ""
    for index, group in enumerate(groups, 1):
        nodes = "".join(
            f'<node ref="{token.rsplit(".", 1)[0]}" pin="{token.rsplit(".", 1)[1]}"/>'
            for token in group
        )
        nets += f'<net code="{index}" name="arbitrary_{index}">{nodes}</net>'
    return (
        f'<export version="E"><components>{components}</components>'
        f'<libparts><libpart lib="Device" part="R"><pins>{pins}</pins></libpart></libparts>'
        f'<nets>{nets}</nets></export>'
    )


def circuit_intent(*, no_connect: bool = False) -> SchematicIntent:
    return SchematicIntent(
        symbols=tuple(SymbolIntent(ref, "Device", "R", "1k") for ref in ("R1", "R2", "R3")),
        nets=(
            NetIntent("A", (PinEndpoint("R1", "1"), PinEndpoint("R2", "1"))),
            NetIntent("B", (PinEndpoint("R2", "2"), PinEndpoint("R3", "1"))),
        ),
        no_connects=(PinEndpoint("R1", "2"),) if no_connect else (),
    )


GOOD_GROUPS = (("R1.1", "R2.1"), ("R2.2", "R3.1"))


class SchematicNetlistTests(unittest.TestCase):
    def test_generated_net_names_do_not_change_connectivity(self) -> None:
        result = verify_intent_netlist(circuit_intent(), parse_kicad_xml(netlist_xml(GOOD_GROUPS)))
        self.assertEqual(result["verified_net_count"], 2)
        self.assertEqual(result["status"], "PASS")

    def test_split_net_is_rejected(self) -> None:
        groups = (("R1.1",), ("R2.1",), ("R2.2", "R3.1"))
        with self.assertRaisesRegex(RuntimeError, "A: split across 2 actual nets"):
            verify_intent_netlist(circuit_intent(), parse_kicad_xml(netlist_xml(groups)))

    def test_short_between_intended_nets_is_rejected(self) -> None:
        groups = (("R1.1", "R2.1", "R2.2", "R3.1"),)
        with self.assertRaisesRegex(RuntimeError, "short/unexpected endpoints"):
            verify_intent_netlist(circuit_intent(), parse_kicad_xml(netlist_xml(groups)))

    def test_unrouted_pin_and_nonexistent_pin_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing net endpoints R2.1"):
            verify_intent_netlist(circuit_intent(), parse_kicad_xml(netlist_xml((("R1.1",), GOOD_GROUPS[1]))))
        missing = netlist_xml((("R1.1", "R2.1"),), pin_count=1)
        with self.assertRaisesRegex(RuntimeError, "missing pins R2.2"):
            verify_intent_netlist(circuit_intent(), parse_kicad_xml(missing))

    def test_no_connect_may_be_omitted_or_exported_as_an_isolated_net(self) -> None:
        for groups in (GOOD_GROUPS, (*GOOD_GROUPS, ("R1.2",))):
            with self.subTest(groups=groups):
                verify_intent_netlist(circuit_intent(no_connect=True), parse_kicad_xml(netlist_xml(groups)))

    def test_no_connect_cannot_be_wired_to_another_pin(self) -> None:
        groups = (*GOOD_GROUPS, ("R1.2", "R3.2"))
        with self.assertRaisesRegex(RuntimeError, "no-connect R1.2: connected"):
            verify_intent_netlist(circuit_intent(no_connect=True), parse_kicad_xml(netlist_xml(groups)))

    def test_unexpected_connection_between_unspecified_pins_is_rejected(self) -> None:
        groups = (*GOOD_GROUPS, ("R1.2", "R3.2"))
        with self.assertRaisesRegex(RuntimeError, "unexpected physical connection"):
            verify_intent_netlist(circuit_intent(), parse_kicad_xml(netlist_xml(groups)))

    def test_virtual_power_flag_omitted_by_kicad_is_not_a_missing_component(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("R1", "Device", "R", "1k"), SymbolIntent("#FLG01", "power", "PWR_FLAG", "PWR_FLAG")),
            nets=(NetIntent("+3V3", (PinEndpoint("R1", "1"), PinEndpoint("#FLG01", "1")), kind="power"),),
        )
        result = verify_intent_netlist(intent, parse_kicad_xml(netlist_xml((("R1.1",),))))
        self.assertEqual(result["virtual_references"], ["#FLG01"])

    def test_single_pin_net_cannot_connect_to_an_unspecified_pin(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
            nets=(NetIntent("VIN", (PinEndpoint("R1", "1"),)),),
        )
        with self.assertRaisesRegex(RuntimeError, "short/unexpected endpoints R1.2"):
            verify_intent_netlist(intent, parse_kicad_xml(netlist_xml((("R1.1", "R1.2"),))))

    def test_malformed_or_incomplete_evidence_is_rejected(self) -> None:
        for document in ("invalid", "<export/>", "<export><components/><libparts/></export>"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_kicad_xml(document)

    def test_pin_cannot_appear_on_two_exported_nets(self) -> None:
        with self.assertRaisesRegex(ValueError, "assigns a pin more than once"):
            parse_kicad_xml(netlist_xml((("R1.1",), ("R1.1",))))

    def test_unknown_exported_pin_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absent from its inventory"):
            parse_kicad_xml(netlist_xml((("R1.9",),)))


class SchematicNetlistExporterTests(unittest.TestCase):
    def test_cli_export_uses_fresh_output_and_reads_xml(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "test.kicad_sch"
            schematic.write_text("(kicad_sch)")
            destinations = []

            def run(command, **kwargs):
                output = Path(command[command.index("--output") + 1])
                self.assertFalse(output.exists())
                destinations.append(output)
                output.write_text(netlist_xml(GOOD_GROUPS))
                self.assertEqual(command[:6], ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml"])
                self.assertEqual(kwargs["cwd"], root)
                return subprocess.CompletedProcess(command, 0, "", "")

            exporter = KiCadCliNetlistExporter("kicad-cli")
            with patch("backend.kicad.netlist.shutil.which", return_value="kicad-cli"), patch("backend.kicad.netlist.subprocess.run", side_effect=run):
                for _ in range(2):
                    self.assertEqual(exporter.export_xml(root, schematic), netlist_xml(GOOD_GROUPS))
            self.assertNotEqual(destinations[0], destinations[1])
            self.assertTrue(all(not destination.exists() for destination in destinations))

    def test_export_failure_timeout_and_missing_output_fail_closed(self) -> None:
        outcomes = [
            subprocess.CompletedProcess([], 1, "", "bad schematic"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.TimeoutExpired("kicad-cli", 60),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "test.kicad_sch"
            schematic.write_text("(kicad_sch)")
            for outcome in outcomes:
                with self.subTest(outcome=outcome), patch("backend.kicad.netlist.shutil.which", return_value="kicad-cli"), patch("backend.kicad.netlist.subprocess.run") as run:
                    if isinstance(outcome, Exception):
                        run.side_effect = outcome
                    else:
                        run.return_value = outcome
                    with self.assertRaisesRegex(RuntimeError, "netlist export failed"):
                        KiCadCliNetlistExporter("kicad-cli").export_xml(root, schematic)
