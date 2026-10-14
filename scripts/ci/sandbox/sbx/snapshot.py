"""Source snapshots: a normalized copy of the engine tree that experiments may patch freely.

The repository is never modified. Text files are normalized to LF so patches
apply identically on Windows checkouts (autocrlf) and in the Linux CI image.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import os
import re
import symtable
import tempfile
from pathlib import Path

from .env import REPO_ROOT, SANDBOX_DIR, WORK_DIR

INCLUDE = ("backend", "scripts", "tests")
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", ".work", ".git"}
SKIP_PATHS = ("scripts/ci/sandbox",)
OVERLAY_DIR = SANDBOX_DIR / "overlays"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")


class PatchError(RuntimeError):
    pass


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def take_snapshot(dest: Path, source: Path = REPO_ROOT) -> dict[str, object]:
    """Copy the engine tree into `dest`; return file count and a content hash."""
    digest = hashlib.sha256()
    files = 0
    for top in INCLUDE:
        base = source / top
        if not base.is_dir():
            continue
        for current, dirnames, filenames in os.walk(base):
            relative = Path(current).relative_to(source).as_posix()
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in SKIP_DIRS
                and f"{relative}/{name}" not in SKIP_PATHS
            )
            for name in sorted(filenames):
                path = Path(current) / name
                data = path.read_bytes()
                if _is_text(data):
                    data = data.replace(b"\r\n", b"\n")
                target = dest / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                digest.update(path.relative_to(source).as_posix().encode() + b"\0" + data)
                files += 1
    return {"files": files, "tree_sha256": digest.hexdigest()}


def list_overlays() -> dict[str, str]:
    """Overlay name -> description (the comment preamble of its patch file)."""
    result: dict[str, str] = {}
    for path in sorted(OVERLAY_DIR.glob("*.patch")):
        preamble = []
        for line in path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"):
            if line.startswith("--- "):
                break
            if line.startswith("#"):
                preamble.append(line.lstrip("# ").rstrip())
        result[path.stem] = " ".join(part for part in preamble if part)
    return result


def parse_patch(text: str) -> list[tuple[str, list[tuple[int, list[str], list[str]]]]]:
    """Parse a unified diff into (path, [(old_start, old_lines, new_lines)])."""
    lines = text.split("\n")
    files: list[tuple[str, list[tuple[int, list[str], list[str]]]]] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            path = lines[index + 1][4:].split("\t")[0].strip()
            path = path[2:] if path.startswith("b/") else path
            hunks: list[tuple[int, list[str], list[str]]] = []
            index += 2
            while index < len(lines) and lines[index].startswith("@@"):
                match = _HUNK.match(lines[index])
                if match is None:
                    raise PatchError(f"malformed hunk header: {lines[index]!r}")
                start = int(match.group(1))
                # The header counts bound the body: a following file's "--- a/..." and
                # "+++ b/..." lines look like removals and additions and must not be eaten.
                old_total = int(match.group(2)) if match.group(2) is not None else 1
                new_total = int(match.group(3)) if match.group(3) is not None else 1
                index += 1
                old: list[str] = []
                new: list[str] = []
                while index < len(lines) and (len(old) < old_total or len(new) < new_total):
                    marker, body = lines[index][:1], lines[index][1:]
                    if marker == "\\":
                        index += 1
                        continue
                    if marker == "":
                        marker = " "  # editors strip the single space of an empty context line
                    if marker not in (" ", "-", "+"):
                        break
                    if marker in (" ", "-"):
                        old.append(body)
                    if marker in (" ", "+"):
                        new.append(body)
                    index += 1
                if len(old) != old_total or len(new) != new_total:
                    raise PatchError(f"hunk at line {start} is shorter than its header says")
                hunks.append((start, old, new))
            files.append((path, hunks))
        else:
            index += 1
    if not files:
        raise PatchError("no file sections found in patch")
    return files


def _locate(lines: list[str], old: list[str], expected: int) -> int:
    if lines[expected:expected + len(old)] == old:
        return expected
    matches = [
        i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old
    ]
    if not matches:
        raise PatchError("hunk context not found (the engine source changed since the patch was written)")
    nearest = min(matches, key=lambda i: abs(i - expected))
    if len(matches) > 1 and sum(abs(i - expected) == abs(nearest - expected) for i in matches) > 1:
        raise PatchError("hunk context is ambiguous")
    return nearest


def apply_patch(text: str, root: Path) -> list[str]:
    """Apply a unified diff to files under `root`; return the patched paths."""
    patched: list[str] = []
    for path, hunks in parse_patch(text.replace("\r\n", "\n")):
        target = root / path
        if not target.is_file():
            raise PatchError(f"{path}: not present in the snapshot")
        lines = target.read_text(encoding="utf-8").split("\n")
        shift = 0
        for start, old, new in hunks:
            index = _locate(lines, old, start - 1 + shift)
            lines[index:index + len(old)] = new
            shift = (index - (start - 1)) + (len(new) - len(old))
        target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        patched.append(path)
    return patched


def import_shadowing(source: str) -> dict[tuple[str, str], str]:
    """Names a function uses before its own local import of the same name.

    A local `import os` makes `os` a local variable of the whole function, so an earlier `os.environ`
    raises UnboundLocalError at run time. compile() cannot see it; an overlay that adds such a use to an
    engine function that later imports the name looks fine until the code runs.
    Returns {(function name, imported name): where}.
    """
    found: dict[tuple[str, str], str] = {}
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        imports: dict[str, int] = {}
        uses: list[tuple[str, int]] = []
        stack = list(function.body)
        while stack:                       # this function's own scope only
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    imports[bound] = min(imports.get(bound, node.lineno), node.lineno)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    imports[bound] = min(imports.get(bound, node.lineno), node.lineno)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                uses.append((node.id, node.lineno))
            stack.extend(ast.iter_child_nodes(node))
        for used, line in uses:
            if used in imports and line < imports[used]:
                found[(function.name, used)] = f"at line {line}, before its local import at line {imports[used]}"
    return found


def new_import_shadowing(before: str, after: str) -> list[str]:
    """Shadowing problems present in `after` that `before` did not have."""
    old = import_shadowing(before)
    return [
        f"{function}() uses `{name}` {where}; that raises UnboundLocalError when the function runs"
        for (function, name), where in sorted(import_shadowing(after).items())
        if (function, name) not in old
    ]


_MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__", "__builtins__",
                   "__annotations__", "__path__", "__class__"}


def undefined_globals(source: str) -> dict[tuple[str, str], str]:
    """Names that a scope reads as globals although the module never defines them.

    Another mistake compile() cannot see: an overlay helper that uses a name the engine only imports
    inside some other function. It surfaces as NameError when the code runs.
    Returns {(scope name, name): scope kind}. Files with `import *` are skipped.
    """
    tree = ast.parse(source)
    if any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
           for node in ast.walk(tree)):
        return {}
    module = symtable.symtable(source, "<overlay-check>", "exec")
    defined: set[str] = set(dir(builtins)) | _MODULE_DUNDERS
    defined |= {
        symbol.get_name() for symbol in module.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    scopes = []
    pending = [module]
    while pending:
        table = pending.pop()
        scopes.append(table)
        pending.extend(table.get_children())
    for table in scopes:                       # `global x` plus an assignment defines x for the module
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                defined.add(symbol.get_name())
    found: dict[tuple[str, str], str] = {}
    for table in scopes:
        for symbol in table.get_symbols():
            name = symbol.get_name()
            is_free_global = symbol.is_global() if table.get_type() != "module" else (
                symbol.is_referenced() and not (symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace())
            )
            if symbol.is_referenced() and is_free_global and name not in defined:
                found[(table.get_name(), name)] = table.get_type()
    return found


def new_undefined_globals(before: str, after: str) -> list[str]:
    """Undefined-name problems present in `after` that `before` did not have."""
    old = undefined_globals(before)
    return [
        f"{scope}() reads `{name}`, which the module never defines; that raises NameError when it runs"
        for (scope, name), _kind in sorted(undefined_globals(after).items())
        if (scope, name) not in old
    ]


def apply_overlays(names: list[str], root: Path) -> list[dict[str, object]]:
    """Apply named overlays in order; syntax-check and import-shadow-check every patched file."""
    applied: list[dict[str, object]] = []
    available = list_overlays()
    for name in names:
        if name not in available:
            raise PatchError(f"unknown overlay {name!r}; available: {', '.join(available) or 'none'}")
        text = (OVERLAY_DIR / f"{name}.patch").read_text(encoding="utf-8").replace("\r\n", "\n")
        before = {
            path: (root / path).read_text(encoding="utf-8")
            for path, _hunks in parse_patch(text)
            if path.endswith(".py") and (root / path).is_file()
        }
        try:
            files = apply_patch(text, root)
        except PatchError as error:
            raise PatchError(f"overlay {name}: {error}") from error
        for relative in files:
            if relative.endswith(".py"):
                source = (root / relative).read_text(encoding="utf-8")
                try:
                    compile(source, relative, "exec", dont_inherit=True)
                except SyntaxError as error:
                    raise PatchError(f"overlay {name}: {relative} no longer compiles: {error}") from error
                problems = new_import_shadowing(before.get(relative, ""), source) + new_undefined_globals(
                    before.get(relative, ""), source)
                if problems:
                    raise PatchError(f"overlay {name}: {relative}: " + "; ".join(problems))
        applied.append({
            "name": name,
            "files": files,
            "patch_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return applied


def check_overlays(names: list[str]) -> list[dict[str, object]]:
    """Dry run: snapshot into a temporary directory, apply, compile, discard."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="snapshot-check-", dir=WORK_DIR) as scratch:
        take_snapshot(Path(scratch))
        return apply_overlays(names, Path(scratch))
