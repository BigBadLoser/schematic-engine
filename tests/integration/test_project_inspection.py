import tempfile
import unittest
from pathlib import Path

from backend.kicad.mcp_pro import McpProReadOnlyAdapter


class ProjectInspectionTests(unittest.TestCase):
    def test_inspection_returns_project_files_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in ("pro", "sch", "pcb"):
                (root / f"controller.kicad_{suffix}").write_text("test")
            project = McpProReadOnlyAdapter().inspect_project(root)
        self.assertEqual(project.name, "controller")
        self.assertEqual(project.schematic_file, "controller.kicad_sch")
        self.assertEqual(project.board_file, "controller.kicad_pcb")
        self.assertEqual(len(project.revisions), 3)
