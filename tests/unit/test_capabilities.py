import unittest
from unittest.mock import patch

from backend.kicad.capabilities import current_capabilities


class CapabilityTests(unittest.TestCase):
    @patch("backend.kicad.capabilities.executable_available")
    def test_runtime_discovery_and_live_proof_are_distinct(self, available: object) -> None:
        available.return_value = True  # type: ignore[attr-defined]
        runtime = {
            "connected": True,
            "protocol_version": "2025-11-25",
            "tools": ["sch_get_symbols", "pcb_get_board_summary"],
            "successful_tools": ["sch_get_symbols"],
        }
        by_operation = {
            item["operation"]: item for item in current_capabilities(runtime)
        }
        self.assertEqual(
            by_operation["mcp_pro_protocol_handshake"]["proof_level"],
            "RUNTIME_VERIFIED",
        )
        self.assertTrue(
            by_operation["mcp_pro_schematic_symbols_read"]["implemented"]
        )
        self.assertEqual(
            by_operation["mcp_pro_schematic_symbols_read"]["proof_level"],
            "LIVE_KICAD_READBACK_VERIFIED",
        )
        self.assertFalse(
            by_operation["mcp_pro_board_summary_read"]["implemented"]
        )
        self.assertTrue(
            by_operation["mcp_pro_board_summary_read"]["available"]
        )
        self.assertEqual(
            by_operation["mcp_pro_board_summary_read"]["proof_level"],
            "RUNTIME_DISCOVERED",
        )
        self.assertIsNone(
            by_operation["mcp_pro_board_summary_read"]["requires_gui"]
        )
        self.assertTrue(by_operation["kicad_cli_available"]["supports_headless"])
