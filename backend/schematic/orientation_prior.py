"""What a human draws: the cost of an orientation from how rarely the KiCad demo sheets draw a part that way.

Pure: names and geometry in, costs out. The table is ``orientation_prior_data.PRIOR`` (generated).
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

from .orientation import Orientation
from .orientation_prior_data import PRIOR
from .placement_refinement import OrientedGeometry

SCALE = 4.0
FLOOR = 0.01
_EPS = 1e-6
_FALLBACK = {"regulator": "ic", "opamp": "ic", "capacitor_polarized": "capacitor"}


def _prefix(reference: str) -> str:
    match = re.match(r"[A-Za-z#]+", reference)
    return match.group(0) if match else ""


def part_class(library: str, symbol_name: str, reference: str, pin_count: int) -> str:
    """The extract.py classifier, without the pin names it used to tell a transistor's kind (the name says it as well)."""
    pre = _prefix(reference).upper()
    nl = symbol_name.lower()
    lib = library
    if lib.startswith("Regulator_"):
        return "regulator"
    if lib.startswith("Amplifier_Operational") or "opamp" in nl:
        return "opamp"
    if pre == "T" or lib.startswith("Transformer") or nl.startswith("transformer"):
        return "transformer"
    if "bridge" in nl:
        return "bridge"
    if re.search(r"(^|_)led", nl) or pre == "LED":
        return "led"
    if pre == "Q" or lib.startswith("Transistor"):
        if "BJT" in lib or re.search(r"(^|_)(npn|pnp)", nl):
            return "transistor_bjt"
        if "FET" in lib or "MOS" in lib or "mos" in nl or "jfet" in nl:
            return "transistor_fet"
        return "other"
    if pre == "D" or lib.startswith("Diode"):
        return "diode"
    if pre == "CP" or "polarized" in nl or re.match(r"cp(_|$)", nl):
        return "capacitor_polarized"
    if pre == "C":
        return "capacitor"
    if pre == "R" and "pot" not in nl:
        return "resistor"
    if pre == "L":
        return "inductor"
    if pre in ("V", "I", "BT"):
        return "source"
    if pre in ("J", "P") or lib.startswith("Conn"):
        return "connector"
    if pre == "SW":
        return "switch"
    if pre == "Y":
        return "crystal"
    if pre == "U":
        return "ic"
    return "other"


def part_context(pin_net_kinds: Mapping[str, str]) -> str:
    """How a part sits in the circuit, from the kind of net on each pin ("signal", "ground", "supply"; "nc" is ignored)."""
    kinds = [kind for kind in pin_net_kinds.values() if kind != "nc"]
    if len(pin_net_kinds) > 2:
        return "multipin"
    if len(kinds) != 2 or len(pin_net_kinds) != 2:
        return "other"
    rails = [kind in ("ground", "supply") for kind in kinds]
    if all(rails):
        return "across_rails"
    if not any(rails):
        return "series"
    return "shunt_ground" if "ground" in kinds else "shunt_supply"


def canonical_orientation(orientation: Orientation) -> str:
    """"rotation/mirror" with mirror y written as mirror x after a half turn."""
    rotation, mirror = orientation.rotation % 360, orientation.mirror
    if mirror == "y":
        rotation, mirror = (rotation + 180) % 360, "x"
    return f"{rotation}/{mirror or 'none'}"


def _direction(vector: tuple[float, float]) -> str:
    x, y = vector
    if abs(x) >= abs(y):
        return "right" if x > 0 else "left"
    return "down" if y > 0 else "up"


def _feature(feature: str, orientation: Orientation, geometry: OrientedGeometry, rail_pins: Iterable[str]) -> str | None:
    if feature == "orientation":
        return canonical_orientation(orientation)
    if feature == "axis":
        points = [pin.point for pin in geometry.pins.values()]
        if len(points) < 2:
            return None
        return "horizontal" if abs(points[0][0] - points[1][0]) > _EPS else "vertical"
    if feature == "rail_pin_dir":
        for pin in rail_pins:
            if pin in geometry.pins:
                return _direction(geometry.pins[pin].normal)
    return None


def orientation_costs(
    part_class: str, context: str, rail_pins: Iterable[str], geometries: Mapping[Orientation, OrientedGeometry],
) -> dict[Orientation, float] | None:
    """SCALE * ln(p_best / max(p, FLOOR)) for every orientation, or None when there is no data for this part."""
    if part_class == "connector":
        return None
    entry = PRIOR.get((part_class, context))
    if entry is None and part_class in _FALLBACK:
        entry = PRIOR.get((_FALLBACK[part_class], context))
    if entry is None:
        return None
    shares = entry["shares"]
    best = max(shares.values())
    rail_pins = tuple(rail_pins)
    costs: dict[Orientation, float] = {}
    for orientation, geometry in geometries.items():
        value = _feature(entry["feature"], orientation, geometry, rail_pins)
        if value is None:
            return None
        costs[orientation] = SCALE * math.log(best / max(shares.get(value, 0.0), FLOOR))
    return costs
