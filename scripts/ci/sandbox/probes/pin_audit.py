"""Are MCP Pro's pin outward normals consistent with the pin's side of the symbol body?

Checks every pin of the fixture symbols at all four rotations: the normal must
point the same way as the nearest body edge to the pin tip.
"""
from __future__ import annotations

from _common import emit, fail

SYMBOLS = (
    ("MCU_ST_STM32F0", "STM32F030F4Px"), ("Device", "R"), ("Device", "C"), ("Device", "Crystal"),
    ("Switch", "SW_Push"), ("Connector_Generic", "Conn_01x02"), ("Connector_Generic", "Conn_01x03"),
    ("Connector_Generic", "Conn_01x04"), ("Amplifier_Operational", "LM741"),
    ("Regulator_Linear", "AMS1117-3.3"), ("power", "PWR_FLAG"),
)
EDGE_NORMAL = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def main() -> int:
    try:
        from kicad_mcp.tools import schematic as sch

        pins_at, normals_at, body_of = sch.get_pin_positions, sch.get_pin_outward_normals, sch._placed_symbol_bbox
    except (ImportError, AttributeError) as error:
        return fail("pin_audit", f"MCP Pro helpers unavailable: {error}")

    checked, bad, lines = 0, [], []
    for library, name in SYMBOLS:
        for rotation in (0, 90, 180, 270):
            try:
                pins = pins_at(library, name, 0.0, 0.0, rotation, 1)
                normals = normals_at(library, name, rotation, 1)
                box = body_of({"x": 0.0, "y": 0.0, "lib_id": f"{library}:{name}", "rotation": rotation, "unit": 1})
            except Exception as error:
                lines.append(f"{library}:{name} rot={rotation}: unavailable ({error})")
                continue
            for pin, (px, py) in pins.items():
                nx, ny = normals.get(pin, (0.0, 0.0))
                distance = {
                    "left": abs(px - box.x_min), "right": abs(px - box.x_max),
                    "top": abs(py - box.y_min), "bottom": abs(py - box.y_max),
                }
                edge = min(distance, key=distance.get)
                expected = EDGE_NORMAL[edge]
                checked += 1
                if abs(nx - expected[0]) > 0.5 or abs(ny - expected[1]) > 0.5:
                    bad.append({"symbol": f"{library}:{name}", "rotation": rotation, "pin": pin,
                                "normal": [round(nx, 2), round(ny, 2)], "nearest_edge": edge})
    lines.insert(0, f"checked {checked} (pin, rotation) pairs; normal disagrees with nearest body edge: {len(bad)}")

    sides = {}
    try:
        pins = pins_at("MCU_ST_STM32F0", "STM32F030F4Px", 0.0, 0.0, 0, 1)
        normals = normals_at("MCU_ST_STM32F0", "STM32F030F4Px", 0, 1)
        names = {(-1, 0): "left", (1, 0): "right", (0, -1): "top", (0, 1): "bottom"}
        sides = {p: names.get((round(normals[p][0]), round(normals[p][1])), "?") for p in sorted(pins, key=int)}
        lines.append(f"STM32F030F4Px pin sides: {sides}")
    except Exception:
        pass
    return emit("pin_audit", lines, {"checked": checked, "disagreements": bad, "stm32_pin_sides": sides}, ok=not bad)


if __name__ == "__main__":
    raise SystemExit(main())
