"""One-time environment setup. Nothing here touches the network until execute() runs."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import env


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    network: bool
    detail: str


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def plan(pins: dict, python: Path) -> list[Step]:
    specs = [f"{name}=={version}" for name, version in pins["pip"].items()]
    bundle = pins["elkjs_bundle"]
    sizes = pins["approx_download_mb"]
    return [
        Step("venv", "create virtual environment", False, f"{python} -m venv {env.VENV_DIR}"),
        Step(
            "pip", "install Python packages", True,
            f"pip install --only-binary=:all: {' '.join(specs)} from PyPI; wheels only, so no "
            f"setup.py runs. About {sizes['pip']} MB ({sizes['pip_note']})",
        ),
        Step(
            "npm", "install Node packages", True,
            f"npm {'ci' if (env.SANDBOX_DIR / 'package-lock.json').is_file() else 'install'} "
            f"--ignore-scripts into {env.NODE_MODULES} from the npm registry (~{sizes['npm']} MB; "
            f"see package.json for exact versions)",
        ),
        Step(
            "elk", "fetch the pinned elkjs bundle", True,
            f"{bundle['url']} ({int(bundle['bytes']) / 1e6:.1f} MB), refused unless its git blob "
            f"sha1 is {bundle['git_blob_sha1']}",
        ),
        Step("freeze", "record installed versions", False, f"pip freeze -> {env.WORK_DIR / 'pip-freeze.txt'}"),
    ]


def describe(steps: list[Step]) -> str:
    lines = []
    for number, step in enumerate(steps, start=1):
        tag = "NETWORK" if step.network else "local  "
        lines.append(f"  {number}. [{tag}] {step.title}\n       {step.detail}")
    return "\n".join(lines)


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise BootstrapError(f"command failed ({completed.returncode}): {' '.join(command)}")


def create_venv(python: Path) -> None:
    """Create the sandbox virtual environment (idempotent; no network)."""
    if env.venv_python().is_file():
        print("  virtual environment already exists")
        return
    _run([str(python), "-m", "venv", str(env.VENV_DIR)])


def _install_node(pins: dict) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise BootstrapError("npm was not found on PATH")
    env.WORK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(env.SANDBOX_DIR / "package.json", env.WORK_DIR / "package.json")
    lock = env.SANDBOX_DIR / "package-lock.json"
    if lock.is_file():
        shutil.copy2(lock, env.WORK_DIR / "package-lock.json")
    verb = "ci" if lock.is_file() else "install"
    _run([npm, verb, "--ignore-scripts", "--no-audit", "--no-fund"], cwd=env.WORK_DIR)
    if not lock.is_file() and (env.WORK_DIR / "package-lock.json").is_file():
        shutil.copy2(env.WORK_DIR / "package-lock.json", lock)
        print(f"  wrote {lock} - commit it to make the Node install reproducible")


def _fetch_bundle(pins: dict) -> None:
    bundle = pins["elkjs_bundle"]
    if env.ELK_BUNDLE.is_file() and git_blob_sha1(env.ELK_BUNDLE.read_bytes()) == bundle["git_blob_sha1"]:
        print("  elk bundle already present and verified")
        return
    with urllib.request.urlopen(bundle["url"], timeout=120) as response:
        data = response.read()
    digest = git_blob_sha1(data)
    if digest != bundle["git_blob_sha1"]:
        raise BootstrapError(f"elkjs bundle hash mismatch: got {digest}, expected {bundle['git_blob_sha1']}")
    env.WORK_DIR.mkdir(parents=True, exist_ok=True)
    env.ELK_BUNDLE.write_bytes(data)
    print(f"  wrote {env.ELK_BUNDLE} ({len(data)} bytes, sha1 verified)")


def execute(pins: dict, python: Path) -> None:
    steps = plan(pins, python)
    specs = [f"{name}=={version}" for name, version in pins["pip"].items()]
    venv_python = env.venv_python()
    for number, step in enumerate(steps, start=1):
        print(f"[{number}/{len(steps)}] {step.title}")
        if step.key == "venv":
            create_venv(python)
        elif step.key == "pip":
            _run([
                str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
                "--only-binary=:all:", *specs,
            ])
        elif step.key == "npm":
            _install_node(pins)
        elif step.key == "elk":
            _fetch_bundle(pins)
        elif step.key == "freeze":
            frozen = subprocess.run(
                [str(venv_python), "-m", "pip", "freeze"], capture_output=True, text=True, check=False,
            )
            (env.WORK_DIR / "pip-freeze.txt").write_text(frozen.stdout, encoding="utf-8")
