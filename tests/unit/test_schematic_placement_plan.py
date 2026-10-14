"""Feeding the placement refinement from a design: how nets are drawn, how much room a pin needs, what may turn."""
from __future__ import annotations

import unittest

from backend.schematic.intent import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.orientation import Orientation
from backend.schematic.placement_plan import (
    GLOBAL_FLAG_MM, GROUND_FACING, LABEL_REACH_CAP_MM, SUPPLY_FACING, candidate_orientations, connections, describe_symbol,
    net_style, orientations_to_request, pin_half_widths, pin_reach, symbols_from_payload, writes_for,
)
from backend.schematic.placement_refinement import (
    LABEL_STUB_MM, POWER_REACH_MM, WIRE_REACH_MM, Placement, Refinement, Cost,
)


def p(reference: str, pin: str) -> PinEndpoint:
    return PinEndpoint(reference, pin)


def design() -> SchematicIntent:
    symbols = tuple(SymbolIntent(ref, "Device", "R", "1k") for ref in ("U1", "R1", "J1", "#FLG1"))
    return SchematicIntent(
        symbols=symbols,
        nets=(
            NetIntent("SDA", (p("J1", "3"), p("U1", "1"), p("R1", "2"))),
            NetIntent("UART_RX", (p("J1", "2"), p("U1", "5")), routing="label", scope="global"),
            NetIntent("SIG", (p("R1", "1"), p("U1", "6")), routing="label", scope="local"),
            NetIntent("+3V3", (p("U1", "8"), p("#FLG1", "1")), kind="power", routing="label", scope="global"),
            NetIntent("GND", (p("J1", "1"),), kind="ground", routing="label"),
        ),
    )


def labelled(net: NetIntent) -> bool:
    return net.routing == "label"


class NetStyleTests(unittest.TestCase):
    def test_power_nets_are_power_whatever_their_routing_and_signals_follow_the_question_asked(self):
        nets = {net.name: net for net in design().all_nets}
        self.assertEqual(net_style(nets["+3V3"], labelled), "power")
        self.assertEqual(net_style(nets["GND"], labelled), "power")
        self.assertEqual(net_style(nets["UART_RX"], labelled), "label")
        self.assertEqual(net_style(nets["SDA"], labelled), "wire")

    def test_connections_leave_out_the_symbols_to_skip(self):
        found = {c.net: c for c in connections(design(), labelled, skip={"#FLG1"})}
        self.assertEqual(found["+3V3"].pins, (("U1", "8"),))
        self.assertEqual(found["SDA"].style, "wire")
        # a one-pin net that is not a power net draws nothing
        self.assertEqual(
            connections(SchematicIntent(symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
                                        nets=(NetIntent("X", (p("R1", "1"),)),)), labelled), [],
        )


class PowerPlanTests(unittest.TestCase):
    def test_a_ground_symbol_is_entered_downward_and_a_supply_symbol_upward(self):
        found = {c.net: c for c in connections(design(), labelled, skip={"#FLG1"})}
        self.assertEqual(found["GND"].facing, GROUND_FACING)
        self.assertEqual(found["+3V3"].facing, SUPPLY_FACING)
        self.assertIsNone(found["SDA"].facing)
        self.assertIsNone(found["UART_RX"].facing)

    def test_the_room_for_a_power_symbol_is_as_wide_as_its_name(self):
        widths = pin_half_widths(design(), labelled, skip={"#FLG1"})
        self.assertIn(("U1", "8"), widths)
        self.assertIn(("J1", "1"), widths)
        self.assertGreater(widths[("U1", "8")], 1.27)          # "+3V3" is wider than a wire
        self.assertNotIn(("U1", "1"), widths)                   # a wire is a wire

    def test_a_long_name_makes_a_wide_symbol(self):
        def width(name):
            intent = SchematicIntent(
                symbols=(SymbolIntent("U1", "Device", "R", "1k"),),
                nets=(NetIntent(name, (p("U1", "1"),), kind="power", routing="label"),),
            )
            return pin_half_widths(intent, labelled)[("U1", "1")]
        self.assertGreater(width("+12V_ANALOG"), width("+5V"))


class ReachTests(unittest.TestCase):
    def setUp(self):
        self.reach = pin_reach(design(), labelled, skip={"#FLG1"})

    def test_a_wire_needs_what_the_router_needs_in_front_of_a_pin(self):
        self.assertEqual(self.reach[("U1", "1")], WIRE_REACH_MM)

    def test_a_power_pin_needs_room_for_its_stub_and_symbol(self):
        self.assertEqual(self.reach[("U1", "8")], POWER_REACH_MM)
        self.assertEqual(self.reach[("J1", "1")], POWER_REACH_MM)

    def test_a_labelled_pin_needs_its_stub_and_the_text_and_a_global_label_its_flag_as_well(self):
        local = self.reach[("U1", "6")]
        glob = self.reach[("U1", "5")]
        self.assertGreater(local, LABEL_STUB_MM)
        self.assertGreater(glob, local)         # a longer name and a flag
        self.assertLessEqual(glob, LABEL_REACH_CAP_MM)

    def test_a_wire_with_one_end_left_after_skipping_reserves_nothing(self):
        intent = SchematicIntent(
            symbols=(SymbolIntent("U1", "Device", "R", "1k"), SymbolIntent("#FLG1", "power", "PWR_FLAG", "F")),
            nets=(NetIntent("A", (p("U1", "1"), p("#FLG1", "1"))),),
        )
        self.assertEqual(pin_reach(intent, labelled, skip={"#FLG1"}), {})

    def test_a_long_label_is_capped(self):
        long_name = "A_VERY_LONG_NET_NAME_THAT_RUNS_ON_AND_ON"
        intent = SchematicIntent(
            symbols=(SymbolIntent("U1", "Device", "R", "1k"), SymbolIntent("U2", "Device", "R", "1k")),
            nets=(NetIntent(long_name, (p("U1", "1"), p("U2", "1")), routing="label", scope="global"),),
        )
        self.assertEqual(pin_reach(intent, labelled)[("U1", "1")], LABEL_REACH_CAP_MM)
        self.assertGreater(GLOBAL_FLAG_MM, 0)


class CandidateTests(unittest.TestCase):
    def test_a_connector_may_take_all_twelve_orientations(self):
        found = candidate_orientations(is_connector=True, pin_count=4, planned=Orientation(0))
        self.assertEqual(len(found), 12)
        self.assertIn(Orientation(90, "x"), found)

    def test_a_two_pin_part_may_take_its_four_turns(self):
        found = candidate_orientations(is_connector=False, pin_count=2, planned=Orientation(0))
        self.assertEqual({o.rotation for o in found}, {0, 90, 180, 270})
        self.assertEqual({o.mirror for o in found}, {""})

    def test_a_part_of_three_or_four_pins_may_take_them_all_too(self):
        for pins in (3, 4):
            found = candidate_orientations(is_connector=False, pin_count=pins, planned=Orientation(0))
            self.assertEqual(len(found), 12)
            self.assertIn(Orientation(270, "y"), found)

    def test_any_larger_symbol_keeps_the_orientation_it_is_planned_to_have(self):
        for pins in (5, 8):
            self.assertEqual(
                candidate_orientations(is_connector=False, pin_count=pins, planned=Orientation(270)), [Orientation(270)],
            )

    def test_the_planned_orientation_is_always_among_them(self):
        found = candidate_orientations(is_connector=False, pin_count=2, planned=Orientation(180, "x"))
        self.assertIn(Orientation(180, "x"), found)

    def test_the_request_to_the_server_names_rotation_and_mirror(self):
        self.assertEqual(
            orientations_to_request({"R1": [Orientation(90), Orientation(0, "y")]}),
            {"R1": [{"rotation": 90, "mirror": ""}, {"rotation": 0, "mirror": "y"}]},
        )


class PayloadTests(unittest.TestCase):
    ORIENTATIONS = [Orientation(0), Orientation(90)]

    def described(self):
        pins = {Orientation(0): {"1": (0.0, -3.81), "2": (0.0, 3.81)}, Orientation(90): {"1": (-3.81, 0.0), "2": (3.81, 0.0)}}
        normals = {Orientation(0): {"1": (0.0, -1.0), "2": (0.0, 1.0)}, Orientation(90): {"1": (-1.0, 0.0), "2": (1.0, 0.0)}}
        boxes = {Orientation(0): (-1.02, -3.81, 1.02, 3.81), Orientation(90): (-3.81, -1.02, 3.81, 1.02)}
        return describe_symbol(
            45.72, 66.04, self.ORIENTATIONS, lambda o: pins[o], lambda o: normals[o], lambda o: boxes[o],
        )

    def test_a_symbol_is_described_by_its_anchor_and_each_orientation_asked_for(self):
        entry = self.described()
        self.assertEqual((entry["x"], entry["y"]), (45.72, 66.04))
        self.assertEqual([(o["rotation"], o["mirror"]) for o in entry["orientations"]], [(0, ""), (90, "")])
        self.assertEqual(entry["orientations"][0]["pins"]["1"], [0.0, -3.81, 0.0, -1.0])
        self.assertEqual(entry["orientations"][1]["box"], [-3.81, -1.02, 3.81, 1.02])

    def test_what_is_described_reads_back_as_symbols_at_their_planned_orientation(self):
        symbols = symbols_from_payload(
            {"symbols": {"R1": self.described()}}, planned={"R1": Orientation(90)}, movable={"R1"}, ordered=set(),
        )
        (symbol,) = symbols
        self.assertEqual((symbol.reference, symbol.position, symbol.orientation, symbol.movable), ("R1", (45.72, 66.04), Orientation(90), True))
        self.assertEqual(set(symbol.geometry), set(self.ORIENTATIONS))
        self.assertEqual(symbol.geometry[Orientation(90)].pins["1"].normal, (-1.0, 0.0))
        self.assertEqual(symbol.geometry[Orientation(0)].box, (-1.02, -3.81, 1.02, 3.81))

    def test_the_body_is_carried_when_the_server_reports_it(self):
        entry = describe_symbol(
            0.0, 0.0, [Orientation(0)], lambda o: {"1": (0.0, -3.81)}, lambda o: {"1": (0.0, -1.0)},
            lambda o: (-10.16, -7.62, 10.16, 7.62), lambda o: (-1.02, -3.81, 1.02, 3.81),
        )
        self.assertEqual(entry["orientations"][0]["body"], [-1.02, -3.81, 1.02, 3.81])
        (symbol,) = symbols_from_payload({"symbols": {"R1": entry}}, planned={}, movable=set())
        geometry = symbol.geometry[Orientation(0)]
        self.assertEqual(geometry.box, (-10.16, -7.62, 10.16, 7.62))
        self.assertEqual(geometry.body, (-1.02, -3.81, 1.02, 3.81))

    def test_a_symbol_the_server_did_not_describe_at_its_planned_orientation_is_an_error(self):
        with self.assertRaises(ValueError):
            symbols_from_payload({"symbols": {"R1": self.described()}}, planned={"R1": Orientation(180)}, movable=set())

    def test_symbols_not_listed_as_movable_or_ordered_are_neither(self):
        (symbol,) = symbols_from_payload({"symbols": {"R1": self.described()}}, planned={}, movable=set())
        self.assertFalse(symbol.movable)
        self.assertFalse(symbol.ordered)
        self.assertEqual(symbol.orientation, Orientation(0))


class WritesTests(unittest.TestCase):
    def test_a_refinement_becomes_moves_rotations_and_mirrors(self):
        cost = Cost(0.0, 0.0, 0, 0, 0, 0, 0)
        refinement = Refinement(
            {"J1": Placement((68.58, 45.72), Orientation(0, "")), "R1": Placement((45.72, 50.8), Orientation(90, "x"))},
            cost, cost,
        )
        moves, rotations, mirrors = writes_for(refinement)
        self.assertEqual(
            moves, [{"reference": "J1", "x_mm": 68.58, "y_mm": 45.72}, {"reference": "R1", "x_mm": 45.72, "y_mm": 50.8}],
        )
        self.assertEqual(rotations, {"J1": 0, "R1": 90})
        self.assertEqual(mirrors, {"J1": "", "R1": "x"})

    def test_a_negative_rail_is_entered_from_above_like_a_ground(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("U1", "Device", "R", "1k"), SymbolIntent("U2", "Device", "R", "1k")),
            nets=(
                NetIntent("VEE", (p("U1", "4"),), kind="power", polarity="negative"),
                NetIntent("VCC", (p("U2", "7"),), kind="power"),
            ),
        )
        facing = {c.net: c.facing for c in connections(intent, lambda net: False)}
        self.assertEqual(facing["VEE"], GROUND_FACING)
        self.assertEqual(facing["VCC"], SUPPLY_FACING)


if __name__ == "__main__":
    unittest.main()
