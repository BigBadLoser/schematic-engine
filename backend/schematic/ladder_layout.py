"""Place a schematic as a left-to-right ladder from its roles and pin geometry.

The chain shares a row, shared trunk nets get rows below it, and attachments hang from the
row of the net they serve.  Pin tips define the rows; symbol bodies sit clear of them.  This
is deliberately a small, deterministic geometry pass: the caller keeps parts classified as
``other`` at their existing positions and can hand these placements to later refinement.

A controller's pins are fanned into lanes (``roles.lanes``): each is a row at the pin's height on the pin's side of the
chip, its series parts laid outward from the pin tip, its shunts hung from the end of the lane by the attachment rules and
a part joining two lanes standing between them (spec section 4, rule 6).  People draw a controller that way, so that the
chip's sides read as a list of its functions.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple

from .designators import is_source_reference, split_unit
from .ladder_plan import GroupRecord, LayoutRecord, NoRecord, StackRecord
from .orientation import Orientation
from .placement_refinement import Connection, Placement, PlacedSymbol, OrientedGeometry, GRID_MM, estimate_net

if TYPE_CHECKING:
    from .ladder_roles import Roles

_EPS = 1e-7


_LEAD_MM = 1.27


class _Placements(dict):
    """The placements, with the references the layout stood beside a part they serve (``beside``)."""
    beside: frozenset = frozenset()


def _core(geom: OrientedGeometry) -> tuple[float, float, float, float]:
    """The box without the lead padding: each side a pin points through is pulled in by the lead length.

    Pin tips are connection points: two parts whose tips meet at a node touch there, and only their bodies must not."""
    l, t, r, b = geom.box
    sides = {(round(pg.normal[0]), round(pg.normal[1])) for pg in geom.pins.values()}
    if (-1, 0) in sides:
        l += _LEAD_MM
    if (1, 0) in sides:
        r -= _LEAD_MM
    if (0, -1) in sides:
        t += _LEAD_MM
    if (0, 1) in sides:
        b -= _LEAD_MM
    return (l, t, max(l, r), max(t, b))


def _reject_coincident_tips(tips) -> None:
    """Raise when two parts' pin tips lie on one point.

    Tips meet a wire, not each other: a stack of parts shares a straight wire with at least a grid step between the
    tips (people never leave two tips directly on each other)."""
    ordered = sorted(tips)
    for i, (ax, ay, a_ref) in enumerate(ordered):
        for bx, by, b_ref in ordered[i + 1:]:
            if bx - ax > _EPS:
                break
            if a_ref != b_ref and abs(ay - by) <= _EPS:
                raise ValueError(f"Pin tips coincide: {a_ref} and {b_ref}")


def _flow_direction(normal) -> int:
    return 1 if normal[0] > .5 or normal[1] > .5 else -1 if normal[0] < -.5 or normal[1] < -.5 else 0


def _upright(symbol: PlacedSymbol) -> OrientedGeometry | None:
    return symbol.geometry.get(Orientation(0, "")) or symbol.geometry.get(symbol.orientation)


class _LaneLike(NamedTuple):
    """A lane with no series part: what a hub pin with one hung part stands for."""
    hub: str
    pin: str
    net: str
    series: tuple
    nets: tuple


class _NoSlot(ValueError):
    """No slot for a hung part; ``part`` is the chain part whose gap before it could grow, ``room`` by how much."""

    def __init__(self, message: str, part: str | None = None, room: float = 0.0) -> None:
        super().__init__(message)
        self.part, self.room = part, room


def _is_one_sided(symbol: PlacedSymbol) -> bool:
    """A connector or header: every pin points the same way."""
    return all(len({tuple(pg.normal) for pg in g.pins.values()}) == 1 for g in symbol.geometry.values())


def _is_multi_pin(symbol: PlacedSymbol) -> bool:
    return max(len(g.pins) for g in symbol.geometry.values()) > 2


def _drawn_flow(symbol: PlacedSymbol, pin: str) -> int:
    """+1 when the pin is an output as drawn, -1 an input, 0 unknown.

    A multi-pin part is judged upright, since its pins mean something (drain above source);
    a two-pin part is judged as it currently lies."""
    g = (_upright(symbol) if _is_multi_pin(symbol) else None) or symbol.geometry.get(symbol.orientation)
    return _flow_direction(g.pins[pin].normal) if g is not None and pin in g.pins else 0


def _pin_roles(symbol: PlacedSymbol, pins: set[str]) -> dict[str, str]:
    """Through and control pins of a part with three or more pins, judged upright, for the pins in use.

    A pin with another pin of the part (in use or not: a rail pin counts) facing the opposite way lies on the part's axis: it is a through pin, ``in`` when it
    faces left or up and ``out`` when it faces right or down (collector/emitter, drain/source, input/output).  Any
    other pin (base, gate, adjust) is a ``control`` pin.  A part with no such pair has no roles: nothing is known
    about which way its signal runs."""
    upright = _upright(symbol)
    if upright is None or not _is_multi_pin(symbol):
        return {}
    usable = sorted(pin for pin in pins if pin in upright.pins)
    result: dict[str, str] = {}
    for pin in usable:
        normal = upright.pins[pin].normal
        opposite = any(
            other != pin and normal[0] * upright.pins[other].normal[0] + normal[1] * upright.pins[other].normal[1] < -.5
            for other in upright.pins
        )
        flow = _flow_direction(normal)
        if opposite and flow:
            result[pin] = "out" if flow > 0 else "in"
        elif not opposite:
            result[pin] = "control"
    return result if any(role != "control" for role in result.values()) else {}


def _facing_choice(by_ref, geoms, placed, chain, index, earlier, pairs, multi, geom_choice, net_pin, pin_roles):
    """The orientation of a chain part that is the second of a pair, or None when the pair rule does not apply.

    A differential pair is drawn face to face: the member before it keeps its orientation and this one is its mirror
    about the vertical axis, so their shared through pins point the same way and the control pins point outward."""
    if not index or not multi or earlier is None:
        return None
    mate, ref = chain[index - 1], chain[index]
    if tuple(sorted((mate, ref))) not in pairs or mate not in placed or mate not in geoms:
        return None
    mate_shared, shared = net_pin(mate, earlier), net_pin(ref, earlier)
    if mate_shared is None or shared is None or mate_shared not in geoms[mate].pins:
        return None
    if pin_roles(mate).get(mate_shared) is None or pin_roles(mate).get(mate_shared) == "control" or pin_roles(ref).get(shared) in (None, "control"):
        return None
    mate_controls = sorted(pin for pin, role in pin_roles(mate).items() if role == "control"
                           if abs(geoms[mate].pins[pin].normal[0]) > .5)
    own_controls = sorted(pin for pin, role in pin_roles(ref).items() if role == "control")
    if not mate_controls or not own_controls:
        return None
    mate_normal = geoms[mate].pins[mate_shared].normal
    mate_control = geoms[mate].pins[mate_controls[0]].normal[0]

    def faces(o, g):
        return (shared in g.pins and own_controls[0] in g.pins
                and g.pins[shared].normal[0] * mate_normal[0] + g.pins[shared].normal[1] * mate_normal[1] > .5
                and g.pins[own_controls[0]].normal[0] * mate_control < -.5)

    try:
        return geom_choice(ref, faces)
    except ValueError:
        return None


def _stack_offset(group_boxes, net_tips, pin_tip, side, others, gap) -> tuple[float, float]:
    """Where a stacked group goes: ``(dx, dy)`` to add to its placements.

    The group's net tips are at its top row.  It lies ``2 * gap`` beyond the host pin's tip (above for a pin that points
    up, below for one that points down) with the net's last tip level with the pin's x; it steps left by whole 2.54 mm
    until the straight drop to the pin clears its own bodies, and outward until it clears every placed body by two gaps (its wires run along its edge)."""
    step = 2 * GRID_MM
    top, bottom = min(b[1] for b in group_boxes.values()), max(b[3] for b in group_boxes.values())
    base_dy = (pin_tip[1] - 2 * gap - bottom) if side < 0 else (pin_tip[1] + 2 * gap - top)
    base_dy = round(base_dy / GRID_MM) * GRID_MM
    base_dx = round((pin_tip[0] - max(t[0] for t in net_tips)) / GRID_MM) * GRID_MM
    tip_y = net_tips[0][1]
    for k in range(60):
        dx = base_dx - k * step
        for j in range(40):
            dy = base_dy + side * j * step
            lo, hi = sorted((tip_y + dy, pin_tip[1]))
            shifted = [(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for b in group_boxes.values()]
            if any(b[0] - gap < pin_tip[0] < b[2] + gap and b[1] < hi - _EPS and lo + _EPS < b[3] for b in shifted):
                break
            grown = [(b[0] - 2 * gap, b[1] - 2 * gap, b[2] + 2 * gap, b[3] + 2 * gap) for b in shifted]
            if not any(g[0] < o[2] - _EPS and o[0] < g[2] - _EPS and g[1] < o[3] - _EPS and o[1] < g[3] - _EPS
                       for g in grown for o in others):
                return dx, dy
    return base_dx, base_dy


def ladder_layout(
    symbols: Sequence[PlacedSymbol],
    nets: Sequence[Connection],
    roles: "Roles",
    *,
    text_widths: Mapping[str, float],
    gap_mm: float = 2.54,
    slot_pitch_mm: float = 7.62,
    record: LayoutRecord | None = None,
) -> dict[str, Placement]:
    """Return deterministic placements for chain, attachment, rung, feedback and pair parts.

    ``record``, when given, is filled with the decisions the layout makes (which row each part is on, what hangs from
    which wire in which order, spans, columns, orientations: see ``ladder_plan``) as a by-product; nothing it holds
    changes what is returned.

    The blocks are first ordered and sorted into rows (see ``_plan_blocks``); each row is placed on its own by
    ``_place_group`` (the rules for one row are in its docstring) and the rows are then stacked: a row starts below
    the extent of the rows above it plus two gaps, left-aligned with the first (people draw parallel circuits fed by
    one input as separate groups, not one long row).  A block joined to the rest by rails alone (a supply) stands in
    a column at the left, above the first row.  A final box check raises ``ValueError`` naming any overlapping pair.
    """
    if gap_mm < 0 or slot_pitch_mm <= 0:
        raise ValueError("gap_mm must be non-negative and slot_pitch_mm must be positive")
    by_ref = {symbol.reference: symbol for symbol in symbols}
    if len(by_ref) != len(symbols):
        raise ValueError("PlacedSymbol references must be unique")
    pins_by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net in sorted(nets, key=lambda n: n.net):
        for ref, pin in sorted(net.pins):
            pins_by_net[net.net].append((ref, pin))
    chains = {block: tuple(chain) for block, chain in roles.chains.items()}
    missing = sorted({ref for chain in chains.values() for ref in chain} - by_ref.keys())
    if missing:
        raise ValueError(f"Role symbols are missing from placement input: {', '.join(missing)}")

    # Stacked pairs (a half bridge) are columns of their own: their parts are on no chain row (see ``_stack_pairs``).
    pair_stacks, pair_spans, pair_feeders = _stack_pairs(by_ref, pins_by_net, roles, chains)
    stack_parts = {ref for item in pair_stacks for ref in (item.upper, item.lower)} | set(pair_spans) | set(pair_feeders)
    stack_owned = {ref for item in pair_stacks for ref in item.hangs}
    stack_nets = {item.net for item in pair_stacks}
    chains = {block: tuple(ref for ref in chain if ref not in stack_parts) for block, chain in chains.items()}
    chains = {block: chain for block, chain in chains.items() if chain}
    row_blocks, hangs_by_row, lone_blocks, stack_rows = _plan_blocks(by_ref, pins_by_net, roles, chains)
    group_of: dict[str, int] = {}
    for index, (blocks, hangs) in enumerate(zip(row_blocks, hangs_by_row)):
        for block in (*blocks, *hangs):
            for ref in chains[block]:
                group_of[ref] = index
    if not row_blocks:
        row_blocks, hangs_by_row = [[]], [{}]

    lanes = dict(getattr(roles, "lanes", None) or {})
    # a lane belongs to its hub's row: the nets along it and the parts hung on them are placed with the hub
    lane_group = {net: group_of[lane.hub] for lane in lanes.values() if lane.hub in group_of for net in lane.nets}

    def group_for_nets(*names: str) -> int:
        groups = {group_of[ref] for name in names for ref, _pin in pins_by_net.get(name, ()) if ref in group_of}
        groups |= {lane_group[name] for name in names if name in lane_group}
        # a stacked group owns the nets it shares with the part it stacks over (its rungs are its own)
        return min(groups, key=lambda g: (g not in stack_rows, g)) if groups else 0

    trunk_group: dict[str, int | None] = {}
    for name in roles.trunks:
        if name in stack_nets:
            continue
        groups = {group_of[ref] for ref, _pin in pins_by_net.get(name, ()) if ref in group_of}
        # A trunk whose chain pins lie in several rows is the wire that reaches them: no row of its own.
        if not groups:
            # a trunk with no chain pin belongs where the rungs that join it stand
            groups = {group_for_nets(rung.net_b if rung.net_a == name else rung.net_a)
                      for rung in roles.rungs.values() if name in (rung.net_a, rung.net_b)}
        trunk_group[name] = None if len(groups) > 1 else (min(groups) if groups else 0)

    results: list[dict[str, Placement]] = []
    for index, (blocks, hangs) in enumerate(zip(row_blocks, hangs_by_row)):
        sub = replace(
            roles,
            chains={block: chains[block] for block in (*blocks, *sorted(hangs))},
            trunks=tuple(name for name in roles.trunks if name not in stack_nets and trunk_group[name] == index),
            attachments={ref: item for ref, item in roles.attachments.items()
                         if ref not in stack_owned and (group_of[item.host] if item.host in group_of else group_for_nets(item.net)) == index},
            rungs={ref: item for ref, item in roles.rungs.items() if group_for_nets(item.net_a, item.net_b) == index},
            feedback={ref: item for ref, item in roles.feedback.items()
                      if group_for_nets(item.net_from, item.net_to) == index},
            pairs=tuple(pair for pair in roles.pairs
                        if min((group_of[ref] for ref in pair if ref in group_of), default=0) == index),
            **({"lanes": {key: lane for key, lane in lanes.items() if group_of.get(lane.hub) == index}} if lanes else {}),
        )
        # A hung part with no slot beside its host asks for room: the gap before the part (after the host when its
        # pin points along the row) grows by the part's width and the group is placed again.
        widen: dict[str, float] = {}
        partial = False          # a plan asked for with ``record.partial`` goes on past a part with no slot, leaving it out
        for attempt in range(7):
            attempt_record = GroupRecord() if record is not None else None
            try:
                results.append(_place_group(symbols, nets, sub, blocks, hangs, text_widths=text_widths,
                                            gap_mm=gap_mm, slot_pitch_mm=slot_pitch_mm,
                                            stack_net=stack_rows[index][2] if index in stack_rows else None,
                                            widen=widen, record=attempt_record, partial=partial))
                if record is not None:
                    record.groups.append(attempt_record)
                break
            except _NoSlot as error:
                if error.part is None or attempt >= 5:
                    if partial or record is None or not record.partial:
                        raise
                    partial = True
                    continue
                widen[error.part] = widen.get(error.part, 0.0) + error.room

    def snap(value: float) -> float:
        return round(value / GRID_MM) * GRID_MM

    def core_box(ref: str, placement: Placement):
        l, t, r, b = _core(by_ref[ref].geometry[placement.orientation])
        return (placement.position[0] + l, placement.position[1] + t, placement.position[0] + r, placement.position[1] + b)

    def box_of(ref: str, placement: Placement):
        l, t, r, b = by_ref[ref].geometry[placement.orientation].box
        return (placement.position[0] + l, placement.position[1] + t, placement.position[0] + r, placement.position[1] + b)

    placed: dict[str, Placement] = {}
    left = bottom = None
    for index, group in enumerate(results):
        boxes = [box_of(ref, pl) for ref, pl in sorted(group.items())]
        if not boxes or index in stack_rows:
            continue
        group_left, group_top = min(b[0] for b in boxes), min(b[1] for b in boxes)
        if left is None:
            dx = dy = 0.0
            left = group_left
        else:
            dx = snap(left - group_left)
            # A group stands under the parts of the rows above it that it shares nets with, not at the left edge, when that
            # shortens those nets: they are then short and straight.  (Never left of the left edge.)
            near, far = [], []
            # (a net that reaches the part starting the rows above, a shared source, is what the group is fed from: it stays at its edge)
            starter = min(placed, key=lambda r: (placed[r].position[0], r), default=None)
            for net_name, pins in pins_by_net.items():
                if net_name in roles.rails or any(r == starter for r, _p in pins):
                    continue
                mine = [pl.position[0] + by_ref[r].geometry[pl.orientation].pins[p].point[0]
                        for r, p in pins if r in group for pl in [group[r]] if p in by_ref[r].geometry[pl.orientation].pins]
                theirs = [pl.position[0] + by_ref[r].geometry[pl.orientation].pins[p].point[0]
                          for r, p in pins if r in placed for pl in [placed[r]] if p in by_ref[r].geometry[pl.orientation].pins]
                if mine and theirs:
                    near.append(sum(mine) / len(mine))
                    far.append(sum(theirs) / len(theirs))
            if near:
                aligned = max(dx, snap(sum(far) / len(far) - sum(near) / len(near)))
                # only when it shortens the nets between them by more than a slot pitch: a nudge is not worth leaving the edge
                gain = sum(abs(f - (n + dx)) - abs(f - (n + aligned)) for n, f in zip(near, far))
                if gain > slot_pitch_mm:
                    dx = aligned
            dy = math.ceil((bottom + 2 * gap_mm - group_top) / GRID_MM - _EPS) * GRID_MM
        if record is not None:
            record.offsets[index] = (dx, dy)
        for ref, pl in group.items():
            placed[ref] = Placement((pl.position[0] + dx, pl.position[1] + dy), pl.orientation)
        group_bottom = max(b[3] for b in boxes) + dy
        bottom = group_bottom if bottom is None else max(bottom, group_bottom)

    # A stacked group goes over (or under) the part it feeds: the run along its top row ends beside the pin and the net
    # drops straight to it, clear of the group's bodies and of everything already placed.
    for index, (host, host_pin, net_name) in sorted(stack_rows.items()):
        group = results[index]
        if not group or host not in placed:
            continue
        pin = by_ref[host].geometry[placed[host].orientation].pins[host_pin]
        net_tips = [(pl.position[0] + g.pins[p].point[0], pl.position[1] + g.pins[p].point[1])
                    for ref, pl in sorted(group.items()) for g in [by_ref[ref].geometry[pl.orientation]]
                    for r, p in pins_by_net[net_name] if r == ref and p in g.pins]
        offset = _stack_offset(
            {ref: core_box(ref, pl) for ref, pl in group.items()}, net_tips,
            (placed[host].position[0] + pin.point[0], placed[host].position[1] + pin.point[1]),
            1 if pin.normal[1] > 0 else -1, [core_box(ref, pl) for ref, pl in placed.items()], gap_mm)
        for ref, pl in group.items():
            placed[ref] = Placement((pl.position[0] + offset[0], pl.position[1] + offset[1]), pl.orientation)
        if record is not None:
            record.offsets[index], record.stacks[index] = offset, (host, host_pin, net_name)
        bottom = max(b for b in [bottom, *(core_box(ref, placed[ref])[3] for ref in group)] if b is not None)

    # Stacked pairs stand right of everything placed, their tops level with the first group's.
    if pair_stacks:
        relative = _place_stacks(pair_stacks, pair_spans, by_ref, pins_by_net, roles, gap_mm, record, pair_feeders)
        rel_boxes = [core_box(ref, pl) for ref, pl in relative.items()]
        base = [core_box(ref, pl) for ref, pl in placed.items()]
        right = max((b[2] for b in base), default=None)
        top = min((b[1] for b in base), default=None)
        dx = 0.0 if right is None else math.ceil((right + 2 * gap_mm - min(b[0] for b in rel_boxes)) / GRID_MM - _EPS) * GRID_MM
        dy = 0.0 if top is None else snap(top - min(b[1] for b in rel_boxes))
        for ref, pl in relative.items():
            placed[ref] = Placement((pl.position[0] + dx, pl.position[1] + dy), pl.orientation)
        left = left if left is not None else min(b[0] for b in rel_boxes) + dx

    # Supplies that touch no signal net: one column at the left, above the first row, supply pin up.  A part the roles
    # leave as ``other`` whose pins are all on rails (a second supply) joins it below the others: it is a supply too.
    rail_only_others = [
        ref for ref in sorted(roles.other)
        if ref in by_ref and not ref.startswith("#")
        and (kinds := {roles.rails.get(name) is not None for name, pins in pins_by_net.items()
                       for r, _pin in pins if r == ref})
        and kinds == {True}
    ]
    # A unit of a multi-unit part whose pins are all on rails (an op-amp power unit) stands beside a placed unit of the same
    # part, as people draw it, not in the supply column.
    def sibling_of(ref: str) -> str | None:
        part, unit = split_unit(ref)
        if unit is None:
            return None
        return next((other for other in sorted(placed) if other != ref and split_unit(other)[0] == part), None)

    beside_refs = [ref for ref in rail_only_others if ref not in placed and sibling_of(ref) is not None]

    def rails_of(ref: str) -> frozenset[str]:
        return frozenset(name for name, pins in pins_by_net.items() if name in roles.rails for r, _pin in pins if r == ref)

    def pin_count(ref: str) -> int:
        return max(len(g.pins) for g in by_ref[ref].geometry.values())

    # A two-pin part across two rails (a bypass capacitor) stands beside the multi-pin part (or power unit) whose power pins
    # use exactly those two rails: the one with the fewest bypass parts so far, then the lowest reference.
    bypass_refs = [ref for ref in rail_only_others
                   if ref not in placed and ref not in beside_refs and pin_count(ref) == 2 and len(rails_of(ref)) == 2]
    lone_refs = ([ref for block in lone_blocks for ref in chains[block]]
                 + [ref for ref in rail_only_others if ref not in placed and ref not in beside_refs and ref not in bypass_refs])
    level = {"supply": 2, "ground": 1, "negative": 0}

    def rail_choice(ref: str) -> tuple[Orientation, OrientedGeometry]:
        """The orientation of a part whose pins are on rails: vertical, the pin of the higher rail up."""
        pin_kind = {pin: roles.rails.get(name) for name, pins in pins_by_net.items() for r, pin in pins if r == ref}

        def lone_key(item):
            orientation, geometry = item
            ys = [pg.point[1] for pg in geometry.pins.values()]
            xs = [pg.point[0] for pg in geometry.pins.values()]
            vertical = (max(ys) - min(ys)) > (max(xs) - min(xs))
            # the pin of the higher rail points up: supply over ground over a negative rail
            ranked = [(level[pin_kind[pin]], pg.point[1]) for pin, pg in geometry.pins.items()
                      if pin_kind.get(pin) in level]
            wrong = int(any(a > b and ya > yb for a, ya in ranked for b, yb in ranked))
            return (not vertical, wrong, orientation.rotation, orientation.mirror)

        return min(by_ref[ref].geometry.items(), key=lone_key)

    beside_done: set[str] = set()

    def stand_beside(ref: str, sibling: str) -> bool:
        """Place ``ref`` next to the placed part ``sibling`` (free space to its right, then its left, room above and below for
        the power symbols); False when there is none."""
        orientation, geometry = rail_choice(ref)
        sib_box = box_of(sibling, placed[sibling])
        stub = max(slot_pitch_mm, 2 * gap_mm) + gap_mm          # the power symbols above and below the unit need room
        others = [core_box(r, pl) for r, pl in placed.items()]
        core = _core(geometry)
        middle = (sib_box[1] + sib_box[3]) / 2 - (geometry.box[1] + geometry.box[3]) / 2
        found = None
        for step in range(60):                                      # to the right of the sibling, then to its left
            for side in (1, -1):
                if side > 0:
                    x = sib_box[2] + 2 * gap_mm + step * 2 * GRID_MM - geometry.box[0]
                else:
                    x = sib_box[0] - 2 * gap_mm - step * 2 * GRID_MM - geometry.box[2]
                anchor = (snap(x), snap(middle))
                grown = (anchor[0] + core[0] - gap_mm, anchor[1] + core[1] - stub,
                         anchor[0] + core[2] + gap_mm, anchor[1] + core[3] + stub)
                if not any(grown[0] < o[2] - _EPS and o[0] < grown[2] - _EPS and grown[1] < o[3] - _EPS and o[1] < grown[3] - _EPS
                           for o in others):
                    found = (anchor, side)
                    break
            if found is not None:
                break
        if found is None:
            return False
        placed[ref] = Placement(found[0], orientation)
        beside_done.add(ref)
        if record is not None:
            record.beside[ref] = (sibling, orientation, found[1])
        return True

    for ref in beside_refs:
        if not stand_beside(ref, sibling_of(ref)):
            lone_refs.append(ref)              # no room beside it: the supply column
    bypassed: dict[str, int] = defaultdict(int)
    for ref in bypass_refs:
        hosts = [host for host in sorted(placed) if host != ref and rails_of(host) == rails_of(ref)
                 and host not in roles.attachments and host not in roles.rungs and host not in roles.feedback   # a hang does not hang from a hang
                 and (pin_count(host) >= 3 or split_unit(host)[1] is not None)]
        for host in sorted(hosts, key=lambda h: (bypassed[h], h)):
            if stand_beside(ref, host):
                bypassed[host] += 1
                if record is not None:
                    # a bypass part hangs from the host's pin on a rail (the supply first), beside it: not a member of a row
                    side_of = record.beside.pop(ref)
                    pin_on = sorted(((roles.rails[name] != "supply", name, pin) for name, pins in pins_by_net.items()
                                     if name in roles.rails for r, pin in pins if r == host), key=lambda t: t[:2])
                    record.bypass[ref] = (host, side_of[1], side_of[2], pin_on[0][1], pin_on[0][2])
                break
        else:
            lone_refs.append(ref)
    if lone_refs:
        first = results[0]
        top = min((box_of(ref, pl)[1] for ref, pl in first.items()), default=0.0)
        left = left if left is not None else 0.0
        chosen: list[tuple[str, Orientation, OrientedGeometry]] = []
        for ref in lone_refs:
            orientation, geometry = rail_choice(ref)
            chosen.append((ref, orientation, geometry))
        if record is not None:
            record.lone = [(ref, orientation) for ref, orientation, _geometry in chosen]
        # each below the one before it, the last two gaps above the first row
        between = max(2 * gap_mm, slot_pitch_mm)         # the pins of two neighbours face each other: room for their stubs
        cursor = top - 2 * gap_mm - sum(g.box[3] - g.box[1] + between for _r, _o, g in chosen) + between
        for ref, orientation, geometry in chosen:
            anchor = (snap(left - geometry.box[0]), snap(cursor - geometry.box[1]))
            placed[ref] = Placement(anchor, orientation)
            cursor = anchor[1] + geometry.box[3] + between

    def core_of(ref: str, placement: Placement):
        l, t, r, b = _core(by_ref[ref].geometry[placement.orientation])
        return (placement.position[0] + l, placement.position[1] + t, placement.position[0] + r, placement.position[1] + b)

    _reject_coincident_tips([
        (placement.position[0] + pg.point[0], placement.position[1] + pg.point[1], ref)
        for ref, placement in placed.items() for pg in by_ref[ref].geometry[placement.orientation].pins.values()])
    final_boxes = [(ref, core_of(ref, placement)) for ref, placement in sorted(placed.items())]
    for i, (left_ref, a) in enumerate(final_boxes):
        for right_ref, b in final_boxes[i + 1:]:
            if a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS:
                raise ValueError(f"Placed symbols overlap: {left_ref} and {right_ref}")
    result = _Placements(placed)
    result.beside = frozenset(beside_done)          # parts standing beside a part they serve, which a harness leaves there
    return result


def _side_rows(by_ref, pins_by_net, roles, placed, geoms, row_pins, hang_nets, feedback_nets, attachments,
               net_pin, gap, snap) -> tuple[dict[str, float], dict[str, str]]:
    """The y of each net that leaves a chain part by a pin off the chain row, up or down, as a row of its own.

    A link leaves a pin along the pin's direction, and people wire the far sides of a transformer or a bridge with a run
    above or below the part, not an L across it.  A net qualifies when one of its pins is a multi-pin chain part's pin
    off the row that points up or down, and it joins another chain part or that part has pins on all four sides.  Its row
    is ``gap`` beyond the tip it must clear (a pin that points sideways from that side of the part: the tip's own y),
    on the outermost of its pins, so two parts sharing the net meet on one straight run; it also clears the supply
    attachments (above) or ground attachments (below) of the chain parts between its parts.  Rows of nets whose spans
    overlap on one side step outward by ``gap``."""
    part_row = {ref: placed[ref].position[1] + geoms[ref].pins[sorted(pins)[0]].point[1]
                for ref, pins in row_pins.items() if pins and ref in placed}

    def tip(ref, pin):
        pg = geoms[ref].pins[pin]
        return (placed[ref].position[0] + pg.point[0], placed[ref].position[1] + pg.point[1])

    def four_sided(ref):
        return {(round(pg.normal[0]), round(pg.normal[1])) for pg in geoms[ref].pins.values()} >= {
            (1, 0), (-1, 0), (0, 1), (0, -1)}

    found = []
    for name, pins in sorted(pins_by_net.items()):
        if name in roles.rails or name in hang_nets or name in feedback_nets:
            continue
        chain_pins = [(ref, pin) for ref, pin in pins if ref in row_pins and ref in placed and pin in geoms[ref].pins]
        off = [(ref, pin) for ref, pin in chain_pins if pin not in row_pins[ref]]
        vertical = [(ref, pin) for ref, pin in off if abs(geoms[ref].pins[pin].normal[1]) > .5]
        if name in roles.trunks and vertical:
            continue
        # A net leaving a multi-pin part by an axis pin that points up or down, to a pin of another part at another
        # height, is wired over what hangs between the two parts (below, for a lower pin); it is a row of its own only
        # when something hangs there, else the chain's own step serves.
        over = []
        if not vertical:
            over = [(ref, pin) for ref, pin in chain_pins if pin in row_pins[ref] and len(geoms[ref].pins) > 2
                    and abs(geoms[ref].pins[pin].normal[1]) > .5
                    and any(r != ref and abs(tip(r, p)[1] - tip(ref, pin)[1]) > _EPS for r, p in chain_pins)]
            if len(over) != 1:
                continue
            vertical = off = over
        elif any(pin in row_pins[ref] for ref, pin in chain_pins if ref in {r for r, _p in vertical}):
            continue
        if len({ref for ref, _pin in chain_pins}) < 2 and not any(four_sided(ref) for ref, _pin in vertical):
            continue
        side = 1 if geoms[vertical[0][0]].pins[vertical[0][1]].normal[1] > 0 else -1
        candidates = []
        for ref, pin in off:
            pg, ty = geoms[ref].pins[pin], tip(ref, pin)[1]
            if abs(pg.normal[1]) > .5:
                if pg.normal[1] * side > 0:
                    candidates.append(ty + side * gap)
            elif (ty - part_row.get(ref, ty)) * side > 0:
                candidates.append(ty)
        if not candidates:
            continue
        y = max(candidates) if side > 0 else min(candidates)
        xs = [tip(ref, pin)[0] for ref, pin in chain_pins]
        span = (min(xs), max(xs))
        wanted = "ground" if side > 0 else "supply"
        blocked = False
        for ref, item in sorted(attachments.items()):
            host_pin = net_pin(item.host, item.net) if item.host in row_pins else None
            if item.side != wanted or item.net == name or host_pin is None or item.host not in placed or ref not in by_ref:
                continue
            if not span[0] - _EPS <= tip(item.host, host_pin)[0] <= span[1] + _EPS:
                continue
            height = max(g.box[3] - g.box[1] for g in by_ref[ref].geometry.values())
            beyond = tip(item.host, host_pin)[1] + side * (height + 2 * gap)
            if over and (beyond - y) * side <= _EPS:
                continue
            blocked = True
            y = max(y, beyond) if side > 0 else min(y, beyond)
        if over and not blocked:
            continue
        found.append((name, side, y, span, bool(over)))
    rows = {}
    toward = {}
    taken = []
    for name, side, y, span, led in found:
        if led:
            toward[name] = "ground" if side < 0 else "supply"      # what hangs on that side faces the drop to the other part
        y = (math.ceil(y / GRID_MM - _EPS) if side > 0 else math.floor(y / GRID_MM + _EPS)) * GRID_MM
        while any(s == side and a[0] <= span[1] + _EPS and span[0] <= a[1] + _EPS and abs(y - other) < gap - _EPS
                  for s, other, a in taken):
            y += side * gap
        rows[name] = y
        taken.append((side, y, span))
    return rows, toward


def _stack_gate(index: int) -> int:
    """Which way the control pins of column ``index`` face: outward, so alternate columns are mirrored."""
    return -1 if index % 2 == 0 else 1


class _Stack(NamedTuple):
    """A stacked pair: the upper part's down pin and the lower part's up pin on one net, the net's sideways parts."""
    upper: str
    lower: str
    net: str
    upper_pin: str
    lower_pin: str
    hangs: tuple[str, ...]               # attachments hung from the junction wire (their host is a pair member)


def _stack_pairs(by_ref, pins_by_net, roles, chains):
    """Find the stacked pairs: ``(stacks left to right, spans)`` with ``spans`` as ``{load: (left net, right net)}``.

    A net joining two chain parts of three pins or more whose pins on it face each other across rows (upright, one
    pin down and the other up, both on the part's through axis) is a junction between a stacked pair.  People draw a
    half bridge, a totem pole or a cascode as a vertical stack, upper part over lower, the junction a straight wire
    between the tips with the load leaving it sideways; a row each would give the junction no trunk, since a drop cannot
    reach a pin that faces away.  A two-pin part joining the junction nets of two neighbouring pairs is a span between them."""
    rails = roles.rails
    chain_refs = {ref for chain in chains.values() for ref in chain}
    block_of = {chain[0]: block for block, chain in chains.items() if len(chain) == 1}
    # a stacked part may be the first or last of a longer chain (the rest of the chain goes on without it)
    at_end = {ref: block for block, chain in chains.items() for ref in chain if len(chain) == 1 or ref in (chain[0], chain[-1])}
    rank = {block: index for index, block in enumerate(chains)}
    barred = {ref for pair in roles.pairs for ref in pair}
    barred |= {lane.hub for lane in (getattr(roles, "lanes", None) or {}).values()}
    ruled_nets = {net for item in roles.rungs.values() for net in (item.net_a, item.net_b)}
    ruled_nets |= {net for item in roles.feedback.values() for net in (item.net_from, item.net_to)}
    eligible: dict[str, set[str]] = defaultdict(set)
    for name, pins in pins_by_net.items():
        if name not in rails:
            for ref, pin in pins:
                eligible[ref].add(pin)
    found: list[_Stack] = []
    taken: set[str] = set()
    for net_name in sorted(pins_by_net):
        if net_name in rails or net_name in ruled_nets:
            continue
        pins = pins_by_net[net_name]
        # the parts the junction is drawn for: multi-pin chain parts, hubs (six pins or more) not counted, whose pins on the net
        # all face one way along the part's axis (a power FET's three source pins on one net are one junction)
        big = sorted({ref for ref, _p in pins if ref in chain_refs and _is_multi_pin(by_ref[ref])
                      and _upright(by_ref[ref]) is not None and len(_upright(by_ref[ref]).pins) < 6})
        if len(big) != 2 or any(ref in taken or ref in barred or ref not in at_end for ref in big):
            continue
        on_pins = {ref: sorted(p for r, p in pins if r == ref) for ref in big}
        geoms = {ref: _upright(by_ref[ref]) for ref in big}
        if any(p not in geoms[ref].pins for ref in big for p in on_pins[ref]):
            continue
        on_net = {ref: on_pins[ref][0] for ref in big}
        if any(_pin_roles(by_ref[ref], eligible[ref]).get(p) not in ("in", "out") for ref in big for p in on_pins[ref]):
            continue

        def facing(ref):
            normals = {(round(geoms[ref].pins[p].normal[0]), round(geoms[ref].pins[p].normal[1])) for p in on_pins[ref]}
            return normals.pop()[1] if len(normals) == 1 else 0

        down = [ref for ref in big if facing(ref) > 0]
        up = [ref for ref in big if facing(ref) < 0]
        if len(down) != 1 or len(up) != 1:
            continue
        upper, lower = down[0], up[0]
        hangs, ok = [], True
        for ref, item in sorted(roles.attachments.items()):
            if item.host in (upper, lower):
                if item.net == net_name:
                    hangs.append(ref)
                else:
                    ok = False
            elif item.net == net_name:
                ok = False
        if not ok:
            continue
        taken |= {upper, lower}
        found.append(_Stack(upper, lower, net_name, on_net[upper], on_net[lower], tuple(hangs)))
    if not found:
        return [], {}, {}
    found.sort(key=lambda s: (rank[at_end[s.upper]], s.net))
    stack_of = {s.net: index for index, s in enumerate(found)}
    loads: dict[str, tuple[int, int]] = {}
    for ref in sorted(block_of):
        if ref in taken or ref in barred or _is_multi_pin(by_ref[ref]):
            continue
        nets = [name for name, pins in sorted(pins_by_net.items()) for r, _p in pins if r == ref]
        if len(nets) == 2 and all(name in stack_of for name in nets) and nets[0] != nets[1]:
            loads[ref] = (stack_of[nets[0]], stack_of[nets[1]])
    # neighbours are the pairs a load joins, so that the load stands between two columns
    order = [0]
    remaining = list(range(1, len(found)))
    while remaining:
        last = order[-1]
        partner = next((i for i in remaining if any({last, i} == set(pair) for pair in loads.values())), remaining[0])
        remaining.remove(partner)
        order.append(partner)
    position = {index: place for place, index in enumerate(order)}
    stacks = [found[index] for index in order]
    spans: dict[str, tuple[str, str]] = {}
    between: set[int] = set()
    for ref, (a, b) in sorted(loads.items()):
        first, second = sorted((position[a], position[b]))
        if second - first != 1 or first in between or stacks[first].hangs or stacks[second].hangs:
            continue
        between.add(first)
        spans[ref] = (stacks[first].net, stacks[second].net)
    # A two-pin part alone on the net of a stacked part's control pin (a gate resistor) feeds it: it stands beside the pin.
    feeders: dict[str, tuple[str, str]] = {}
    for item in stacks:
        for ref in (item.upper, item.lower):
            upright = _upright(by_ref[ref])
            controls = [p for p, role in _pin_roles(by_ref[ref], set(upright.pins)).items() if role == "control"]
            for pin in sorted(controls):
                if abs(upright.pins[pin].normal[0]) < .5:
                    continue
                for net_name, pins in pins_by_net.items():
                    if net_name in rails or len(pins) != 2 or (ref, pin) not in pins:
                        continue
                    other = next(r for r, _p in pins if r != ref) if len({r for r, _p in pins}) == 2 else None
                    if (other in block_of and other not in taken and other not in barred and other not in spans
                            and other not in feeders and not _is_multi_pin(by_ref[other])):
                        feeders[other] = (ref, pin)
    return stacks, spans, feeders


def _place_stacks(stacks, spans, by_ref, pins_by_net, roles, gap, record, feeders=None):
    """Coordinates of the stacked pairs, relative to the first column's junction tip (its upper tip at y = 0).

    Columns stand left to right with their control pins facing outward (alternate columns mirrored); the lower part's up
    pin tip is straight below the upper's down pin tip, a wire's length apart; a span's part stands between two
    neighbouring columns with its tips on the junction wires; a hung part stands on the load side of the wire, its tip a
    stub's length from it."""
    def snap_up(value: float) -> float:
        return math.ceil(value / GRID_MM - _EPS) * GRID_MM

    def candidates(ref: str):
        return sorted(by_ref[ref].geometry.items(), key=lambda item: (item[0].rotation, item[0].mirror))

    def pick_axis(ref: str, pin: str, normal_y: int, gate: int):
        control = [p for p, role in _pin_roles(by_ref[ref], set(_upright(by_ref[ref]).pins)).items() if role == "control"]
        best = None
        for orientation, geometry in candidates(ref):
            pg = geometry.pins.get(pin)
            if pg is None or pg.normal[1] * normal_y < .5 or abs(pg.normal[0]) > .5:
                continue
            wrong = int(any(geometry.pins[p].normal[0] * gate < -.5 for p in control if p in geometry.pins))
            key = (wrong, orientation.rotation, orientation.mirror)
            if best is None or key < best[0]:
                best = (key, orientation, geometry)
        if best is None:
            raise ValueError(f"No stacked orientation for {ref}")
        return best[1], best[2]

    def net_pin(ref: str, net_name: str) -> str:
        return next(p for r, p in pins_by_net[net_name] if r == ref)

    def hang_orientation(ref: str, net_name: str, ground: bool):
        signal = net_pin(ref, net_name)
        best = None
        for orientation, geometry in candidates(ref):
            pg = geometry.pins.get(signal)
            if pg is None or abs(pg.normal[0]) > .5 or abs(pg.normal[1]) < .5:
                continue
            wrong = int((pg.normal[1] > 0) == ground)       # a ground-side part hangs below the wire: its signal pin points up
            key = (wrong, orientation.rotation, orientation.mirror)
            if best is None or key < best[0]:
                best = (key, orientation, geometry, signal)
        if best is None:
            raise ValueError(f"No vertical orientation for {ref}")
        return best[1], best[2], best[3]

    columns = []
    wire = 4 * GRID_MM          # room for a load's row between the tips: a grid-clear 2.54 mm above and below it
    for index, stack in enumerate(stacks):
        gate = _stack_gate(index)
        ou, gu = pick_axis(stack.upper, stack.upper_pin, 1, gate)
        ol, gl = pick_axis(stack.lower, stack.lower_pin, -1, gate)
        pu, pl_ = gu.pins[stack.upper_pin].point, gl.pins[stack.lower_pin].point
        cu, cl = _core(gu), _core(gl)
        wire = max(wire, gap + (cu[3] - pu[1]) - (cl[1] - pl_[1]))
        columns.append((stack, gate, ou, gu, ol, gl, pu, pl_, cu, cl))
    wire = math.ceil(wire / (2 * GRID_MM) - _EPS) * 2 * GRID_MM
    y_row = wire / 2

    feeders = feeders or {}
    infos = []        # per column: left reach, right reach (from the junction wire), hung parts as (ref, x, orientation, geometry, signal pin)
    for stack, gate, ou, gu, ol, gl, pu, pl_, cu, cl in columns:
        reach_left = max(pu[0] - cu[0], pl_[0] - cl[0])
        reach_right = max(cu[2] - pu[0], cl[2] - pl_[0])
        side = -gate                                     # the load side: away from the control pins
        edge = reach_right if side > 0 else -reach_left
        hung = []
        for ref in stack.hangs:
            orientation, geometry, signal = hang_orientation(ref, stack.net, roles.attachments[ref].side == "ground")
            core, tip = _core(geometry), geometry.pins[signal].point
            if side > 0:
                x = snap_up(edge + gap + (tip[0] - core[0]))
                edge = x + (core[2] - tip[0])
            else:
                x = -snap_up(-(edge - gap - (tip[0] - core[2])))
                edge = x - (tip[0] - core[0])
            hung.append((ref, x, orientation, geometry, signal))
        reach_left, reach_right = max(reach_left, -edge), max(reach_right, edge)
        # a feeder of a control pin stands beside the pin, on its side: its near tip a gap from the pin's tip
        fed = []
        for part, geometry, point, top in ((stack.upper, gu, pu, -pu[1]), (stack.lower, gl, pl_, wire - pl_[1])):
            for ref, (host, pin) in sorted(feeders.items()):
                if host != part:
                    continue
                gate_net = next(name for name, pins in pins_by_net.items() if (host, pin) in pins)
                gate = geometry.pins[pin]
                direction = 1 if gate.normal[0] > 0 else -1
                signal = net_pin(ref, gate_net)
                orientation, f_geometry = min(          # lying along the row, its pin facing the gate when an orientation has that
                    ((o, g) for o, g in candidates(ref) if signal in g.pins and abs(g.pins[signal].normal[1]) < .5),
                    key=lambda item: item[1].pins[signal].normal[0] * direction)
                f_core, f_tip = _core(f_geometry), f_geometry.pins[signal].point
                near = (gate.point[0] - point[0]) + direction * gap
                far = near + (f_core[2] - f_tip[0] if direction > 0 else -(f_tip[0] - f_core[0]))
                if direction > 0:
                    reach_right = max(reach_right, far)
                else:
                    reach_left = max(reach_left, -far)
                fed.append((ref, near - f_tip[0], top + gate.point[1] - f_tip[1], orientation, "right" if direction > 0 else "left",
                            f"{host}.{pin}", gate_net))
        infos.append((reach_left, reach_right, hung, fed))

    span_of = {}
    for ref, (net_a, net_b) in spans.items():
        span_of[next(i for i, c in enumerate(columns) if c[0].net == net_a)] = (ref, net_a, net_b)
    centres: list[float] = []
    span_place: dict[int, tuple[str, str, str, Orientation, tuple[float, float]]] = {}
    for index in range(len(columns)):
        if index == 0:
            centres.append(0.0)
            continue
        before, here = infos[index - 1], infos[index]
        if index - 1 in span_of:
            ref, net_a, net_b = span_of[index - 1]
            first_pin, second_pin = net_pin(ref, net_a), net_pin(ref, net_b)
            chosen = next(((o, g) for o, g in candidates(ref)
                           if first_pin in g.pins and g.pins[first_pin].normal[0] < -.5 and abs(g.pins[first_pin].normal[1]) < .5), None)
            if chosen is None:
                raise ValueError(f"No horizontal orientation for {ref}")
            orientation, geometry = chosen
            a_pt, b_pt = geometry.pins[first_pin].point, geometry.pins[second_pin].point
            left_tip = snap_up(centres[-1] + before[1] + gap)
            right_tip = left_tip + (b_pt[0] - a_pt[0])
            span_place[index - 1] = (ref, net_a, net_b, orientation, (left_tip - a_pt[0], y_row - a_pt[1]))
            centres.append(snap_up(right_tip + gap + here[0]))
        else:
            centres.append(snap_up(centres[-1] + before[1] + gap + here[0]))

    out: dict[str, Placement] = {}
    for index, (stack, gate, ou, gu, ol, gl, pu, pl_, cu, cl) in enumerate(columns):
        cx = centres[index]
        out[stack.upper] = Placement((cx - pu[0], -pu[1]), ou)
        out[stack.lower] = Placement((cx - pl_[0], wire - pl_[1]), ol)
        hung = infos[index][2]
        for ref, x, orientation, geometry, signal in hung:
            point = geometry.pins[signal].point
            out[ref] = Placement((cx + x - point[0], y_row - point[1]), orientation)
        for ref, x, y, orientation, _side, _host, _net in infos[index][3]:
            out[ref] = Placement((cx + x, y), orientation)
        if record is not None:
            span_rec = [(item[0], item[1], item[2], item[3]) for i, item in span_place.items() if i == index]
            record.pair_stacks.append(StackRecord(
                stack.upper, stack.lower, stack.net, stack.upper_pin, ou, ol,
                [(ref, "right" if x > 0 else "left", orientation) for ref, x, orientation, _g, _s in hung], span_rec,
                [(ref, net_, host_, side, orientation) for ref, _x, _y, orientation, side, host_, net_ in infos[index][3]]))
    for ref, _a, _b, orientation, anchor in span_place.values():
        out[ref] = Placement(anchor, orientation)
    for ref, placement in out.items():
        for value in placement.position:
            if abs(round(value / GRID_MM) * GRID_MM - value) > 1e-6:
                raise ValueError(f"Stacked part {ref} is off the grid")
    return out


def _plan_blocks(by_ref, pins_by_net, roles, chains):
    """Order the blocks, group them into chain rows and find the blocks that hang from a control pin.

    Returns ``(rows, hangs, lone, stacks)``: per row the block names left to right, per row the hanging blocks as
    ``{block: (host part, host pin, net)}``, the blocks whose parts meet the rest of the circuit by rails alone, and the
    rows that stack over a part's off-row pin as ``{row index: (host part, host pin, net)}``.

    A net joining blocks orders them.  A multi-pin part's through pins say which side a neighbour is on (what meets
    the input pin comes before the part, what meets the output pin after); a block whose last part meets a control
    pin hangs from it.  Otherwise a chain's last part meeting the next chain's first orders them, then the
    single-part rule (drawn flow; a part with no direction of its own follows the block it touches), then the order
    of the roles.  Blocks follow one another on a row only when an ordering net joins the row's last block to them;
    any other block starts a new row."""
    rails = roles.rails
    eligible: dict[str, set[str]] = defaultdict(set)
    for name, pins in pins_by_net.items():
        if name not in rails:
            for ref, pin in pins:
                eligible[ref].add(pin)
    part_roles = {ref: _pin_roles(by_ref[ref], eligible[ref]) for chain in chains.values() for ref in chain}
    rank = {block: index for index, block in enumerate(chains)}
    block_of = {ref: block for block, chain in chains.items() for ref in chain}

    def through_chained(ref: str) -> bool:
        """Both through pins of the part (in and out) meet another chain part: the power or signal path runs
        straight through it and its control pin takes a hung feeder."""
        linked = set()
        for name, pin_list in pins_by_net.items():
            if name in rails:
                continue
            for r, pin in pin_list:
                if r == ref and part_roles[ref].get(pin) in ("in", "out") and any(
                        other != ref and other in block_of for other, _p in pin_list):
                    linked.add(part_roles[ref][pin])
        return linked >= {"in", "out"}

    # A connector in a longer chain meets the circuit through its chain pin; its other pins fan out and say nothing
    # of the order of the blocks.
    fan_out: set[tuple[str, str]] = set()
    for chain in chains.values():
        for index, ref in enumerate(chain):
            if len(chain) > 1 and _is_one_sided(by_ref[ref]):
                neighbours = set(chain[max(index - 1, 0):index] + chain[index + 1:index + 2])
                for name, pin_list in pins_by_net.items():
                    if any(r == ref for r, _p in pin_list) and not any(r in neighbours for r, _p in pin_list):
                        fan_out.update((r, p) for r, p in pin_list if r == ref)

    hangs: dict[str, tuple[str, str, str]] = {}
    decided: set[frozenset] = set()
    edges: dict[str, set[str]] = {block: set() for block in chains}
    links: set[tuple[str, str]] = set()

    for net_name, pin_list in pins_by_net.items():
        if net_name in rails:
            continue
        pin_list = [entry for entry in pin_list if entry not in fan_out]
        touching = sorted({
            (block_of[ref], chains[block_of[ref]].index(ref), ref, pin) for ref, pin in pin_list if ref in block_of
        })
        bus = len({block for block, _i, _r, _p in touching}) >= 3
        for a, ia, ref_a, pin_a in touching:
            for b, ib, ref_b, pin_b in touching:
                if a == b:
                    continue
                role_a, role_b = part_roles[ref_a].get(pin_a), part_roles[ref_b].get(pin_b)
                if (role_a == "control" and role_b in ("in", "out") and ia == 0 and ib == len(chains[b]) - 1
                        and not through_chained(ref_a)):
                    # A through pin of one part meets the control pin of the next stage: the path continues on the row.
                    decided.add(frozenset((a, b)))
                    if a not in edges[b]:
                        edges[b].add(a)
                    continue
                if role_a is None or role_b is not None:
                    continue
                if role_a == "control":
                    if ib != len(chains[b]) - 1:
                        continue
                    if through_chained(ref_a):
                        if b not in hangs:
                            hangs[b] = (ref_a, pin_a, net_name)
                            decided.add(frozenset((a, b)))
                    else:
                        # A part the path does not run straight through (a transistor with one axis pin on a rail or a
                        # support) is entered by its control pin, as people draw a stage: the feeder is on the row before it.
                        decided.add(frozenset((a, b)))
                        if b not in edges[a]:
                            edges[b].add(a)
                else:
                    decided.add(frozenset((a, b)))
                    first, second = (b, a) if role_a == "in" else (a, b)
                    if bus and rank[first] > rank[second]:
                        # a net shared by three blocks or more feeds some and is only a branch for others: the
                        # order of the roles says which, not the pin
                        continue
                    if first not in edges[second]:
                        edges[first].add(second)
    # A block cannot hang from a block that hangs, nor from itself.
    for block in sorted(hangs):
        if block_of[hangs[block][0]] in hangs or block_of[hangs[block][0]] == block:
            del hangs[block]
    for net_name, pin_list in pins_by_net.items():
        if net_name in rails:
            continue
        pin_list = [entry for entry in pin_list if entry not in fan_out]
        endpoints = sorted({
            (block_of[ref], chains[block_of[ref]].index(ref)) for ref, _pin in pin_list if ref in block_of
        })
        for a, ia in endpoints:
            for b, ib in endpoints:
                if a == b or frozenset((a, b)) in decided or a in edges[b] or b in edges[a]:
                    continue
                if len(chains[a]) > 1 and ia == len(chains[a]) - 1 and ib == 0:
                    edges[a].add(b)
                elif len(chains[a]) == len(chains[b]) == 1:
                    ref_a, ref_b = chains[a][0], chains[b][0]
                    pin_a = next(pin for ref, pin in pin_list if ref == ref_a)
                    pin_b = next(pin for ref, pin in pin_list if ref == ref_b)
                    na, nb = _drawn_flow(by_ref[ref_a], pin_a), _drawn_flow(by_ref[ref_b], pin_b)
                    if na > 0 and nb < 0 and not (rank[a] > rank[b] and not _is_multi_pin(by_ref[ref_a])):
                        # (a two-pin part lies as it was found, so its drawn flow says nothing against the order of the roles)
                        edges[a].add(b)
                    elif not (na and nb and na != nb) or (na > 0 and nb < 0):
                        # (a two-pin part's flow against the order of the roles is no evidence: the two are neighbours)
                        links.add(tuple(sorted((a, b))))
                elif ia == len(chains[a]) - 1 and ib == 0 and rank[a] < rank[b]:
                    edges[a].add(b)

    for block in hangs:
        edges[block] = set()
    for after in edges.values():
        after.difference_update(hangs)

    # A block whose net meets a part at a pin that points up or down stacks on that side of the part when the part is
    # already placed on the main row by the block before it: people keep the path on one line and stack a part's load
    # above it.  A part the path runs straight through is turned onto the row instead and has no such pin.
    stacks: dict[str, tuple[str, str, str]] = {}
    for net_name, pin_list in sorted(pins_by_net.items()):
        if net_name in rails:
            continue
        for host_ref, host_pin in sorted(pin_list):
            if host_ref not in block_of or part_roles[host_ref].get(host_pin) not in ("in", "out") or through_chained(host_ref):
                continue
            upright = _upright(by_ref[host_ref])
            normal = upright.pins[host_pin].normal if upright is not None and host_pin in upright.pins else (0.0, 0.0)
            if abs(normal[1]) < .5 or abs(normal[0]) > .5 or block_of[host_ref] in hangs:
                continue
            for other_ref, other_pin in sorted(pin_list):
                stack = block_of.get(other_ref)
                if (stack is None or stack == block_of[host_ref] or stack in hangs or stack in stacks
                        or part_roles[other_ref].get(other_pin) is not None or block_of[host_ref] in stacks
                        or not any(block_of[host_ref] in after for before, after in edges.items() if before != stack)):
                    continue
                stacks[stack] = (host_ref, host_pin, net_name)
    for block in stacks:
        edges[block] = set()
        links -= {pair for pair in links if block in pair}
    for after in edges.values():
        after.difference_update(stacks)

    def ordered(block: str) -> bool:
        return bool(edges[block]) or any(block in e for e in edges.values())

    # A part with no drawn direction of its own follows the block it touches: once one block of a
    # link is ordered, the other comes after it (a filter inductor then its load, not the reverse).
    changed = True
    while changed:
        changed = False
        for a, b in sorted(links):
            for first, second in ((a, b), (b, a)):
                if (first not in hangs and second not in hangs and ordered(first) and not ordered(second)
                        and second not in edges[first] and first not in edges[second]):
                    edges[first].add(second)
                    changed = True
                    break
    live = [block for block in chains if block not in hangs and block not in stacks]
    indegree = {block: 0 for block in live}
    for block in live:
        for after in edges[block]:
            if after in indegree:
                indegree[after] += 1
    ready = sorted((block for block, degree in indegree.items() if degree == 0), key=rank.get)
    order: list[str] = []
    while ready:
        block = ready.pop(0)
        order.append(block)
        for after in sorted(edges[block]):
            if after in indegree:
                indegree[after] -= 1
                if indegree[after] == 0:
                    ready.append(after)
                    ready.sort(key=rank.get)
    order.extend(sorted(set(live) - set(order), key=rank.get))

    def rail_only(block: str) -> bool:
        """The block's parts reach the rest of the circuit through rails alone."""
        names = [name for name, pins in pins_by_net.items() for ref, _pin in pins if ref in chains[block]]
        return bool(names) and all(name in rails for name in names)

    related = {block: ordered(block) or any(block in pair for pair in links) for block in live}
    lone = [block for block in order if not related[block] and rail_only(block)]
    rows: list[list[str]] = []
    for block in order:
        if block in lone:
            continue
        for row in rows:
            if block in edges[row[-1]] or tuple(sorted((row[-1], block))) in links:
                row.append(block)
                break
        else:
            rows.append([block])
    row_of = {block: index for index, row in enumerate(rows) for block in row}
    row_hangs: list[dict[str, tuple[str, str, str]]] = [dict() for _ in rows]
    for block, (host, pin, net_name) in sorted(hangs.items()):
        if block_of[host] in row_of:
            row_hangs[row_of[block_of[host]]][block] = (host, pin, net_name)
        else:
            rows.append([block])
            row_hangs.append({})
    row_stacks: dict[int, tuple[str, str, str]] = {}
    for block, (host, pin, net_name) in sorted(stacks.items()):
        if block_of[host] in row_of:
            row_stacks[len(rows)] = (host, pin, net_name)
        rows.append([block])
        row_hangs.append({})
    return rows, row_hangs, lone, row_stacks


def _place_group(
    symbols: Sequence[PlacedSymbol],
    nets: Sequence[Connection],
    roles: "Roles",
    order: Sequence[str],
    hangs: Mapping[str, tuple[str, str, str]],
    *,
    text_widths: Mapping[str, float],
    gap_mm: float = 2.54,
    slot_pitch_mm: float = 7.62,
    stack_net: str | None = None,
    widen: Mapping[str, float] | None = None,
    record: GroupRecord | None = None,
    partial: bool = False,
) -> dict[str, Placement]:
    """Place one chain row with everything that hangs from it.

    ``order`` names the blocks laid on the row, left to right; ``hangs`` maps a block that feeds a control pin
    to ``(host part, host pin, net)``: it is not on the row but stands as a column from that pin.

    The chain runs through a multi-pin part by its through pins (a pair facing opposite ways: drain/source,
    input/output); a pin off that axis (gate, base, adjust) is a control pin and never a chain pin.  People keep
    the power or signal path straight through a device and hang its control.  The block feeding a through pin is
    on the row beside the part; the block feeding a control pin stands in a column from that pin, perpendicular to
    the row on the side the pin points to, its last part nearest the pin and each part upright, and nothing else
    is placed in that column.  A control pin that carries such a column points down when an orientation allows.

    Available geometry keys are the only legal orientations.  Chain parts follow each role
    chain; single-part blocks are ordered by drawn flow (right or down is out, left or up is in); two-pin chain parts lie horizontally with the earlier net on the left.  Multi-pin
    chain parts take the available orientation whose chain-input pin points left and chain-output
    pin points right (the chain then runs left to right through the part, as people draw a
    transistor), else one where either does, else rotation zero, mirrored-y when the upright
    input points right and output left; ties go to the lowest rotation, then no mirror.  Attachments and rungs stand vertically;
    attachment rail pins point down for ground and up for supply.  Feedback parts lie above
    the supply row, pairs that follow one another in a chain face each other (the second is the mirror of the first,
    control pins outward, the shared net one straight wire with its hung part at the midpoint), other pairs sit side
    by side, and other parts are left to the caller.  A part entered at a control pin stays on the main row, whatever
    height the part before it leaves at; a block that stacks over a part's up- or down-pointing pin is placed by
    ``ladder_layout``, not here (``stack_net`` names the net its last part brings to the pin).

    Trunk rows follow chain order below y=0, separated by the tallest intervening body plus
    two gaps or the rung pin spacing. The input tip anchors a multi-pin part on the incoming row and the row steps to its output tip (a source feeding the row stands upright at its left end, signal pin up on the row);
    terminal feedback nets identify an output even when its upright pin points left.
    Columns are a gap apart, or a gap plus a slot pitch per hung part plus a gap when parts hang from the net between them. Rungs are placed first so attachments can reject foreign
    routes crossing their bodies or pin leads. Attachment slots start at a host pin facing
    the row or beyond its facing box edge, then advance by slot pitch; grown boxes and
    neighbouring pin tips keep them clear. Rungs anchor at their upper tip, allowing a
    lower wire extension. Feedback spans its net columns above the supply area. Standalone
    pairs use upright adjacent slots; members already in a chain retain their chain slots.

    A net joining one block's last chain part to the next block's first is a chain net (no trunk row) and its
    host's support columns are reserved before that next part.  A part hangs from the pin it serves: its row is the
    y of the host's multi-pin chain pin on the net (a trunk keeps its row); a pin pointing at the part gets the
    first slot at its x, standing off along the pin by whole 2.54 mm steps until the boxes clear.

    Every anchor is snapped to the 1.27 mm
    grid.  A final box check raises ``ValueError`` naming any overlapping placed pair.
    """
    if gap_mm < 0 or slot_pitch_mm <= 0:
        raise ValueError("gap_mm must be non-negative and slot_pitch_mm must be positive")
    rec = record if record is not None else NoRecord()
    by_ref = {symbol.reference: symbol for symbol in symbols}
    if len(by_ref) != len(symbols):
        raise ValueError("PlacedSymbol references must be unique")
    net_by_name = {net.net: net for net in nets}
    pins_by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net in sorted(nets, key=lambda n: n.net):
        for ref, pin in sorted(net.pins):
            pins_by_net[net.net].append((ref, pin))

    def box_at(ref: str, geom: OrientedGeometry, anchor: tuple[float, float]):
        l, t, r, b = geom.box
        x, y = anchor
        return (x + l, y + t, x + r, y + b)

    def core_at(ref: str, geom: OrientedGeometry, anchor: tuple[float, float]):
        l, t, r, b = _core(geom)
        return (anchor[0] + l, anchor[1] + t, anchor[0] + r, anchor[1] + b)

    def boxes_overlap(a, b):
        return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS

    def snap(value: float) -> float:
        return round(value / GRID_MM) * GRID_MM

    def geom_choice(ref: str, predicate):
        symbol = by_ref[ref]
        candidates = sorted(symbol.geometry.items(), key=lambda item: (item[0].rotation, item[0].mirror))
        accepted = [(orientation, geometry) for orientation, geometry in candidates if predicate(orientation, geometry)]
        if accepted:
            return accepted[0]
        raise ValueError(f"No available orientation for {ref}")

    def net_pin(ref: str, net_name: str) -> str | None:
        choices = sorted(pin for r, pin in pins_by_net.get(net_name, ()) if r == ref)
        return choices[0] if choices else None

    feedback_nets = {net for item in roles.feedback.values() for net in (item.net_from, item.net_to)}
    role_cache: dict[str, dict[str, str]] = {}

    def pin_roles(ref: str) -> dict[str, str]:
        if ref not in role_cache:
            used = {pin for name, pins in pins_by_net.items() if name not in roles.rails for r, pin in pins if r == ref}
            role_cache[ref] = _pin_roles(by_ref[ref], used)
        return role_cache[ref]

    def infer_terminal_pin(
        ref: str, excluded_net: str | None, horizontal_direction: int, *, prefer_feedback: bool = False,
    ) -> str | None:
        """Infer a missing chain-end pin from non-rail connectivity and its upright direction."""
        symbol = by_ref[ref]
        upright = symbol.geometry.get(Orientation(0, ""))
        if upright is None:
            upright = symbol.geometry.get(symbol.orientation)
        if upright is None:
            return None
        candidates = []
        feedback_candidates = []
        for name, pins in pins_by_net.items():
            if name in roles.rails or name == excluded_net:
                continue
            pin = net_pin(ref, name)
            if pin is None or pin not in upright.pins or pin_roles(ref).get(pin) == "control":
                continue
            if name in feedback_nets:
                feedback_candidates.append(name)
            if upright.pins[pin].normal[0] * horizontal_direction > _EPS:
                candidates.append(name)
        if prefer_feedback and feedback_candidates:
            candidates = feedback_candidates
        elif not candidates:
            candidates = feedback_candidates
        if candidates:
            candidates.sort()
            return net_pin(ref, candidates[0])
        return None

    def sharing_nets(a: str, b: str) -> list[str]:
        return sorted((net for net, pins in pins_by_net.items()
                       if net not in roles.rails and any(r == a for r, _ in pins) and any(r == b for r, _ in pins)),
                      key=lambda name: (len(pins_by_net[name]), name))

    def sharing_net(a: str, b: str) -> str | None:
        shared = sharing_nets(a, b)
        return shared[0] if shared else None

    all_chains = {block: tuple(chain) for block, chain in roles.chains.items()}
    chains = {block: all_chains[block] for block in order}
    hung_chains = {block: all_chains[block] for block in sorted(hangs)}
    block_order = list(chains)
    chain_parts = {ref for chain in all_chains.values() for ref in chain}
    missing_chain_symbols = sorted(chain_parts - by_ref.keys())
    if missing_chain_symbols:
        raise ValueError(f"Role symbols are missing from placement input: {', '.join(missing_chain_symbols)}")
    attachments = roles.attachments
    rungs = roles.rungs
    feedback = roles.feedback
    pairs = tuple(sorted(tuple(sorted(pair)) for pair in roles.pairs))
    lane_roles = [lane for _key, lane in sorted((getattr(roles, "lanes", None) or {}).items())
                  if lane.hub in chain_parts and lane.hub in by_ref]
    requested = chain_parts | set(attachments) | set(rungs) | set(feedback) | {r for pair in pairs for r in pair}
    host_refs = {item.host for item in attachments.values()}
    missing_symbols = sorted((requested | host_refs) - by_ref.keys())
    if missing_symbols:
        raise ValueError(f"Role symbols are missing from placement input: {', '.join(missing_symbols)}")
    placed: dict[str, Placement] = {}
    left_out: set[str] = set()          # parts a partial plan found no slot for
    geoms: dict[str, OrientedGeometry] = {}
    rows: dict[str, float] = {}
    net_columns: dict[str, list[float]] = defaultdict(list)

    def lane_width(ref: str, spans: bool) -> float:
        """The length a lane part takes along the lane: its width lying down (a series part), or standing up (a span)."""
        widths = [g.box[2] - g.box[0] for g in by_ref[ref].geometry.values()]
        return (min(widths) if spans else max(widths)) + 2 * GRID_MM if widths else 0.0

    def lane_room(hub: str, geometry: OrientedGeometry, side: int, row_pin_names) -> float:
        """The room a hub's lanes on one side (``side`` +1 right, -1 left) need beside it: the longest lane's parts end to end
        and a slot pitch for what hangs from its end."""
        longest = 0.0
        for lane in lane_roles:
            pg = geometry.pins.get(lane.pin) if lane.hub == hub else None
            if pg is None or pg.normal[0] * side < .5 or lane.pin in row_pin_names:
                continue
            same_side = {name for name, pins in pins_by_net.items() if name not in roles.rails
                         for r, pin in pins if r == hub and pin in geometry.pins and geometry.pins[pin].normal[0] * side > .5}
            length = 0.0
            for index_, part in enumerate(lane.series):
                length += lane_width(part, lane.nets[index_ + 1] in same_side and lane.nets[index_ + 1] != lane.net)
            if any(a.net in lane.nets for a in attachments.values()):
                length += slot_pitch_mm
            longest = max(longest, length)
        return longest

    # Each block gets its own chain row; blocks share the page by following one another horizontally.
    terminal_chain_nets: set[str] = set()
    row_pins: dict[str, set[str]] = {}       # per multi-pin chain part, the pins that are on the chain row
    chain_net_y: dict[str, float] = {}
    pair_mid: dict[str, float] = {}          # a net two paired parts face each other across: the x between their tips
    x_block = 0.0
    main_y = 0.0          # the row the chain's input pins lie on: a pin the chain leaves by, off the row, does not move it
    row_y = 0.0           # where the chain row runs now: it steps where a part's output pin is off its input pin
    after_source = False
    for block_index, (block, chain) in enumerate(chains.items()):
        if not chain:
            continue
        prev_right = x_block
        for index, ref in enumerate(chain):
            if ref not in by_ref:
                continue
            symbol = by_ref[ref]
            earlier = sharing_net(chain[index - 1], ref) if index else None
            later = sharing_net(ref, chain[index + 1]) if index + 1 < len(chain) else None
            interface_host = None
            if earlier is None and index == 0:
                for previous in reversed(block_order[:block_index]):
                    other_chain = chains[previous]
                    if other_chain and (earlier := sharing_net(other_chain[-1], ref)) is not None:
                        interface_host = other_chain[-1]
                        break
            if later is None and index == len(chain) - 1:
                for following in block_order[block_index + 1:]:
                    other_chain = chains[following]
                    if other_chain and (later := sharing_net(ref, other_chain[0])) is not None:
                        break
            if later is None and index == len(chain) - 1 and block_index == len(chains) - 1 and stack_net is not None:
                # a stacked group's net leaves its last part towards the part it stacks over, on the right
                pin = net_pin(ref, stack_net)
                if pin is not None and any(pin in g.pins and g.pins[pin].normal[0] > .5 for g in symbol.geometry.values()):
                    later = stack_net
            in_pin, out_pin = net_pin(ref, earlier) if earlier else None, net_pin(ref, later) if later else None
            if in_pin is not None and in_pin == out_pin:
                # one net fed the part and would also carry it on (a supply shared by several blocks): the part is
                # only entered by it
                later = out_pin = None
            multi = max(len(geometry.pins) for geometry in symbol.geometry.values()) > 2
            if multi:
                # The chain enters and leaves a part through its through pins; a control pin is never one of them.
                if (index == 0 and in_pin is not None and pin_roles(ref).get(in_pin) == "control"
                        and any(host == ref for host, _pin, _net in hangs.values())):
                    earlier = in_pin = interface_host = None
                if index == len(chain) - 1 and out_pin is not None and pin_roles(ref).get(out_pin) == "control":
                    later = out_pin = None
            terminal_input_net = terminal_output_net = None
            if multi and in_pin is None and not (index == 0 and out_pin is not None and _is_one_sided(symbol)):
                in_pin = infer_terminal_pin(ref, later, -1)
                if in_pin is not None:
                    terminal_input_net = next(name for name in pins_by_net
                                              if name not in roles.rails and net_pin(ref, name) == in_pin)
            if multi and out_pin is None and not (index == len(chain) - 1 and in_pin is not None and _is_one_sided(symbol)):
                out_pin = infer_terminal_pin(ref, earlier or terminal_input_net, 1, prefer_feedback=True)
                if out_pin is not None:
                    terminal_output_net = next(name for name in pins_by_net
                                               if name not in roles.rails and net_pin(ref, name) == out_pin)
            terminal_chain_nets.update(name for name in (terminal_input_net, terminal_output_net) if name)

            # A connector or header has all its pins on one side: it stands at the end of the row with the pins towards
            # the circuit (an input connector's pins point right, an output connector's left).
            one_sided = _is_one_sided(symbol)
            at_start = index == 0 and out_pin is not None and in_pin is None
            at_end = index == len(chain) - 1 and in_pin is not None and out_pin is None

            def faces_circuit(g: OrientedGeometry) -> bool:
                chain_pin = out_pin if at_start else in_pin
                if chain_pin not in g.pins:
                    return False
                nx = g.pins[chain_pin].normal[0]
                return nx > .5 if at_start else nx < -.5

            def rails_beyond_chain_pin(g: OrientedGeometry) -> bool:
                """A ground pin lies below the pin on the row, a supply pin above it."""
                chain_pin = out_pin if at_start else in_pin
                for name, kind in roles.rails.items():
                    for r_, pin_ in pins_by_net.get(name, ()):
                        if r_ == ref and pin_ in g.pins and chain_pin in g.pins and g.pins[pin_].point[1] != g.pins[chain_pin].point[1]:
                            if (g.pins[pin_].point[1] > g.pins[chain_pin].point[1]) != (kind != "supply"):
                                return False
                return True

            rails_fit = any(faces_circuit(g) and rails_beyond_chain_pin(g) for g in symbol.geometry.values())

            def flow_score(g: OrientedGeometry) -> int:
                """0 when input points left and output right, 1 when only one does, else 2."""
                if one_sided and (at_start or at_end):
                    return 0 if faces_circuit(g) and rails_beyond_chain_pin(g) else 1 if faces_circuit(g) else 2
                left_in = in_pin in g.pins and g.pins[in_pin].normal[0] < -.5
                right_out = out_pin in g.pins and g.pins[out_pin].normal[0] > .5
                return 0 if left_in and right_out else 1 if left_in or right_out else 2

            if multi:
                upright = symbol.geometry.get(Orientation(0, ""))
                if upright is None:
                    mirror_y = False
                elif in_pin in upright.pins and out_pin in upright.pins:
                    mirror_y = upright.pins[in_pin].normal[0] > .5 and upright.pins[out_pin].normal[0] < -.5
                else:
                    mirror_y = False
                # Turn the part so the chain runs left to right through it; a transistor drawn with
                # drain above source would otherwise run the chain vertically through its own body.
                best_flow = min((flow_score(g) for g in symbol.geometry.values()), default=2)
            else:
                mirror_y = False
                best_flow = 2

            rail_pins = sorted(pin for name in roles.rails for r, pin in pins_by_net.get(name, ()) if r == ref)
            signal_pins = sorted(pin for name, pins in pins_by_net.items() if name not in roles.rails
                                 for r, pin in pins if r == ref)
            # A source feeding the row stands upright at its left end: signal pin up onto the row, rail pin down.
            source_pins = None
            if not multi and index == 0 and is_source_reference(ref):
                if len(rail_pins) == 1 and len(signal_pins) == 1:
                    source_pins = (signal_pins[0], rail_pins[0])
                elif out_pin is not None and len(symbol_pins := sorted(
                        {pin for g in symbol.geometry.values() for pin in g.pins})) == 2 and out_pin in symbol_pins:
                    # no rail pin of its own: the chain pin goes up onto the row, the other pin down (what hangs
                    # from it, a sense resistor, stands under it)
                    source_pins = (out_pin, next(pin for pin in symbol_pins if pin != out_pin))

            def source_candidate(o, g):
                signal, rail = g.pins.get(source_pins[0]), g.pins.get(source_pins[1])
                return (signal is not None and rail is not None and signal.normal[1] < -.5 and rail.normal[1] > .5
                        and abs(signal.point[0] - rail.point[0]) <= GRID_MM + _EPS)

            source_upright = source_pins is not None and any(
                source_candidate(o, g) for o, g in symbol.geometry.items())

            def chain_candidate(o, g):
                if source_upright:
                    return source_candidate(o, g)
                if one_sided and at_end and multi:
                    # a connector at the end of a row has its pins toward the circuit (an output connector's point left)
                    return faces_circuit(g) and (rails_beyond_chain_pin(g) or not rails_fit)
                if multi:
                    if best_flow < 2:
                        return flow_score(g) == best_flow
                    return o.rotation == 0 and o.mirror == ("y" if mirror_y else "")
                if one_sided and (at_start or at_end):
                    return faces_circuit(g) and (rails_beyond_chain_pin(g) or not rails_fit)
                if in_pin in g.pins and out_pin in g.pins:
                    pi, po = g.pins[in_pin], g.pins[out_pin]
                    return (abs(pi.point[1] - po.point[1]) <= _EPS
                            and pi.point[0] < po.point[0] - _EPS
                            and pi.normal[0] < -_EPS and po.normal[0] > _EPS)
                pins = sorted(g.pins.items(), key=lambda item: item[0])
                if len(pins) == 2:
                    (name_a, pa), (name_b, pb) = pins
                    left_pin, right_pin = (name_a, name_b) if pa.point[0] < pb.point[0] else (name_b, name_a)
                    left, right = g.pins[left_pin], g.pins[right_pin]
                    horizontal = (abs(left.point[1] - right.point[1]) <= _EPS
                                  and left.point[0] < right.point[0] - _EPS
                                  and left.normal[0] < -_EPS and right.normal[0] > _EPS)
                    if not horizontal:
                        return False
                    if out_pin is not None:
                        return out_pin == right_pin
                    if in_pin is not None:
                        return in_pin == left_pin
                    return True
                return o.rotation in (0, 180)

            hang_pins = [pin for host, pin, _net in hangs.values() if host == ref]
            try:
                orientation, geom = geom_choice(ref, lambda o, g: chain_candidate(o, g) and all(
                    pin in g.pins and g.pins[pin].normal[1] > .5 for pin in hang_pins))
            except ValueError:
                orientation, geom = geom_choice(ref, chain_candidate)
            facing = _facing_choice(by_ref, geoms, placed, chain, index, earlier, pairs, multi, geom_choice, net_pin, pin_roles)
            if facing is not None:
                orientation, geom = facing
            # The chain enters a part on its input pin and leaves from the output pin at that pin's own height: the row
            # steps there and later parts follow it.
            # A chain pin that points up or down while the other chain pin faces along the row is not on the row: the
            # part is entered through its output pin's row and the net on the other pin leaves it as a side row.
            in_off_row = (multi and in_pin in geom.pins and out_pin in geom.pins
                          and abs(geom.pins[in_pin].normal[0]) < .5 and geom.pins[out_pin].normal[0] > .5)
            row_pin = in_pin if in_pin in geom.pins and not in_off_row else out_pin
            py = geom.pins[row_pin].point[1] if row_pin in geom.pins else 0.0
            left, _top, right, _bottom = geom.box
            text_gap = gap_mm
            if index > 0 or interface_host is not None:
                # The room between two chain parts (or across a block interface, the same thing) is what the parts hung
                # from the net between them need: their slots, on whichever side has more, plus a gap either side.  The
                # text of a horizontal part sits above or below it and a hung vertical part's text sits beside it,
                # which the slot pitch already allows for: no width is reserved for text.
                between = earlier
                above = below = 0
                if between is not None:
                    above = sum(1 for item in attachments.values() if item.net == between and item.side == "supply")
                    below = sum(1 for item in attachments.values() if item.net == between and item.side != "supply")
                    below += sum(1 for item in rungs.values() if between in (item.net_a, item.net_b))
                hung = max(above, below)
                beside_columns = False
                beside_nets: set[str] = set()
                # What hangs from a pin of the part before this one that points along the row at it (the outer base of
                # a pair's second member) stands beside that part, in columns of its own.
                before = chain[index - 1] if index else interface_host
                if before is not None and before in geoms and placed.get(before) is not None:
                    for pin_name, pin_geom in sorted(geoms[before].pins.items()):
                        name = next((n for n, pins in pins_by_net.items() if n not in roles.rails and (before, pin_name) in pins), None)
                        if (name is None or name == between or pin_geom.normal[0] < .5
                                or net_pin(before, name) != pin_name):
                            continue
                        beside = max(
                            sum(1 for item in attachments.values() if item.net == name and item.side == "supply"),
                            sum(1 for item in attachments.values() if item.net == name and item.side != "supply"))
                        if beside:
                            hung += beside
                            beside_columns = True
                            beside_nets.add(name)
                if hung:
                    # each hung part takes a slot pitch or its own width, whichever is more
                    hung_width = max((min((g.box[2] - g.box[0] for g in by_ref[ref_].geometry.values()
                                           if all(abs(pg.normal[1]) > .5 for pg in g.pins.values())), default=0.0)
                                      for ref_, item in attachments.items() if ref_ in by_ref and item.net == between),
                                     default=0.0)
                    text_gap = gap_mm + hung * max(slot_pitch_mm, hung_width) + gap_mm
                    if beside_columns:
                        # two kinds of column stand side by side: the wider part's column is wider than a pitch
                        widest = max((min(g.box[2] - g.box[0] for g in by_ref[ref_].geometry.values())
                                      for ref_, item in attachments.items() if ref_ in by_ref
                                      and (item.net == between or item.net in beside_nets)), default=0.0)
                        text_gap += gap_mm + max(0.0, widest + gap_mm - slot_pitch_mm)
                # A rail pin beside a chain pin on the same side of a multi-pin part takes a slot of its own between the
                # part and the chain neighbour: its power symbol hangs there and the chain wire passes it.
                if between is not None:
                    def rail_slots(part, geometry):
                        pin_id = net_pin(part, between)
                        if pin_id is None or pin_id not in geometry.pins or not _is_multi_pin(by_ref[part]):
                            return 0
                        nx, ny = geometry.pins[pin_id].normal
                        rail_names = [n for n in roles.rails if n != between]
                        return sum(1 for n in rail_names for r_, p_ in pins_by_net.get(n, ())
                                   if r_ == part and p_ != pin_id and p_ in geometry.pins
                                   and geometry.pins[p_].normal[0] * nx + geometry.pins[p_].normal[1] * ny > .5)
                    slots = rail_slots(ref, geom) + (rail_slots(before, geoms[before]) if before in geoms else 0)
                    text_gap += slots * slot_pitch_mm
            text_gap += (widen or {}).get(ref, 0.0)
            if lane_roles and (index > 0 or interface_host is not None):
                # What a hub's lanes need beside it: the part before it makes room for the lanes on its right, this part
                # for the lanes on its left.
                lane_before = chain[index - 1] if index else interface_host
                if lane_before in geoms:
                    text_gap += lane_room(lane_before, geoms[lane_before], 1, row_pins.get(lane_before, set()))
                text_gap += lane_room(ref, geom, -1, {pin for pin in (in_pin, out_pin) if pin})
            if index == 0:
                x = x_block - left
                if interface_host is not None:
                    x = max(x, placed[interface_host].position[0] + geoms[interface_host].box[2] + text_gap - left)
            else:
                x = prev_right + text_gap - left
            # A part entered at a control pin stays on the main row when the part before it left from a pin off the row:
            # the path is kept on one line and the net runs an L from the upper pin to the control pin.
            entry_y = main_y if (multi and in_pin is not None and in_pin in geom.pins and pin_roles(ref).get(in_pin) == "control"
                                 and index == 0 and interface_host is not None) else row_y
            anchor = (snap(x), snap(entry_y - py))
            if facing is not None:
                # Face to face with the member before it: their shared pins on one straight wire, the bodies a gap
                # apart, the tips a whole 2.54 mm step so the part hung from the wire stands at its midpoint.
                mate = chain[index - 1]
                mate_pin = geoms[mate].pins[net_pin(mate, earlier)]
                mate_tip = (placed[mate].position[0] + mate_pin.point[0], placed[mate].position[1] + mate_pin.point[1])
                own = geom.pins[net_pin(ref, earlier)].point
                step = 2 * GRID_MM
                x = placed[mate].position[0] + math.ceil((prev_right + gap_mm - left - placed[mate].position[0]) / step - _EPS) * step
                anchor = (snap(x), snap(mate_tip[1] - own[1]))
                pair_mid[earlier] = (mate_tip[0] + anchor[0] + own[0]) / 2
            placed[ref], geoms[ref] = Placement(anchor, orientation), geom
            rec.chain(block, ref, row_pin if row_pin in geom.pins else None, anchor[1] + py, orientation,
                      like=chain[index - 1] if facing is not None else None)
            prev_right = anchor[0] + right
            if multi:
                row_pins[ref] = {pin for pin in (in_pin, out_pin) if pin in geom.pins and not (in_off_row and pin == in_pin)}
            for net_name in dict.fromkeys((earlier, later, terminal_input_net, terminal_output_net)):
                if net_name and not (in_off_row and net_pin(ref, net_name) == in_pin):
                    pg = geom.pins[net_pin(ref, net_name)]
                    net_columns[net_name].append(anchor[0] + pg.point[0])
                    chain_net_y[net_name] = anchor[1] + pg.point[1]
            if facing is None:
                main_y = anchor[1] + py
            end_pin = out_pin if out_pin in geom.pins else row_pin
            if end_pin in geom.pins:
                row_y = anchor[1] + geom.pins[end_pin].point[1]
            after_source = source_upright
        # A block joined to the next by a net follows it at the usual gap when it is a source, else with room between.
        x_block = prev_right + (0.0 if after_source else max(gap_mm, slot_pitch_mm * 2))

    # A block that feeds a control pin stands as a column from that pin, perpendicular to the row, on the side the pin
    # points to: its last part nearest the pin, each part upright, whole 2.54 mm steps along the line until clear.
    for block, (host, host_pin, hang_net) in sorted(hangs.items()):
        chain = hung_chains[block]
        host_pg = geoms[host].pins[host_pin]
        direction = 1 if host_pg.normal[1] >= 0 else -1
        tip = (placed[host].position[0] + host_pg.point[0], placed[host].position[1] + host_pg.point[1])
        for index in range(len(chain) - 1, -1, -1):
            ref = chain[index]
            near_net = hang_net if index == len(chain) - 1 else sharing_net(ref, chain[index + 1])
            far_net = sharing_net(chain[index - 1], ref) if index else None
            near = net_pin(ref, near_net) if near_net else None
            far = net_pin(ref, far_net) if far_net else None
            if near is None:
                raise ValueError(f"No pin of {ref} joins {host} pin {host_pin}")

            def faces_host(g):
                return near in g.pins and g.pins[near].normal[1] * direction < -.5 and abs(g.pins[near].normal[0]) < .5

            def column(o, g):
                if not faces_host(g):
                    return False
                if far is None:
                    return True
                return (far in g.pins and g.pins[far].normal[1] * direction > .5
                        and abs(g.pins[far].point[0] - g.pins[near].point[0]) <= GRID_MM + _EPS)

            try:
                orientation, geom = geom_choice(ref, column)
            except ValueError:
                orientation, geom = geom_choice(ref, lambda o, g: faces_host(g))
            near_point = geom.pins[near].point
            chosen = None
            for step in range(12):
                anchor = (snap(tip[0] - near_point[0]), snap(tip[1] + direction * step * 2 * GRID_MM - near_point[1]))
                box = core_at(ref, geom, anchor)
                grown = (box[0] - gap_mm, box[1] - gap_mm, box[2] + gap_mm, box[3] + gap_mm)
                near_tip = (anchor[0] + near_point[0], anchor[1] + near_point[1])
                if any(boxes_overlap(grown, core_at(other, geoms[other], placed[other].position)) for other in placed):
                    continue
                if any(math.dist(near_tip, (placed[other].position[0] + pg.point[0], placed[other].position[1] + pg.point[1]))
                       < 2.54 - _EPS for other in placed for pg in geoms[other].pins.values()):
                    continue
                chosen = anchor
                break
            if chosen is None:
                raise ValueError(f"No column slot for {ref} below {host} pin {host_pin}")
            placed[ref], geoms[ref] = Placement(chosen, orientation), geom
            rec.column(block, host, host_pin, ref, direction, orientation)
            if far is not None:
                tip = (chosen[0] + geom.pins[far].point[0], chosen[1] + geom.pins[far].point[1])

    # The trunk rows follow their declared role order, excluding nets already used between chain members.
    chain_nets = {net for chain in chains.values() for a, b in zip(chain, chain[1:]) for net in sharing_nets(a, b)}
    chain_nets.update(terminal_chain_nets)
    chain_nets.update(net for chain in hung_chains.values() for a, b in zip(chain, chain[1:]) for net in sharing_nets(a, b))
    chain_nets.update(hang_net for _host, _pin, hang_net in hangs.values())
    for before, after in zip(block_order, block_order[1:]):
        before_chain, after_chain = chains[before], chains[after]
        if before_chain and after_chain:
            chain_nets.update(sharing_nets(before_chain[-1], after_chain[0]))
    if stack_net is not None:
        chain_nets.add(stack_net)
    chain_nets.discard(None)
    side_rows, led_nets = _side_rows(
        by_ref, pins_by_net, roles, placed, geoms, row_pins, {net for _h, _p, net in hangs.values()},
        feedback_nets, attachments, net_pin, gap_mm, snap,
    )
    chain_nets.difference_update(side_rows)
    # A net whose one chain pin is a control pin pointing outward along the chain row (a pair's outer base) is wired at
    # that pin's height: what hangs from it stands there, not on a row of its own below.
    outer_nets: set[str] = set()
    for name in roles.trunks:
        tips = [(ref, pin) for ref, pin in pins_by_net.get(name, ()) if ref in placed and ref in chain_parts]
        if len(tips) == 1 and name not in chain_nets and name not in side_rows:
            ref, pin = tips[0]
            pg = geoms[ref].pins.get(pin)
            if pg is not None and abs(pg.normal[0]) > .5 and pin_roles(ref).get(pin) == "control" and ref in row_pins:
                chain_nets.add(name)
                outer_nets.add(name)
                chain_net_y[name] = placed[ref].position[1] + pg.point[1]
    for name in chain_nets:
        rows[name] = chain_net_y.get(name, 0.0)
    rows.update(side_rows)

    # Lanes: each non-chain pin of a hub with parts on it is a row of its own at the pin's height, on the pin's side of the
    # hub.  A lane whose pin is a chain pin, or points up or down, is not drawn here: its parts are left for the end.
    hub_side_y: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for hub in sorted({lane.hub for lane in lane_roles if lane.hub in placed}):
        for name, pins in sorted(pins_by_net.items()):
            if name in roles.rails:
                continue
            for r, pin in pins:
                pg = geoms[hub].pins.get(pin) if r == hub else None
                if pg is not None and abs(pg.normal[0]) > .5:
                    hub_side_y[hub][name].append((1 if pg.normal[0] > 0 else -1, placed[hub].position[1] + pg.point[1]))
    lane_infos: list[tuple] = []          # (lane, direction, pin tip) of the lanes drawn, top to bottom
    lane_leftover: list[str] = []         # lane parts with no lane to stand on
    lane_nets: set[str] = set()
    for lane in sorted(lane_roles, key=lambda l: (l.hub, placed[l.hub].position[1] + geoms[l.hub].pins[l.pin].point[1]
                                                  if l.hub in placed and l.pin in geoms[l.hub].pins else 0.0, l.pin)):
        pg = geoms[lane.hub].pins.get(lane.pin) if lane.hub in placed else None
        if (pg is None or abs(pg.normal[0]) < .5 or lane.pin in row_pins.get(lane.hub, ())
                or lane.net in chain_nets or lane.net in side_rows):
            lane_leftover.extend(lane.series)
            continue
        direction = 1 if pg.normal[0] > 0 else -1
        tip = (placed[lane.hub].position[0] + pg.point[0], placed[lane.hub].position[1] + pg.point[1])
        lane_infos.append((lane, direction, tip))
        for index_, name in enumerate(lane.nets):
            if index_ == 0 or name not in hub_side_y[lane.hub]:
                rows[name] = tip[1]
                lane_nets.add(name)

    # A hub with lanes also fans its other pins that carry a hung part into lanes of their own (a lane of one shunt part), so
    # that every lane of the hub is placed in one order.
    drawn_pins = {(lane.hub, lane.pin) for lane, _d, _t in lane_infos}
    for hub in sorted({lane.hub for lane, _d, _t in lane_infos}):
        for pin, pg in sorted(geoms[hub].pins.items()):
            net_name = next((n for n, pins in pins_by_net.items() if (hub, pin) in pins), None)
            if (net_name is None or net_name in roles.rails or abs(pg.normal[0]) < .5 or (hub, pin) in drawn_pins
                    or pin in row_pins.get(hub, ()) or net_name in chain_nets or net_name in side_rows
                    or not any(a.net == net_name and a.host == hub for a in attachments.values())):
                continue
            tip = (placed[hub].position[0] + pg.point[0], placed[hub].position[1] + pg.point[1])
            lane_infos.append((_LaneLike(hub, pin, net_name, (), (net_name,)), 1 if pg.normal[0] > 0 else -1, tip))

    def lane_hang_side(item) -> int:
        """Which way what hangs from the end of the lane goes: +1 down (a ground part), -1 up (a supply part), 0 unknown."""
        lane = item[0]
        sides = {a.side for a in attachments.values() if a.net in lane.nets}
        if "ground" in sides and "supply" not in sides:
            return 1
        if "supply" in sides and "ground" not in sides:
            return -1
        return 0

    # Parts that hang down are placed from the lowest lane up and those that hang up from the highest down, so that a
    # lane's hung part stands beyond the ends of the lanes it passes: the lanes end in a staircase, not a crossing.
    lane_infos.sort(key=lambda item: (item[0].hub, item[1], {1: 0, -1: 1, 0: 2}[lane_hang_side(item)],
                                      -item[2][1] if lane_hang_side(item) == 1 else item[2][1]))

    def lane_span_ends(hub: str, net_a: str, net_b: str) -> tuple[int, float, float] | None:
        """``(side, y of net_a's pin, y of net_b's pin)`` when both nets leave the hub by pins on one side, at two heights."""
        for side_a, y_a in hub_side_y[hub].get(net_a, ()):
            for side_b, y_b in hub_side_y[hub].get(net_b, ()):
                if side_a == side_b and abs(y_a - y_b) > _EPS:
                    return side_a, y_a, y_b
        return None

    def lane_side_y(hub: str, net: str, side: int, not_y: float) -> float | None:
        """The height of the hub's pin on ``net`` on ``side`` other than ``not_y``, or None."""
        return next((y for s_, y in hub_side_y[hub].get(net, ()) if s_ == side and abs(y - not_y) > _EPS), None)

    lane_hub_of = {name: lane.hub for lane, _d, _t in lane_infos for name in lane.nets[:1]}
    lane_rungs = {ref for ref, rung in rungs.items()
                  if rung.net_a in lane_hub_of and lane_span_ends(lane_hub_of[rung.net_a], rung.net_a, rung.net_b)}
    lane_rungs |= {ref for ref, rung in rungs.items()
                   if rung.net_b in lane_hub_of and lane_span_ends(lane_hub_of[rung.net_b], rung.net_a, rung.net_b)}
    trunk_nets = [name for name in roles.trunks if name not in chain_nets and name not in side_rows
                  and name not in lane_nets]
    # Rows that a rung joins follow one another, so a rung never spans the row of a net it does not serve.
    joined: dict[str, list[str]] = defaultdict(list)
    for _ref, rung in sorted(rungs.items()):
        if rung.net_a in trunk_nets and rung.net_b in trunk_nets:
            joined[rung.net_a].append(rung.net_b)
            joined[rung.net_b].append(rung.net_a)
    ordered_trunks: list[str] = []
    for name in trunk_nets:
        pending = [name]
        while pending:
            current = pending.pop()
            if current in ordered_trunks:
                continue
            ordered_trunks.append(current)
            pending.extend(sorted((n for n in set(joined[current]) if n not in ordered_trunks), reverse=True))
    trunk_nets = ordered_trunks
    row_y = max([0.0, *chain_net_y.values()])
    for trunk in trunk_nets:
        separation = max(2 * gap_mm + GRID_MM, slot_pitch_mm)
        for ref, rung in sorted(rungs.items()):
            if trunk not in (rung.net_a, rung.net_b):
                continue
            other_net = rung.net_b if rung.net_a == trunk else rung.net_a
            if other_net not in rows:
                continue
            upper_pin, lower_pin = net_pin(ref, rung.net_a), net_pin(ref, rung.net_b)
            if ref not in by_ref or upper_pin is None or lower_pin is None:
                continue
            rung_sizes = [
                max(abs(geometry.pins[upper_pin].point[1] - geometry.pins[lower_pin].point[1]),
                    (geometry.body[3] - geometry.body[1] + 2 * gap_mm) if geometry.body is not None
                    else abs(geometry.pins[upper_pin].point[1] - geometry.pins[lower_pin].point[1]))
                for _orientation, geometry in sorted(by_ref[ref].geometry.items(), key=lambda item: (item[0].rotation, item[0].mirror))
                if upper_pin in geometry.pins and lower_pin in geometry.pins
                and abs(geometry.pins[upper_pin].point[0] - geometry.pins[lower_pin].point[0]) <= GRID_MM + _EPS
            ]
            if rung_sizes:
                separation = max(separation, max(rung_sizes))
        row_y += separation
        rows[trunk] = snap(row_y)
    # All chain nets lie on y=0.  Supply and ground attachment rows lie beyond the ladder.
    for name in roles.trunks:
        rows.setdefault(name, 0.0)
    supply_y = -max(slot_pitch_mm, 2 * gap_mm + GRID_MM)

    # The first trunk slot column is the average pin column on the net, or after the chain if it has no chain pins.
    all_right = max((box_at(ref, geoms[ref], p.position)[2] for ref, p in placed.items()), default=0.0)
    for trunk in trunk_nets:
        cols = net_columns.get(trunk, [])
        if cols:
            net_columns[trunk] = [snap(sum(cols) / len(cols))]

    def row_for_net(net_name: str) -> float:
        return rows.get(net_name, 0.0)

    # Attachments hang from their net row. Start at the host pin when it points toward the row; otherwise step to its right.
    occupied: list[tuple[str, tuple[float, float, float, float]]] = [
        (ref, core_at(ref, geoms[ref], p.position)) for ref, p in placed.items()
    ]

    # Rungs bridge two trunk/chain rows. Align both tips to their row lines and keep slots clear.
    for ref, rung in sorted(rungs.items()):
        if ref not in by_ref or ref in lane_rungs:
            continue
        def rung_row(net_name: str) -> float:
            """The height of a net a rung ends on: its row, or for a net with no row (a dead end on a sideways pin) the height
            of its chain pin."""
            if net_name in rows or net_name in trunk_nets:
                return row_for_net(net_name)
            tips = [placed[r].position[1] + geoms[r].pins[p].point[1] for r, p in pins_by_net.get(net_name, ())
                    if r in chain_parts and r in placed and p in geoms[r].pins]
            return snap(tips[0]) if tips else row_for_net(net_name)

        upper, lower = sorted((rung.net_a, rung.net_b), key=rung_row)
        upper_y = rung_row(upper)
        if abs(rung_row(lower) - upper_y) <= _EPS:
            raise ValueError(f"Rung {ref} connects nets without separate rows: {upper} and {lower}")
        pin_a, pin_b = net_pin(ref, rung.net_a), net_pin(ref, rung.net_b)
        if pin_a is None or pin_b is None:
            continue
        upper_pin = pin_a if rung_row(rung.net_a) <= rung_row(rung.net_b) else pin_b
        lower_pin = pin_b if upper_pin == pin_a else pin_a
        def rung_candidate(o, g):
            return (upper_pin in g.pins and lower_pin in g.pins
                    and abs(g.pins[upper_pin].point[0] - g.pins[lower_pin].point[0]) <= GRID_MM + _EPS
                    and g.pins[upper_pin].point[1] < g.pins[lower_pin].point[1] - _EPS)
        orientation, geom = geom_choice(ref, rung_candidate)
        pu, pl = geom.pins[upper_pin].point, geom.pins[lower_pin].point
        x0 = snap(sum(net_columns.get(rung.net_a, []) + net_columns.get(rung.net_b, [])) / max(1, len(net_columns.get(rung.net_a, []) + net_columns.get(rung.net_b, []))))
        if not net_columns.get(rung.net_a) and not net_columns.get(rung.net_b):
            x0 = snap(all_right + slot_pitch_mm)
        candidates = [x0]
        for step in range(1, 33):
            candidates.extend((x0 - step * slot_pitch_mm, x0 + step * slot_pitch_mm))
        def rung_anchor(x):
            return (snap(x - (pu[0] + pl[0]) / 2), snap(upper_y - pu[1]))

        def free_rung_slot(x):
            test_anchor = rung_anchor(x)
            test_box = core_at(ref, geom, test_anchor)
            if any(boxes_overlap(test_box, b) for _other, b in occupied):
                return None
            # its tips keep a wire's length from every placed tip
            tips = [(test_anchor[0] + pg.point[0], test_anchor[1] + pg.point[1]) for pg in geom.pins.values()]
            if any(math.dist(tip, (placed[other].position[0] + pg.point[0], placed[other].position[1] + pg.point[1])) < 2.54 - _EPS
                   for tip in tips for other in placed for pg in geoms[other].pins.values()):
                return None
            return test_anchor

        anchor = next((test_anchor for x in candidates if (test_anchor := free_rung_slot(x)) is not None), None)
        if anchor is None:
            raise ValueError(f"No rung slot for {ref} between {upper} and {lower}")
        placed[ref], geoms[ref] = Placement(anchor, orientation), geom
        occupied.append((ref, core_at(ref, geom, anchor)))
        rec.span(ref, upper, lower, orientation, "rung")


    pin_nets = {endpoint: name for name, pins in pins_by_net.items() for endpoint in pins}

    def segments_intersect(a, b):
        (ax, ay), (bx, by) = a
        (cx, cy), (dx, dy) = b
        av = abs(ax - bx) <= _EPS
        bv = abs(cx - dx) <= _EPS
        if av and bv:
            return abs(ax - cx) <= _EPS and max(min(ay, by), min(cy, dy)) <= min(max(ay, by), max(cy, dy)) + _EPS
        if not av and not bv:
            return abs(ay - cy) <= _EPS and max(min(ax, bx), min(cx, dx)) <= min(max(ax, bx), max(cx, dx)) + _EPS
        vertical, horizontal = (a, b) if av else (b, a)
        x, y1 = vertical[0]
        _x, y2 = vertical[1]
        x1, y = horizontal[0]
        x2, _y = horizontal[1]
        return min(y1, y2) - _EPS <= y <= max(y1, y2) + _EPS and min(x1, x2) - _EPS <= x <= max(x1, x2) + _EPS

    def segment_hits_box(segment, box):
        (x1, y1), (x2, y2) = segment
        if abs(x1 - x2) <= _EPS:
            return (box[0] + _EPS < x1 < box[2] - _EPS
                    and max(min(y1, y2), box[1]) < min(max(y1, y2), box[3]) - _EPS)
        if abs(y1 - y2) <= _EPS:
            return (box[1] + _EPS < y1 < box[3] - _EPS
                    and max(min(x1, x2), box[0]) < min(max(x1, x2), box[2]) - _EPS)
        return False

    def pin_lead(pin: str, geom: OrientedGeometry, anchor: tuple[float, float]):
        pg = geom.pins[pin]
        body = geom.body if geom.body is not None else geom.box
        x, y = anchor
        tip = (x + pg.point[0], y + pg.point[1])
        nx, ny = pg.normal
        if nx > .5:
            start = (x + body[2], tip[1])
        elif nx < -.5:
            start = (x + body[0], tip[1])
        elif ny > .5:
            start = (tip[0], y + body[3])
        else:
            start = (tip[0], y + body[1])
        if math.dist(start, tip) <= _EPS:
            return None
        return (start, tip)

    baseline_cache: dict[tuple[str, int], set] = {}

    def route_hits(poses, ref: str, candidate_nets) -> set:
        """What the estimated routes of the candidate's nets cross: ``(net, other part, "body" or "lead")``."""
        found: set[tuple[str, str, str]] = set()
        routes: list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]] = []
        for net in nets:
            if net.style != "wire":
                continue
            endpoints = [(r, pin) for r, pin in pins_by_net.get(net.net, ()) if r in poses and pin in poses[r][1].pins]
            if len(endpoints) < 2:
                continue
            tips = []
            for r, pin in endpoints:
                position, pg = poses[r][0], poses[r][1].pins[pin]
                point = (position[0] + pg.point[0], position[1] + pg.point[1])
                # tips lying on one point are one node: nothing to route between them
                if not any(math.dist(point, other) <= _EPS for other, _n in tips):
                    tips.append((point, pg.normal))
            for route in estimate_net(tips):
                routes.extend((net.net, (a, b)) for a, b in zip(route.points, route.points[1:]) if math.dist(a, b) > _EPS)

        leads = []
        bodies = []
        for name, (position, symbol_geom) in poses.items():
            body = symbol_geom.body if symbol_geom.body is not None else symbol_geom.box
            bodies.append((name, (position[0] + body[0], position[1] + body[1],
                                  position[0] + body[2], position[1] + body[3])))
            for pin in symbol_geom.pins:
                lead = pin_lead(pin, symbol_geom, position)
                if lead is not None:
                    leads.append((name, pin_nets.get((name, pin)), lead))

        for net_name, segment in routes:
            for body_ref, body in bodies:
                # Check new candidate routes against every body and every existing route against this body's footprint.
                if (net_name in candidate_nets or body_ref == ref) and segment_hits_box(segment, body):
                    # A stub that leaves a tip lying on a pin tip of this body on the same net is a node, not a crossing.
                    if any(pin_nets.get((body_ref, pin)) == net_name and any(
                            math.dist(end, (poses[body_ref][0][0] + pg.point[0], poses[body_ref][0][1] + pg.point[1])) <= _EPS
                            for end in segment) for pin, pg in poses[body_ref][1].pins.items()):
                        continue
                    found.add((net_name, body_ref, "body"))
            for lead_ref, lead_net, lead in leads:
                if (lead_ref == ref or net_name in candidate_nets) and lead_net != net_name and segments_intersect(segment, lead):
                    found.add((net_name, lead_ref, "lead"))
        return found

    def candidate_route_clear(ref: str, anchor: tuple[float, float], geom: OrientedGeometry) -> bool:
        """Reject a support pose if an estimated signal route crosses a foreign pin lead or body that the same routes
        did not cross without it: a wire already bound to pass a part (a far pin of the net past a body) is not the
        candidate's doing and does not close every slot."""
        poses = {name: (placed[name].position, geoms[name]) for name in placed}
        candidate_nets = {name for name, pins in pins_by_net.items() if any(r == ref for r, _p in pins)}
        key = (ref, len(placed))
        if key not in baseline_cache:
            baseline_cache[key] = route_hits(poses, ref, candidate_nets)
        poses[ref] = (anchor, geom)
        return route_hits(poses, ref, candidate_nets) <= baseline_cache[key]

    def placing_order(item):
        """A part straight over (under) the pin it serves first: where it stands depends on nothing else.  Then what
        hangs from an outer control pin: its wire runs along the row and later parts must not stand in it."""
        ref, attachment = item
        host_pin = net_pin(attachment.host, attachment.net) if attachment.host in geoms else None
        facing = (host_pin in geoms[attachment.host].pins and geoms[attachment.host].pins[host_pin].normal[1]
                  * (1 if attachment.side == "ground" else -1) > .5) if host_pin is not None else False
        on_led = attachment.net in led_nets
        after_drop = on_led and attachment.side != led_nets[attachment.net]
        key = (on_led, after_drop, not facing and not after_drop, attachment.net not in outer_nets,
               attachment.side != "supply", ref)
        # A part hung from another hung part (an LED under its series resistor) is a column of two: it is placed right
        # after its host, before anything else claims the room beneath it.  Of a pair hung on each other the one on the
        # ground side hangs from the other.
        host_item = attachments.get(attachment.host)
        if host_item is not None and attachment.host != ref and (host_item.host != ref or attachment.side == "ground"):
            return (*placing_order((attachment.host, host_item)), 1)
        return key

    cleared: set[str] = set()

    def clear_row(net_name: str) -> None:
        """Raise (lower) the row of a net wired over the parts between two others to clear what stands there, now that
        those parts are placed: their bodies, with a gap each side of the wire, not an estimate of their height."""
        cleared.add(net_name)
        tips = [(placed[r].position[0] + geoms[r].pins[p].point[0], placed[r].position[1] + geoms[r].pins[p].point[1])
                for r, p in pins_by_net.get(net_name, ()) if r in chain_parts and r in placed and p in geoms[r].pins]
        if len(tips) < 2:
            return
        side = -1 if led_nets[net_name] == "ground" else 1
        y = rows[net_name]
        for other, box in occupied:
            if other in attachments and attachments[other].net != net_name and other in placed                     and box[2] > min(t[0] for t in tips) and box[0] < max(t[0] for t in tips):
                edge = box[1] if side < 0 else box[3]
                y = min(y, edge - 2 * gap_mm) if side < 0 else max(y, edge + 2 * gap_mm)
        rows[net_name] = (math.floor(y / GRID_MM + _EPS) if side < 0 else math.ceil(y / GRID_MM - _EPS)) * GRID_MM

    def place_attachment(ref: str, attachment, lane_start: tuple[float, int] | None = None, far_first: bool = False) -> None:
        if ref not in by_ref or attachment.net not in net_by_name:
            return
        signal_pin = net_pin(ref, attachment.net)
        rail_pin = net_pin(ref, attachment.rail) if attachment.rail else signal_pin      # a tap has no rail pin
        if rail_pin is None or signal_pin is None:
            return
        if attachment.net in led_nets and attachment.net not in cleared:
            clear_row(attachment.net)
        target_y = row_for_net(attachment.net)
        side_sign = 1 if attachment.side == "ground" else -1
        # A part hangs from the wire it joins: where that net leaves a multi-pin chain host, the wire runs at
        # the host pin's y, which is not the nominal row when the pin sits off it (a base, a collector).
        # A trunk keeps its own row.
        pin_host = attachment.host
        pin_name = net_pin(pin_host, attachment.net) if pin_host in chain_parts and pin_host in placed else None
        if (attachment.net not in trunk_nets and attachment.net not in side_rows
                and pin_name is not None and pin_name in geoms[pin_host].pins):
            target_y = snap(placed[pin_host].position[1] + geoms[pin_host].pins[pin_name].point[1])
        def attachment_candidate(o, g):
            if signal_pin not in g.pins or rail_pin not in g.pins:
                return False
            signal, rail = g.pins[signal_pin], g.pins[rail_pin]
            if len(g.pins) > 2 or not attachment.rail:
                # a tap (a one-pin part) or a clamp (a dual diode with its rails left and right): its signal pin points at the wire, the body beyond it
                body = g.body if g.body is not None else g.box
                return (abs(signal.normal[0]) < .5 and signal.normal[1] * side_sign < -.5
                        and (body[1] >= signal.point[1] - _EPS if side_sign > 0 else body[3] <= signal.point[1] + _EPS))
            if abs(signal.point[0] - rail.point[0]) > GRID_MM + _EPS:
                return False
            if (rail.point[1] - signal.point[1]) * side_sign <= _EPS:
                return False
            if rail.normal[1] * side_sign <= _EPS:
                return False
            body = g.body if g.body is not None else g.box
            return body[1] >= signal.point[1] - _EPS if side_sign > 0 else body[3] <= signal.point[1] + _EPS

        orientation, geom = geom_choice(ref, attachment_candidate)
        sig = geom.pins[signal_pin]
        host = attachment.host
        # A part joins the wire beside the pin it serves: on a wire between a two-pin part and a multi-pin one that is
        # the multi-pin part's pin (a base pull-down stands by the base, not by the coupling capacitor).
        wire_parts = sorted({r for r, _p in pins_by_net.get(attachment.net, ())
                             if r in chain_parts and r in placed and r != ref}) if attachment.net not in trunk_nets else []
        multi_parts = [r for r in wire_parts if max(len(g.pins) for g in by_ref[r].geometry.values()) > 2]
        if multi_parts and host not in multi_parts:
            host = multi_parts[0]
        host_pin = net_pin(host, attachment.net) if host in by_ref else None
        toward = False
        preferred = None
        host_tip_x = None
        slot_direction = 1
        if host in by_ref and host_pin is not None:
            host_geom = geoms.get(host, by_ref[host].geometry.get(by_ref[host].orientation))
            host_pos = placed[host].position if host in placed else by_ref[host].position
            if host_geom is None or host_pin not in host_geom.pins:
                host_geom = None
        else:
            host_geom = None
        if host_geom is not None:
            hp = host_geom.pins[host_pin]
            host_tip = (host_pos[0] + hp.point[0], host_pos[1] + hp.point[1])
            toward = hp.normal[1] * (target_y - host_tip[1]) > _EPS or (
                abs(target_y - host_tip[1]) <= _EPS and hp.normal[1] * side_sign > _EPS
            )
            host_tip_x = host_tip[0]
            if toward:
                preferred = host_tip[0]
            elif hp.normal[0] < -_EPS:
                slot_direction = -1
        if preferred is None:
            host_geom = geoms.get(host, by_ref[host].geometry.get(by_ref[host].orientation))
            host_pos = placed[host].position if host in placed else by_ref[host].position
            host_box = box_at(host, host_geom, host_pos) if host_geom is not None else (all_right, 0, all_right, 0)
            preferred = (host_box[0] - slot_pitch_mm if slot_direction < 0
                         else host_box[2] + slot_pitch_mm)
        candidates = [snap(preferred + slot_direction * k * 2 * GRID_MM) for k in range(0, 100)]
        if lane_start is not None:
            # hung from the end of a lane: the first slot is a wire's length beyond the last tip, then outward
            candidates = [snap(lane_start[0] + lane_start[1] * k * 2 * GRID_MM) for k in range(1, 100)]
        if attachment.net in led_nets and attachment.side != led_nets[attachment.net]:
            # The row's wire drops to the other part at the row's far end: what hangs on the drop's side stands before
            # that end, what hangs opposite it at or beyond the last of those, so the drop passes nothing.
            floor_x = max((placed[r].position[0] + geoms[r].pins[net_pin(r, attachment.net)].point[0] + 2 * GRID_MM
                           for r, a in attachments.items() if r in placed and r != ref and a.net == attachment.net
                           and a.side == led_nets[attachment.net] and net_pin(r, attachment.net) in geoms[r].pins),
                          default=None)
            if floor_x is not None:
                candidates = [x for x in candidates if x >= floor_x - _EPS]
        # Straight under (or over) a host pin that points at the part: the first slots stand off along the
        # pin by whole grid pairs, so the two boxes clear each other and one short wire joins the tips.
        offsets = [(x, 0.0) for x in candidates]
        # On the wire between two chain parts the slots run along that wire from the serving pin inward, nearest
        # first; the row-side candidates above are the fallback when the wire is full.
        wire_x = [placed[r].position[0] + geoms[r].pins[pin].point[0]
                  for r in wire_parts for r2, pin in pins_by_net.get(attachment.net, ()) if r2 == r
                  and abs(placed[r].position[1] + geoms[r].pins[pin].point[1] - target_y) <= _EPS]
        if not toward and host_tip_x is not None and len(wire_x) >= 2:
            others = [x for x in wire_x if abs(x - host_tip_x) > _EPS]
            if others:
                inward = 1 if sum(others) / len(others) > host_tip_x else -1
                lo, hi = min(wire_x), max(wire_x)
                along = [snap(host_tip_x + inward * k * 2 * GRID_MM) for k in range(1, 64)]
                offsets = [(x, 0.0) for x in along if lo - _EPS <= x <= hi + _EPS] + offsets
        # On the wire between two paired parts that face each other the first slot is the midpoint of the wire.
        at_mid = attachment.net in pair_mid and attachment.net not in trunk_nets
        if at_mid:
            offsets.insert(0, (snap(pair_mid[attachment.net]), 0.0))
        chosen = None
        rejected = {"crowded": 0, "near_pin": 0, "route": 0}
        # Tips of two parts never coincide: a stack shares a straight wire with at least one grid step between the tips.
        # A part whose tip would sit on another's stands off along the wire, away from the row, by whole 2.54 mm steps;
        # the first slot under a host pin that points at the part may stand off the same way until the boxes clear.
        first_toward = at_mid or (toward and host_tip_x is not None and abs(target_y - host_tip[1]) <= _EPS
                                  and abs(hp.normal[1]) > .5)
        # a part of a lane first looks for a slot clear where it stands, all the way out, before it stands off along its wire
        for pass_steps in ((1, 6) if lane_start is not None or far_first else (6,)):
            for slot_index, (x, _unused) in enumerate(offsets):
                max_steps = pass_steps
                for step in range(max_steps):
                    stand_off = side_sign * step * 2 * GRID_MM
                    anchor = (snap(x - sig.point[0]), snap(target_y + stand_off - sig.point[1]))
                    box = core_at(ref, geom, anchor)
                    grown = (box[0] - gap_mm, box[1] - gap_mm, box[2] + gap_mm, box[3] + gap_mm)
                    tip = (anchor[0] + sig.point[0], anchor[1] + sig.point[1])
                    crowded = any(boxes_overlap(grown, b) for _other, b in occupied)
                    near_pin = any(
                        math.dist(tip, (placed[other_ref].position[0] + other_pin.point[0],
                                        placed[other_ref].position[1] + other_pin.point[1])) < 2.54 - _EPS
                        for other_ref, other_geom in geoms.items() for other_pin in other_geom.pins.values())
                    if crowded:
                        rejected["crowded"] += 1
                    elif near_pin:
                        rejected["near_pin"] += 1
                    elif not candidate_route_clear(ref, anchor, geom):
                        rejected["route"] += 1
                    else:
                        chosen = (anchor, box)
                        break
                    # A part stands off along its wire when its tip is on another's, or when the net it leaves is crossed
                    # by a wire a nearer slot cannot clear (a pull-up above the run that passes over its pin).
                    if not (first_toward and slot_index == 0) and not near_pin and not (not crowded and step < 5):
                        break
                if chosen is not None:
                    break
            if chosen is not None:
                break
        if chosen is None and partial:
            left_out.add(ref)            # a part left out is in no structure of the plan, and is not tried again
            return
        if chosen is None:
            # Say why every slot failed: the counts tell a crowded row from a blocked route.
            where = ""
            if host_geom is not None:
                where = (f" (host {host} pin {host_pin} normal {host_geom.pins[host_pin].normal}, "
                         f"row y={target_y:g}, ")
            else:
                where = f" (host {host}, row y={target_y:g}, "
            # The room before a chain part was too small for what hangs from its pins: say which part, so the group
            # can be placed again with more.
            room_for = host if host in chain_parts else None
            if room_for is not None and host_geom is not None and host_pin in host_geom.pins and host_geom.pins[host_pin].normal[0] > .5:
                chain_of_host = next(chain for chain in chains.values() if host in chain)
                following = chain_of_host.index(host) + 1
                room_for = chain_of_host[following] if following < len(chain_of_host) else None
            raise _NoSlot(
                f"No attachment slot for {ref} on {attachment.net}{where}{sum(rejected.values())} candidates: "
                f"{rejected['crowded']} crowded, {rejected['near_pin']} near a pin, "
                f"{rejected['route']} route blocked)",
                room_for, max(slot_pitch_mm, min((g.box[2] - g.box[0] for g in by_ref[ref].geometry.values()
                                                  if all(abs(pg.normal[1]) > .5 for pg in g.pins.values())), default=0.0)))
        anchor, box = chosen
        placed[ref], geoms[ref] = Placement(anchor, orientation), geom
        occupied.append((ref, box))
        rec.hang(ref, attachment.net, host, host_pin, attachment.side, orientation)

    def lane_free(ref: str, geom: OrientedGeometry, anchor: tuple[float, float]) -> bool:
        """The part stands clear: its body a gap from every body, its tips a wire's length from every other tip."""
        box = core_at(ref, geom, anchor)
        grown = (box[0] - gap_mm, box[1] - gap_mm, box[2] + gap_mm, box[3] + gap_mm)
        if any(boxes_overlap(grown, other) for _name, other in occupied):
            return False
        return not any(
            math.dist((anchor[0] + pg.point[0], anchor[1] + pg.point[1]),
                      (placed[other].position[0] + og.point[0], placed[other].position[1] + og.point[1])) < 2.54 - _EPS
            for pg in geom.pins.values() for other in placed for og in geoms[other].pins.values())

    lane_placed: set[str] = set()

    def lane_place(ref: str, geom: OrientedGeometry, tip_offset: tuple[float, float],
                   x: float, y: float, direction: int) -> tuple[float, float] | None:
        """Anchor for a part whose pin offset ``tip_offset`` is to stand at ``(x, y)`` or the first slot beyond it (outward
        in ``direction``, whole 2.54 mm steps): body clear, tips apart, the nets' estimated routes crossing nothing new."""
        spare = None
        for step in range(120):
            anchor = (snap(x + direction * step * 2 * GRID_MM - tip_offset[0]), snap(y - tip_offset[1]))
            if not lane_free(ref, geom, anchor):
                continue
            if candidate_route_clear(ref, anchor, geom):
                return anchor, True
            spare = spare or anchor
        return spare, False

    def lane_series(ref: str, near_net: str, far_net: str, y: float, edge: float, direction: int) -> float | None:
        """A series part along the lane: lying down, its pin on the near net toward the hub, the other beyond it."""
        near_pin, far_pin = net_pin(ref, near_net), net_pin(ref, far_net)
        if near_pin is None or far_pin is None:
            return None

        def along(o, g):
            if near_pin not in g.pins or far_pin not in g.pins:
                return False
            near, far = g.pins[near_pin], g.pins[far_pin]
            return (abs(near.point[1] - far.point[1]) <= _EPS and (far.point[0] - near.point[0]) * direction > _EPS
                    and near.normal[0] * direction < -_EPS and far.normal[0] * direction > _EPS)

        try:
            orientation, geom = geom_choice(ref, along)
        except ValueError:
            return None
        near = geom.pins[near_pin].point
        anchor, _clear = lane_place(ref, geom, near, edge + direction * 2 * GRID_MM, y, direction)
        if anchor is None:
            return None
        placed[ref], geoms[ref] = Placement(anchor, orientation), geom
        occupied.append((ref, core_at(ref, geom, anchor)))
        lane_placed.add(ref)
        rec.span(ref, near_net, far_net, orientation, "series")
        return anchor[0] + geom.pins[far_pin].point[0]

    def lane_span(ref: str, net_a: str, y_a: float, net_b: str, y_b: float, edge: float, direction: int,
                  lands_net: str) -> float | None:
        """A part standing between two lanes of one hub: the upper tip on the upper lane, the lower on the lower."""
        (up_net, up_y), (low_net, low_y) = sorted(((net_a, y_a), (net_b, y_b)), key=lambda item: item[1])
        up_pin, low_pin = net_pin(ref, up_net), net_pin(ref, low_net)
        if up_pin is None or low_pin is None:
            return None

        def standing(o, g):
            return (up_pin in g.pins and low_pin in g.pins
                    and abs(g.pins[up_pin].point[0] - g.pins[low_pin].point[0]) <= GRID_MM + _EPS
                    and g.pins[up_pin].point[1] < g.pins[low_pin].point[1] - _EPS)

        try:
            orientation, geom = geom_choice(ref, standing)
        except ValueError:
            return None
        up = geom.pins[up_pin].point
        # One tip lands on its lane and the other stands off it by what the lanes' pitch lacks (the lane's wire joins it
        # with a jog).  The tip that lands is the one of the lane the part hangs from when that lane's wire runs on past
        # it (a T onto a run must be straight), else the one of the lane the part ends on.
        low = geom.pins[low_pin].point
        up, row_y = (low, low_y) if lands_net == low_net else (up, up_y)
        anchor, _clear = lane_place(ref, geom, up, edge + direction * 2 * GRID_MM, row_y, direction)
        if anchor is None:
            return None
        placed[ref], geoms[ref] = Placement(anchor, orientation), geom
        occupied.append((ref, core_at(ref, geom, anchor)))
        lane_placed.add(ref)
        rec.span(ref, up_net, low_net, orientation, "lane")
        return anchor[0] + up[0]

    for lane, direction, tip in lane_infos:
        y, edge = tip[1], tip[0]
        if lane.series:
            rec.lane(lane.net, f"{lane.hub}.{lane.pin}", lane.series, y)
        for index_, net_name in enumerate(lane.nets):
            # what hangs from this net's wire comes before the next part along the lane: the wire's room is its own
            for ref, rung in sorted(rungs.items()):
                if ref in lane_rungs and ref not in placed and net_name in (rung.net_a, rung.net_b):
                    other = rung.net_b if rung.net_a == net_name else rung.net_a
                    other_y = lane_side_y(lane.hub, other, direction, y)
                    if other_y is not None:
                        reached = lane_span(ref, net_name, y, other, other_y, edge, direction, net_name)
                        if reached is not None and (reached - edge) * direction > 0:
                            edge = reached
            for ref, item in sorted(attachments.items(), key=placing_order):
                if item.net == net_name and ref not in placed and ref not in left_out and (index_ > 0 or item.host == lane.hub):
                    place_attachment(ref, item, (edge, direction) if index_ > 0 else None, True)
                    if ref in placed:
                        lane_placed.add(ref)
                        reached = placed[ref].position[0] + geoms[ref].pins[net_pin(ref, net_name)].point[0]
                        if (reached - edge) * direction > 0:
                            edge = reached
            if index_ >= len(lane.series):
                break
            part, far_net = lane.series[index_], lane.nets[index_ + 1]
            far_y = lane_side_y(lane.hub, far_net, direction, y) if far_net != lane.net else None
            if far_y is not None and index_ + 1 == len(lane.series):
                reached = lane_span(part, net_name, y, far_net, far_y, edge, direction, far_net)
            else:
                reached = lane_series(part, net_name, far_net, y, edge, direction)
            if reached is None:
                lane_leftover.extend(lane.series[index_:])
                break
            if (reached - edge) * direction > 0:
                edge = reached

    for ref, attachment in sorted(attachments.items(), key=placing_order):
        if ref not in lane_placed and ref not in left_out:
            place_attachment(ref, attachment)

    def feedback_slot(net_name: str, column: float) -> float:
        """The x where a feedback end joins its net: on the chain segment that enters a multi-pin part's pin, at the
        slot nearest that part (past the slots rail parts hang in), never in the pin's own column."""
        pins = [(r, p) for r, p in pins_by_net.get(net_name, ()) if r in chain_parts and r in placed and p in geoms[r].pins]
        multi = [(r, p) for r, p in pins if len(geoms[r].pins) > 2]
        if net_name in trunk_nets or len(pins) < 2 or not multi:
            return column
        ref, pin = sorted(multi)[0]
        tips = [(placed[r].position[0] + geoms[r].pins[p].point[0], placed[r].position[1] + geoms[r].pins[p].point[1])
                for r, p in pins]
        own = (placed[ref].position[0] + geoms[ref].pins[pin].point[0], placed[ref].position[1] + geoms[ref].pins[pin].point[1])
        others = [t[0] for t in tips if abs(t[1] - own[1]) <= _EPS and abs(t[0] - own[0]) > _EPS]
        if not others:
            return column
        inward = 1 if sum(others) / len(others) > own[0] else -1
        hung = [placed[r].position[0] + geoms[r].pins[net_pin(r, net_name)].point[0]
                for r in attachments if r in placed and attachments[r].net == net_name and net_pin(r, net_name) in geoms[r].pins]
        lo, hi = min(others + [own[0]]), max(others + [own[0]])
        # A pin beside this one, in its column and pointing the same way, has a stub or a wire of its own along that
        # side: the drop stands clear of it, one slot further out.
        own_normal = geoms[ref].pins[pin].normal
        crowded = any(other != pin and abs(pg.point[0] - geoms[ref].pins[pin].point[0]) <= _EPS
                      and pg.normal[0] * own_normal[0] + pg.normal[1] * own_normal[1] > .5
                      for other, pg in geoms[ref].pins.items())
        for first in (3, 2, 1) if crowded else (1,):
            for k in range(first, 64):
                x = snap(own[0] + inward * k * 2 * GRID_MM)
                if not lo - _EPS <= x <= hi + _EPS:
                    break
                if all(abs(x - h) >= 2 * GRID_MM - _EPS for h in hung):
                    return x
        return column

    # Feedback parts span their two chain-net columns on a row above every supply attachment.
    feedback_gap = supply_y - max(slot_pitch_mm, 2 * gap_mm + GRID_MM)
    for ref, item in sorted(feedback.items()):
        if ref not in by_ref:
            continue
        pin_from, pin_to = net_pin(ref, item.net_from), net_pin(ref, item.net_to)
        if pin_from is None or pin_to is None:
            continue
        from_x = sum(net_columns.get(item.net_from, [all_right + slot_pitch_mm])) / len(net_columns.get(item.net_from, [all_right + slot_pitch_mm]))
        to_x = sum(net_columns.get(item.net_to, [from_x + slot_pitch_mm])) / len(net_columns.get(item.net_to, [from_x + slot_pitch_mm]))
        slot_from, slot_to = feedback_slot(item.net_from, from_x), feedback_slot(item.net_to, to_x)
        from_slotted, to_slotted = slot_from != from_x, slot_to != to_x
        from_x, to_x = slot_from, slot_to
        from_left = from_x <= to_x
        orientation, geom = geom_choice(ref, lambda o, g: pin_from in g.pins and pin_to in g.pins
                                        and abs(g.pins[pin_from].point[1] - g.pins[pin_to].point[1]) <= _EPS
                                        and ((g.pins[pin_from].point[0] < g.pins[pin_to].point[0]) == from_left)
                                        and abs(g.pins[pin_from].normal[1]) <= _EPS
                                        and abs(g.pins[pin_to].normal[1]) <= _EPS)
        # Above the chain row of its nets, which steps where a part's output pin is off its input pin.
        feedback_y = min((row_for_net(name) if name in chain_nets else 0.0)
                         for name in (item.net_from, item.net_to)) + feedback_gap
        anchor_x = snap((from_x + to_x - geom.pins[pin_from].point[0] - geom.pins[pin_to].point[0]) / 2)
        # An end that joins a wire at a slot lands on it; the other end follows the part's own length.
        if from_slotted and not to_slotted:
            anchor_x = snap(from_x - geom.pins[pin_from].point[0])
        elif to_slotted and not from_slotted:
            anchor_x = snap(to_x - geom.pins[pin_to].point[0])
        anchor_y = snap(feedback_y - geom.pins[pin_from].point[1])
        # Two parts on the same two nets (parallel capacitors) stand side by side, the second on the next free
        # slots along the wire, never on the first's pins.
        for _shift in range(64):
            anchor = (anchor_x, anchor_y)
            box = core_at(ref, geom, anchor)
            grown = (box[0] - gap_mm, box[1], box[2] + gap_mm, box[3])
            if not any(boxes_overlap(grown, other) for _name, other in occupied) and not any(
                    math.dist((anchor[0] + pg.point[0], anchor[1] + pg.point[1]),
                              (placed[other].position[0] + og.point[0], placed[other].position[1] + og.point[1]))
                    < 2 * GRID_MM - _EPS
                    for pg in geom.pins.values() for other in placed for og in geoms[other].pins.values()):
                break
            anchor_x = snap(anchor_x + 2 * GRID_MM)
        placed[ref], geoms[ref] = Placement(anchor, orientation), geom
        occupied.append((ref, core_at(ref, geom, anchor)))
        rec.span(ref, item.net_from, item.net_to, orientation, "feedback")

    # Paired multi-pin parts keep rotation zero and occupy adjacent columns.
    for first, second in pairs:
        refs = (first, second)
        pair_base = max((b[2] for _r, b in occupied), default=0.0) + slot_pitch_mm
        first_anchor_x = None
        first_existing = placed.get(first)
        second_existing = placed.get(second)
        for index, ref in enumerate(refs):
            if ref not in by_ref or ref in placed:
                continue
            orientation, geom = geom_choice(ref, lambda o, g: o.rotation == 0 and o.mirror == "" and len(g.pins) >= 3)
            if index == 0 and second_existing is not None:
                second_box = box_at(second, geoms[second], second_existing.position)
                anchor_x = snap(second_box[0] - slot_pitch_mm - geom.box[2])
                anchor_y = second_existing.position[1]
            elif index == 1 and first_existing is not None:
                first_box = box_at(first, geoms[first], first_existing.position)
                anchor_x = snap(first_box[2] + slot_pitch_mm - geom.box[0])
                anchor_y = first_existing.position[1]
            elif index == 0:
                first_anchor_x = snap(pair_base - geom.box[0])
                anchor_x = first_anchor_x
                anchor_y = 0.0
            else:
                separation = max(slot_pitch_mm, geom.box[2] - geom.box[0])
                anchor_x = snap((first_anchor_x if first_anchor_x is not None else pair_base - geom.box[0]) + separation)
                anchor_y = 0.0
            outward = -1 if index == 0 and second_existing is not None else 1
            for _shift in range(64):
                # a member beside a partner already placed stands on the first free slots outward, clear of what hangs
                # there (a rung, an attachment)
                box = core_at(ref, geom, (anchor_x, anchor_y))
                grown = (box[0] - gap_mm, box[1] - gap_mm, box[2] + gap_mm, box[3] + gap_mm)
                if not any(boxes_overlap(grown, other) for _name, other in occupied) or not (
                        (index == 0 and second_existing is not None) or (index == 1 and first_existing is not None)):
                    break
                anchor_x = snap(anchor_x + outward * 2 * GRID_MM)
            anchor = (anchor_x, anchor_y)
            placed[ref], geoms[ref] = Placement(anchor, orientation), geom
            occupied.append((ref, core_at(ref, geom, anchor)))
            rec.free(ref, orientation)

    # Lane parts with no lane to stand on (the hub pin is a chain pin, or points up or down) stand in a row under everything.
    leftover = [ref for ref in dict.fromkeys(lane_leftover) if ref in by_ref and ref not in placed]
    if leftover:
        bottom = max((b[3] for _name, b in occupied), default=0.0)
        cursor = min((b[0] for _name, b in occupied), default=0.0)
        for ref in leftover:
            orientation, geom = min(by_ref[ref].geometry.items(), key=lambda item: (item[0].rotation, item[0].mirror))
            anchor = (snap(cursor - geom.box[0]), snap(bottom + 2 * gap_mm - geom.box[1]))
            placed[ref], geoms[ref] = Placement(anchor, orientation), geom
            occupied.append((ref, core_at(ref, geom, anchor)))
            rec.free(ref, orientation)
            cursor = anchor[0] + geom.box[2] + 2 * gap_mm

    # A trunk that one sideways pin of one multi-pin part serves, whose other chain pins all lie in rows on one side of that pin, is
    # free in the plan: it has no row of its own, the wire leaves the pin and goes on to them (a row beyond the part would
    # make the drop cross the lanes and pins on the way).  The plan leaves its height to the legaliser.
    def free_trunk(name: str) -> bool:
        serving = [(r, p) for r, p in pins_by_net.get(name, ()) if r in chain_parts and r in placed and p in geoms[r].pins]
        sideways = [(r, p) for r, p in serving if abs(geoms[r].pins[p].normal[0]) > .5 and _is_multi_pin(by_ref[r])]
        if len(sideways) != 1:
            return False
        r, p = sideways[0]
        y = placed[r].position[1] + geoms[r].pins[p].point[1]
        others = [placed[r2].position[1] + geoms[r2].pins[p2].point[1] for r2, p2 in serving if (r2, p2) != (r, p)]
        return all(o < y - _EPS for o in others) or all(o > y + _EPS for o in others)

    rec.rows({name: rows[name] for name in side_rows}, {name: rows[name] for name in trunk_nets if not free_trunk(name)}, chain_nets)
    unplaced = sorted(requested - placed.keys())
    if unplaced and not partial:
        raise ValueError(f"Could not place role symbols: {', '.join(unplaced)}")

    # Snapping is repeated here so every special placement follows the same anchor rule.
    for ref, placement in tuple(placed.items()):
        placed[ref] = Placement((snap(placement.position[0]), snap(placement.position[1])), placement.orientation)

    _reject_coincident_tips([
        (placed[ref].position[0] + pg.point[0], placed[ref].position[1] + pg.point[1], ref)
        for ref in placed for pg in geoms[ref].pins.values()])
    final_boxes = [(ref, core_at(ref, geoms[ref], placement.position)) for ref, placement in sorted(placed.items())]
    for i, (left_ref, a) in enumerate(final_boxes):
        for right_ref, b in final_boxes[i + 1:]:
            if boxes_overlap(a, b):
                raise ValueError(f"Placed symbols overlap: {left_ref} and {right_ref}")
    return placed
