"""Opt-in timing of the schematic build: nested stages, aggregated MCP calls, one JSON dump.

Enabled by the environment variable ``KICAD_AI_TIMING=1``; otherwise every call is a near-free no-op and nothing is
recorded. Used from one process (a lock keeps the tables consistent if worker threads report MCP calls).
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_lock = threading.Lock()
_stack: list[tuple[str, float]] = []           # open stages: (full path, start)
_stages: dict[str, list[float]] = {}           # path -> [total seconds, calls, seconds of MCP calls made directly in it]
_tools: dict[str, list[float]] = {}            # tool name -> [total seconds, calls]
_started = time.perf_counter()


def enabled() -> bool:
    return os.environ.get("KICAD_AI_TIMING", "").strip() not in ("", "0")


def reset() -> None:
    global _started
    with _lock:
        _stack.clear()
        _stages.clear()
        _tools.clear()
        _started = time.perf_counter()


def current() -> str:
    """Full path of the innermost open stage ('' outside any, or when disabled)."""
    return _stack[-1][0] if _stack else ""


def _close(depth: int) -> None:
    """Close the stage at ``depth`` and any left open above it (an exception may have skipped their exit)."""
    now = time.perf_counter()
    with _lock:
        while len(_stack) > depth:
            path, start = _stack.pop()
            row = _stages.setdefault(path, [0.0, 0, 0.0])
            row[0] += now - start
            row[1] += 1


@contextmanager
def stage(name: str) -> Iterator[None]:
    if not enabled():
        yield
        return
    with _lock:
        depth = len(_stack)
        _stack.append((f"{_stack[-1][0]}/{name}" if _stack else name, time.perf_counter()))
    try:
        yield
    finally:
        _close(depth)


def count(name: str, seconds: float) -> None:
    """One aggregated event (an MCP tool call): added to the tool's row and to the innermost open stage."""
    if not enabled():
        return
    with _lock:
        row = _tools.setdefault(name, [0.0, 0])
        row[0] += seconds
        row[1] += 1
        if _stack:
            _stages.setdefault(_stack[-1][0], [0.0, 0, 0.0])[2] += seconds


def snapshot() -> dict:
    with _lock:
        return {
            "wall_seconds": time.perf_counter() - _started,
            "stages": {p: {"seconds": r[0], "calls": int(r[1]), "mcp_seconds": r[2]} for p, r in sorted(_stages.items())},
            "mcp_tools": {n: {"seconds": r[0], "calls": int(r[1])} for n, r in sorted(_tools.items())},
        }


def dump(path: str | Path) -> None:
    if not enabled():
        return
    _close(0)
    Path(path).write_text(json.dumps(snapshot(), indent=2, sort_keys=True), encoding="utf-8")
