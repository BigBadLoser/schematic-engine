import unittest

from backend.kicad.project_graph import build_project_graph


class ProjectGraphTests(unittest.TestCase):
    def test_builds_component_net_and_footprint_nodes_from_read_only_results(self) -> None:
        graph = build_project_graph({
            "symbols": {"result": "Symbols (1 total):\n- J1 Conn_01x03 Connector_Generic:Conn_01x03 @ (125.73, 68.58) rot=90 unit=1 footprint=Connector_Wire:Part"},
            "connectivity": {"result": "Connectivity groups (1 total):\n- Group 1: GND | pins=J1:2 | points=9"},
            "footprints": {"result": "Footprints (1 total):\n- J1 (Conn_01x03) @ (133.88, 83.83) mm layer=BL_F_Cu id=c2d31103..."},
        })
        self.assertEqual(graph["total_matches"], 3)
        self.assertFalse(graph["truncated"])
        self.assertIn({"kind": "connects", "from": "net:GND", "to": "component:J1"}, graph["edges"])
        self.assertIn({"kind": "implements", "from": "component:J1", "to": "footprint:c2d31103"}, graph["edges"])
        self.assertFalse(graph["warnings"])
