"""Finding A1: the box placement uses for symbol extent versus the real (tight) body.

`_symbol_bbox_bounds` is what the engine's placement, ELK sizing and compaction
call; `_placed_symbol_bbox` is what the router uses.
"""
from __future__ import annotations

from _common import emit, fail

CASES = (
    ("Device:R", 0), ("Device:R", 90), ("Device:C", 0), ("Device:LED", 0),
    ("Amplifier_Operational:LM358", 0), ("Connector_Generic:Conn_01x04", 0),
    ("Regulator_Linear:AMS1117-3.3", 0), ("MCU_ST_STM32F0:STM32F030F4Px", 0),
)


def main() -> int:
    try:
        from kicad_mcp.tools import schematic as sch

        placement_box, router_box = sch._symbol_bbox_bounds, sch._placed_symbol_bbox
    except (ImportError, AttributeError) as error:
        return fail("footprint_probe", f"MCP Pro helpers unavailable: {error}")

    rows, lines = [], []
    for lib_id, rotation in CASES:
        symbol = {"x": 0.0, "y": 0.0, "lib_id": lib_id, "rotation": rotation, "unit": 1}
        try:
            wide = placement_box(symbol)
            tight = router_box(symbol)
            tight = (tight.x_min, tight.y_min, tight.x_max, tight.y_max)
        except Exception as error:  # library missing, etc.
            lines.append(f"{lib_id} rot={rotation}: unavailable ({error})")
            continue
        size = lambda box: (round(box[2] - box[0], 2), round(box[3] - box[1], 2))  # noqa: E731
        ratio = (size(wide)[0] * size(wide)[1]) / max(1e-9, size(tight)[0] * size(tight)[1])
        rows.append({"symbol": lib_id, "rotation": rotation, "placement_box_mm": size(wide),
                     "tight_box_mm": size(tight), "area_ratio": round(ratio, 1)})
        lines.append(
            f"{lib_id:<34} rot={rotation:<3} placement {size(wide)[0]} x {size(wide)[1]} mm"
            f"  vs tight {size(tight)[0]} x {size(tight)[1]} mm  (x{ratio:.1f} area)"
        )
    default = (getattr(sch, "_SYMBOL_HALF_W_MM", None), getattr(sch, "_SYMBOL_HALF_H_MM", None))
    lines.append(f"default half-extent constants: {default}")
    return emit("footprint_probe", lines, {"cases": rows, "default_half_extent_mm": default})


if __name__ == "__main__":
    raise SystemExit(main())
