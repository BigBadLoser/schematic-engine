"""Shared helpers for the sandbox probes. Each probe prints one JSON object on stdout."""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[2]
for path in (SANDBOX, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def mcp_version() -> str | None:
    try:
        return importlib.metadata.version("kicad-mcp-pro")
    except importlib.metadata.PackageNotFoundError:
        return None


def emit(name: str, summary: list[str], data: object, *, ok: bool = True) -> int:
    print(json.dumps({
        "name": name,
        "ok": ok,
        "kicad_mcp_version": mcp_version(),
        "summary": summary,
        "data": data,
    }))
    return 0 if ok else 1


def fail(name: str, message: str) -> int:
    return emit(name, [message], {"error": message}, ok=False)
