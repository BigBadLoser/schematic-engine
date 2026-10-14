from __future__ import annotations

import unittest

from backend.schematic.intent import NetIntent, PinEndpoint, SchematicIntent, SymbolIntent
from backend.schematic.relations import (
    NodeClusterConstraint,
    ParallelBankConstraint,
    PinRelationConstraint,
    SeriesRelationConstraint,
    anchor_side,
    branch,
    compile_schematic_relations,
    parallel_bank,
    pull_to_rail,
    series,
    shunt_to_rail,
)


def ep(reference: str, pin: str) -> PinEndpoint:
    return PinEndpoint(reference, pin)


class SchematicRelationsTest(unittest.TestCase):
    def setUp(self) -> None:
        symbols = tuple(
            SymbolIntent(ref, "Device", "R", ref)
            for ref in ("U1", "R1", "SW1", "R2", "X1", "C1", "C2", "C3", "C4")
        )
        self.intent = SchematicIntent(
            symbols=symbols,
            nets=(
                NetIntent("NRST", (ep("U1", "4"), ep("R1", "2"), ep("SW1", "1"))),
                NetIntent("BOOT0", (ep("U1", "1"), ep("R2", "1"))),
                NetIntent("OSC_IN", (ep("U1", "2"), ep("X1", "1"), ep("C3", "1")), kind="clock"),
                NetIntent("OSC_OUT", (ep("U1", "3"), ep("X1", "2"), ep("C4", "1")), kind="clock"),
                NetIntent(
                    "+3V3",
                    (
                        ep("U1", "16"), ep("U1", "5"),
                        ep("R1", "1"), ep("C1", "1"), ep("C2", "1"),
                    ),
                    kind="power",
                ),
                NetIntent(
                    "GND",
                    (
                        ep("SW1", "2"), ep("R2", "2"),
                        ep("C1", "2"), ep("C2", "2"),
                        ep("C3", "2"), ep("C4", "2"),
                    ),
                    kind="ground",
                ),
            ),
        )

    def test_level_one_compiler(self) -> None:
        relations = (
            anchor_side("R1", "SW1", anchor=ep("U1", "4"), side="left", distance_steps=4),
            pull_to_rail("R1", node=ep("U1", "4"), rail="+3V3"),
            shunt_to_rail("SW1", node=ep("U1", "4"), rail="GND"),
            anchor_side("R2", anchor=ep("U1", "1"), side="left", distance_steps=2),
            pull_to_rail("R2", node=ep("U1", "1"), rail="GND"),
            series(
                "X1",
                first=ep("U1", "2"),
                second=ep("U1", "3"),
                side="left",
                distance_steps=4,
            ),
            branch("C3", anchor=ep("X1", "1"), side="left", distance_steps=2),
            shunt_to_rail(
                "C3", node=ep("X1", "1"), rail="GND", rail_group="osc_ground"
            ),
            branch("C4", anchor=ep("X1", "2"), side="left", distance_steps=2),
            shunt_to_rail(
                "C4", node=ep("X1", "2"), rail="GND", rail_group="osc_ground"
            ),
            parallel_bank(
                ("C1", "C2"),
                anchors=(ep("U1", "16"), ep("U1", "5")),
                near_rail="+3V3",
                far_rail="GND",
                side="above",
                distance_steps=3,
                group_prefix="decoupling",
            ),
        )
        compiled = compile_schematic_relations(self.intent, relations)

        self.assertEqual(len(compiled.placements), 6)
        self.assertTrue(
            any(isinstance(item, NodeClusterConstraint) for item in compiled.placements)
        )
        self.assertTrue(
            any(isinstance(item, SeriesRelationConstraint) for item in compiled.placements)
        )
        self.assertTrue(
            any(isinstance(item, ParallelBankConstraint) for item in compiled.placements)
        )
        reset = next(
            item
            for item in compiled.placements
            if isinstance(item, NodeClusterConstraint)
        )
        self.assertEqual(reset.anchor, ep("U1", "4"))
        self.assertEqual(reset.side, "left")
        reset_members = {member.reference: member for member in reset.members}
        self.assertEqual(reset_members["R1"].far_pin, "1")
        self.assertEqual(reset_members["R1"].extend_toward, "above")
        self.assertEqual(reset_members["SW1"].extend_toward, "below")

        osc_ground = next(
            group for group in compiled.power_groups if group.name == "osc_ground"
        )
        self.assertEqual(
            {endpoint.token for endpoint in osc_ground.endpoints},
            {"C3.2", "C4.2"},
        )
        self.assertEqual(osc_ground.direction, "below")
        decoupling_near = next(
            group for group in compiled.power_groups
            if group.name == "decoupling:near"
        )
        self.assertEqual(
            {endpoint.token for endpoint in decoupling_near.endpoints},
            {"C1.1", "C2.1", "U1.16", "U1.5"},
        )
        self.assertEqual(decoupling_near.direction, "above")

        nrst_node = next(
            node for node in compiled.node_connections if node.net == "NRST"
        )
        self.assertEqual(
            {endpoint.token for endpoint in nrst_node.members},
            {"R1.2", "SW1.1"},
        )
        self.assertTrue(nrst_node.shared_node)
        self.assertEqual(len(compiled.placement_groups), 4)
        left_groups = [
            group for group in compiled.placement_groups
            if group.side == "left"
        ]
        self.assertEqual(len(left_groups), 3)
        oscillator_group = next(
            group
            for group in left_groups
            if {"X1", "C3", "C4"} <= set(group.references)
        )
        self.assertEqual(len(oscillator_group.placement_indexes), 3)
        self.assertEqual(len(oscillator_group.root_placement_indexes), 1)
        self.assertNotIn("offset_x_mm", reset.__dict__)
        self.assertNotIn("rotation", reset.__dict__)

    def test_a_branch_with_no_rail_can_say_which_way_its_far_pin_goes(self) -> None:
        def placement(*relations):
            compiled = compile_schematic_relations(self.intent, relations)
            return next(item for item in compiled.placements if isinstance(item, PinRelationConstraint))

        plain = placement(branch("R2", anchor=ep("U1", "1"), side="above"))
        self.assertEqual((plain.far_pin, plain.extend_toward), (None, None))
        extended = placement(branch("R2", anchor=ep("U1", "1"), side="above", extend="above"))
        self.assertEqual((extended.pin, extended.far_pin, extended.extend_toward), ("1", "2", "above"))
        # a rail decorator points the part itself, and wins
        decorated = placement(
            branch("R2", anchor=ep("U1", "1"), side="above", extend="left"),
            pull_to_rail("R2", node=ep("U1", "1"), rail="GND"),
        )
        self.assertEqual((decorated.far_pin, decorated.extend_toward), ("2", "below"))


if __name__ == "__main__":
    unittest.main()
