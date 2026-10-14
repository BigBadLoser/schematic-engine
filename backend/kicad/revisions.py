from __future__ import annotations

import hashlib
from pathlib import Path

from .models import DocumentRevision


TRACKED_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")


def hash_document(path: Path, root: Path) -> DocumentRevision:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return DocumentRevision(
        path=path.relative_to(root).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=path.stat().st_size,
    )


def project_revisions(root: Path) -> tuple[DocumentRevision, ...]:
    """Hash all KiCad project documents, including hierarchical sheets in subfolders."""
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in TRACKED_SUFFIXES
            and ".kicad-ai" not in path.parts
        ),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    return tuple(hash_document(path, root) for path in files)
