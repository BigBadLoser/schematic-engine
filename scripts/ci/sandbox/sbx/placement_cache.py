"""A disk cache of the solver's answers, so a build whose plan and solver have not changed does not solve again.

The key is a hash of everything the answer depends on: the plan, the symbols (geometry, orientations, positions), the nets,
the gap and pitch, the hint, the seed list, the deterministic budget and the source text of the solver, the plan module
and the placement model. A change to any of them is a miss; ``KICAD_AI_LADDER_CACHE=0`` turns the cache off. The entry
holds the placements, the wires the solver drew (``Placements.wires``), the status and the solver's own statistics, as JSON
that reads back to equal objects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import env

CACHE_ENV = "KICAD_AI_LADDER_CACHE"
SOURCES = ("backend/schematic/ladder_solve.py", "backend/schematic/ladder_plan.py", "backend/schematic/placement_refinement.py")


def enabled() -> bool:
    return os.environ.get(CACHE_ENV, "1") != "0"


def directory() -> Path:
    return env.WORK_DIR / "cache" / "placements"


def source_digest() -> str:
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update((env.REPO_ROOT / name).read_bytes())
    return digest.hexdigest()


def key(plan: Any, symbols: Any, nets: Any, *, gap_mm: float, pitch_mm: float, hint: Any, seeds: Any, budget: float) -> str:
    """The hash of what an answer depends on; raises for inputs that are not real (a stand-in plan has no ``to_json``)."""
    digest = hashlib.sha256()
    parts = (json.dumps(plan.to_json(), sort_keys=True), repr(tuple(symbols)), repr(tuple(nets)), repr((gap_mm, pitch_mm)),
             repr(sorted((ref, repr(place)) for ref, place in (hint or {}).items())), repr(tuple(seeds) if seeds else None),
             repr(float(budget)), source_digest())
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _route(route: Any) -> dict[str, Any]:
    return {"points": [list(p) for p in route.points], "bends": route.bends, "length": route.length, "pins": list(route.pins)}


def encode(placements: Any, status: str) -> dict[str, Any]:
    wires = {}
    for net, given in sorted((getattr(placements, "wires", None) or {}).items()):
        many = not hasattr(given, "points")
        wires[net] = {"many": many, "routes": [_route(r) for r in (given if many else [given])]}
    return {
        "status": status,
        "solver": getattr(placements, "solver", "cp-sat"),
        "placements": {ref: {"position": list(p.position), "rotation": p.orientation.rotation, "mirror": p.orientation.mirror}
                       for ref, p in sorted(placements.items())},
        "wires": wires,
        "timing": getattr(placements, "timing", None),
        "deterministic_time": getattr(placements, "deterministic_time", None),
        "stats": getattr(placements, "stats", None),
    }


def decode(document: dict[str, Any]) -> Any:
    from backend.schematic.ladder_solve import Placements
    from backend.schematic.orientation import Orientation
    from backend.schematic.placement_refinement import Placement, Route

    wires = {}
    for net, item in document["wires"].items():
        routes = [Route(tuple(tuple(p) for p in r["points"]), r["bends"], r["length"], tuple(r["pins"])) for r in item["routes"]]
        wires[net] = routes if item["many"] else routes[0]
    result = Placements({ref: Placement(tuple(v["position"]), Orientation(v["rotation"], v["mirror"]))
                         for ref, v in document["placements"].items()}, solver=document["solver"], wires=wires)
    result.status = document["status"]
    result.timing = document["timing"]
    result.deterministic_time = document["deterministic_time"]
    result.stats = document["stats"] or {}
    return result


def load(name: str) -> Any | None:
    path = directory() / f"{name}.json"
    try:
        return decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def store(name: str, placements: Any, status: str) -> None:
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(encode(placements, status), sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, path)
