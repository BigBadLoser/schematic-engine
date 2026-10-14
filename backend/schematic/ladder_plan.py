"""The plan a ladder layout is: which part sits on which row, hangs from which wire, spans which two wires; no coordinates.

The ladder placer (``ladder_layout``) is two things at once: a grammar of how engineers arrange a sheet (the signal
path through a device's axis pins, supports hung from the pin they serve, feedback over, supplies up and grounds down,
pairs face to face, a control feeder hung as a column) and a legaliser that turns that structure into coordinates
(slot searches, stand-offs, retries).  This module is the contract between the two, so each can be worked on alone: a
``Plan`` is pure data, JSON-able and without a single coordinate, and a legaliser (``ladder_solve``, or any other
program) assigns every part an anchor on the 1.27 mm grid that satisfies it.

How to read a plan
------------------
``rows``     horizontal lines parts sit on, in order from the top of the sheet to the bottom.  A ``chain`` row lists the
             chain parts that lie on it, left to right (chain order, face-to-face pair members included); the row
             steps (a new chain row begins) wherever the chain leaves a part at another height than it entered, so a
             stepped chain is several chain rows.  A ``trunk`` row is the line of a signal net three or more parts share,
             and a ``side`` row the line of a net that leaves a chain part by a pin off the chain row, up or down; both
             list no part: the parts on those nets hang from them.  ``above`` names the row directly over this one (None
             for the topmost).  A group stacked over (or under) a part (a load network above a collector) is rows in
             the same top-to-bottom order, placed over the pin it feeds.  The ``supplies`` kind is reserved for a row
             of supply symbols; the grammar currently puts free supplies in a column (below).
``hangs``    a part hung from a wire: the tip of its signal pin lands on the wire's line, the body lies beyond it
             (``side``), and ``rank`` orders the hangs that serve one pin from the same wire on the same side, nearest the
             pin first.  ``wire`` is a net: a trunk or side row names its row; any other net is the straight wire
             between the chain pins on it, at the height of ``host``'s pin (the pin of the chain part it serves, as
             "REF.PIN").
``spans``    a part whose two tips join two wires (a rung between two rows, a feedback part between two chain nets).
             ``from_wire`` is the upper wire of a rung, the earlier net of a feedback part.
``columns``  a vertical run of parts, top to bottom: hung from ``pin`` ("REF.PIN") when the pin is not empty (a control
             feeder: its part nearest the pin is its last chain part), else free-standing in the sheet's left margin
             above the first row (supplies that share no signal net with the circuit).
             A column whose ``pin`` reads "net:<name>" is a stacked pair (a half bridge): its two members, upper first, stand on
             one vertical line with the upper's down-facing pin on that net straight over the lower's up-facing pin and the
             net one straight vertical wire between the tips; parts hung from that wire sideways are ``hangs`` with side
             "left" or "right" (their tip lands on that vertical wire, host "UPPER.PIN"), and a part joining the wires of
             two stacked pairs is a ``span`` between those nets (a load across a bridge).  The ``net:`` columns are
             listed left to right (the legaliser keeps that order); the control pins face outward, and a two-pin part alone
             on a control pin's net is a hang on that gate's net (host "Q1.G", side the pin's side), not a chain row part.
``orientations``  the orientation of every part the plan places.
``pins``     per chain part, the pin whose tip lies on the part's chain row (the pin the chain enters by; "" when the
             part's anchor itself lies on the row).
``sequence`` (an addition to the section 4b sketch) per placed group, its chain parts left to right in one sequence
             across the row steps: a stepped chain is several rows, and a row's ``order`` alone does not say which row's
             parts lie further right.

The recording
-------------
The plan is a by-product of ``ladder_layout``: it fills a ``LayoutRecord`` with the decisions it makes (row of each chain
part, hosts and slot order of the hung parts, spans, columns, orientations) as it makes them, and ``plan_from_layout``
builds the ``Plan`` from the record.  The heuristic's coordinates are not recorded; the only thing taken from them is the
vertical order of rows (the heuristic decides it by a row's y).
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from .orientation import Orientation

if TYPE_CHECKING:
    from .ladder_roles import Roles
    from .placement_refinement import Connection, OrientedGeometry, Placement, PlacedSymbol, Route

_EPS = 1e-6
PLAN_VERSION = 1
LEAD_MM = 1.27           # the lead a pin tip stands off its symbol's body: padding in the box, not body


def body_box(geometry: "OrientedGeometry", lead_mm: float = LEAD_MM) -> tuple[float, float, float, float]:
    """The box less the lead on each side a pin points through (relative to the anchor): what must not overlap.

    Pin tips are connection points: two parts whose tips meet at a node touch there, and only their bodies must not.
    Same as the placer's own ``ladder_layout._core``."""
    left, top, right, bottom = geometry.box
    sides = {(round(pin.normal[0]), round(pin.normal[1])) for pin in geometry.pins.values()}
    if (-1, 0) in sides:
        left += lead_mm
    if (1, 0) in sides:
        right -= lead_mm
    if (0, -1) in sides:
        top += lead_mm
    if (0, 1) in sides:
        bottom -= lead_mm
    return (left, top, max(left, right), max(top, bottom))


@dataclass(frozen=True)
class Row:
    """One horizontal line parts sit on; rows are ordered top to bottom."""
    name: str                # "<block>#<n>" for a chain row, "<net>" for a trunk/side row, "supplies"
    kind: Literal["chain", "trunk", "lane", "side", "supplies"]
    above: str | None        # the row this one lies under (None for the topmost); lanes name their hub pin
    order: tuple[str, ...]   # references left to right in the order the grammar fixed (chain parts, pair members)


@dataclass(frozen=True)
class Hang:
    """A part hung from a wire."""
    reference: str
    wire: str                # the net whose wire the tip lands on
    host: str                # the pin the part serves ("Q1.3"), which fixes where along the wire it prefers to be
                             # (ranking only: any part's pin, a hung, span or lane part's included; the wire is what the tip lands on)
    side: Literal["above", "below", "left", "right"]
    orientation: Orientation  # chosen by the grammar (rail pin toward the rail, etc.)
    rank: int                # among hangs on the same wire, pin and side, nearest the host first (0 = nearest)


@dataclass(frozen=True)
class Span:
    """A part whose two tips join two rows or two columns (a rung, a feedback part, a bridge)."""
    reference: str
    from_wire: str
    to_wire: str
    orientation: Orientation


@dataclass(frozen=True)
class Column:
    """A vertical run hung from a pin (a control feeder, a source with its sense resistor)."""
    pin: str
    order: tuple[str, ...]
    orientations: tuple[Orientation, ...]    # top to bottom


@dataclass(frozen=True)
class Plan:
    rows: tuple[Row, ...]
    hangs: tuple[Hang, ...]
    spans: tuple[Span, ...]
    columns: tuple[Column, ...]
    orientations: dict[str, Orientation]     # every placed part
    pins: dict[str, str]                     # which pin of each chain part lies on its row ("U1.3")
    # Not in the section 4b sketch: per placed group, its chain parts left to right in one sequence across the row steps.
    # A stepped chain is several rows, and a row's ``order`` alone does not say which row's parts lie further right.
    sequence: tuple[tuple[str, ...], ...] = ()

    # JSON ----------------------------------------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The plan as plain dicts, lists and strings: ``json.dumps`` writes it, ``from_json`` reads it back."""
        return {
            "version": PLAN_VERSION,
            "rows": [{"name": r.name, "kind": r.kind, "above": r.above, "order": list(r.order)} for r in self.rows],
            "hangs": [{"reference": h.reference, "wire": h.wire, "host": h.host, "side": h.side,
                       "orientation": _orientation_json(h.orientation), "rank": h.rank} for h in self.hangs],
            "spans": [{"reference": s.reference, "from_wire": s.from_wire, "to_wire": s.to_wire,
                       "orientation": _orientation_json(s.orientation)} for s in self.spans],
            "columns": [{"pin": c.pin, "order": list(c.order),
                         "orientations": [_orientation_json(o) for o in c.orientations]} for c in self.columns],
            "orientations": {ref: _orientation_json(o) for ref, o in sorted(self.orientations.items())},
            "pins": dict(sorted(self.pins.items())),
            "sequence": [list(group) for group in self.sequence],
        }

    @classmethod
    def from_json(cls, data: "Mapping[str, Any] | str") -> "Plan":
        """Rebuild a plan from ``to_json``'s output (a dict, or the JSON text of one)."""
        if isinstance(data, str):
            data = json.loads(data)
        if data.get("version", PLAN_VERSION) != PLAN_VERSION:
            raise ValueError(f"unknown plan version {data.get('version')!r}")
        return cls(
            rows=tuple(Row(r["name"], r["kind"], r["above"], tuple(r["order"])) for r in data["rows"]),
            hangs=tuple(Hang(h["reference"], h["wire"], h["host"], h["side"], _orientation(h["orientation"]), h["rank"])
                        for h in data["hangs"]),
            spans=tuple(Span(s["reference"], s["from_wire"], s["to_wire"], _orientation(s["orientation"]))
                        for s in data["spans"]),
            columns=tuple(Column(c["pin"], tuple(c["order"]), tuple(_orientation(o) for o in c["orientations"]))
                          for c in data["columns"]),
            orientations={ref: _orientation(o) for ref, o in data["orientations"].items()},
            pins=dict(data["pins"]),
            sequence=tuple(tuple(group) for group in data.get("sequence", ())),
        )

    def dumps(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True)

    # reading ------------------------------------------------------------------------------------------------------

    def parts(self) -> set[str]:
        """Every reference the plan places."""
        return set(self.orientations)

    def summary(self) -> str:
        """The plan in a few lines a person can read (rows top to bottom, then hangs, spans and columns)."""
        def turn(o: Orientation) -> str:
            return f"{o.rotation}{o.mirror}"

        lines = []
        for row in self.rows:
            parts = " ".join(f"{ref}[{self.pins.get(ref, '')}]" for ref in row.order)
            lines.append(f"row {row.name} ({row.kind}, under {row.above or '-'}): {parts}".rstrip(": "))
        for hang in self.hangs:
            lines.append(f"hang {hang.reference}@{turn(hang.orientation)} on {hang.wire} {hang.side} of {hang.host} rank {hang.rank}")
        for span in self.spans:
            lines.append(f"span {span.reference}@{turn(span.orientation)} {span.from_wire} -> {span.to_wire}")
        for column in self.columns:
            lines.append(f"column from {column.pin or '(margin)'}: " + " ".join(
                f"{ref}@{turn(o)}" for ref, o in zip(column.order, column.orientations)))
        return "\n".join(lines)


def _orientation_json(orientation: Orientation) -> dict[str, Any]:
    return {"rotation": orientation.rotation, "mirror": orientation.mirror}


def _orientation(data: Mapping[str, Any]) -> Orientation:
    return Orientation(int(data["rotation"]), str(data.get("mirror", "")))


# ---------------------------------------------------------------------------------------------------------------------
# the record ``ladder_layout`` fills while it decides


@dataclass
class GroupRecord:
    """What one placed group (a chain row with everything that hangs from it) decided, in the order it decided it.

    ``ladder_layout`` creates one per attempt (a retry starts a new one) and calls the methods below at its decision
    points; none of them changes what the layout does."""
    chain_parts: list[tuple[str, str, str, float, Orientation]] = field(default_factory=list)   # block, ref, row pin, row y, orientation
    column_parts: list[tuple[str, str, str, str, int, Orientation]] = field(default_factory=list)  # block, host, host pin, ref, direction, orientation
    spans: list[tuple[str, str, str, Orientation, str]] = field(default_factory=list)           # ref, from, to, orientation, kind
    hung: list[tuple[str, str, str, str, str, Orientation]] = field(default_factory=list)       # ref, net, host, host pin, side, orientation
    standalone: list[tuple[str, Orientation]] = field(default_factory=list)                      # pair members placed beside the rest
    side_rows: dict[str, float] = field(default_factory=dict)
    lanes: list[tuple[str, str, tuple[str, ...], float]] = field(default_factory=list)          # net, hub pin ("U9.4"), series parts, y
    trunk_rows: dict[str, float] = field(default_factory=dict)
    chain_nets: frozenset = frozenset()

    def chain(self, block: str, ref: str, row_pin: str | None, row_y: float, orientation: Orientation,
              like: str | None = None) -> None:
        """A chain part on the row.  ``like`` names the part this one faces (a pair is drawn face to face, the second the
        mirror of the first): it is on that part's row by the same pin, whatever pin it enters the chain by."""
        if like is not None:
            mate = next((item for item in reversed(self.chain_parts) if item[1] == like), None)
            if mate is not None:
                row_pin, row_y = mate[2] or None, mate[3]
        self.chain_parts.append((block, ref, row_pin or "", float(row_y), orientation))

    def column(self, block: str, host: str, host_pin: str, ref: str, direction: int, orientation: Orientation) -> None:
        self.column_parts.append((block, host, host_pin, ref, direction, orientation))

    def span(self, ref: str, from_net: str, to_net: str, orientation: Orientation, kind: str) -> None:
        self.spans.append((ref, from_net, to_net, orientation, kind))

    def hang(self, ref: str, net: str, host: str, host_pin: str | None, side: str, orientation: Orientation) -> None:
        self.hung.append((ref, net, host, host_pin or "", side, orientation))

    def free(self, ref: str, orientation: Orientation) -> None:
        self.standalone.append((ref, orientation))

    def lane(self, net: str, hub_pin: str, series: Sequence[str], y: float) -> None:
        """A lane drawn from a hub pin: the net on the pin, the series parts along it in order, the row's y."""
        self.lanes.append((net, hub_pin, tuple(series), float(y)))

    def rows(self, side_rows: Mapping[str, float], trunk_rows: Mapping[str, float], chain_nets) -> None:
        self.side_rows, self.trunk_rows = dict(side_rows), dict(trunk_rows)
        self.chain_nets = frozenset(chain_nets)


@dataclass
class StackRecord:
    """A stacked pair the layout recorded: two parts on one vertical column, the upper's down pin over the lower's up pin."""
    upper: str
    lower: str
    net: str                 # the junction net: one straight wire between the two tips
    upper_pin: str
    upper_orientation: Orientation
    lower_orientation: Orientation
    hangs: list[tuple[str, str, Orientation]] = field(default_factory=list)       # ref, side ("left" or "right"), orientation
    spans: list[tuple[str, str, str, Orientation]] = field(default_factory=list)   # ref, from net, to net, orientation
    feeders: list[tuple[str, str, str, str, Orientation]] = field(default_factory=list)   # ref, gate net, host pin "Q1.1", side, orientation


class NoRecord:
    """Stands in for a ``GroupRecord`` when nobody asked for a plan: every hook does nothing."""

    def chain(self, *args, **kwargs) -> None: ...
    def column(self, *args, **kwargs) -> None: ...
    def span(self, *args, **kwargs) -> None: ...
    def hang(self, *args, **kwargs) -> None: ...
    def free(self, *args, **kwargs) -> None: ...
    def lane(self, *args, **kwargs) -> None: ...
    def rows(self, *args, **kwargs) -> None: ...


@dataclass
class LayoutRecord:
    """What ``ladder_layout`` decided across the groups: filled by it, read by ``plan_from_record``."""
    groups: list[GroupRecord] = field(default_factory=list)
    offsets: dict[int, tuple[float, float]] = field(default_factory=dict)      # group index -> where its rows ended up
    stacks: dict[int, tuple[str, str, str]] = field(default_factory=dict)      # group index -> (host, host pin, net) it stacks over
    lone: list[tuple[str, Orientation]] = field(default_factory=list)          # free supplies, top to bottom
    # Ask ``ladder_layout`` to go on past a hung part it finds no slot for (the plan then lists it, the placements
    # lack it) instead of raising: the decisions after the failure are still worth having as a plan.
    partial: bool = False
    pair_stacks: list[StackRecord] = field(default_factory=list)                  # stacked pairs, left to right
    beside: dict[str, tuple[str, Orientation, int]] = field(default_factory=dict)      # a power unit -> (the placed unit it stands beside, orientation, +1 right of it or -1 left)


def plan_from_record(record: LayoutRecord, symbols: Sequence["PlacedSymbol"], nets: Sequence["Connection"]) -> Plan:
    """Build the plan from what the layout recorded (see the module docstring for what each part of it means)."""
    del symbols
    pin_of_ref_net: dict[tuple[str, str], str] = {}
    for net in nets:
        for ref, pin in sorted(net.pins):
            pin_of_ref_net.setdefault((ref, net.net), pin)

    rows: list[tuple[float, int, str, Row]] = []         # y, kind order, name, row
    hangs: list[Hang] = []
    spans: list[Span] = []
    columns: list[Column] = []
    orientations: dict[str, Orientation] = {}
    pins: dict[str, str] = {}
    block_rows: dict[str, int] = defaultdict(int)
    sequence: list[tuple[str, ...]] = []
    pending_rank: dict[tuple[str, str, str], int] = defaultdict(int)

    for index, group in enumerate(record.groups):
        dy = record.offsets.get(index, (0.0, 0.0))[1]
        if group.chain_parts:
            sequence.append(tuple(item[1] for item in group.chain_parts))
        # chain rows: consecutive chain parts at one height are one row
        segment: list[tuple[str, str, str, float, Orientation]] = []

        def close_segment() -> None:
            if not segment:
                return
            block = segment[0][0]
            name = f"{block}#{block_rows[block]}"
            block_rows[block] += 1
            order = tuple(item[1] for item in segment)
            rows.append((segment[0][3] + dy, 0, name, Row(name, "chain", None, order)))
            segment.clear()

        for item in group.chain_parts:
            _block, ref, row_pin, row_y, orientation = item
            if segment and abs(segment[-1][3] - row_y) > _EPS:
                close_segment()
            segment.append(item)
            orientations[ref] = orientation
            pins[ref] = row_pin
        close_segment()
        for name, y in sorted(group.side_rows.items()):
            rows.append((y + dy, 1, name, Row(name, "side", None, ())))
        for name, y in sorted(group.trunk_rows.items()):
            rows.append((y + dy, 2, name, Row(name, "trunk", None, ())))
        for net, hub_pin, series, y in sorted(group.lanes, key=lambda item: (item[1], item[3])):
            rows.append((y + dy, 3, net, Row(net, "lane", hub_pin, series)))

        # control feeders: a column from the host pin, top to bottom
        feeders: dict[tuple[str, str, str], list] = defaultdict(list)
        for block, host, host_pin, ref, direction, orientation in group.column_parts:
            feeders[(block, host, host_pin)].append((ref, direction, orientation))
        for (block, host, host_pin), items in sorted(feeders.items()):
            nearest_first = items                       # the layout places the part nearest the pin first
            direction = nearest_first[0][1]
            top_to_bottom = nearest_first if direction > 0 else nearest_first[::-1]
            columns.append(Column(f"{host}.{host_pin}", tuple(i[0] for i in top_to_bottom),
                                  tuple(i[2] for i in top_to_bottom)))
            for ref, _direction, orientation in items:
                orientations[ref] = orientation

        for ref, from_net, to_net, orientation, _kind in group.spans:
            spans.append(Span(ref, from_net, to_net, orientation))
            orientations[ref] = orientation
        for ref, orientation in group.standalone:
            orientations[ref] = orientation

        for ref, net, host, host_pin, side, orientation in group.hung:
            hang_side = "below" if side == "ground" else "above"
            key = (net, hang_side, f"{host}.{host_pin}")
            hangs.append(Hang(ref, net, f"{host}.{host_pin}" if host_pin else host, hang_side, orientation,
                              pending_rank[key]))
            pending_rank[key] += 1
            orientations[ref] = orientation

    for stack in record.pair_stacks:
        columns.append(Column(f"net:{stack.net}", (stack.upper, stack.lower),
                              (stack.upper_orientation, stack.lower_orientation)))
        orientations[stack.upper], orientations[stack.lower] = stack.upper_orientation, stack.lower_orientation
        for rank, (ref, side, orientation) in enumerate(stack.hangs):
            hangs.append(Hang(ref, stack.net, f"{stack.upper}.{stack.upper_pin}", side, orientation, rank))
            orientations[ref] = orientation
        for ref, from_net, to_net, orientation in stack.spans:
            spans.append(Span(ref, from_net, to_net, orientation))
            orientations[ref] = orientation

    for stack in record.pair_stacks:
        for ref, net, host, side, orientation in stack.feeders:
            hangs.append(Hang(ref, net, host, side, orientation, 0))
            orientations[ref] = orientation

    for ref, orientation in record.lone:
        orientations[ref] = orientation
    if record.lone:
        columns.append(Column("", tuple(ref for ref, _o in record.lone), tuple(o for _r, o in record.lone)))

    ordered = sorted(rows, key=lambda item: item[:3])
    named: list[Row] = []
    above: str | None = None
    for _y, _kind, name, row in ordered:
        if row.kind == "lane":
            named.append(row)                # a lane lies by its hub pin, not under the row before it
            continue
        named.append(Row(row.name, row.kind, above, row.order))
        above = row.name
    sequence_groups = [list(group) for group in sequence]
    # A power unit stands beside a unit of its part: in that unit's row when the unit is at an end of it (a wire runs between
    # two parts of a row and the unit must not stand on it), else with the supplies at the left.
    for ref, (sibling, orientation, _side) in sorted(record.beside.items()):
        orientations[ref] = orientation
        pins[ref] = ""
        for index, row in enumerate(named):
            if row.kind == "chain" and sibling in row.order and row.order.index(sibling) in (0, len(row.order) - 1):
                after = row.order.index(sibling) != 0 or len(row.order) == 1
                where = row.order.index(sibling) + (1 if after else 0)
                named[index] = Row(row.name, row.kind, row.above, row.order[:where] + (ref,) + row.order[where:])
                for group in sequence_groups:
                    if sibling in group:
                        group.insert(group.index(sibling) + (1 if after else 0), ref)
                        break
                break
        else:
            pins.pop(ref, None)
            columns.append(Column("", (ref,), (orientation,)))
    sequence = [tuple(group) for group in sequence_groups]
    return Plan(
        rows=tuple(named),
        hangs=tuple(sorted(hangs, key=lambda h: (h.wire, h.side, h.host, h.rank, h.reference))),
        spans=tuple(sorted(spans, key=lambda s: s.reference)),
        columns=tuple(columns),
        orientations=dict(sorted(orientations.items())),
        pins=dict(sorted(pins.items())),
        sequence=tuple(sequence),
    )


def plan_and_layout(
    symbols: Sequence["PlacedSymbol"], nets: Sequence["Connection"], roles: "Roles", *,
    text_widths: Mapping[str, float] | None = None, gap_mm: float = 2.54, slot_pitch_mm: float = 7.62,
    partial: bool = False,
) -> tuple[Plan, "dict[str, Placement]"]:
    """The plan the heuristic placer follows and the placements it makes from it (the placements are its own business).

    With ``partial`` a hung part the placer finds no slot for does not stop it: it is in the plan, not in the placements."""
    from .ladder_layout import ladder_layout

    record = LayoutRecord(partial=partial)
    placements = ladder_layout(symbols, nets, roles, text_widths=text_widths or {}, gap_mm=gap_mm,
                               slot_pitch_mm=slot_pitch_mm, record=record)
    return plan_from_record(record, symbols, nets), placements


def plan_from_layout(
    symbols: Sequence["PlacedSymbol"], nets: Sequence["Connection"], roles: "Roles", *,
    text_widths: Mapping[str, float] | None = None, gap_mm: float = 2.54, slot_pitch_mm: float = 7.62,
) -> Plan:
    """The ``Plan`` the heuristic placer's decisions make, without its coordinates."""
    return plan_and_layout(symbols, nets, roles, text_widths=text_widths, gap_mm=gap_mm,
                           slot_pitch_mm=slot_pitch_mm)[0]


def plan_routes(plan: Plan, symbols: Sequence["PlacedSymbol"], nets: Sequence["Connection"]) -> "dict[str, list[Route]]":
    """The wires the plan draws on a placement, net by net, as the routes the placement model scores.

    The plan is the intended wiring, and the router draws it: a net of three or more pins is a trunk with a drop from each
    pin (a drop along the pin's normal, none for a tip on the trunk, a lead of one gap and a drop for a sideways pin off it),
    at the height of the tips of the parts hung from it when it has hangs, else at the cheapest height the pins allow; a
    stack's junction net is the one straight vertical wire between the two tips.  Nets the plan does not wire (two pins,
    a pin on a part it does not place, a trunk no row can carry) are left out, and the estimate scores them."""
    from .placement_refinement import (
        ROUTE_MARGIN_MM, TRUNK_ROWS, TRUNK_STEP_MM, Route, Weights, _trunk_on_row, _trunk_rows, route_charge,
    )

    by_ref = {symbol.reference: symbol for symbol in symbols}
    weights = Weights()
    LEAD_STEPS, LEAD_STEP_MM = 20, 1.27         # a lead may be longer than the gap, to clear what stands in its way

    def tip(ref: str, pin: str) -> tuple[tuple[float, float], tuple[float, float]]:
        symbol = by_ref[ref]
        item = symbol.geometry[symbol.orientation].pins[pin]
        return (symbol.position[0] + item.point[0], symbol.position[1] + item.point[1]), item.normal

    stacks = {column.pin[4:]: column.order for column in plan.columns if column.pin.startswith("net:")}

    def obstacles(net) -> tuple:
        """What a route of ``net`` must keep clear of: other pins' tips, bodies (the margin the checker keeps from a part the
        net does not join) and the rooms the rail pins' symbols are drawn in."""
        own = set(net.pins)
        members = {ref for ref, _pin in net.pins}
        found = []
        for ref, symbol in by_ref.items():
            geometry = symbol.geometry[symbol.orientation]
            x0, y0 = symbol.position
            foreign = tuple(sorted((x0 + item.point[0], y0 + item.point[1])
                                   for pin, item in geometry.pins.items() if (ref, pin) not in own))
            mine = tuple(sorted((x0 + item.point[0], y0 + item.point[1])
                                for pin, item in geometry.pins.items() if (ref, pin) in own))
            solid = geometry.body or geometry.box
            found.append((foreign, (x0 + solid[0], y0 + solid[1], x0 + solid[2], y0 + solid[3]), mine,
                          0.0 if ref in members else ROUTE_MARGIN_MM))
        for other in nets:
            if other.style != "power":
                continue
            for ref, pin in other.pins:
                if ref not in by_ref or pin not in by_ref[ref].geometry[by_ref[ref].orientation].pins:
                    continue
                (tx, ty), (nx, ny) = tip(ref, pin)
                ex, ey = tx + nx * 7.62, ty + ny * 7.62
                found.append(((), (min(tx, ex) - 1.27 * abs(ny), min(ty, ey) - 1.27 * abs(nx),
                                   max(tx, ex) + 1.27 * abs(ny), max(ty, ey) + 1.27 * abs(nx)), (), 0.0))
        return tuple(found)

    routes: "dict[str, list[Route]]" = {}
    for whole in nets:
        if whole.style != "wire" or len(whole.pins) < 2:
            continue
        # The pins the plan places are wired by the plan; a pin on a part it leaves out is joined afterwards (see
        # ``attach_unplanned``). The routes' ``pins`` index the whole net's pins.
        kept = [i for i, (ref, pin) in enumerate(whole.pins)
                if ref in plan.orientations and ref in by_ref
                and pin in by_ref[ref].geometry[by_ref[ref].orientation].pins]
        if len(kept) < 2:
            continue
        net = replace(whole, pins=tuple(whole.pins[i] for i in kept))

        def remap(built, kept=kept):
            return [replace(r, pins=tuple(kept[i] for i in r.pins)) for r in built]

        pins = [tip(ref, pin) for ref, pin in net.pins]
        if net.net in stacks:
            upper, lower = stacks[net.net]
            ends = [next((i for i, (ref, _pin) in enumerate(net.pins) if ref == part), None) for part in (upper, lower)]
            if None in ends:
                continue
            (xu, yu), (xl, yl) = pins[ends[0]][0], pins[ends[1]][0]
            routes[net.net] = remap([Route(((xu, yu), (xl, yl)), 0, abs(yl - yu), tuple(ends))])
            continue
        if len(pins) < 3:
            continue
        on_row = [hang.reference for hang in plan.hangs if hang.wire == net.net and hang.side in ("above", "below")]
        heights = [pins[i][0][1] for i, (ref, _pin) in enumerate(net.pins) if ref in on_row]
        for span in getattr(plan, "spans", ()):
            # A rung between two rows lands its upper tip exactly on its upper wire's trunk.
            if span.reference not in by_ref or net.net not in (span.from_wire, span.to_wire):
                continue
            symbol = by_ref[span.reference]
            local = {pin: item.point for pin, item in symbol.geometry[symbol.orientation].pins.items()}
            ends = [(i, pin) for i, (ref, pin) in enumerate(net.pins) if ref == span.reference]
            if len(ends) == 1 and len(local) == 2 and abs(local[ends[0][1]][0] - [p[0] for q, p in local.items() if q != ends[0][1]][0]) < _EPS:
                other = next(p for q, p in local.items() if q != ends[0][1])
                if local[ends[0][1]][1] < other[1]:
                    heights.append(pins[ends[0][0]][0][1])
        around = obstacles(net)

        def charge(routes):
            return route_charge(routes, around, weights.short, weights.through)

        def dodged(y):
            """The trunk on row ``y`` with each sideways pin's lead as long as it takes to clear what lies in its way."""
            leads: dict[int, float] = {}
            built = _trunk_on_row(pins, y, leads)
            if built is None:
                return None
            best = (charge(built), built)
            for i, (point, normal) in enumerate(pins):
                if not normal[0] or abs(normal[0]) < .5 or abs(point[1] - y) < _EPS:
                    continue
                for step in range(LEAD_STEPS):
                    trial = {**leads, i: TRUNK_STEP_MM + step * LEAD_STEP_MM}
                    candidate = _trunk_on_row(pins, y, trial)
                    if candidate is None:
                        continue
                    price = charge(candidate)
                    if price < best[0] - _EPS:
                        best, leads = (price, candidate), trial
                    if price < _EPS:
                        break
                built = best[1]
            return built

        def priced(built):
            return sum(r.length + 5.0 * r.bends for r in built) + charge(built)

        if heights:
            built = dodged(heights[0])
        else:
            rows: set[float] = set()
            for point, normal in pins:
                if abs(normal[0]) > .5:
                    rows.add(round(point[1], 4))
                else:
                    rows.update(round(point[1] + normal[1] * k * TRUNK_STEP_MM, 4) for k in range(TRUNK_ROWS + 1))
            candidates = [c for c in (dodged(y) for y in sorted(rows)) if c]
            swapped = [((p[1], p[0]), (n[1], n[0])) for p, n in pins]
            flipped = _trunk_rows(swapped)
            if flipped is not None:
                candidates.append([replace(r, points=tuple((q[1], q[0]) for q in r.points)) for r in flipped])
            built = min(candidates, key=priced) if candidates else None
        if built:
            routes[net.net] = remap(built)
    return routes


def attach_unplanned(plan: Plan, symbols: Sequence["PlacedSymbol"], nets: Sequence["Connection"],
                     routes: "dict[str, list[Route]]") -> "dict[str, list[Route]]":
    """``routes`` with every pin on a part the plan leaves out joined to its net's wire the way the estimate joins a pin to a
    run already drawn (at right angles to it, a T), so that a net the plan wires in part is scored as a whole."""
    from .placement_refinement import _attach, _segments, estimate_route

    by_ref = {symbol.reference: symbol for symbol in symbols}
    done = dict(routes)
    for net in nets:
        if net.net not in routes:
            continue
        tree = [segment for route in routes[net.net] for segment in _segments(route.points)]
        extra = []
        for i, (ref, pin) in enumerate(net.pins):
            if ref in plan.orientations or ref not in by_ref:
                continue
            symbol = by_ref[ref]
            item = symbol.geometry[symbol.orientation].pins.get(pin)
            if item is None or not tree:
                continue
            point = (symbol.position[0] + item.point[0], symbol.position[1] + item.point[1])
            route = _attach(point, item.normal, tree)
            if route is None:
                continue
            extra.append(replace(route, pins=(i,)))
            tree += _segments(route.points)
        if extra:
            done[net.net] = [*routes[net.net], *extra]
    return done
