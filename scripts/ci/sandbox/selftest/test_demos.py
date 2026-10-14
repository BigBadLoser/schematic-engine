"""KiCad demo circuits as engine inputs: the netlist reader, the circuit builder and the data it writes (no KiCad needed)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sbx import demos  # noqa: E402

# Two resistors in series from a source to ground, with an unconnected pin on the source and a net KiCad named by a label.
NETLIST = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R1"><value>1k</value><libsource lib="Device" part="R"/>
      <units><unit name="A"><pins><pin num="1"/><pin num="2"/></pins></unit></units></comp>
    <comp ref="R2"><value>2k</value><libsource lib="Device" part="R"/>
      <units><unit name="A"><pins><pin num="1"/><pin num="2"/></pins></unit></units></comp>
    <comp ref="V1"><value>5</value><libsource lib="Simulation_SPICE" part="VDC"/>
      <units><unit name="A"><pins><pin num="1"/><pin num="2"/><pin num="3"/></pins></unit></units></comp>
  </components>
  <nets>
    <net code="1" name="GND"><node ref="R2" pin="2" pintype="passive"/><node ref="V1" pin="2" pintype="passive"/></net>
    <net code="2" name="/mid"><node ref="R1" pin="2" pintype="passive"/><node ref="R2" pin="1" pintype="passive"/></net>
    <net code="3" name="Net-(R1-Pad1)"><node ref="R1" pin="1" pintype="passive"/><node ref="V1" pin="1" pintype="passive"/></net>
  </nets>
</export>
"""

DEMO = demos.Demo(
    "demo_divider", "x.kicad_sch",
    blocks=(
        demos.DemoBlock("source", ("V1",), produces=("V1.1",)),
        demos.DemoBlock("divider", ("R1", "R2"), consumes=("R1.1",)),
    ),
)


class NetlistTests(unittest.TestCase):
    def test_parts_carry_their_library_value_and_pins(self):
        netlist = demos.parse_netlist(NETLIST)
        self.assertEqual(netlist.parts["R1"], demos.Part("R1", "Device", "R", "1k", ("1", "2")))
        self.assertEqual(netlist.parts["V1"].pins, ("1", "2", "3"))

    def test_nets_carry_their_pins_and_pin_types(self):
        names = {net.name: net.nodes for net in demos.parse_netlist(NETLIST).nets}
        self.assertEqual(names["/mid"], (("R1", "2", "passive"), ("R2", "1", "passive")))


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.circuit = demos.build_circuit(DEMO, demos.parse_netlist(NETLIST))
        self.nets = {net.name: net for net in self.circuit.nets}

    def test_a_label_loses_its_slash_and_an_unnamed_net_is_named_after_its_first_pin(self):
        self.assertEqual(set(self.nets), {"GND", "mid", "N_R1_1"})

    def test_ground_is_drawn_with_ground_symbols_and_the_rest_are_signals(self):
        self.assertEqual(self.nets["GND"].kind, "ground")
        self.assertEqual(self.nets["mid"].kind, "signal")

    def test_a_power_net_nothing_drives_gets_a_flag_that_joins_the_block_of_its_first_part(self):
        self.assertEqual(self.circuit.flags, (("#FLG1", "GND"),))
        members = {name: members for name, members, _in, _out in self.circuit.blocks}
        self.assertIn("#FLG1", members["divider"])          # R2 is the first part on GND
        self.assertNotIn("#FLG1", members["source"])

    def test_a_pin_nothing_joins_is_a_no_connect(self):
        self.assertEqual(self.circuit.no_connects, (("V1", "3"),))

    def test_block_flow_is_named_by_pins_and_becomes_nets(self):
        flow = {name: (inputs, outputs) for name, _members, inputs, outputs in self.circuit.blocks}
        self.assertEqual(flow["source"], ((), ("N_R1_1",)))
        self.assertEqual(flow["divider"], (("N_R1_1",), ()))

    def test_every_part_must_be_in_exactly_one_block(self):
        lonely = demos.Demo("demo_x", "x", blocks=(demos.DemoBlock("only", ("R1",)),))
        with self.assertRaises(ValueError):
            demos.build_circuit(lonely, demos.parse_netlist(NETLIST))
        twice = demos.Demo("demo_x", "x", blocks=(demos.DemoBlock("a", ("R1", "R2", "V1")), demos.DemoBlock("b", ("R1",))))
        with self.assertRaises(ValueError):
            demos.build_circuit(twice, demos.parse_netlist(NETLIST))

    def test_a_flow_pin_that_is_on_no_net_is_an_error(self):
        bad = demos.Demo("demo_x", "x", blocks=(demos.DemoBlock("a", ("V1", "R1", "R2"), produces=("V1.3",)),))
        with self.assertRaises(ValueError):
            demos.build_circuit(bad, demos.parse_netlist(NETLIST))

    def test_a_ground_net_that_does_not_exist_is_an_error(self):
        wrong = demos.Demo("demo_x", "x", blocks=DEMO.blocks, ground=("GNDA",))
        with self.assertRaises(ValueError):
            demos.build_circuit(wrong, demos.parse_netlist(NETLIST))

    def test_a_net_may_be_renamed(self):
        renamed = demos.Demo("demo_x", "x", blocks=DEMO.blocks, rename={"/mid": "OUT"})
        circuit = demos.build_circuit(renamed, demos.parse_netlist(NETLIST))
        self.assertIn("OUT", {net.name for net in circuit.nets})


class DataModuleTests(unittest.TestCase):
    """The module the engine runs read is plain data: it must run, and give back what the circuit said."""

    def test_the_generated_module_builds_a_valid_intent(self):
        circuit = demos.build_circuit(DEMO, demos.parse_netlist(NETLIST))
        namespace: dict[str, object] = {}
        source = demos.render_function(circuit, "a divider")
        exec(compile(demos.HEADER + "\n\n" + source, "<generated>", "exec"), namespace)
        intent = namespace["demo_divider"]()
        self.assertEqual(intent.references, ("V1", "R1", "R2", "#FLG1"))
        self.assertEqual({net.name for net in intent.all_nets}, {"GND", "mid", "N_R1_1"})
        self.assertEqual([endpoint.token for endpoint in intent.no_connects], ["V1.3"])
        ground = next(net for net in intent.all_nets if net.name == "GND")
        self.assertEqual((ground.kind, ground.routing, ground.expected_driver_count), ("ground", "label", 1))
        self.assertEqual([block.name for block in intent.blocks], ["source", "divider"])
        self.assertFalse(intent.relations)

    def test_the_checked_in_module_matches_what_the_demos_generate_to(self):
        """Every demo the sandbox knows is in the data module, and each builds."""
        from scripts import schematic_demo_circuits as module

        self.assertEqual(set(module.DEMO_CIRCUITS), set(demos.DEMOS))
        for build in module.DEMO_CIRCUITS.values():
            intent = build()
            members = [reference for block in intent.blocks for reference in block.members]
            self.assertEqual(sorted(members), sorted(intent.references))       # every symbol is in one block

    def test_the_demos_chosen_use_only_standard_library_symbols(self):
        from scripts import schematic_demo_circuits as module

        for build in module.DEMO_CIRCUITS.values():
            for symbol in build().symbols:
                self.assertNotIn(":", symbol.library)                           # a library name, not a project nickname
                self.assertTrue(symbol.library in {"Device", "Diode", "Regulator_Linear", "Simulation_SPICE",
                                                   "Transistor_BJT", "Transistor_FET", "power"}, symbol.library)


if __name__ == "__main__":
    unittest.main()
