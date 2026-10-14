"""The routing step's recovery from a libavoid result it would otherwise refuse: which failures are re-routed, how obstacles
are widened for the retry, and what a final failure says about the attempts made."""
from __future__ import annotations

import importlib.util
import unittest

NEEDS_MCP_PRO = unittest.skipUnless(importlib.util.find_spec("kicad_mcp"), "needs kicad-mcp-pro (the server imports it)")


@NEEDS_MCP_PRO
class RouteRecoveryTests(unittest.TestCase):
    def setUp(self):
        from backend.kicad import mcp_extended_server as server

        self.server = server

    def test_a_body_hit_or_a_foreign_pin_is_rerouted_and_a_short_is_not(self):
        should = self.server.should_reroute_failure
        self.assertTrue(should("libavoid route intersects symbol body: U1 x=144.78"))
        self.assertTrue(should("Net-aware route intersects foreign pin U1:3 on e1"))
        self.assertFalse(should("libavoid overlapped routes from distinct nets: 'A' and 'B'"))
        self.assertFalse(should("Net-aware grouped route intersects foreign net at e1"))

    def test_only_obstacle_nodes_are_widened(self):
        nodes = [
            {"id": "obstacle::U1", "x": 10.0, "y": 10.0, "width": 5.0, "height": 5.0},
            {"id": "pin::1", "x": 1.0, "y": 1.0, "width": 0.0, "height": 0.0},
        ]
        self.server.widen_obstacle_nodes(nodes, 1.27)
        self.assertEqual(nodes[0], {"id": "obstacle::U1", "x": 8.73, "y": 8.73, "width": 7.54, "height": 7.54})
        self.assertEqual(nodes[1]["x"], 1.0)

    def test_a_final_failure_says_what_was_tried(self):
        error = self.server.failure_with_repair_counts(ValueError("libavoid route intersects symbol body: U1"), 1, 0)
        self.assertTrue(str(error).endswith("(after 1 reroute, 0 diagonal repairs)"))


if __name__ == "__main__":
    unittest.main()
