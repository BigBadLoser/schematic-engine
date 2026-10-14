import unittest

from backend.kicad.project_graph import build_project_graph


class ProjectGraphDiagnosticTests(unittest.TestCase):
    def test_text_parse_failure_is_reported(self) -> None:
        graph = build_project_graph(
            {
                "symbols": {
                    "result": (
                        "Symbols (2 total):\n"
                        "- U1 MCU MCU:Part @ (10, 20) rot=0 unit=1\n"
                        "- upstream format changed"
                    )
                }
            }
        )
        diagnostic = graph["parse_diagnostics"][0]
        self.assertEqual(diagnostic["parsed_items"], 1)
        self.assertEqual(diagnostic["candidate_items"], 2)
        self.assertTrue(graph["warnings"])

    def test_named_net_identity_does_not_depend_on_point_count(self) -> None:
        graph = build_project_graph(
            {
                "symbols": {
                    "result": "Symbols (1 total):\n- U1 MCU MCU:Part @ (10, 20) rot=0 unit=1"
                },
                "connectivity": {
                    "result": "Connectivity groups (1 total):\n- Group 1: GND | pins=U1:1 | points=99"
                },
            }
        )
        net = next(node for node in graph["nodes"] if node["kind"] == "net")
        self.assertEqual(net["id"], "net:GND")
