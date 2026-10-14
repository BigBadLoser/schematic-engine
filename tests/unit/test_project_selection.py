import tempfile
import unittest
from pathlib import Path

from backend.kicad.mcp_pro import McpProReadOnlyAdapter


class ProjectSelectionTests(unittest.TestCase):
    def test_prefers_project_named_root_schematic_over_child_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "controller.kicad_pro").write_text("{}", encoding="utf-8")
            (root / "controller.kicad_sch").write_text("", encoding="utf-8")
            (root / "Display.kicad_sch").write_text("", encoding="utf-8")
            metadata = McpProReadOnlyAdapter().inspect_project(root)
        self.assertEqual(metadata.schematic_file, "controller.kicad_sch")

    def test_multiple_schematics_without_project_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "A.kicad_sch").write_text("", encoding="utf-8")
            (root / "B.kicad_sch").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Ambiguous KiCad root schematic"):
                McpProReadOnlyAdapter().inspect_project(root)
