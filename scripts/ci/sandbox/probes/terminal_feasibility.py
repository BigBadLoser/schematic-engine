"""Can the terminal planner give every power pin of a saved placement a stub and a power symbol?

Plays the engine's terminal stage on a run's pre-route scene (`pre-route-presentation-scene.json`, saved before any
terminal, label or wire is written, so it exists even when the build later failed): every pin of a power or ground
net gets a stub and a power symbol in turn, each seeing the ones before it. A pin with no exit is a placement the
engine would send back to the layout loop; the reasons say what turned its candidates away.

Uses the newest run with scenes, or the run named in SBX_FEASIBILITY_RUN. A net whose power symbol the library does
not have (VIN, say) is a label in the engine and is planned here as a power symbol anyway, so those are approximate.
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

from _common import REPO, SANDBOX, emit, fail

FIXTURES = ("mcu", "noninverting_amplifier", "rc_filter", "linear_regulator")
GRID = 1.27
CLEARANCE = 3.81


def newest_run() -> Path | None:
    wanted = os.environ.get("SBX_FEASIBILITY_RUN")
    runs = sorted((SANDBOX / ".work" / "runs").glob(f"*{wanted}*" if wanted else "*"))
    for run in reversed(runs):
        if any((run / "artifacts").glob("schematic-engine-*/pre-route-presentation-scene.json")):
            return run
    return None


def simulate(run: Path, fixture: str):
    from backend.schematic.presentation_geometry import TEXT_KINDS, Bounds
    from backend.schematic.terminal_placement import choose_safe_terminal, power_symbol_footprint
    from sbx import kpi

    scene_path = run / "artifacts" / f"schematic-engine-{fixture}" / "pre-route-presentation-scene.json"
    if not scene_path.is_file():
        return None
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    geometry = kpi.geometry_from_scene_dict(scene)
    intent = kpi.load_intent(fixture, run / "src")
    # A symbol is its body and pin tips, as in the engine's tight layout.
    bounds: dict[str, list[float]] = {}
    for owner, rect in geometry.bodies:
        bounds[owner] = list(rect)
    for _token, (point, owner) in geometry.pins.items():
        box = bounds.setdefault(owner, [point[0], point[1], point[0], point[1]])
        box[0], box[1] = min(box[0], point[0]), min(box[1], point[1])
        box[2], box[3] = max(box[2], point[0]), max(box[3], point[1])
    text_kinds = {kind.value for kind in TEXT_KINDS}
    text = tuple(
        Bounds(i["bounds"]["left"], i["bounds"]["top"], i["bounds"]["right"], i["bounds"]["bottom"])
        for i in scene["items"] if i["kind"] in text_kinds
    )
    pin_tips = {token: point for token, (point, _owner) in geometry.pins.items()}
    segments: list = []
    points: list = []
    footprints: list = []
    placed = 0
    failures: list[tuple[str, str, str]] = []
    for net in intent.all_nets:
        if net.kind not in ("power", "ground"):
            continue
        for endpoint in net.endpoints:
            token = f"{endpoint.reference}.{endpoint.pin}"
            if token not in geometry.pins:
                continue
            point, owner = geometry.pins[token]
            normal = geometry.pin_dirs.get(token) or (0.0, -1.0)
            end = power_symbol_footprint(net.name, -1.0 if net.kind == "ground" else 1.0)
            own = bounds.get(owner)
            try:
                plan = choose_safe_terminal(
                    source=(round(point[0], 4), round(point[1], 4)), preferred_normal=normal,
                    own_bounds=Bounds(*own) if own else None,
                    foreign_bounds=tuple(Bounds(*b) for o, b in bounds.items() if o != owner),
                    foreign_pin_points=tuple([p for t, p in pin_tips.items() if t != token] + points),
                    existing_wire_segments=tuple(segments), occupied_terminals=tuple(points),
                    grid_mm=GRID, clearance_mm=CLEARANCE, end_symbol=end,
                    avoid_boxes=tuple(footprints), soft_boxes=text,
                )
            except ValueError as error:
                failures.append((token, net.name, str(error).split("turned away:")[-1].strip(" )")))
                continue
            placed += 1
            segments.extend(plan.segments)
            points.append(plan.position)
            footprints.append(end.footprint_at(plan.position))
    return placed, failures


def main() -> int:
    run = newest_run()
    if run is None:
        return fail("terminal_feasibility", "no sandbox run with pre-route scenes; run `sandbox.py run` first")
    try:
        import backend.schematic.terminal_placement  # noqa: F401
        from sbx import kpi  # noqa: F401
    except ImportError as error:
        return fail("terminal_feasibility", f"repository modules unavailable: {error}")

    lines = [f"run {run.name}"]
    data: dict[str, object] = {"run": run.name, "repo": str(REPO), "fixtures": {}}
    stuck = 0
    for fixture in FIXTURES:
        result = simulate(run, fixture)
        if result is None:
            continue
        placed, failures = result
        stuck += len(failures)
        lines.append(f"{fixture:24} placed {placed:3d}   no exit {len(failures)}")
        for token, net, why in failures[:4]:
            lines.append(f"    {token:8} {net:6} {why}")
        data["fixtures"][fixture] = {  # type: ignore[index]
            "placed": placed, "no_exit": [{"pin": t, "net": n, "reasons": w} for t, n, w in failures],
        }
    return emit("terminal_feasibility", lines, data, ok=stuck == 0)


if __name__ == "__main__":
    raise SystemExit(main())
