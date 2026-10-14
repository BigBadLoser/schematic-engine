"""Paths, pins and tool discovery for the experiment sandbox."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SANDBOX_DIR.parents[2]
WORK_DIR = Path(os.environ.get("SBX_WORK_DIR") or SANDBOX_DIR / ".work").resolve()
VENV_DIR = WORK_DIR / "venv"
RUNS_DIR = WORK_DIR / "runs"
OUT_DIR = WORK_DIR / "out"
NODE_MODULES = WORK_DIR / "node_modules"
ELK_BUNDLE = WORK_DIR / "elk.bundled.js"


def load_pins() -> dict:
    return json.loads((SANDBOX_DIR / "pins.json").read_text(encoding="utf-8"))


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def run_capture(command: list[str], *, timeout: float = 20.0, cwd: Path | None = None) -> str | None:
    """Stdout of a short-lived command, or None if it cannot run or fails."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def interpreter_version(python: Path) -> tuple[int, ...] | None:
    out = run_capture([str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"])
    return parse_version(out) if out else None


def find_python(minimum: tuple[int, ...]) -> Path | None:
    """First interpreter at or above `minimum`: the current one, the Windows launcher, or PATH."""
    candidates = [Path(sys.executable)]
    if os.name == "nt":
        launched = run_capture(["py", f"-{minimum[0]}.{minimum[1]}", "-c", "import sys; print(sys.executable)"])
        if launched:
            candidates.append(Path(launched))
    for name in (f"python{minimum[0]}.{minimum[1]}", f"python{minimum[0]}", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        version = interpreter_version(candidate)
        if version and version >= minimum:
            return candidate
    return None


def package_version(python: Path, distribution: str) -> str | None:
    return run_capture([
        str(python), "-c",
        f"import importlib.metadata as m; print(m.version({distribution!r}))",
    ])


def _kicad_version_key(path: str) -> tuple[int, ...]:
    parts = Path(path).parts
    return parse_version(parts[-3]) if len(parts) >= 3 else (0,)


def find_kicad_cli() -> Path | None:
    explicit = os.environ.get("KICAD_AI_KICAD_CLI")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    found = shutil.which("kicad-cli")
    if found:
        return Path(found)
    if os.name == "nt":
        roots = {os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"}
        hits: list[str] = []
        for root in filter(None, roots):
            hits.extend(glob.glob(os.path.join(root, "KiCad", "*", "bin", "kicad-cli.exe")))
        if hits:
            return Path(max(hits, key=_kicad_version_key))
    return None


def find_symbol_dir(kicad_cli: Path | None) -> Path | None:
    explicit = os.environ.get("KICAD10_SYMBOL_DIR")
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    candidates = []
    if kicad_cli is not None:
        candidates.append(kicad_cli.resolve().parent.parent / "share" / "kicad" / "symbols")
    candidates.append(Path("/usr/share/kicad/symbols"))
    return next((path for path in candidates if path.is_dir()), None)


def git_state() -> dict[str, object]:
    head = run_capture(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
    dirty = run_capture(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT)
    return {"head": head, "dirty": bool(dirty) if dirty is not None else None}


def node_info() -> dict[str, str | None]:
    return {
        "node": run_capture(["node", "--version"]),
        "npm": run_capture([shutil.which("npm") or "npm", "--version"]),
    }
