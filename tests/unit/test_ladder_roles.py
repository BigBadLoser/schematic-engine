from __future__ import annotations

import random
import unittest

from backend.schematic import FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.ladder_roles import Attachment, Feedback, Lane, Rung, _is_source, classify
from scripts.schematic_demo_circuits import DEMO_CIRCUITS


def ep(reference: str, pin: str) -> PinEndpoint:
    return PinEndpoint(reference, pin)


def toy() -> tuple[SchematicIntent, dict[str, int]]:
    symbols = (
        SymbolIntent("V1", "Simulation_SPICE", "V", "source"),
        SymbolIntent("A1", "Device", "R", "series"),
        SymbolIntent("U1", "Amplifier", "IC", "host"),
        SymbolIntent("Z1", "Device", "R", "tail", role="output_connector"),
        SymbolIntent("C1", "Device", "C", "cap"),
        SymbolIntent("C2", "Device", "C", "supply cap"),
        SymbolIntent("R2", "Device", "R", "rung"),
        SymbolIntent("F1", "Device", "R", "feedback"),
        SymbolIntent("Q1", "Transistor_BJT", "Q", "pair"),
        SymbolIntent("Q2", "Transistor_BJT", "Q", "pair"),
        SymbolIntent("O1", "Device", "R", "other", role="power_source"),
        SymbolIntent("#FLG1", "power", "PWR_FLAG", "flag", role="power_source"),
    )
    nets = (
        NetIntent("n0", (ep("V1", "1"), ep("A1", "1"), ep("F1", "1"))),
        NetIntent("n1", (ep("A1", "2"), ep("U1", "1"), ep("Q1", "1"), ep("Q2", "1"))),
        NetIntent("n2", (ep("U1", "2"), ep("Z1", "1"), ep("F1", "2"))),
        NetIntent("n3", (ep("U1", "3"), ep("Q1", "2"), ep("Q2", "2"), ep("R2", "1"), ep("C1", "1"))),
        NetIntent("n4", (ep("R2", "2"), ep("Q1", "3"), ep("Q2", "3"))),
        NetIntent("GND", (ep("C1", "2"), ep("O1", "2")), kind="ground"),
        NetIntent("supply", (ep("C2", "1"), ep("O1", "1")), kind="power"),
        NetIntent("n5", (ep("C2", "2"), ep("Q1", "4"))),
    )
    intent = SchematicIntent(
        symbols=symbols,
        nets=nets,
        blocks=(FunctionalBlockIntent("main", ("V1", "A1", "U1", "Z1"), outputs=("n2",)),),
    )
    return intent, {"V1": 2, "A1": 2, "U1": 3, "Z1": 1, "C1": 2, "C2": 2, "R2": 2,
                    "F1": 2, "Q1": 3, "Q2": 3, "O1": 2, "#FLG1": 1}


class LadderRoleTests(unittest.TestCase):
    def test_toy_exercises_roles_and_auxiliary_exclusion(self):
        intent, counts = toy()
        roles = classify(intent, counts)
        self.assertEqual(roles.rails, {"GND": "ground", "supply": "supply"})
        self.assertEqual(roles.chains["main"], ("V1", "A1", "U1", "Z1"))
        self.assertEqual(roles.trunks, ("n0", "n1", "n2", "n3", "n4"))
        self.assertEqual(roles.attachments["C1"], Attachment("U1", "n3", "GND", "ground"))
        self.assertEqual(roles.attachments["C2"], Attachment("Q1", "n5", "supply", "supply"))
        self.assertEqual(roles.rungs["R2"], Rung("n3", "n4"))
        self.assertEqual(roles.feedback["F1"], Feedback("n0", "n2"))
        self.assertIn(("Q1", "Q2"), roles.pairs)
        self.assertEqual(roles.other, ("O1",))
        self.assertNotIn("#FLG1", " ".join(roles.other))

    def test_all_four_demo_intents_classify_without_circuit_rules(self):
        pin_counts = {
            "demo_lm317": {"V1": 2, "T1": 4, "R3": 2, "D1": 4, "C1": 2, "U1": 3,
                           "D2": 2, "D3": 2, "R4": 2, "R1": 2, "C2": 2, "C3": 2, "R5": 2, "R2": 2},
            "demo_buck": {"V102": 2, "V101": 2, "R101": 2, "Q1": 3, "D101": 2, "L101": 2,
                          "C101": 2, "R103": 2},
            "demo_ampac": {"V1": 2, "V3": 2, "C1": 2, "R3": 2, "R9": 2, "Q2": 3, "R4": 2,
                           "R10": 2, "Q3": 3, "R5": 2, "R6": 2, "R11": 2, "C2": 2, "Q1": 3,
                           "R8": 2, "R12": 2, "L2": 2, "R7": 2, "C3": 2, "R1": 2, "R2": 2,
                           "L1": 2, "C4": 2},
            "demo_opamp": {"V3": 2, "V1": 2, "V2": 2, "R2": 2, "U1": 5, "R1": 2, "C1": 2,
                           "R4": 2, "R3": 2, "Q1": 3, "R5": 2, "R6": 2, "C2": 2, "R7": 2},
        }
        for name, factory in DEMO_CIRCUITS.items():
            with self.subTest(demo=name):
                intent = factory()
                roles = classify(intent, pin_counts[name])
                if name == "demo_lm317":
                    self.assertEqual(roles.chains["source"], ("V1", "T1"))
                    self.assertEqual(roles.chains["rectifier"], ("D1",))
                    self.assertEqual(roles.chains["regulator"], ("U1",))
                    self.assertEqual(set(roles.attachments), {"R2", "R3", "C1", "C2", "C3", "R1"})
                    self.assertEqual(roles.chains["load"], ("R5",))
                    self.assertEqual(roles.attachments["C3"].host, "U1")
                    self.assertEqual(set(roles.rungs), {"R4", "D3"})
                    self.assertEqual(roles.feedback, {"D2": Feedback("rect", "out")})
                elif name == "demo_buck":
                    self.assertEqual(roles.chains["switch"], ("Q1",))
                    self.assertEqual(roles.chains["filter"], ("L101",))
                    self.assertEqual(roles.attachments["D101"].host, "Q1")
                    self.assertEqual(roles.attachments["C101"].host, "L101")
                elif name == "demo_ampac":
                    self.assertEqual(roles.chains["input_pair"], ("C1", "Q2", "Q3"))
                    self.assertIn(("Q2", "Q3"), roles.pairs)
                else:
                    self.assertEqual(roles.rails["-5V"], "negative")
                    self.assertEqual(roles.chains["opamp_stage"], ("R2", "U1"))
                    self.assertEqual(roles.chains["bjt_stage"], ("C1", "Q1", "C2"))
                    self.assertEqual(roles.feedback, {"R1": Feedback("N_R1_2", "out")})

                rng = random.Random(23)
                symbols, nets, blocks = list(intent.symbols), list(intent.nets), list(intent.blocks)
                rng.shuffle(symbols)
                rng.shuffle(nets)
                rng.shuffle(blocks)
                shuffled = SchematicIntent(symbols=tuple(symbols), nets=tuple(nets), blocks=tuple(blocks))
                self.assertEqual(classify(shuffled, pin_counts[name]), roles)

    def test_tied_hub_singleton_and_disconnected_fallback(self):
        tied = SchematicIntent(
            symbols=(SymbolIntent("B1", "Device", "X", "b"), SymbolIntent("A1", "Device", "X", "a")),
            blocks=(FunctionalBlockIntent("tie", ("B1", "A1")),),
        )
        self.assertEqual(classify(tied, {"A1": 3, "B1": 3}).chains, {"tie": ("A1",)})
        disconnected = SchematicIntent(
            symbols=(SymbolIntent("A1", "Device", "X", "a"), SymbolIntent("B1", "Device", "X", "b")),
            blocks=(FunctionalBlockIntent("split", ("A1", "B1")),),
        )
        self.assertEqual(classify(disconnected, {"A1": 2, "B1": 4}).chains, {"split": ("B1",)})
        singleton = SchematicIntent(
            symbols=(SymbolIntent("Z1", "Device", "X", "z"),),
            blocks=(FunctionalBlockIntent("one", ("Z1",)),),
        )
        self.assertEqual(classify(singleton, {"Z1": 1}).chains, {"one": ("Z1",)})

    def test_shortest_chain_through_hub_is_simple_when_two_routes_overlap(self):
        symbols = (
            SymbolIntent("V1", "Simulation_SPICE", "V", "source"),
            SymbolIntent("A1", "Device", "X", "a"),
            SymbolIntent("B1", "Device", "X", "b"),
            SymbolIntent("H1", "Amplifier", "X", "hub"),
            SymbolIntent("C1", "Device", "X", "c"),
            SymbolIntent("T1", "Device", "X", "sink", role="output_connector"),
        )
        nets = (
            NetIntent("sa", (ep("V1", "1"), ep("A1", "1"))),
            NetIntent("ah", (ep("A1", "2"), ep("H1", "1"))),
            NetIntent("sb", (ep("V1", "2"), ep("B1", "1"))),
            NetIntent("bh", (ep("B1", "2"), ep("H1", "2"))),
            NetIntent("at", (ep("A1", "3"), ep("T1", "1"))),
            NetIntent("hc", (ep("H1", "3"), ep("C1", "1"))),
            NetIntent("ct", (ep("C1", "2"), ep("T1", "2"))),
        )
        intent = SchematicIntent(
            symbols=symbols,
            nets=nets,
            blocks=(FunctionalBlockIntent("main", tuple(symbol.reference for symbol in symbols), outputs=("ct", "at")),),
        )
        roles = classify(intent, {"V1": 2, "A1": 3, "B1": 2, "H1": 4, "C1": 2, "T1": 2})
        self.assertEqual(len(roles.chains["main"]), len(set(roles.chains["main"])))
        self.assertIn("H1", roles.chains["main"])

    def test_explicit_input_connector_seed_takes_precedence_over_input_net_holders(self):
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("J1", "Connector", "Conn_01x02", "input", role="input_connector"),
                SymbolIntent("U1", "Amplifier", "IC", "host"),
                SymbolIntent("RLOAD", "Device", "R", "load", role="output_connector"),
            ),
            nets=(
                NetIntent("in", (ep("J1", "2"), ep("U1", "1"))),
                NetIntent("out", (ep("U1", "2"), ep("RLOAD", "1"))),
            ),
            blocks=(FunctionalBlockIntent("main", ("J1", "U1", "RLOAD"), inputs=("in",), outputs=("out",)),),
        )
        roles = classify(intent, {"J1": 2, "U1": 3, "RLOAD": 2})
        self.assertEqual(roles.chains["main"], ("J1", "U1", "RLOAD"))

    def test_spice_sources_are_identified_by_symbol_function(self):
        opamp = SymbolIntent("U1", "Simulation_SPICE", "OPAMP", "opamp")
        transistor = SymbolIntent("Q1", "Simulation_SPICE", "NPN", "transistor")
        controlled_source = SymbolIntent("SRC1", "Simulation_SPICE", "ESOURCE", "controlled")
        behavioral_source = SymbolIntent("SRC2", "Simulation_SPICE", "BSOURCE", "behavioral")
        self.assertFalse(_is_source("U1", opamp))
        self.assertFalse(_is_source("Q1", transistor))
        self.assertTrue(_is_source("SRC1", controlled_source))
        self.assertTrue(_is_source("SRC2", behavioral_source))
        for name in ("IBIS_DEVICE", "IBIS_DRIVER", "VOLTMETER_DIFF"):
            self.assertFalse(_is_source("U2", SymbolIntent("U2", "Simulation_SPICE", name, name)))
        for name in ("VDC", "ISIN", "GSOURCE", "CCVS"):
            self.assertTrue(_is_source("SRC3", SymbolIntent("SRC3", "Simulation_SPICE", name, name)))
        # a symbol named VBIAS is a source by its library (or role), not by the name alone
        self.assertTrue(_is_source("VBIAS", SymbolIntent("VBIAS", "Simulation_SPICE", "VDC", "bias")))
        self.assertFalse(_is_source("VBIAS", SymbolIntent("VBIAS", "Device", "R", "source")))

        intent = SchematicIntent(
            symbols=(
                controlled_source,
                SymbolIntent("U1", "Amplifier", "IC", "host"),
                SymbolIntent("J2", "Connector", "Conn_01x01", "output", role="output_connector"),
            ),
            nets=(
                NetIntent("n1", (ep("SRC1", "1"), ep("U1", "1"))),
                NetIntent("n2", (ep("U1", "2"), ep("J2", "1"))),
            ),
            blocks=(FunctionalBlockIntent("controlled", ("SRC1", "U1", "J2"), outputs=("n2",)),),
        )
        roles = classify(intent, {"SRC1": 2, "U1": 3, "J2": 1})
        self.assertEqual(roles.chains["controlled"], ("SRC1", "U1", "J2"))

    def test_declared_output_is_a_chain_boundary_net_for_feedback(self):
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("V1", "Simulation_SPICE", "VDC", "source"),
                SymbolIntent("A1", "Device", "R", "series"),
                SymbolIntent("U1", "Amplifier", "IC", "host"),
                SymbolIntent("F1", "Device", "R", "feedback"),
            ),
            nets=(
                NetIntent("n0", (ep("V1", "1"), ep("A1", "1"), ep("F1", "1"))),
                NetIntent("n1", (ep("A1", "2"), ep("U1", "1"))),
                NetIntent("out", (ep("U1", "2"), ep("F1", "2"))),
            ),
            blocks=(FunctionalBlockIntent("main", ("V1", "A1", "U1"), outputs=("out",)),),
        )
        roles = classify(intent, {"V1": 2, "A1": 2, "U1": 3, "F1": 2})
        self.assertEqual(roles.chains["main"], ("V1", "A1", "U1"))
        self.assertEqual(roles.feedback["F1"], Feedback("n0", "out"))

    def test_trunk_counts_pins_not_unique_parts(self):
        intent = SchematicIntent(
            symbols=(SymbolIntent("A1", "Device", "X", "a"), SymbolIntent("B1", "Device", "X", "b")),
            nets=(NetIntent("multi", (ep("A1", "1"), ep("A1", "2"), ep("B1", "1"))),),
        )
        self.assertEqual(classify(intent, {"A1": 2, "B1": 2}).trunks, ("multi",))

    def test_a_rail_a_block_declares_is_its_chain_net_and_joins_the_blocks(self):
        # a buck (U1 with its inductor) feeds +5V to an LDO stage (U2 with its output capacitor): +5V is the
        # interface, +3V3 the LDO's output; the output capacitor hangs from the regulator on +3V3, the input one on +5V
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("V1", "Simulation_SPICE", "VDC", "source"),
                SymbolIntent("U1", "Regulator_Switching", "BUCK", "buck"),
                SymbolIntent("U2", "Regulator_Linear", "LDO", "ldo"),
                SymbolIntent("C1", "Device", "C", "in"),
                SymbolIntent("C2", "Device", "C", "out"),
            ),
            nets=(
                NetIntent("VIN", (ep("V1", "1"), ep("U1", "1"))),
                NetIntent("+5V", (ep("U1", "2"), ep("U2", "1"), ep("C1", "1")), kind="power"),
                NetIntent("+3V3", (ep("U2", "2"), ep("C2", "1")), kind="power"),
                NetIntent("GND", (ep("V1", "2"), ep("U1", "3"), ep("U2", "3"), ep("C1", "2"), ep("C2", "2")), kind="ground"),
            ),
            blocks=(FunctionalBlockIntent("buck", ("V1", "U1"), outputs=("+5V",)),
                    FunctionalBlockIntent("ldo", ("U2", "C1", "C2"), inputs=("+5V",), outputs=("+3V3",))),
        )
        roles = classify(intent, {"V1": 2, "U1": 3, "U2": 3, "C1": 2, "C2": 2})
        self.assertEqual(roles.chains["buck"], ("V1", "U1"))
        self.assertEqual(roles.chains["ldo"], ("U2",))
        self.assertNotIn("+5V", roles.rails)
        self.assertNotIn("+3V3", roles.rails)
        self.assertEqual(roles.attachments["C1"], Attachment("U1", "+5V", "GND", "ground"))
        self.assertEqual(roles.attachments["C2"], Attachment("U2", "+3V3", "GND", "ground"))
        self.assertEqual(roles.other, ())
        # a rail no block declares stays a rail
        undeclared = SchematicIntent(symbols=intent.symbols, nets=intent.nets,
                                     blocks=(FunctionalBlockIntent("buck", ("V1", "U1")), FunctionalBlockIntent("ldo", ("U2", "C1", "C2"))))
        self.assertIn("+5V", classify(undeclared, {"V1": 2, "U1": 3, "U2": 3, "C1": 2, "C2": 2}).rails)

    def test_a_block_that_passes_nothing_on_is_ordered_after_those_that_do(self):
        intent = SchematicIntent(
            symbols=(SymbolIntent("U1", "Regulator_Linear", "LDO", "a"), SymbolIntent("R1", "Device", "R", "led"),
                     SymbolIntent("U2", "Regulator_Linear", "LDO", "b")),
            nets=(NetIntent("n", (ep("U1", "1"), ep("U2", "1"), ep("R1", "1"))), NetIntent("q", (ep("U2", "2"),))),
            blocks=(FunctionalBlockIntent("a_led", ("R1",), inputs=("n",)), FunctionalBlockIntent("b_reg", ("U2",), inputs=("n",), outputs=("q",)),
                    FunctionalBlockIntent("c_src", ("U1",), outputs=("n",))),
        )
        self.assertEqual(list(classify(intent, {"U1": 3, "U2": 3, "R1": 2}).chains), ["c_src", "b_reg", "a_led"])

    def test_symbol_and_net_shuffling_does_not_change_roles(self):
        intent, counts = toy()
        expected = classify(intent, counts)
        rng = random.Random(17)
        symbols = list(intent.symbols)
        nets = list(intent.nets)
        rng.shuffle(symbols)
        rng.shuffle(nets)
        shuffled = SchematicIntent(symbols=tuple(symbols), nets=tuple(nets), blocks=intent.blocks)
        self.assertEqual(classify(shuffled, counts), expected)

    def test_a_hubs_non_chain_pins_start_lanes_of_the_two_pin_parts_in_series_from_them(self):
        symbols = (
            SymbolIntent("V1", "Simulation_SPICE", "V", "source"),
            SymbolIntent("U1", "Device", "IC", "hub"),
            SymbolIntent("Z1", "Device", "R", "tail", role="output_connector"),
            SymbolIntent("R5", "Device", "R", "series"),
            SymbolIntent("R6", "Device", "R", "series to the next pin"),
            SymbolIntent("C5", "Device", "C", "shunt"),
        )
        nets = (
            NetIntent("n0", (ep("V1", "1"), ep("U1", "1"))),
            NetIntent("n1", (ep("U1", "2"), ep("Z1", "1"))),
            NetIntent("x", (ep("U1", "3"), ep("R5", "1"))),
            NetIntent("y", (ep("R5", "2"), ep("C5", "1"), ep("R6", "1"))),
            NetIntent("z", (ep("U1", "4"), ep("R6", "2"))),
            NetIntent("GND", (ep("V1", "2"), ep("C5", "2"), ep("U1", "5"), ep("U1", "6")), kind="ground"),
        )
        intent = SchematicIntent(symbols=symbols, nets=nets,
                                 blocks=(FunctionalBlockIntent("main", ("V1", "U1", "Z1"), outputs=("n1",)),))
        counts = {"V1": 2, "U1": 6, "Z1": 1, "R5": 2, "R6": 2, "C5": 2}
        roles = classify(intent, counts)
        self.assertEqual(roles.lanes, {"U1.3": Lane("U1", "3", "x", ("R5", "R6"), ("x", "y", "z"))})
        self.assertEqual(roles.attachments["C5"].net, "y")            # the shunt stays an attachment, hung from the lane
        self.assertNotIn("R5", roles.other)
        self.assertNotIn("R6", roles.other)
        self.assertEqual(classify(intent, {**counts, "U1": 5}).lanes, {})        # a part of five pins is not a hub
        # with no lane they stand between a dead-end net on the part and the trunk y: rungs between the two nets
        small = classify(intent, {**counts, "U1": 5})
        self.assertEqual(small.other, ())
        self.assertEqual((small.rungs["R5"], small.rungs["R6"]), (Rung("x", "y"), Rung("z", "y")))
        rng = random.Random(5)
        shuffled_symbols, shuffled_nets = list(symbols), list(nets)
        rng.shuffle(shuffled_symbols)
        rng.shuffle(shuffled_nets)
        shuffled = SchematicIntent(symbols=tuple(shuffled_symbols), nets=tuple(shuffled_nets), blocks=intent.blocks)
        self.assertEqual(classify(shuffled, counts), roles)


class RoleReviewTests(unittest.TestCase):
    """Fixes from the outside review of ladder_roles: path search, net-labelled traversal, lanes, block order, sources."""

    def test_a_multi_pin_part_feeding_a_chain_part_from_an_input_net_is_a_further_chain(self):
        # a battery switch beside a USB diode, both feeding the pass transistor: the diode and the transistor are the main
        # path; the switch enters by the other input net and meets the transistor by a net of its own
        def circuit(switch_pins):
            symbols = (SymbolIntent("D1", "Device", "D", "usb"), SymbolIntent("Q1", "Transistor_FET", "Q", "pass"),
                       SymbolIntent("SW1", "Switch", "SW", "battery"))
            nets = (
                NetIntent("usb", (ep("D1", "1"),)),
                NetIntent("vin", (ep("D1", "2"), ep("Q1", "2"))),
                NetIntent("sw_out", (ep("Q1", "3"), ep("SW1", "1"))),
                NetIntent("batt", (ep("SW1", "2"),)),
            )
            block = FunctionalBlockIntent("power", ("D1", "Q1", "SW1"), inputs=("usb", "batt"), outputs=("vin",))
            intent = SchematicIntent(symbols=symbols, nets=nets, blocks=(block,))
            return classify(intent, {"D1": 2, "Q1": 3, "SW1": switch_pins})
        roles = circuit(3)
        self.assertEqual(roles.chains, {"power#1": ("D1", "Q1"), "power#2": ("SW1",)})
        self.assertNotIn("SW1", roles.other)
        self.assertEqual(circuit(2).chains, {"power": ("D1", "Q1")})          # a two-pin part is a support, not a link

    def test_a_clamp_on_a_node_is_an_attachment_not_a_chain_part(self):
        # a dual diode: one pin on the signal node between two stages, the other two on the supply and ground rails
        symbols = (SymbolIntent("R1", "Device", "R", "series"), SymbolIntent("U1", "Amplifier", "IC", "amp"),
                   SymbolIntent("D1", "Device", "D", "clamp"), SymbolIntent("V1", "Simulation_SPICE", "VDC", "in"))
        nets = (
            NetIntent("in", (ep("V1", "1"), ep("R1", "1"))),
            NetIntent("node", (ep("R1", "2"), ep("D1", "3"), ep("U1", "1"))),
            NetIntent("VCC", (ep("D1", "2"), ep("U1", "3")), kind="power"),
            NetIntent("GND", (ep("D1", "1"), ep("V1", "2"), ep("U1", "2")), kind="ground"),
        )
        intent = SchematicIntent(symbols=symbols, nets=nets, blocks=(
            FunctionalBlockIntent("protect", ("V1", "R1", "D1"), outputs=("node",)),
            FunctionalBlockIntent("amp", ("U1",), inputs=("node",))))
        roles = classify(intent, {"V1": 2, "R1": 2, "D1": 3, "U1": 3})
        self.assertNotIn("D1", {ref for chain in roles.chains.values() for ref in chain})
        self.assertEqual(roles.attachments["D1"], Attachment("R1", "node", "VCC", "supply"))

    def test_a_multi_pin_part_no_chain_took_that_shares_a_signal_net_with_a_chain_part_is_a_chain_of_its_own(self):
        symbols = (SymbolIntent("Q1", "T", "FET", "high"), SymbolIntent("Q2", "T", "FET", "low"), SymbolIntent("L1", "Device", "L", "l"))
        nets = (
            NetIntent("vin", (ep("Q1", "5"),)),
            NetIntent("sw", (ep("Q1", "1"), ep("Q2", "5"), ep("L1", "1"))),
            NetIntent("out", (ep("L1", "2"),)),
            NetIntent("gate", (ep("Q2", "4"),)),
            NetIntent("GND", (ep("Q2", "1"), ep("Q2", "2")), kind="ground"),
        )
        intent = SchematicIntent(symbols=symbols, nets=nets, blocks=(FunctionalBlockIntent("stage", ("Q1", "L1"), inputs=("vin",), outputs=("out",)),))
        roles = classify(intent, {"Q1": 5, "Q2": 5, "L1": 2})
        self.assertEqual(roles.chains["Q2"], ("Q2",))
        self.assertNotIn("Q2", roles.other)

    def test_a_two_pin_part_between_a_trunk_and_a_dead_end_net_is_a_rung_and_a_supply_stays_other(self):
        symbols = (SymbolIntent("U1", "Device", "IC", "hub"), SymbolIntent("U2", "Device", "IC", "other"),
                   SymbolIntent("C1", "Device", "C", "x"), SymbolIntent("R1", "Device", "R", "pull-up"),
                   SymbolIntent("V9", "Device", "R", "rail to rail"))
        nets = (
            NetIntent("rail3", (ep("U1", "1"), ep("U2", "1"), ep("C1", "1"), ep("R1", "1"))),          # a trunk that is no rail
            NetIntent("pg", (ep("U1", "2"), ep("R1", "2"))),                                           # a dead end on a chain pin
            NetIntent("VCC", (ep("U1", "3"), ep("V9", "1")), kind="power"),
            NetIntent("GND", (ep("C1", "2"), ep("V9", "2"), ep("U2", "2")), kind="ground"),
        )
        intent = SchematicIntent(symbols=symbols, nets=nets, blocks=(FunctionalBlockIntent("main", ("U1", "U2")),))
        roles = classify(intent, {"U1": 4, "U2": 3, "C1": 2, "R1": 2, "V9": 2})
        self.assertEqual(roles.rungs["R1"], Rung("pg", "rail3"))
        self.assertIn("V9", roles.other)                                   # both pins on rails: a supply, not a pull-up

    def test_a_dense_block_finds_its_chain_in_milliseconds(self):
        import time
        from backend.schematic.ladder_roles import _shortest_path_through
        parts = [f"P{i:02d}" for i in range(12)]
        graph = {a: {b for b in parts if b != a} for a in parts}
        labels = {a: {b: {"n"} for b in parts if b != a} for a in parts}
        started = time.perf_counter()
        for use in (None, labels):
            self.assertEqual(_shortest_path_through(graph, "P00", "P05", "P11", use), ("P00", "P05", "P11"))
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_the_path_through_the_hub_is_as_short_as_the_exhaustive_search_finds(self):
        from collections import deque
        from backend.schematic.ladder_roles import _shortest_path_through

        def oracle(graph, start, hub, end):          # the old search of simple paths: the oracle
            queue = deque([(start, (start,))])
            while queue:
                current, path = queue.popleft()
                if current == end and hub in path:
                    return path
                for neighbor in sorted(graph.get(current, ())):
                    if neighbor not in path:
                        queue.append((neighbor, path + (neighbor,)))
            return None

        rng = random.Random(23)
        checked = 0
        for _ in range(300):
            n = rng.randint(3, 8)
            parts = [f"P{i}" for i in range(n)]
            graph = {p: set() for p in parts}
            for a in parts:
                for b in parts:
                    if a < b and rng.random() < rng.choice((.25, .4, .6)):
                        graph[a].add(b)
                        graph[b].add(a)
            start, hub, end = rng.sample(parts, 3)
            expected = oracle(graph, start, hub, end)
            found = _shortest_path_through(graph, start, hub, end)
            self.assertEqual(found is None, expected is None, (graph, start, hub, end))
            if found is not None:
                checked += 1
                self.assertEqual(found, expected)           # the same path, not only the same length
                self.assertEqual((found[0], found[-1]), (start, end))
                self.assertEqual(len(set(found)), len(found))
                self.assertIn(hub, found)
                self.assertTrue(all(b in graph[a] for a, b in zip(found, found[1:])))
        self.assertGreater(checked, 100)
        # the hub at an end of the path
        graph = {"A": {"B"}, "B": {"A", "C"}, "C": {"B"}}
        self.assertEqual(_shortest_path_through(graph, "A", "A", "C"), ("A", "B", "C"))
        self.assertEqual(_shortest_path_through(graph, "A", "C", "C"), ("A", "B", "C"))
        self.assertEqual(_shortest_path_through(graph, "B", "B", "B"), ("B",))          # a one-part path through its own hub
        self.assertIsNone(_shortest_path_through(graph, "A", "B", "A"))

    def test_the_joined_paths_use_no_part_twice_when_a_simple_pair_exists(self):
        from backend.schematic.ladder_roles import _shortest_path_through
        # the shortest way to the hub (S - A - H) and the shortest on from it (H - A - E) share A: the pair is not simple
        graph = {"S": {"A", "B"}, "A": {"S", "H", "E"}, "B": {"S", "C"}, "C": {"B", "H"}, "H": {"A", "C", "E2"},
                 "E2": {"H", "E"}, "E": {"A", "E2"}}
        path = _shortest_path_through(graph, "S", "H", "E")
        self.assertEqual(len(set(path)), len(path))
        self.assertEqual((path[0], path[-1]), ("S", "E"))
        self.assertIn("H", path)

    def test_a_part_is_left_by_a_different_net_than_it_was_entered_by(self):
        from backend.schematic.ladder_roles import _shortest_path, _shortest_path_through
        # B has two pins on X (A and D meet it there) and one on Y; C joins A by Y and D by Z
        labels = {"A": {"B": {"X"}, "C": {"Y"}}, "B": {"A": {"X"}, "D": {"X"}},
                  "C": {"A": {"Y"}, "D": {"Z"}}, "D": {"B": {"X"}, "C": {"Z"}}}
        graph = {a: set(b) for a, b in labels.items()}
        self.assertEqual(_shortest_path(graph, "A", "D"), ("A", "B", "D"))
        self.assertEqual(_shortest_path(graph, "A", "D", labels), ("A", "C", "D"))      # B would be entered and left by X
        # the same through a hub: S enters H by X, so H goes on to E by Q (net Y then W), not by P (net X)
        labels = {"S": {"H": {"X"}}, "H": {"S": {"X"}, "P": {"X"}, "Q": {"Y"}}, "P": {"H": {"X"}, "E": {"Z"}},
                  "Q": {"H": {"Y"}, "E": {"W"}}, "E": {"P": {"Z"}, "Q": {"W"}}}
        graph = {a: set(b) for a, b in labels.items()}
        self.assertEqual(_shortest_path_through(graph, "S", "H", "E"), ("S", "H", "P", "E"))
        self.assertEqual(_shortest_path_through(graph, "S", "H", "E", labels), ("S", "H", "Q", "E"))
        # a longer path is never taken for it: parts that all meet on one net are one hop apart
        lone = {"A": {"B": {"X"}}, "B": {"A": {"X"}, "D": {"X"}}, "D": {"B": {"X"}}}
        self.assertEqual(_shortest_path_through({a: set(b) for a, b in lone.items()}, "A", "B", "D", lone), ("A", "B", "D"))

    def test_a_lane_goes_on_with_the_next_part_when_the_first_cannot_(self):
        from backend.schematic.ladder_roles import Roles, assign_lanes
        roles = Roles({"GND": "ground"}, (), {"main": ("U1", "Z1")}, {}, {}, {}, (), ("R3", "R5"))
        endpoints = {
            "x": (("U1", "3"), ("R3", "1"), ("R5", "1")),
            "m": (("R3", "2"), ("Z1", "2")),                    # R3's far net reaches the chain elsewhere: not on the lane
            "y": (("R5", "2"),),
            "n1": (("U1", "2"), ("Z1", "1")),
            "GND": (("U1", "5"), ("U1", "6")),
        }
        lanes = assign_lanes(roles, endpoints, {"U1": 6, "Z1": 2, "R3": 2, "R5": 2}).lanes
        self.assertEqual(lanes, {"U1.3": Lane("U1", "3", "x", ("R5",), ("x", "y"))})

    def test_a_dependency_cycle_is_broken_where_it_is_and_the_acyclic_order_kept(self):
        from backend.schematic.ladder_roles import _block_flow_order
        blocks = (
            FunctionalBlockIntent("A", ("R1",), inputs=("c",), outputs=("a",)),
            FunctionalBlockIntent("B", ("R2",), inputs=("a",), outputs=("b",)),
            FunctionalBlockIntent("C", ("R3",), inputs=("b",), outputs=("c",)),
            FunctionalBlockIntent("Z", ("R4",), outputs=("z",)),
            FunctionalBlockIntent("Y", ("R5",), inputs=("z",), outputs=("y",)),
        )
        order = [block.name for block in _block_flow_order(blocks)]
        self.assertEqual(sorted(order), ["A", "B", "C", "Y", "Z"])
        self.assertLess(order.index("Z"), order.index("Y"))           # the acyclic order, not alphabetical
        self.assertEqual(order[:3], ["A", "B", "C"])                  # the cycle by rank, as one unit

    def test_a_source_is_decided_by_role_then_symbol_then_the_designators_whole_prefix(self):
        from backend.schematic.designators import is_source
        # 1. an explicit role says source, whatever the symbol and the name
        self.assertTrue(is_source("X1", "Device", "R", "power_source"))
        self.assertTrue(is_source("IC1", "Device", "R", "Power_Source"))
        # 2. the symbol: Simulation_SPICE sources, a battery; not the library's other parts
        for name in ("VSIN", "VDC", "IDC", "VPULSE", "ESOURCE"):
            self.assertTrue(is_source("X1", "Simulation_SPICE", name), name)
        self.assertTrue(is_source("BT1", "Device", "Battery_Cell"))
        self.assertTrue(is_source("X1", "Simulation_SPICE:VDC"))                # a library id in place of the two names
        for name in ("IBIS_DEVICE", "VOLTMETER_DIFF", "OPAMP"):
            self.assertFalse(is_source("X1", "Simulation_SPICE", name), name)
        self.assertFalse(is_source("X1", "Device", "R", "load"))
        # 3. the designator, its prefix letters whole
        for reference in ("V1", "I2", "v3"):
            self.assertTrue(is_source(reference, "Device", "R"), reference)
        for reference in ("IC1", "VR1", "VBIAS", "U1", "V"):
            self.assertFalse(is_source(reference, "Device", "R"), reference)
        self.assertTrue(_is_source("V1", SymbolIntent("V1", "Device", "R", "x")))
        self.assertFalse(_is_source("IC1", SymbolIntent("IC1", "Device", "R", "x")))


if __name__ == "__main__":
    unittest.main()
