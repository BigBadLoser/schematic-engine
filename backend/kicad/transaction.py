from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Callable, TypeVar

from backend.kicad.revisions import hash_document


_TRANSACTION_LOCK = RLock()
_T = TypeVar("_T")


def _atomic_restore(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".rollback",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SchematicFileTransaction:
    """Exact-file snapshot/rollback around a higher-level schematic workflow.

    MCP Pro owns placement, routing, field autoplacement, readability repair, and
    connectivity-aware cosmetic fixes. This wrapper remains companion-owned so a
    multi-tool workflow can still be restored atomically if any later step fails.
    """

    def __init__(self, root: Path, schematic_file: Path) -> None:
        self.root = root.resolve()
        self.schematic_file = schematic_file.resolve()
        if not self.schematic_file.is_file():
            raise ValueError(f"Schematic file does not exist: {self.schematic_file}")
        try:
            self.schematic_file.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Schematic file must be inside the selected project") from error

    def run(self, operation: Callable[[], _T]) -> tuple[_T, str, str]:
        with _TRANSACTION_LOCK:
            before_bytes = self.schematic_file.read_bytes()
            before = hash_document(self.schematic_file, self.root)
            try:
                result = operation()
                after = hash_document(self.schematic_file, self.root)
                if after.sha256 == before.sha256:
                    raise RuntimeError(
                        "Schematic workflow completed without changing the document"
                    )
                return result, before.sha256, after.sha256
            except Exception:
                _atomic_restore(self.schematic_file, before_bytes)
                restored = hash_document(self.schematic_file, self.root)
                if restored.sha256 != before.sha256:
                    raise RuntimeError(
                        "Schematic workflow failed and rollback verification also failed"
                    )
                raise
