import tempfile
import unittest
from pathlib import Path

from backend.kicad.revisions import project_revisions


class RevisionTests(unittest.TestCase):
    def test_tracks_kicad_documents_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "motor.kicad_pro").write_text("pro")
            (root / "motor.kicad_sch").write_text("sch")
            (root / "notes.txt").write_text("ignore")
            sheets = root / "sheets"
            sheets.mkdir()
            (sheets / "power.kicad_sch").write_text("child")
            revisions = project_revisions(root)

        self.assertEqual(
            [item.path for item in revisions],
            ["motor.kicad_pro", "motor.kicad_sch", "sheets/power.kicad_sch"],
        )
        self.assertTrue(all(len(item.sha256) == 64 for item in revisions))
