"""The ladder plan: what ``ladder_layout`` decides, recorded as data without coordinates."""
from __future__ import annotations

import json
import random
import unittest

from backend.schematic.ladder_layout import _core, ladder_layout
from backend.schematic.ladder_plan import (
    Column, Hang, LayoutRecord, Plan, Row, Span, body_box, plan_and_layout, plan_from_layout,
)
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Connection
from tests.unit import test_ladder_layout as layout_tests    # the module, so its test classes are not collected twice

FLAT, Roles, either_way_up, toy = layout_tests.FLAT, layout_tests.Roles, layout_tests.either_way_up, layout_tests.toy


def scenarios():
    """Named (symbols, nets, roles) problems that between them exercise every kind of decision the plan records."""
    out = {"toy": toy()}
    blocks = layout_tests.LadderBlockPlanTests("control_toy")
    out["control feeder"] = blocks.control_toy()
    parallel = blocks.parallel_toy()
    out["parallel rows"] = parallel
    symbols, nets, roles = parallel
    supply = either_way_up("VS")
    out["free supply"] = (
        [*symbols, supply],
        [*nets, Connection("VCC", (("VS", "1"),), style="power"), Connection("GND", (("VS", "2"), ("B2", "2")), style="power")],
        Roles({"VCC": "supply", "GND": "ground"}, ("in",), {**roles.chains, "supplies": ("VS",)}, {}, {}, {}, (), ()),
    )
    side = layout_tests.LadderSideRowTests("toy")
    out["side rows"] = side.toy()
    out["entered by the top pin"] = side.toy(entered_by_the_top_pin=True)
    pair = layout_tests.LadderPairAndStackTests("pair_toy")
    out["pair"] = pair.pair_toy()
    out["stack"] = pair.stack_toy()
    return out


def plan_of(symbols, nets, roles) -> Plan:
    return plan_from_layout(symbols, nets, roles, text_widths={})


def numbers(value):
    """Every float in a JSON-able structure."""
    if isinstance(value, float):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from numbers(item)


class PlanRecordingTests(unittest.TestCase):
    def test_the_toy_ladder_plan_names_rows_hangs_and_spans(self):
        plan = plan_of(*toy())
        chain = next(row for row in plan.rows if row.kind == "chain")
        self.assertEqual(chain.order, ("J1", "U1", "RLOAD"))
        self.assertEqual(chain.name, "main#0")
        self.assertEqual(plan.pins["U1"], "VI")                       # the chain enters the chip by its input pin
        hangs = {h.reference: h for h in plan.hangs}
        self.assertEqual(set(hangs), {"C3", "R1", "C2", "RTERM", "CLOAD"})
        self.assertEqual((hangs["C3"].wire, hangs["C3"].host, hangs["C3"].side), ("out", "U1.VO", "below"))
        self.assertEqual({hangs["R1"].host, hangs["C2"].host}, {"U1.ADJ"})
        self.assertEqual(sorted((hangs["R1"].rank, hangs["C2"].rank)), [0, 1])            # one nearer the pin than the other
        self.assertEqual({s.reference: (s.from_wire, s.to_wire) for s in plan.spans},
                         {"R4": ("out", "adj"), "D3": ("out", "adj")})
        self.assertEqual({row.name for row in plan.rows if row.kind == "trunk"}, {"out", "adj", "load"} - {"out"})
        self.assertEqual(set(plan.orientations), {s.reference for s in toy()[0]})

    def test_rows_run_top_to_bottom_each_naming_the_one_over_it(self):
        plan = plan_of(*toy())
        self.assertIsNone(plan.rows[0].above)
        for over, row in zip(plan.rows, plan.rows[1:]):
            self.assertEqual(row.above, over.name)
        names = [row.name for row in plan.rows]
        self.assertLess(names.index("main#0"), names.index("adj"))             # the chain row is over the trunk row

    def test_a_control_feeder_is_a_column_from_the_pin_it_feeds_nearest_part_first_down(self):
        plan = plan_of(*layout_tests.LadderBlockPlanTests("control_toy").control_toy())
        self.assertEqual([c.pin for c in plan.columns], ["Q.G"])
        self.assertEqual(plan.columns[0].order, ("F2", "F1"))             # the last part of the block stands nearest the pin
        self.assertEqual(len(plan.columns[0].orientations), 2)
        chain = next(row for row in plan.rows if row.kind == "chain")
        self.assertEqual(chain.order, ("V", "Q", "L"))                    # the feeder is not on the row

    def test_parts_that_share_no_signal_net_are_a_free_column(self):
        plan = plan_of(*scenarios()["free supply"])
        free = [c for c in plan.columns if c.pin == ""]
        self.assertEqual([c.order for c in free], [("VS",)])
        self.assertEqual(len([row for row in plan.rows if row.kind == "chain"]), 2)       # two parallel stages

    def test_a_pair_is_two_members_of_one_chain_row_and_a_stack_has_a_row_of_its_own(self):
        pair = plan_of(*scenarios()["pair"])
        chain = next(row for row in pair.rows if row.kind == "chain")
        self.assertEqual(chain.order, ("F", "QA", "QB"))
        self.assertEqual(pair.sequence, (("F", "QA", "QB"),))
        stack = plan_of(*scenarios()["stack"])
        self.assertEqual(len([row for row in stack.rows if row.kind == "chain"]), 2)      # the path, and the load stacked over Q1
        self.assertEqual(len(stack.sequence), 2)

    def test_a_side_row_and_an_entered_by_the_top_pin_chain_are_in_the_plan(self):
        plan = plan_of(*scenarios()["side rows"])
        self.assertIn("side", {row.kind for row in plan.rows})
        hung = {h.reference: h for h in plan.hangs}
        self.assertEqual(hung["H"].side, "above")
        self.assertEqual(hung["G"].side, "below")

    def test_every_placed_part_has_its_orientation_in_the_plan_and_the_layout_is_unchanged_by_recording(self):
        for name, (symbols, nets, roles) in scenarios().items():
            with self.subTest(name):
                plain = ladder_layout(symbols, nets, roles, text_widths={})
                plan, recorded = plan_and_layout(symbols, nets, roles, text_widths={})
                self.assertEqual(recorded, plain)
                self.assertEqual(plan.orientations, {ref: place.orientation for ref, place in sorted(plain.items())})
                record = LayoutRecord()
                self.assertEqual(ladder_layout(symbols, nets, roles, text_widths={}, record=record), plain)
                self.assertTrue(record.groups)

    def test_the_plan_holds_no_coordinates(self):
        for name, scenario in scenarios().items():
            with self.subTest(name):
                data = plan_of(*scenario).to_json()
                self.assertEqual(list(numbers(data)), [])
                json.dumps(data)

    def test_the_plan_does_not_depend_on_the_order_of_the_input(self):
        rng = random.Random(23)
        for name, (symbols, nets, roles) in scenarios().items():
            expected = plan_of(symbols, nets, roles)
            for _ in range(5):
                shuffled_symbols, shuffled_nets = symbols[:], nets[:]
                rng.shuffle(shuffled_symbols)
                rng.shuffle(shuffled_nets)
                with self.subTest(name):
                    self.assertEqual(plan_of(shuffled_symbols, shuffled_nets, roles), expected)


class PlanDataTests(unittest.TestCase):
    def sample(self) -> Plan:
        turn = Orientation(270, "y")
        return Plan(
            rows=(Row("main#0", "chain", None, ("J1", "U1")), Row("adj", "trunk", "main#0", ())),
            hangs=(Hang("C1", "adj", "U1.3", "below", FLAT, 0), Hang("C2", "adj", "U1.3", "below", turn, 1)),
            spans=(Span("R4", "out", "adj", turn),),
            columns=(Column("U1.3", ("F2", "F1"), (FLAT, turn)), Column("", ("V1",), (FLAT,))),
            orientations={"J1": FLAT, "U1": turn, "C1": FLAT},
            pins={"J1": "SIG", "U1": "VI"},
            sequence=(("J1", "U1"),),
        )

    def test_json_round_trips_through_text(self):
        plan = self.sample()
        self.assertEqual(Plan.from_json(plan.to_json()), plan)
        self.assertEqual(Plan.from_json(json.dumps(plan.to_json())), plan)
        self.assertEqual(Plan.from_json(plan.dumps()), plan)

    def test_the_demo_and_toy_plans_round_trip(self):
        for name, scenario in scenarios().items():
            with self.subTest(name):
                plan = plan_of(*scenario)
                self.assertEqual(Plan.from_json(json.loads(json.dumps(plan.to_json()))), plan)

    def test_an_unknown_version_is_refused(self):
        data = self.sample().to_json()
        data["version"] = 99
        with self.assertRaises(ValueError):
            Plan.from_json(data)

    def test_the_summary_names_every_hang_and_span(self):
        text = self.sample().summary()
        for word in ("C1", "C2", "R4", "main#0", "adj", "column from U1.3"):
            self.assertIn(word, text)

    def test_a_body_is_the_box_less_the_lead_on_each_side_a_pin_points_through(self):
        symbols, _nets, _roles = toy()
        for symbol in symbols:
            for geometry in symbol.geometry.values():
                self.assertEqual(body_box(geometry), _core(geometry))
        u1 = next(s for s in symbols if s.reference == "U1").geometry[FLAT]
        self.assertEqual(body_box(u1), (-5.08 + 1.27, -5.08, 5.08 - 1.27, 5.08 - 1.27))


if __name__ == "__main__":
    unittest.main()


class StackedPairPlanTests(unittest.TestCase):
    def test_a_half_bridge_is_recorded_as_a_column_with_the_load_hung_sideways(self):
        symbols, nets, roles = layout_tests.half_bridge()
        plan = plan_of(symbols, nets, roles)
        self.assertEqual([(c.pin, c.order) for c in plan.columns], [("net:j0", ("QH0", "QL0"))])
        self.assertEqual(plan.columns[0].orientations, (plan.orientations["QH0"], plan.orientations["QL0"]))
        self.assertEqual([(h.reference, h.wire, h.host, h.side) for h in plan.hangs], [("RL", "j0", "QH0.S", "right")])
        self.assertFalse({row.name for row in plan.rows if row.kind == "trunk"})
        self.assertEqual(plan.parts(), {"QH0", "QL0", "RL"})
        self.assertEqual(Plan.from_json(plan.to_json()), plan)
        self.assertIn("column from net:j0: QH0@0 QL0@0", plan.summary())

    def test_two_half_bridges_and_their_load_are_two_columns_and_a_span(self):
        symbols, nets, roles = layout_tests.half_bridge(legs=2)
        plan = plan_of(symbols, nets, roles)
        self.assertEqual([(c.pin, c.order) for c in plan.columns], [("net:j0", ("QH0", "QL0")), ("net:j1", ("QH1", "QL1"))])
        self.assertEqual([(s.reference, s.from_wire, s.to_wire) for s in plan.spans], [("RL", "j0", "j1")])
        self.assertEqual(plan.hangs, ())
        self.assertEqual(plan.orientations["QH1"].mirror, "y")           # gates outward
        rng = random.Random(3)
        for _ in range(4):
            shuffled_symbols, shuffled_nets = symbols[:], nets[:]
            rng.shuffle(shuffled_symbols)
            rng.shuffle(shuffled_nets)
            self.assertEqual(plan_of(shuffled_symbols, shuffled_nets, roles), plan)


    def test_gate_feeders_are_hangs_on_the_gate_wires_and_the_columns_are_listed_left_to_right(self):
        symbols, nets, roles = layout_tests.half_bridge(legs=2, feeders=True)
        plan = plan_of(symbols, nets, roles)
        self.assertEqual([c.pin for c in plan.columns], ["net:j0", "net:j1"])
        self.assertEqual({(h.reference, h.wire, h.host, h.side, h.rank) for h in plan.hangs},
                         {("RGH0", "gH0", "QH0.G", "left", 0), ("RGL0", "gL0", "QL0.G", "left", 0),
                          ("RGH1", "gH1", "QH1.G", "right", 0), ("RGL1", "gL1", "QL1.G", "right", 0)})
        self.assertFalse([row for row in plan.rows if row.kind == "chain"])          # no chain row holds a feeder
        self.assertEqual(plan.parts(), {s.reference for s in symbols})
        self.assertEqual(Plan.from_json(plan.to_json()), plan)


class PartialPlanTests(unittest.TestCase):
    def test_a_part_the_partial_plan_leaves_out_is_in_no_structure(self):
        symbols, nets, roles = toy()
        wide = layout_tests.part("C3", {"1": layout_tests.PinGeometry((0.0, 5.08), layout_tests.DOWN),
                                        "2": layout_tests.PinGeometry((0.0, -5.08), layout_tests.UP)},
                                 (-400.0, -5.08, 400.0, 5.08), body=(-2.0, -2.0, 2.0, 2.0))
        symbols = [wide if s.reference == "C3" else s for s in symbols]
        plan, placed = plan_and_layout(symbols, nets, roles, text_widths={}, partial=True)
        self.assertNotIn("C3", placed)
        structures = ([ref for row in plan.rows for ref in row.order] + [h.reference for h in plan.hangs]
                      + [s.reference for s in plan.spans] + [ref for c in plan.columns for ref in c.order])
        self.assertNotIn("C3", structures)
        self.assertNotIn("C3", plan.orientations)
        for ref in set(structures):
            self.assertEqual(structures.count(ref), 1, ref)          # nothing is in two structures
