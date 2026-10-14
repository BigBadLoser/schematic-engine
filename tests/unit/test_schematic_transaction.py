import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.kicad.transaction import SchematicFileTransaction


class SchematicTransactionTests(unittest.TestCase):
    def test_successful_workflow_changes_document(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "fixture.kicad_sch"
            schematic.write_bytes(b"ORIGINAL")

            def operation() -> str:
                schematic.write_bytes(b"UPDATED")
                return "ok"

            result, before_sha, after_sha = SchematicFileTransaction(
                root, schematic
            ).run(operation)

            self.assertEqual(result, "ok")
            self.assertNotEqual(before_sha, after_sha)
            self.assertEqual(schematic.read_bytes(), b"UPDATED")

    def test_failure_restores_exact_original_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "fixture.kicad_sch"
            original = b"ORIGINAL-BYTES"
            schematic.write_bytes(original)

            def operation() -> None:
                schematic.write_bytes(b"PARTIAL-MUTATION")
                raise RuntimeError("synthetic workflow failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic workflow failure"):
                SchematicFileTransaction(root, schematic).run(operation)

            self.assertEqual(schematic.read_bytes(), original)

    def test_unchanged_workflow_is_rejected_and_rolled_back(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            schematic = root / "fixture.kicad_sch"
            original = b"ORIGINAL"
            schematic.write_bytes(original)

            with self.assertRaisesRegex(RuntimeError, "without changing"):
                SchematicFileTransaction(root, schematic).run(lambda: "noop")

            self.assertEqual(schematic.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
