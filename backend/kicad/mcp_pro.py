from __future__ import annotations

from pathlib import Path
from typing import Any

from .capabilities import current_capabilities
from .mcp_client import McpHttpClient, make_client
from .models import ProjectMetadata
from .project_graph import build_project_graph
from .revisions import project_revisions
from .service import KiCadService


def _single_or_ambiguous(files: list[Path], kind: str) -> Path | None:
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    names = ", ".join(path.name for path in files)
    raise ValueError(f"Ambiguous KiCad {kind}: multiple candidates found ({names})")


def _select_primary_document(root: Path, project_file: Path | None, suffix: str, kind: str) -> Path | None:
    files = sorted(root.glob(f"*{suffix}"), key=lambda path: path.name.casefold())
    if not files:
        return None

    # KiCad's normal top-level convention is <project-name>.kicad_sch/.kicad_pcb.
    # Prefer that exact stem instead of alphabetically selecting a child sheet.
    if project_file is not None:
        preferred = root / f"{project_file.stem}{suffix}"
        if preferred in files:
            return preferred

    return _single_or_ambiguous(files, kind)


class McpProReadOnlyAdapter(KiCadService):
    """Read-only KiCad adapter. Semantic/agent orchestration lives above this boundary."""

    def __init__(self, client: McpHttpClient | None = None) -> None:
        self.client = client or make_client()

    def inspect_project(self, root: Path) -> ProjectMetadata:
        root = root.resolve()
        project_file = _single_or_ambiguous(
            sorted(root.glob("*.kicad_pro"), key=lambda path: path.name.casefold()),
            "project file",
        )
        schematic_file = _select_primary_document(
            root, project_file, ".kicad_sch", "root schematic"
        )
        board_file = _select_primary_document(
            root, project_file, ".kicad_pcb", "board"
        )
        return ProjectMetadata(
            root=str(root),
            name=project_file.stem if project_file else root.name,
            project_file=project_file.name if project_file else None,
            schematic_file=schematic_file.name if schematic_file else None,
            board_file=board_file.name if board_file else None,
            revisions=project_revisions(root),
        )

    def read_project(self, root: Path) -> dict[str, Any]:
        root = root.resolve()
        metadata = self.inspect_project(root)
        if not metadata.project_file and not metadata.schematic_file and not metadata.board_file:
            raise ValueError("No KiCad project, schematic, or board files were found in this directory")

        read = self.client.read_project(
            metadata.root,
            pcb_file=str(root / metadata.board_file) if metadata.board_file else None,
            sch_file=str(root / metadata.schematic_file) if metadata.schematic_file else None,
        )
        read["project_graph"] = build_project_graph(read)
        read["available_documents"] = {
            "project": metadata.project_file is not None,
            "schematic": metadata.schematic_file is not None,
            "board": metadata.board_file is not None,
        }
        return read

    def capability_registry(self) -> list[dict[str, object]]:
        try:
            self.client.probe()
        except RuntimeError:
            pass
        return current_capabilities(self.client.runtime_state())
