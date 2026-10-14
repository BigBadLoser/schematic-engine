"""Corpus sheets as engine inputs: inferred net kinds, blocks by net, libraries, units and geometry (a synthetic sheet; no
``.work``, no KiCad, no MCP server)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import corpus, demos, ladder  # noqa: E402

LIB = """(lib_symbols
  (symbol "power:GND" (power) (pin_names (offset 0))
    (symbol "GND_0_1" (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))
  (symbol "power:-5V" (power)
    (symbol "-5V_0_1" (pin power_in line (at 0 0 90) (length 0) (name "-5V") (number "1"))))
  (symbol "power:+3.3V" (power)
    (symbol "+3.3V_0_1" (pin power_in line (at 0 0 90) (length 0) (name "+3V3") (number "1"))))
  (symbol "mylib:Op" (symbol "Op_1_1" (pin input line (at -5 0 0) (length 2) (name "IN") (number "1"))))
)"""


def placed(lib_id: str, reference: str, value: str, unit: int = 1) -> str:
    return (
        f'\n\t(symbol\n\t\t(lib_id "{lib_id}")\n\t\t(at 0 0 0)\n\t\t(unit {unit})\n'
        f'\t\t(property "Reference" "{reference}" (at 0 0 0))\n\t\t(property "Value" "{value}" (at 0 0 0))\n\t)'
    )


SHEET = (
    "(kicad_sch\n" + LIB + placed("power:GND", "#PWR01", "GND") + placed("power:-5V", "#PWR02", "-5V")
    + placed("power:+3.3V", "#PWR03", "+3.3V") + placed("mylib:Op", "U1", "Op") + placed("mylib:Op", "U1", "Op", unit=2)
    + '\n\t(wire (pts (xy 0 0) (xy 10 0)))\n\t(wire (pts (xy 10 0) (xy 10 5.5)))\n)\n'
)

NETLIST = """<export version="E"><components>
 <comp ref="J1"><value>in</value><libsource lib="Connector" part="Conn_01x02"/><units><unit name="A"><pins><pin num="1"/><pin num="2"/><pin num="3"/></pins></unit></units></comp>
 <comp ref="R1"><value>1k</value><libsource lib="Device" part="R"/><units><unit name="A"><pins><pin num="1"/><pin num="2"/></pins></unit></units></comp>
 <comp ref="U1"><value>Op</value><libsource lib="mylib" part="Op"/><units><unit name="A"><pins><pin num="1"/><pin num="2"/><pin num="3"/></pins></unit></units></comp>
 </components><nets>
 <net code="1" name="GND"><node ref="J1" pin="2" pintype="passive"/><node ref="R1" pin="2" pintype="passive"/><node ref="U1" pin="3" pintype="power_in"/></net>
 <net code="2" name="/IN"><node ref="J1" pin="1" pintype="passive"/><node ref="R1" pin="1" pintype="passive"/></net>
 <net code="3" name="+3V3"><node ref="U1" pin="2" pintype="power_in"/></net>
 <net code="4" name="/PORT"><node ref="U1" pin="1" pintype="input"/></net>
 <net code="5" name="unconnected-(J1-Pad3)"><node ref="J1" pin="3" pintype="passive"/></net>
 </nets></export>"""

ENTRY = corpus.CorpusCircuit(
    "tiny", "x/tiny.kicad_sch",
    blocks=(corpus.B("source", "J1", outputs="IN"), corpus.B("stage", "R1 U1", "IN", "PORT")),
    input_connectors=("J1",),
)


class InferenceTests(unittest.TestCase):
    def test_rails_come_from_power_symbols_and_the_ground_pattern(self):
        self.assertEqual(corpus.infer_rails(SHEET), {"GND": "ground", "-5V": "power", "+3.3V": "power"})

    def test_an_older_sheet_names_the_net_after_the_power_pin(self):
        self.assertEqual(corpus.infer_rails(SHEET, {"GND", "+3V3"}), {"GND": "ground", "-5V": "power", "+3V3": "power"})

    def test_a_label_without_a_power_symbol_is_a_signal(self):
        built = corpus.circuit_from(ENTRY, demos.parse_netlist(NETLIST), SHEET)
        kinds = {net.name: net.kind for net in built.intent.nets}
        self.assertEqual(kinds, {"GND": "ground", "IN": "signal", "+3V3": "power", "PORT": "signal"})


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.built = corpus.circuit_from(ENTRY, demos.parse_netlist(NETLIST), SHEET)

    def test_a_named_net_of_one_pin_is_kept_and_an_unconnected_pin_is_not(self):
        names = {net.name for net in self.built.intent.nets}
        self.assertIn("PORT", names)
        self.assertEqual(self.built.circuit.no_connects, (("J1", "3"),))

    def test_blocks_name_nets_and_the_entry_names_connectors(self):
        blocks = {b.name: b for b in self.built.intent.blocks}
        self.assertEqual(blocks["stage"].inputs, ("IN",))
        self.assertEqual(blocks["stage"].outputs, ("PORT",))
        roles = {s.reference: s.role for s in self.built.intent.symbols}
        self.assertEqual(roles["J1"], "input_connector")
        self.assertIsNone(roles["R1"])

    def test_an_input_may_be_given_as_a_pin(self):
        entry = corpus.CorpusCircuit("tiny", "x", blocks=(corpus.B("a", "J1 R1", outputs="U1.2"), corpus.B("b", "U1", "R1.1")))
        built = corpus.circuit_from(entry, demos.parse_netlist(NETLIST), SHEET)
        self.assertEqual(built.circuit.blocks[0][3], ("+3V3",))

    def test_an_unknown_net_is_an_error(self):
        entry = corpus.CorpusCircuit("tiny", "x", blocks=(corpus.B("a", "J1 R1 U1", outputs="NOPE"),))
        with self.assertRaises(ValueError):
            corpus.circuit_from(entry, demos.parse_netlist(NETLIST), SHEET)

    def test_negative_supply_is_marked_by_polarity_only(self):
        sheet = SHEET.replace("+3.3V", "-3V3")
        netlist = NETLIST.replace("+3V3", "-3V3")
        built = corpus.circuit_from(ENTRY, demos.parse_netlist(netlist), sheet)
        net = next(n for n in built.intent.nets if n.name == "-3V3")
        self.assertEqual((net.kind, net.polarity), ("power", "negative"))


class LibraryTests(unittest.TestCase):
    def test_embedded_symbols_are_read_whole(self):
        found = corpus.embedded_symbols(SHEET)
        self.assertEqual(set(found), {"power:GND", "power:-5V", "power:+3.3V", "mylib:Op"})
        self.assertIn('"Op_1_1"', found["mylib:Op"])

    def test_a_library_that_is_not_kicads_is_written_from_the_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "in" / "s.kicad_sch"
            sheet.parent.mkdir()
            sheet.write_text(SHEET, encoding="utf-8")
            copy, units, where = corpus.prepare_project(sheet, Path(tmp) / "out", None)
            self.assertEqual(where["mylib"], "embedded")
            table = (Path(tmp) / "out" / "sym-lib-table").read_text(encoding="utf-8")
            self.assertIn('(name "mylib")', table)
            library = (Path(tmp) / "out" / "mylib.kicad_sym").read_text(encoding="utf-8")
            self.assertIn('(symbol "Op"', library)
            self.assertNotIn("mylib:Op", library)

    def test_a_project_file_that_lacks_the_pins_the_sheet_has_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "sym-lib-table").write_text(
                '(sym_lib_table (lib (name "mylib")(type "KiCad")(uri "${KIPRJMOD}/my.kicad_sym")(options "")(descr "")))',
                encoding="utf-8",
            )
            sheet = folder / "s.kicad_sch"
            sheet.write_text(SHEET.replace('(number "1"))))\n  (symbol "power:GND"', '(number "1"))))\n  (symbol "power:GND"'), encoding="utf-8")
            (folder / "my.kicad_sym").write_text('(kicad_symbol_lib (symbol "Op"))', encoding="utf-8")
            self.assertEqual(corpus.resolve_libraries(sheet, SHEET, None)["mylib"], "embedded")
            (folder / "my.kicad_sym").write_text(
                '(kicad_symbol_lib (symbol "Op" (symbol "Op_1_1" (pin input line (at -5 0 0) (name "IN") (number "1")))))',
                encoding="utf-8",
            )
            self.assertEqual(corpus.resolve_libraries(sheet, SHEET, None)["mylib"], "project")

    def test_a_part_placed_in_several_units_is_listed_by_its_unit_numbers(self):
        split, units = corpus._split_units(SHEET)
        self.assertEqual(units, {"U1": [1, 2]})
        self.assertEqual(split, SHEET)                  # the sheet is read as it is: the server finds a unit by its number


def payload_for(*references: str, pins: int = 2) -> dict:
    def orientation(rotation: int, mirror: str) -> dict:
        tips = {str(n): [float(n), 0.0, 1.0, 0.0] for n in range(1, pins + 1)}
        return {"rotation": rotation, "mirror": mirror, "pins": tips, "box": [-2.0, -1.0, 2.0, 1.0], "body": [-1.0, -1.0, 1.0, 1.0]}

    return {"symbols": {
        r: {"x": 50.0, "y": 50.0, "orientations": [orientation(rot, m) for rot in (0, 90, 180, 270) for m in ("", "x", "y")]}
        for r in references
    }}


class GeometryTests(unittest.TestCase):
    def test_a_multi_unit_part_is_expanded_to_one_symbol_per_unit_with_its_own_pins(self):
        from backend.schematic.intent import expand_units
        built = corpus.circuit_from(ENTRY, demos.parse_netlist(NETLIST), SHEET)
        expanded = expand_units(built.intent, {"U1": {1: {"1", "2"}, 2: {"3"}}})
        self.assertEqual({s.reference for s in expanded.symbols}, {"J1", "R1", "U1:1", "U1:2"})
        payload = payload_for("J1", "R1")
        payload["symbols"]["U1:1"] = payload_for("U1:1", pins=2)["symbols"]["U1:1"]
        payload["symbols"]["U1:2"] = payload_for("U1:2", pins=1)["symbols"]["U1:2"]
        payload["symbols"]["U1:2"]["orientations"] = [
            {**o, "pins": {"3": o["pins"]["1"]}} for o in payload["symbols"]["U1:2"]["orientations"]
        ]
        symbols, nets, _reach, _half = ladder.symbols_and_nets(expanded, payload)
        by_reference = {s.reference: s for s in symbols}
        self.assertEqual(sorted(by_reference["U1:1"].geometry[by_reference["U1:1"].orientation].pins), ["1", "2"])
        self.assertEqual(sorted(by_reference["U1:2"].geometry[by_reference["U1:2"].orientation].pins), ["3"])
        pins = {c.net: {p for p, _ in [(r, q) for r, q in c.pins]} for c in nets}
        self.assertIn("U1:2", {r for c in nets for r, _p in c.pins})                # GND's U1 pin 3 is unit 2's

    def test_symbols_nets_and_reach_come_from_the_intent(self):
        built = corpus.circuit_from(ENTRY, demos.parse_netlist(NETLIST), SHEET)
        payload = payload_for("J1", "R1")
        payload["symbols"]["U1"] = payload_for("U1", pins=3)["symbols"]["U1"]
        symbols, nets, reach, half = ladder.symbols_and_nets(built.intent, payload)
        by_reference = {s.reference: s for s in symbols}
        self.assertEqual(set(by_reference), {"J1", "R1", "U1"})
        self.assertEqual(by_reference["R1"].position, (0.0, 0.0))                # the human's anchor is not an input
        self.assertEqual(len(by_reference["R1"].geometry), 4)                     # two pins: rotations only
        self.assertEqual(len(by_reference["U1"].geometry), 12)                    # three: mirrors too
        styles = {c.net: c.style for c in nets}
        self.assertEqual(styles["GND"], "power")
        self.assertEqual(styles["IN"], "wire")
        self.assertIn(("R1", "1"), reach)

    def test_a_pin_the_geometry_lacks_is_named(self):
        built = corpus.circuit_from(ENTRY, demos.parse_netlist(NETLIST), SHEET)
        payload = payload_for("J1", "R1")
        payload["symbols"]["U1"] = payload_for("U1", pins=2)["symbols"]["U1"]
        with self.assertRaisesRegex(LookupError, r"U1.*3"):
            ladder.symbols_and_nets(built.intent, payload)

    def test_the_human_sheets_wire_is_summed(self):
        self.assertAlmostEqual(ladder.human_wire_mm(SHEET), 15.5)


if __name__ == "__main__":
    unittest.main()
