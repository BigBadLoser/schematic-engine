"""For each part of four demo circuits: class, context and the cheapest feature value (synthetic pin geometry, no KiCad)."""
from __future__ import annotations

from backend.schematic.orientation import Orientation
from backend.schematic.orientation_prior import orientation_costs, part_class, part_context
from backend.schematic.orientation_prior_data import PRIOR
from backend.schematic.placement_refinement import OrientedGeometry, PinGeometry, rotate_geometry
from scripts.schematic_demo_circuits import DEMO_CIRCUITS

ALL = [Orientation(r, m) for r in (0, 90, 180, 270) for m in ("", "x", "y")]


def geometries(pins):
    if len(pins) == 2:   # pins[0] on top, pins[1] below, as a resistor stands
        base = OrientedGeometry({pins[0]: PinGeometry((0.0, -3.81), (0.0, -1.0)), pins[1]: PinGeometry((0.0, 3.81), (0.0, 1.0))}, (-1, -4, 1, 4))
    else:                # inputs left, outputs right, the last pin below
        spec = {p: PinGeometry((-5.08, 2.54 * i), (-1.0, 0.0)) if i < len(pins) // 2 else PinGeometry((5.08, 2.54 * i), (1.0, 0.0))
                for i, p in enumerate(pins)}
        base = OrientedGeometry(spec, (-5, -5, 5, 5))
    return {o: rotate_geometry(base, o.rotation, o.mirror) for o in ALL}


for name in ("demo_buck", "demo_lm317", "demo_opamp", "demo_ampac"):
    intent = DEMO_CIRCUITS[name]() if callable(DEMO_CIRCUITS[name]) else DEMO_CIRCUITS[name]
    kinds = {}
    for net in intent.all_nets:
        k = "supply" if net.kind == "power" else "ground" if net.kind == "ground" else "signal"
        for e in net.endpoints:
            kinds[(e.reference, e.pin)] = k
    for sym in intent.symbols:
        pins = sorted({p for (r, p) in kinds if r == sym.reference}, key=lambda x: (len(x), x))
        if len(pins) < 2 or sym.authoring_auxiliary:
            continue
        pk = {p: kinds[(sym.reference, p)] for p in pins}
        cls = part_class(sym.library, sym.symbol_name, sym.reference, len(pins))
        ctx = part_context(pk)
        rails = [p for p, k in pk.items() if k in ("ground", "supply")]
        geo = geometries(pins)
        costs = orientation_costs(cls, ctx, rails, geo)
        if costs is None:
            print(f"{name:11} {sym.reference:5} {cls:12} {ctx:13} none")
            continue
        best = min(costs, key=lambda o: (costs[o], o.rotation, o.mirror))
        feature = PRIOR.get((cls, ctx), PRIOR.get(({"regulator": "ic", "opamp": "ic", "capacitor_polarized": "capacitor"}.get(cls), ctx), {})).get("feature")
        print(f"{name:11} {sym.reference:5} {cls:12} {ctx:13} {feature}: cheapest {best.rotation}/{best.mirror or 'none'} cost 0")
