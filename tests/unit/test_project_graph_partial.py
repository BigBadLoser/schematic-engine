import unittest

from backend.kicad.project_graph import build_project_graph


class PartialProjectGraphTests(unittest.TestCase):
    def test_schematic_only_graph_is_valid(self) -> None:
        graph = build_project_graph(
            {
                "symbols": {
                    "result": "Symbols (1 total):\n- U1 MCU MCU:Part @ (10, 20) rot=0 unit=1"
                },
                "connectivity": {
                    "result": "Connectivity groups (1 total):\n- Group 1: GND | pins=U1:1 | points=2"
                },
            }
        )
        self.assertEqual(graph["returned_items"], 2)
        self.assertEqual(len(graph["edges"]), 1)

    def test_board_only_graph_is_valid(self) -> None:
        graph = build_project_graph(
            {
                "footprints": {
                    "result": "Footprints (1 total):\n- J1 (Connector) @ (1, 2) mm layer=F.Cu id=abc123..."
                }
            }
        )
        self.assertEqual(graph["returned_items"], 1)
        self.assertEqual(graph["edges"], [])
