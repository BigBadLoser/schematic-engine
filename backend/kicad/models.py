from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DocumentRevision:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ProjectMetadata:
    root: str
    name: str
    project_file: str | None
    schematic_file: str | None
    board_file: str | None
    revisions: tuple[DocumentRevision, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "revisions": [asdict(item) for item in self.revisions],
        }

