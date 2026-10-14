"""Do the repo's lib->page transform and MCP Pro's `get_pin_positions` agree?

kicad_geometry._transform (scene reader) and MCP Pro (placement) are independent
implementations. They agree unmirrored; this probe measures every rotation and
mirror combination on real symbols. It does NOT say which one KiCad follows;
see transform_oracle.py for that.
"""
from __future__ import annotations

from _common import emit, fail

SYMBOLS = (("Device", "R"), ("Amplifier_Operational", "LM358"), ("Connector_Generic", "Conn_01x04"))
CENTRE = (50.8, 50.8)


def main() -> int:
    try:
        from backend.schematic.kicad_geometry import _transform
        from kicad_mcp.tools.schematic import get_pin_positions
    except (ImportError, AttributeError) as error:
        return fail("transform_xcheck", f"imports unavailable: {error}")

    cx, cy = CENTRE
    matrix: dict[str, dict[str, str]] = {}
    mismatches = 0
    for library, name in SYMBOLS:
        base = get_pin_positions(library, name, 0.0, 0.0, 0, 1)
        if not base:
            continue
        # Unrotated, unmirrored: page = (x, -y_lib), so recover library coordinates.
        lib_pins = {pin: (x, -y) for pin, (x, y) in base.items()}
        for rotation in (0, 90, 180, 270):
            for mirror in ("", "x", "y"):
                try:
                    mcp = get_pin_positions(library, name, cx, cy, rotation, 1, mirror)
                except TypeError:
                    return fail("transform_xcheck", "get_pin_positions has no mirror argument in this MCP Pro")
                bad = sum(
                    abs(mcp[pin][0] - _transform(px, py, cx, cy, float(rotation), mirror or None)[0]) > 1e-3
                    or abs(mcp[pin][1] - _transform(px, py, cx, cy, float(rotation), mirror or None)[1]) > 1e-3
                    for pin, (px, py) in lib_pins.items()
                )
                mismatches += bad
                matrix.setdefault(f"{library}:{name}", {})[f"rot{rotation}/{mirror or 'none'}"] = (
                    "agree" if not bad else f"DISAGREE ({bad}/{len(lib_pins)} pins)"
                )
    disagreeing = sorted({k for per in matrix.values() for k, v in per.items() if v != "agree"})
    lines = [
        f"{len(matrix)} symbols x 12 orientations; disagreeing orientations: {disagreeing or 'none'}",
    ]
    return emit("transform_xcheck", lines, {"matrix": matrix, "pin_mismatches": mismatches}, ok=not mismatches)


if __name__ == "__main__":
    raise SystemExit(main())
