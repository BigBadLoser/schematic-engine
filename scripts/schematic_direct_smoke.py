"""Direct-build smoke runner: one fixture built once at a given placement (``backend.schematic.direct_build``).

Usage: schematic_direct_smoke.py ARTIFACT_DIR PLACEMENT_JSON, with KICAD_AI_SCHEMATIC_FIXTURE naming the fixture.
PLACEMENT_JSON is ``{reference: {"position": [x, y], "rotation": 0, "mirror": ""}}``. Leaves the artifacts the sandbox reads
(``mcu_smoke.kicad_sch``, the render, ``report.json``, ``timing.json``, ``direct-build.json``).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.kicad.mcp_pro import McpProReadOnlyAdapter
from backend.kicad.render import KiCadCliSchematicRenderer
from backend.schematic import timing
from backend.schematic.composition_metrics import composition_metrics
from backend.schematic.direct_build import build_direct
from backend.schematic.direct_workflow import DirectWorkflow
from backend.schematic.kicad_geometry import measured_scene_from_kicad
from backend.schematic.orientation import Orientation
from backend.schematic.placement_refinement import Placement
from scripts.schematic_validation_fixtures import build_fixture


def load_placement(path: Path) -> dict[str, Placement]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        reference: Placement(
            (float(item["position"][0]), float(item["position"][1])),
            Orientation(int(item.get("rotation", 0)), str(item.get("mirror", ""))),
        )
        for reference, item in data.items()
    }


def prepare_libraries(libraries: Path, project: Path, intent):
    """A fixture whose parts are not all in KiCad's libraries brings a project of its own (``SBX_DIRECT_LIBS``: a folder with
    a ``sym-lib-table`` and ``library-nicknames.json``). The table goes into the build's project folder, which is where the
    server looks for libraries; the intent's symbols are placed under the nickname the table gives their library."""
    shutil.copy2(libraries / "sym-lib-table", project / "sym-lib-table")
    names = libraries / "library-nicknames.json"
    renamed = json.loads(names.read_text(encoding="utf-8")) if names.is_file() else {}
    symbols = tuple(replace(s, library=renamed.get(s.library, s.library)) for s in intent.symbols)
    return replace(intent, symbols=symbols)


def main() -> int:
    artifact_root = Path(sys.argv[1]).resolve()
    placement = load_placement(Path(sys.argv[2]))
    artifact_root.mkdir(parents=True, exist_ok=True)
    fixture = REPO_ROOT / "tests" / "fixtures" / "schematic_engine" / "engine_smoke.kicad_sch"
    fixture_name = os.getenv("KICAD_AI_SCHEMATIC_FIXTURE", "").strip()
    intent = build_fixture(fixture_name)
    with tempfile.TemporaryDirectory(prefix="kicad-ai-direct-") as directory:
        project = Path(directory)
        schematic = project / "mcu_smoke.kicad_sch"
        if os.getenv("SBX_DIRECT_LIBS"):
            intent = prepare_libraries(Path(os.environ["SBX_DIRECT_LIBS"]), project, intent)
        shutil.copy2(fixture, schematic)
        adapter = McpProReadOnlyAdapter()
        workflow = DirectWorkflow(adapter.client, adapter, artifact_root=artifact_root)
        try:
            built = build_direct(workflow, project, schematic, intent, placement)
        except Exception as exc:
            (artifact_root / "failure.json").write_text(json.dumps({"error": str(exc)}, indent=2), encoding="utf-8")
            raise
        result = built["result"]
        with timing.stage("render"):
            rendered = KiCadCliSchematicRenderer().render_svg(project, schematic, output_dir=project / "render")
        timing.dump(artifact_root / "timing.json")
        shutil.copy2(schematic, artifact_root / "mcu_smoke.kicad_sch")
        for path in rendered.svg_paths:
            shutil.copy2(path, artifact_root / Path(path).name)
        scene, geometry_notes = measured_scene_from_kicad(schematic)
        snapshot = scene.to_dict(include_conflicts=False)
        snapshot["coverage"] = geometry_notes
        (artifact_root / "presentation-scene.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        report = {
            "fixture": fixture_name,
            "mode": "direct",
            "presentation_geometry": geometry_notes,
            "composition_metrics": composition_metrics(scene),
            "workflow": result.to_dict(),
            "render": {"svg_paths": [Path(p).name for p in rendered.svg_paths], "command": list(rendered.command)},
        }
        (artifact_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"references={','.join(result.references)}")
        print(f"cosmetic_score={result.cosmetic_score.get('cosmetic_score')}")
        print(f"visual_qa={result.visual_qa.get('status')}")
        print(f"rendered={len(rendered.svg_paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
