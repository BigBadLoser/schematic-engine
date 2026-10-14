import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.kicad.render import KiCadCliSchematicRenderer


class SchematicRenderTests(unittest.TestCase):
    @patch("backend.kicad.render.shutil.which", return_value="/usr/bin/kicad-cli")
    @patch("backend.kicad.render.subprocess.run")
    def test_invokes_headless_svg_export(
        self,
        run: object,
        which: object,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "fixture.kicad_sch"
            schematic.write_text("(kicad_sch)", encoding="utf-8")
            output = root / ".kicad-ai" / "renders" / "fixture"

            def fake_run(command, **kwargs):
                output.mkdir(parents=True, exist_ok=True)
                (output / "fixture.svg").write_text("<svg/>", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            run.side_effect = fake_run  # type: ignore[attr-defined]
            renderer = KiCadCliSchematicRenderer()

            result = renderer.render_svg(root, schematic)

            command = list(result.command)
            self.assertEqual(command[:4], ["/usr/bin/kicad-cli", "sch", "export", "svg"])
            self.assertIn("--exclude-drawing-sheet", command)
            self.assertIn("--output", command)
            self.assertEqual(result.svg_paths, (str(output / "fixture.svg"),))

    @patch("backend.kicad.render.shutil.which", return_value=None)
    def test_fails_cleanly_when_kicad_cli_is_missing(self, which: object) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "fixture.kicad_sch"
            schematic.write_text("(kicad_sch)", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "kicad-cli is not available"):
                KiCadCliSchematicRenderer(executable="missing-kicad-cli").render_svg(
                    root, schematic
                )


if __name__ == "__main__":
    unittest.main()
