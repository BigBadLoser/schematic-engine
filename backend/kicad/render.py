from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SchematicRenderResult:
    output_dir: str
    svg_paths: tuple[str, ...]
    svg_documents: tuple[str, ...]
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["svg_paths"] = list(self.svg_paths)
        payload["svg_documents"] = list(self.svg_documents)
        payload["command"] = list(self.command)
        return payload


class KiCadCliSchematicRenderer:
    """Headless schematic SVG rendering through KiCad's supported CLI."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = (
            executable
            or os.environ.get("KICAD_AI_KICAD_CLI")
            or shutil.which("kicad-cli")
            or "kicad-cli"
        )

    def available(self) -> bool:
        if Path(self.executable).is_file():
            return True
        return shutil.which(self.executable) is not None

    def render_svg(
        self,
        root: Path,
        schematic_file: Path,
        *,
        output_dir: Path | None = None,
        pages: str | None = None,
        theme: str | None = None,
        exclude_drawing_sheet: bool = True,
        no_background_color: bool = False,
    ) -> SchematicRenderResult:
        root = root.resolve()
        schematic_file = schematic_file.resolve()
        if not schematic_file.is_file():
            raise ValueError(f"Schematic file does not exist: {schematic_file}")
        try:
            schematic_file.relative_to(root)
        except ValueError as error:
            raise ValueError("Schematic file must be inside the selected project") from error
        if not self.available():
            raise RuntimeError(
                "kicad-cli is not available. Install KiCad or set KICAD_AI_KICAD_CLI."
            )

        destination = (
            output_dir.resolve()
            if output_dir is not None
            else (root / ".kicad-ai" / "renders" / schematic_file.stem).resolve()
        )
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise ValueError("Render output directory must be inside the project") from error
        destination.mkdir(parents=True, exist_ok=True)

        command = [
            self.executable,
            "sch",
            "export",
            "svg",
        ]
        if pages:
            command.extend(["--pages", pages])
        if theme:
            command.extend(["--theme", theme])
        if exclude_drawing_sheet:
            command.append("--exclude-drawing-sheet")
        if no_background_color:
            command.append("--no-background-color")
        command.extend(["--output", str(destination), str(schematic_file)])

        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise RuntimeError(f"KiCad schematic SVG export failed: {detail}")

        svg_paths = tuple(
            str(path)
            for path in sorted(destination.glob("*.svg"), key=lambda item: item.name.casefold())
        )
        if not svg_paths:
            raise RuntimeError(
                f"KiCad reported a successful schematic export but no SVG appeared in {destination}"
            )
        return SchematicRenderResult(
            output_dir=str(destination),
            svg_paths=svg_paths,
            svg_documents=tuple(
                Path(path).read_text(encoding="utf-8", errors="strict")
                for path in svg_paths
            ),
            command=tuple(command),
        )
