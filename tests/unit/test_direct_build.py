import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.schematic import direct_build, timing
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Placement


class _Transaction:
    def __init__(self, root, schematic_file) -> None:
        pass

    def run(self, operation):
        return operation(), "before", "after"


def _symbol(reference: str, auxiliary: bool = False) -> SimpleNamespace:
    return SimpleNamespace(reference=reference, authoring_auxiliary=auxiliary, to_mcp=lambda: {"reference": reference})


def _endpoint(reference: str) -> SimpleNamespace:
    return SimpleNamespace(reference=reference)


class DirectBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        timing.reset()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sheet = self.root / "a.kicad_sch"
        self.sheet.write_text("x", encoding="utf-8")
        self.intent = SimpleNamespace(
            symbols=[_symbol("R2"), _symbol("R1"), _symbol("#FLG1", True)],
            references=("R1", "R2", "#FLG1"),
            all_nets=[SimpleNamespace(endpoints=[_endpoint("R1"), _endpoint("#FLG1")])],
        )
        self.calls: list[str] = []
        self.moves: list[dict] = []
        workflow = Mock()
        workflow._artifact_root = self.root
        workflow.available.return_value = True
        workflow.client.list_tools.return_value = {"run_erc", "schematic_connectivity_gate"}
        workflow._symbol_placements_from_listing.return_value = {
            "R1": {"x_mm": 10.0, "y_mm": 10.0}, "R2": {"x_mm": 20.0, "y_mm": 10.0}, "#FLG1": {"x_mm": 12.0, "y_mm": 10.0},
        }
        workflow._initial_layout_policy.return_value.describe.return_value = "tight"

        def write(root, sheet, name, arguments):
            if name == "sch_move_symbol":
                self.moves.append(arguments)
            return {}

        workflow._write.side_effect = write
        workflow._read.return_value = {}
        workflow._visual_qa_codes.return_value = set()
        workflow._run_cosmetic_loop.return_value = ({"cosmetic_score": 90.0}, ())
        workflow._place_fields.return_value = True
        for name in ("_apply_layout_policy", "_rebuild_at_mcp_placements", "_apply_no_connects", "_route_semantic_nets",
                     "_place_fields", "_assert_acceptance_gate", "_assert_erc_has_no_errors", "_verify_references"):
            getattr(workflow, name).side_effect = (lambda n: lambda *a, **k: self.calls.append(n) or getattr(
                workflow, n).return_value)(name)
        workflow._route_semantic_nets.return_value = ()
        self.workflow = workflow
        self.placement = {
            "R2": Placement((30.0, 5.0), Orientation(90, "")),
            "R1": Placement((15.0, 5.0), Orientation(180, "x")),
            "#FLG9": Placement((0.0, 0.0), Orientation(0, "")),
        }

    def _build(self) -> dict:
        patches = (
            patch.object(direct_build, "SchematicFileTransaction", _Transaction),
            patch.object(direct_build, "measured_scene_from_kicad", return_value=(Mock(to_dict=lambda **k: {}), {})),
            patch.object(direct_build, "parse_kicad_xml"),
            patch.object(direct_build, "verify_intent_netlist", return_value={"status": "PASS"}),
            patch.dict(os.environ, {"KICAD_AI_TIMING": "1"}),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return direct_build.build_direct(self.workflow, self.root, self.sheet, self.intent, self.placement)

    def test_stage_sequence_and_files(self) -> None:
        self._build()
        data = json.loads((self.root / "timing.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [p for p in data["stages"] if "/" not in p], ["checks", "create", "fields", "place", "terminals_and_routes"],
        )
        self.assertEqual(
            self.calls[:4], ["_apply_layout_policy", "_rebuild_at_mcp_placements", "_apply_no_connects", "_route_semantic_nets"],
        )
        self.assertEqual(self.calls[4], "_place_fields")
        record = json.loads((self.root / "direct-build.json").read_text(encoding="utf-8"))
        self.assertEqual(record["stages"], ["create", "place", "terminals_and_routes", "fields", "checks"])
        self.assertEqual(record["checks"]["netlist_verification"], "PASS")
        self.assertEqual(record["placement"]["R1"], {"position": [15.0, 5.0], "rotation": 180, "mirror": "x"})
        self.assertEqual(record["placement_ignored"], ["#FLG9"])

    def test_placement_in_sorted_order_with_orientation_plan(self) -> None:
        built = self._build()
        moved = [m["reference"] for m in self.moves]
        self.assertEqual(moved, ["R1", "R2", "#FLG1"])       # parts sorted, then the flag that follows R1
        self.assertEqual((self.moves[2]["x_mm"], self.moves[2]["y_mm"]), (17.0, 5.0))   # R1 moved by (+5, -5)
        orientation = built["result"].annotations[0]
        self.assertEqual(orientation["rotations"], {"R1": 180, "R2": 90})
        self.assertEqual(orientation["mirrors"], {"R1": "x"})
        self.workflow._rebuild_at_mcp_placements.assert_called_once()
        names = [c.args[2] for c in self.workflow._write.call_args_list]
        self.assertIn("sch_translate_references_to_margin", names)
        self.assertIn("sch_set_symbol_mirror", names)
        self.assertIn("sch_modify_field_orientation", names)

    def test_missing_reference_is_refused(self) -> None:
        del self.placement["R2"]
        with self.assertRaisesRegex(ValueError, "R2"):
            self._build()


if __name__ == "__main__":
    unittest.main()
