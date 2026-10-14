import unittest

from backend.schematic import (
    NetIntent,
    PinEndpoint,
    PlacementHint,
    SchematicAuthoringPolicy,
    SchematicIntent,
    SymbolIntent,
)


class SchematicPolicyTests(unittest.TestCase):
    def test_semantic_roles_map_to_supported_mcp_design_intent(self) -> None:
        policy = SchematicAuthoringPolicy()
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("J1", "Connector_Generic", "Conn_01x01", "INPUT", role="input_connector"),
                SymbolIntent("U1", "MCU", "MCU", "MCU", role="mcu"),
                SymbolIntent("R1", "Device", "R", "1k", role="analog"),
            ),
            nets=(
                NetIntent(
                    "CRITICAL_SIG",
                    (PinEndpoint("J1", "1"), PinEndpoint("U1", "1")),
                    importance="critical",
                ),
            ),
        )

        payload = policy.mcp_design_intent(intent)

        self.assertEqual(payload["connector_refs"], ["J1"])
        self.assertEqual(payload["digital_refs"], ["U1"])
        self.assertEqual(payload["analog_refs"], ["R1"])
        self.assertEqual(payload["critical_nets"], ["CRITICAL_SIG"])

    def test_decoupling_hint_maps_to_supported_mcp_design_intent(self) -> None:
        policy = SchematicAuthoringPolicy()
        intent = SchematicIntent(
            symbols=(
                SymbolIntent("U1", "MCU", "MCU", "MCU", role="mcu"),
                SymbolIntent("C1", "Device", "C", "100nF", role="analog"),
            ),
            nets=(
                NetIntent(
                    "VCC",
                    (PinEndpoint("U1", "1"), PinEndpoint("C1", "1")),
                    kind="power",
                ),
            ),
            placement_hints=(PlacementHint("decouples", "C1", "U1.1"),),
        )

        payload = policy.mcp_design_intent(intent)

        self.assertEqual(
            payload["decoupling_pairs"],
            [{"ic_ref": "U1", "cap_refs": ["C1"], "max_distance_mm": 4.0}],
        )


if __name__ == "__main__":
    unittest.main()
