import unittest

from backend.schematic import (
    ConnectionIntent,
    FunctionalBlockIntent,
    NetIntent,
    PinEndpoint,
    PlacementHint,
    SchematicIntent,
    SymbolIntent,
)


class SchematicIntentTests(unittest.TestCase):
    def test_pin_cannot_belong_to_two_nets(self) -> None:
        with self.assertRaisesRegex(ValueError, "R1.1 belongs to multiple nets"):
            SchematicIntent(
                symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
                nets=(NetIntent("A", (PinEndpoint("R1", "1"),)), NetIntent("B", (PinEndpoint("R1", "1"),))),
            )

    def test_connections_cannot_reassign_a_pin_already_on_a_net(self) -> None:
        with self.assertRaisesRegex(ValueError, "R1.1 belongs to multiple nets"):
            SchematicIntent(
                symbols=(SymbolIntent("R1", "Device", "R", "1k"), SymbolIntent("R2", "Device", "R", "1k")),
                nets=(NetIntent("A", (PinEndpoint("R1", "1"),)),),
                connections=(ConnectionIntent("B", PinEndpoint("R1", "1"), PinEndpoint("R2", "1")),),
            )

    def test_semantic_intent_contains_no_layout_coordinates(self) -> None:
        symbol = SymbolIntent(
            reference="R1",
            library="ProjectParts",
            symbol_name="R",
            value="1k",
            footprint="ProjectParts:R_0603",
        )

        payload = symbol.to_mcp()

        self.assertNotIn("x_mm", payload)
        self.assertNotIn("y_mm", payload)
        self.assertNotIn("rotation", payload)

    def test_validates_connections_blocks_and_hints_against_known_refs(self) -> None:
        symbols = (
            SymbolIntent("R1", "Device", "R", "1k"),
            SymbolIntent("C1", "Device", "C", "100nF"),
        )
        intent = SchematicIntent(
            symbols=symbols,
            connections=(
                ConnectionIntent(
                    "FILTER_OUT",
                    PinEndpoint("R1", "2"),
                    PinEndpoint("C1", "1"),
                ),
            ),
            blocks=(
                FunctionalBlockIntent(
                    name="rc_filter",
                    label="RC FILTER",
                    members=("R1", "C1"),
                    presentation="frame",
                ),
            ),
            placement_hints=(PlacementHint("below", "C1", "R1"),),
        )

        self.assertEqual(intent.references, ("R1", "C1"))

    def test_unknown_block_member_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown symbols"):
            SchematicIntent(
                symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
                blocks=(FunctionalBlockIntent("bad", ("R1", "C9")),),
            )

    def test_net_intent_supports_single_external_endpoint(self) -> None:
        intent = SchematicIntent(
            symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
            nets=(
                NetIntent(
                    "VIN",
                    (PinEndpoint("R1", "1"),),
                    routing="label",
                ),
            ),
        )
        self.assertEqual(intent.all_nets[0].name, "VIN")

    def test_functional_block_hierarchy_cycle_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "hierarchy contains a cycle"):
            SchematicIntent(
                symbols=(
                    SymbolIntent("R1", "Device", "R", "1k"),
                    SymbolIntent("R2", "Device", "R", "1k"),
                ),
                blocks=(
                    FunctionalBlockIntent(
                        "a",
                        ("R1",),
                        parent="b",
                    ),
                    FunctionalBlockIntent(
                        "b",
                        ("R2",),
                        parent="a",
                    ),
                ),
            )

    def test_repeated_instance_source_must_exist(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown repeated instance source"):
            SchematicIntent(
                symbols=(SymbolIntent("R1", "Device", "R", "1k"),),
                blocks=(
                    FunctionalBlockIntent(
                        "channel_1",
                        ("R1",),
                        repeated_instance_of="channel_template",
                    ),
                ),
            )

    def test_rich_net_and_block_semantics_remain_coordinate_free(self) -> None:
        r2 = PinEndpoint("R1", "2")
        c1 = PinEndpoint("C1", "1")
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("R1", "Device", "R", "1k", role="analog"),
                SymbolIntent("C1", "Device", "C", "100nF", role="analog"),
            ),
            nets=(
                NetIntent(
                    "FILTER_OUT",
                    (r2, c1),
                    signal_class="analog",
                    drivers=(r2,),
                    sinks=(c1,),
                    expected_driver_count=1,
                    scope="local",
                    importance="high",
                ),
            ),
            blocks=(
                FunctionalBlockIntent(
                    "rc_filter",
                    ("R1", "C1"),
                    role="filter",
                    inputs=("FILTER_OUT",),
                    outputs=("FILTER_OUT",),
                    presentation="frame",
                ),
            ),
            placement_hints=(
                PlacementHint("left_of", "R1", "C1"),
                PlacementHint("belongs_to", "C1", "rc_filter"),
            ),
        )
        self.assertEqual(intent.nets[0].drivers, (r2,))
        self.assertEqual(intent.blocks[0].role, "filter")
        self.assertNotIn("x_mm", repr(intent))
        self.assertNotIn("y_mm", repr(intent))

    def test_only_a_negative_power_rail_hangs(self) -> None:
        pin = (PinEndpoint("U1", "4"),)
        self.assertTrue(NetIntent("VEE", pin, kind="power", polarity="negative").hangs)
        self.assertFalse(NetIntent("VCC", pin, kind="power", polarity="positive").hangs)
        self.assertFalse(NetIntent("-5V", pin, kind="power").hangs)  # the name is not read
        self.assertFalse(NetIntent("GND", pin, kind="ground", polarity="negative").hangs)
        self.assertFalse(NetIntent("SIG", pin, polarity="negative").hangs)


if __name__ == "__main__":
    unittest.main()

