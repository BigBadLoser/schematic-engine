"""Symbol libraries read once, and what is worked out from them remembered.

MCP Pro reads a whole symbol library (``Device.kicad_sym`` is more than a megabyte) and parses it again every time it is
asked for one symbol's bounds, pin positions or pin directions, about 15 ms each. Placing a cluster of parts beside a pin
asks about hundreds of candidate positions, so one ``sch_place_node_cluster`` took a minute on a netlist with a transistor
and a few parts around its base (KiCad's demo circuits, ``auto-demo1``) and the client gave up waiting after thirty
seconds. Nothing about a library changes during a run, so this keeps the text of each library file, keyed by its path and
modification time, and the answers derived from it, keyed by the file and the question.

``install`` patches the MCP Pro module in place and is safe to call twice. A library edited on disk is read again.
"""
from __future__ import annotations

import copy
import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

_TEXT: dict[tuple[str, int, str, str], str] = {}
_ANSWERS: dict[tuple, Any] = {}
_MISSING = object()
# More than this and the memory is not worth what is kept: start again.
_LIMIT = 200_000

# What MCP Pro answers from a library file alone, by name; the first argument of each is the library.
DERIVED = ("get_symbol_primitive_bounds", "get_pin_positions", "get_pin_outward_normals")


class CachedLibraryFile(type(Path())):  # type: ignore[misc]
    """A library path whose text is read once."""

    def read_text(self, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> str:
        try:
            stamp = self.stat().st_mtime_ns
        except OSError:
            return super().read_text(encoding=encoding, errors=errors)
        key = (str(self), stamp, encoding or "", errors or "")
        found = _TEXT.get(key)
        if found is None:
            found = super().read_text(encoding=encoding, errors=errors)
            if len(_TEXT) > 64:
                _TEXT.clear()
            _TEXT[key] = found
        return found


def _remembered(module: Any, name: str) -> None:
    original: Callable[..., Any] = getattr(module, name)
    if getattr(original, "_library_cached", False):
        return

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        library = args[0] if args else kwargs.get("library")
        source = module._symbol_library_file(library) if library else None
        try:
            stamp = (str(source), source.stat().st_mtime_ns) if source is not None else None
        except OSError:
            return original(*args, **kwargs)
        key = (name, stamp, args, tuple(sorted(kwargs.items())))
        found = _ANSWERS.get(key, _MISSING)
        if found is _MISSING:
            found = original(*args, **kwargs)
            if len(_ANSWERS) > _LIMIT:
                _ANSWERS.clear()
            _ANSWERS[key] = found
        return copy.deepcopy(found)

    wrapper._library_cached = True  # type: ignore[attr-defined]
    setattr(module, name, wrapper)


def install(module: Any) -> None:
    """Cache library reads and the answers derived from them in an MCP Pro schematic module."""
    lookup = module._symbol_library_file
    if not getattr(lookup, "_library_cached", False):

        @functools.wraps(lookup)
        def cached_lookup(library: str) -> Path | None:
            found = lookup(library)
            return None if found is None else CachedLibraryFile(found)

        cached_lookup._library_cached = True  # type: ignore[attr-defined]
        module._symbol_library_file = cached_lookup
    for name in DERIVED:
        if hasattr(module, name):
            _remembered(module, name)


def clear() -> None:
    """Forget everything (for tests)."""
    _TEXT.clear()
    _ANSWERS.clear()
