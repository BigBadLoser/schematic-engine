"""Snapshots and the unified-diff applier (no network, no KiCad)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SANDBOX = Path(__file__).resolve().parents[1]
if str(SANDBOX) not in sys.path:
    sys.path.insert(0, str(SANDBOX))

from sbx import snapshot  # noqa: E402


def make_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class SnapshotTests(unittest.TestCase):
    def test_copies_engine_dirs_normalizes_crlf_and_skips_noise(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
            make_tree(Path(source), {
                "backend/a.py": b"x = 1\r\ny = 2\r\n",
                "scripts/b.py": b"print('b')\n",
                "scripts/ci/sandbox/own.py": b"skipped",
                "scripts/node_modules/dep.js": b"skipped",
                "backend/__pycache__/a.pyc": b"skipped",
                "tests/t.py": b"t\n",
                "docs/readme.md": b"not part of the engine tree",
                "blob.bin": b"\x00\x01\x02",
                "backend/data.bin": b"\x00\r\n\x01",
            })
            info = snapshot.take_snapshot(Path(dest), Path(source))
            out = Path(dest)
            self.assertEqual((out / "backend/a.py").read_bytes(), b"x = 1\ny = 2\n")
            self.assertEqual((out / "backend/data.bin").read_bytes(), b"\x00\r\n\x01")  # binary untouched
            for skipped in ("scripts/ci/sandbox/own.py", "scripts/node_modules/dep.js", "docs/readme.md", "blob.bin"):
                self.assertFalse((out / skipped).exists(), skipped)
            self.assertFalse((out / "backend/__pycache__").exists())
            self.assertEqual(info["files"], 4)

    def test_hash_ignores_line_ending_style_and_detects_content_change(self):
        hashes = []
        for data in (b"a\r\nb\r\n", b"a\nb\n", b"a\nc\n"):
            with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
                make_tree(Path(source), {"backend/x.py": data})
                hashes.append(snapshot.take_snapshot(Path(dest), Path(source))["tree_sha256"])
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[1], hashes[2])


PATCH = """\
# demo
--- a/backend/m.py
+++ b/backend/m.py
@@ -2,3 +2,4 @@
 two
+two and a half
 three
 four
@@ -8,2 +9,2 @@
-eight
+EIGHT
 nine
"""
ORIGINAL = "\n".join(["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", ""])


class PatchTests(unittest.TestCase):
    def apply(self, original: str, patch: str) -> str:
        with tempfile.TemporaryDirectory() as root:
            make_tree(Path(root), {"backend/m.py": original.encode()})
            snapshot.apply_patch(patch, Path(root))
            return (Path(root) / "backend/m.py").read_text(encoding="utf-8")

    def test_applies_multiple_hunks(self):
        result = self.apply(ORIGINAL, PATCH)
        self.assertIn("two and a half", result)
        self.assertIn("EIGHT", result)
        self.assertNotIn("\neight\n", result)
        self.assertTrue(result.endswith("nine\n"))

    def test_finds_hunks_when_line_numbers_have_drifted(self):
        drifted = "added1\nadded2\nadded3\n" + ORIGINAL
        result = self.apply(drifted, PATCH)
        self.assertIn("two and a half", result)
        self.assertIn("EIGHT", result)

    def test_stale_context_fails_loudly(self):
        with self.assertRaises(snapshot.PatchError):
            self.apply(ORIGINAL.replace("three", "THREE"), PATCH)

    def test_equidistant_matches_for_a_drifted_hunk_fail_loudly(self):
        # The hunk claims line 3, but its context sits at lines 1 and 5: a tie, so refuse to guess.
        patch = "--- a/backend/m.py\n+++ b/backend/m.py\n@@ -3,2 +3,2 @@\n x\n-y\n+z\n"
        with self.assertRaises(snapshot.PatchError):
            self.apply("x\ny\na\nb\nx\ny\n", patch)

    def test_exact_line_number_is_trusted_even_if_the_context_repeats(self):
        patch = "--- a/backend/m.py\n+++ b/backend/m.py\n@@ -5,2 +5,2 @@\n x\n-y\n+z\n"
        self.assertEqual(self.apply("x\ny\na\nb\nx\ny\n", patch), "x\ny\na\nb\nx\nz\n")

    def test_crlf_patch_text_is_accepted(self):
        result = self.apply(ORIGINAL, PATCH.replace("\n", "\r\n"))
        self.assertIn("EIGHT", result)

    def test_two_files_in_one_patch_are_both_applied(self):
        # The second file's "--- a/..." / "+++ b/..." lines directly follow the first file's last
        # hunk and look like a removal and an addition; the hunk header counts must stop the first
        # hunk before them.
        patch = (
            "--- a/backend/m.py\n+++ b/backend/m.py\n@@ -1,2 +1,2 @@\n-one\n+ONE\n two\n"
            "--- a/backend/n.py\n+++ b/backend/n.py\n@@ -1,2 +1,2 @@\n alpha\n-beta\n+BETA\n"
        )
        with tempfile.TemporaryDirectory() as root:
            make_tree(Path(root), {"backend/m.py": b"one\ntwo\n", "backend/n.py": b"alpha\nbeta\n"})
            patched = snapshot.apply_patch(patch, Path(root))
            self.assertEqual(sorted(patched), ["backend/m.py", "backend/n.py"])
            self.assertEqual((Path(root) / "backend/m.py").read_text(encoding="utf-8"), "ONE\ntwo\n")
            self.assertEqual((Path(root) / "backend/n.py").read_text(encoding="utf-8"), "alpha\nBETA\n")

    def test_hunk_shorter_than_its_header_is_rejected(self):
        patch = "--- a/backend/m.py\n+++ b/backend/m.py\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n"
        with self.assertRaises(snapshot.PatchError):
            self.apply("one\ntwo\nthree\n", patch)

    def test_no_newline_marker_is_not_content(self):
        patch = "--- a/backend/m.py\n+++ b/backend/m.py\n@@ -1,2 +1,2 @@\n one\n-two\n\\ No newline at end of file\n+TWO\n"
        (_path, hunks), = snapshot.parse_patch(patch)
        self.assertEqual(hunks, [(1, ["one", "two"], ["one", "TWO"])])


class ImportShadowingTests(unittest.TestCase):
    """An overlay line that uses `os` before the function's own `import os` compiles but crashes when run."""

    def test_use_before_the_functions_own_local_import_is_found(self):
        source = "def f():\n    value = os.getcwd()\n    import os\n    return value\n"
        self.assertEqual(list(snapshot.import_shadowing(source)), [("f", "os")])

    def test_use_after_the_import_or_with_a_module_level_import_is_fine(self):
        self.assertEqual(snapshot.import_shadowing("def f():\n    import os\n    return os.getcwd()\n"), {})
        self.assertEqual(snapshot.import_shadowing("import os\n\n\ndef f():\n    return os.getcwd()\n"), {})

    def test_from_imports_count_too(self):
        source = "def f():\n    value = Path('.')\n    from pathlib import Path\n    return value\n"
        self.assertEqual(list(snapshot.import_shadowing(source)), [("f", "Path")])

    def test_only_problems_the_overlay_adds_are_reported(self):
        before = "def f():\n    a = os.sep\n    import os\n"
        after = "def f():\n    a = os.sep\n    b = os.sep\n    import os\n"
        self.assertEqual(snapshot.new_import_shadowing(before, after), [])   # f() was already like that
        problems = snapshot.new_import_shadowing(before, after + "\n\ndef g():\n    c = os.sep\n    import os\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("g()", problems[0])
        self.assertIn("UnboundLocalError", problems[0])

    def test_an_overlay_that_introduces_one_is_rejected_when_applied(self):
        patch = (
            "# demo\n--- a/backend/m.py\n+++ b/backend/m.py\n@@ -1,3 +1,4 @@\n def f():\n"
            "+    first = os.getcwd()\n     import os\n     return os.sep\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            overlays, root = Path(directory) / "overlays", Path(directory) / "tree"
            overlays.mkdir()
            (overlays / "bad.patch").write_text(patch, encoding="utf-8")
            make_tree(root, {"backend/m.py": b"def f():\n    import os\n    return os.sep\n"})
            with mock.patch.object(snapshot, "OVERLAY_DIR", overlays):
                with self.assertRaises(snapshot.PatchError) as raised:
                    snapshot.apply_overlays(["bad"], root)
        self.assertIn("UnboundLocalError", str(raised.exception))


class UndefinedGlobalTests(unittest.TestCase):
    """An overlay helper that uses a name the engine only imports inside another function compiles but fails."""

    def test_a_name_the_module_never_defines_is_found(self):
        self.assertEqual(list(snapshot.undefined_globals("def f():\n    return missing(1)\n")), [("f", "missing")])

    def test_module_names_builtins_and_later_definitions_are_fine(self):
        source = "import os\n\n\ndef f():\n    return g(len(os.sep))\n\n\ndef g(x):\n    return x\n"
        self.assertEqual(snapshot.undefined_globals(source), {})

    def test_a_name_imported_only_inside_another_function_is_undefined_elsewhere(self):
        source = "def a():\n    from os.path import join\n    return join('x')\n\n\ndef b():\n    return join('y')\n"
        self.assertEqual(list(snapshot.undefined_globals(source)), [("b", "join")])

    def test_global_declarations_and_closures_are_understood(self):
        source = (
            "def setter():\n    global shared\n    shared = 1\n\n\ndef reader():\n    return shared\n\n\n"
            "def outer():\n    value = 1\n\n    def inner():\n        return value\n    return inner\n"
        )
        self.assertEqual(snapshot.undefined_globals(source), {})

    def test_star_imports_disable_the_check_and_only_new_findings_are_reported(self):
        self.assertEqual(snapshot.undefined_globals("from os import *\n\n\ndef f():\n    return nope\n"), {})
        before = "def f():\n    return missing_a\n"
        problems = snapshot.new_undefined_globals(before, before + "\n\ndef g():\n    return missing_b\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("missing_b", problems[0])
        self.assertIn("NameError", problems[0])

    def test_an_overlay_that_introduces_one_is_rejected_when_applied(self):
        patch = (
            "# demo\n--- a/backend/m.py\n+++ b/backend/m.py\n@@ -1,2 +1,6 @@\n def f():\n     return 1\n"
            "+\n+\n+def helper():\n+    return not_defined_anywhere()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            overlays, root = Path(directory) / "overlays", Path(directory) / "tree"
            overlays.mkdir()
            (overlays / "bad.patch").write_text(patch, encoding="utf-8")
            make_tree(root, {"backend/m.py": b"def f():\n    return 1\n"})
            with mock.patch.object(snapshot, "OVERLAY_DIR", overlays):
                with self.assertRaises(snapshot.PatchError) as raised:
                    snapshot.apply_overlays(["bad"], root)
        self.assertIn("NameError", str(raised.exception))


class ShippedOverlayTests(unittest.TestCase):
    def test_every_overlay_applies_to_the_current_tree_and_they_compose(self):
        names = list(snapshot.list_overlays())
        self.assertGreaterEqual(len(names), 3)
        for combination in ([name] for name in names):
            snapshot.check_overlays(combination)
        snapshot.check_overlays(names)

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_applier_matches_git_apply_modulo_line_endings(self):
        names = list(snapshot.list_overlays())
        touched = sorted({
            path
            for name in names
            for path, _hunks in snapshot.parse_patch(
                (snapshot.OVERLAY_DIR / f"{name}.patch").read_text(encoding="utf-8").replace("\r\n", "\n")
            )
        })
        self.assertIn("backend/kicad/mcp_extended_server.py", touched)
        with tempfile.TemporaryDirectory() as ours, tempfile.TemporaryDirectory() as theirs:
            snapshot.take_snapshot(Path(ours))
            snapshot.take_snapshot(Path(theirs))
            snapshot.apply_overlays(names, Path(ours))
            for name in names:
                patch = (snapshot.OVERLAY_DIR / f"{name}.patch").read_bytes().replace(b"\r\n", b"\n")
                done = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-p1", "-"], input=patch, cwd=theirs,
                    capture_output=True, check=False,
                )
                self.assertEqual(done.returncode, 0, done.stderr.decode())
            for relative in touched:
                self.assertEqual(
                    (Path(ours) / relative).read_bytes(),
                    (Path(theirs) / relative).read_bytes().replace(b"\r\n", b"\n"),
                    relative,
                )


if __name__ == "__main__":
    unittest.main()
