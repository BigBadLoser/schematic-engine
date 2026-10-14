from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .intent import NetIntent, PinEndpoint, SchematicIntent


RelationSide = Literal["left", "right", "above", "below"]
RelationSideSpec = RelationSide | Literal["auto"]
RelationKind = Literal[
    "anchor_side",
    "branch",
    "series",
    "pull_to_rail",
    "shunt_to_rail",
    "parallel_bank",
]


@dataclass(frozen=True)
class AnchorSideRelation:
    subjects: tuple[str, ...]
    anchor: PinEndpoint
    side: RelationSide
    distance_steps: int = 2
    kind: Literal["anchor_side"] = "anchor_side"

    def __post_init__(self) -> None:
        if not self.subjects:
            raise ValueError("anchor_side requires at least one subject")
        if len(set(self.subjects)) != len(self.subjects):
            raise ValueError("anchor_side subjects must be unique")
        if self.distance_steps < 1:
            raise ValueError("distance_steps must be positive")


@dataclass(frozen=True)
class BranchRelation:
    subject: str
    anchor: PinEndpoint
    side: RelationSideSpec = "auto"
    distance_steps: int = 2
    kind: Literal["branch"] = "branch"
    # The way the part's far pin points, for a part that no rail points: None leaves it turned as its library draws
    # it (a diode lies on its side). A rail decorator, when there is one, decides instead.
    extend: RelationSide | None = None

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("branch subject is required")
        if self.distance_steps < 1:
            raise ValueError("distance_steps must be positive")


@dataclass(frozen=True)
class SeriesRelation:
    subject: str
    first: PinEndpoint
    second: PinEndpoint
    side: RelationSide
    distance_steps: int = 2
    kind: Literal["series"] = "series"

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("series subject is required")
        if self.first == self.second:
            raise ValueError("series anchors must differ")
        if self.distance_steps < 1:
            raise ValueError("distance_steps must be positive")


@dataclass(frozen=True)
class PullToRailRelation:
    subject: str
    node: PinEndpoint
    rail: str
    rail_group: str | None = None
    kind: Literal["pull_to_rail"] = "pull_to_rail"

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.rail.strip():
            raise ValueError("pull_to_rail requires subject and rail")


@dataclass(frozen=True)
class ShuntToRailRelation:
    subject: str
    node: PinEndpoint
    rail: str
    rail_group: str | None = None
    kind: Literal["shunt_to_rail"] = "shunt_to_rail"

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.rail.strip():
            raise ValueError("shunt_to_rail requires subject and rail")


@dataclass(frozen=True)
class ParallelBankRelation:
    subjects: tuple[str, ...]
    anchors: tuple[PinEndpoint, ...]
    near_rail: str
    far_rail: str
    side: RelationSide
    distance_steps: int = 2
    group_prefix: str = "parallel_bank"
    kind: Literal["parallel_bank"] = "parallel_bank"

    def __post_init__(self) -> None:
        if not self.subjects:
            raise ValueError("parallel_bank requires at least one subject")
        if len(self.subjects) != len(self.anchors):
            raise ValueError("parallel_bank subjects and anchors must be one-to-one")
        if len(set(self.subjects)) != len(self.subjects):
            raise ValueError("parallel_bank subjects must be unique")
        if not self.near_rail.strip() or not self.far_rail.strip():
            raise ValueError("parallel_bank rails are required")
        if self.near_rail == self.far_rail:
            raise ValueError("parallel_bank rails must differ")
        if self.distance_steps < 1:
            raise ValueError("distance_steps must be positive")


SchematicRelation: TypeAlias = (
    AnchorSideRelation
    | BranchRelation
    | SeriesRelation
    | PullToRailRelation
    | ShuntToRailRelation
    | ParallelBankRelation
)


@dataclass(frozen=True)
class PinRelationConstraint:
    reference: str
    pin: str
    anchor: PinEndpoint
    side: RelationSide
    distance_steps: int
    far_pin: str | None = None
    extend_toward: RelationSide | None = None
    source_kind: str = "anchor_side"


@dataclass(frozen=True)
class NodeClusterMemberConstraint:
    reference: str
    pin: str
    far_pin: str | None = None
    extend_toward: RelationSide | None = None


@dataclass(frozen=True)
class NodeClusterConstraint:
    anchor: PinEndpoint
    side: RelationSide
    distance_steps: int
    members: tuple[NodeClusterMemberConstraint, ...]


@dataclass(frozen=True)
class SeriesRelationConstraint:
    reference: str
    pin_a: str
    pin_b: str
    anchor_a: PinEndpoint
    anchor_b: PinEndpoint
    side: RelationSide
    distance_steps: int


@dataclass(frozen=True)
class ParallelBankConstraint:
    references: tuple[str, ...]
    near_pins: tuple[str, ...]
    far_pins: tuple[str, ...]
    anchors: tuple[PinEndpoint, ...]
    side: RelationSide
    distance_steps: int
    far_direction: RelationSide


PlacementConstraint: TypeAlias = (
    PinRelationConstraint
    | NodeClusterConstraint
    | SeriesRelationConstraint
    | ParallelBankConstraint
)


@dataclass(frozen=True)
class PlacementGroupConstraint:
    name: str
    placement_indexes: tuple[int, ...]
    root_placement_indexes: tuple[int, ...]
    references: tuple[str, ...]
    root_anchors: tuple[PinEndpoint, ...]
    side: RelationSide


@dataclass(frozen=True)
class PowerGroupConstraint:
    name: str
    net: str
    endpoints: tuple[PinEndpoint, ...]
    direction: RelationSide


@dataclass(frozen=True)
class NodeConnectionConstraint:
    net: str
    anchor: PinEndpoint
    members: tuple[PinEndpoint, ...]
    shared_node: bool = False


@dataclass(frozen=True)
class CompiledSchematicRelations:
    placements: tuple[PlacementConstraint, ...]
    placement_groups: tuple[PlacementGroupConstraint, ...]
    power_groups: tuple[PowerGroupConstraint, ...]
    node_connections: tuple[NodeConnectionConstraint, ...]

    @property
    def handled_power_tokens(self) -> frozenset[str]:
        return frozenset(
            endpoint.token
            for group in self.power_groups
            for endpoint in group.endpoints
        )


def anchor_side(
    *subjects: str,
    anchor: PinEndpoint,
    side: RelationSide,
    distance_steps: int = 2,
) -> AnchorSideRelation:
    return AnchorSideRelation(tuple(subjects), anchor, side, distance_steps)


def branch(
    subject: str,
    *,
    anchor: PinEndpoint,
    side: RelationSideSpec = "auto",
    distance_steps: int = 2,
    extend: RelationSide | None = None,
) -> BranchRelation:
    return BranchRelation(subject, anchor, side, distance_steps, extend=extend)


def series(
    subject: str,
    *,
    first: PinEndpoint,
    second: PinEndpoint,
    side: RelationSide,
    distance_steps: int = 2,
) -> SeriesRelation:
    return SeriesRelation(subject, first, second, side, distance_steps)


def pull_to_rail(
    subject: str,
    *,
    node: PinEndpoint,
    rail: str,
    rail_group: str | None = None,
) -> PullToRailRelation:
    return PullToRailRelation(subject, node, rail, rail_group)


def shunt_to_rail(
    subject: str,
    *,
    node: PinEndpoint,
    rail: str,
    rail_group: str | None = None,
) -> ShuntToRailRelation:
    return ShuntToRailRelation(subject, node, rail, rail_group)


def parallel_bank(
    subjects: tuple[str, ...],
    *,
    anchors: tuple[PinEndpoint, ...],
    near_rail: str,
    far_rail: str,
    side: RelationSide,
    distance_steps: int = 2,
    group_prefix: str = "parallel_bank",
) -> ParallelBankRelation:
    return ParallelBankRelation(
        subjects=subjects,
        anchors=anchors,
        near_rail=near_rail,
        far_rail=far_rail,
        side=side,
        distance_steps=distance_steps,
        group_prefix=group_prefix,
    )


def _net_for_endpoint(intent: SchematicIntent, endpoint: PinEndpoint) -> NetIntent:
    matches = [net for net in intent.all_nets if endpoint in net.endpoints]
    if len(matches) != 1:
        raise ValueError(
            f"{endpoint.token} must belong to exactly one net; found {len(matches)}"
        )
    return matches[0]


def _net_named(intent: SchematicIntent, name: str) -> NetIntent:
    matches = [net for net in intent.all_nets if net.name == name]
    if len(matches) != 1:
        raise ValueError(f"net {name!r} must exist exactly once")
    return matches[0]


def _pin_on_net(intent: SchematicIntent, reference: str, net_name: str) -> PinEndpoint:
    net = _net_named(intent, net_name)
    matches = [endpoint for endpoint in net.endpoints if endpoint.reference == reference]
    if len(matches) != 1:
        raise ValueError(
            f"{reference} must have exactly one pin on {net_name}; found {len(matches)}"
        )
    return matches[0]


def _rail_direction(intent: SchematicIntent, rail: str) -> RelationSide:
    net = _net_named(intent, rail)
    if net.kind == "ground" or rail.upper() in {"GND", "VSS", "AGND", "DGND"}:
        return "below"
    return "above"


def _dedupe_endpoints(values: list[PinEndpoint]) -> tuple[PinEndpoint, ...]:
    result: list[PinEndpoint] = []
    seen: set[PinEndpoint] = set()
    for endpoint in values:
        if endpoint in seen:
            continue
        seen.add(endpoint)
        result.append(endpoint)
    return tuple(result)


def compile_schematic_relations(
    intent: SchematicIntent,
    relations: tuple[SchematicRelation, ...],
) -> CompiledSchematicRelations:
    """Compile semantic drawing relationships into coordinate-free constraints."""

    known_refs = set(intent.references)
    decorators: dict[str, PullToRailRelation | ShuntToRailRelation] = {}
    primary_subjects: set[str] = set()

    for relation in relations:
        subjects: tuple[str, ...]
        if isinstance(relation, AnchorSideRelation):
            subjects = relation.subjects
        elif isinstance(relation, ParallelBankRelation):
            subjects = relation.subjects
        else:
            subjects = (relation.subject,)

        missing = sorted(set(subjects) - known_refs)
        if missing:
            raise ValueError(
                f"{relation.kind} references unknown symbols: {', '.join(missing)}"
            )

        if isinstance(relation, (PullToRailRelation, ShuntToRailRelation)):
            if relation.subject in decorators:
                raise ValueError(
                    f"{relation.subject} has multiple rail decorators"
                )
            _net_for_endpoint(intent, relation.node)
            _net_named(intent, relation.rail)
            decorators[relation.subject] = relation

    placements: list[PlacementConstraint] = []
    node_connections: list[NodeConnectionConstraint] = []
    grouped_power: dict[
        tuple[str, str, RelationSide],
        list[PinEndpoint],
    ] = {}

    def add_power(
        group_name: str,
        net: str,
        endpoints: list[PinEndpoint],
        direction: RelationSide | None = None,
    ) -> None:
        resolved_direction = direction or _rail_direction(intent, net)
        key = (group_name, net, resolved_direction)
        grouped_power.setdefault(key, []).extend(endpoints)

    def decorate(
        reference: str,
        node_net: str,
    ) -> tuple[str | None, RelationSide | None]:
        decorator = decorators.get(reference)
        if decorator is None:
            return None, None
        decorator_node_net = _net_for_endpoint(intent, decorator.node).name
        if decorator_node_net != node_net:
            raise ValueError(
                f"{reference} rail decorator node is on {decorator_node_net}, "
                f"but placement node is on {node_net}"
            )
        node_pin = _pin_on_net(intent, reference, node_net)
        rail_pin = _pin_on_net(intent, reference, decorator.rail)
        group_name = decorator.rail_group or (
            f"{decorator.kind}:{reference}:{decorator.rail}"
        )
        add_power(group_name, decorator.rail, [rail_pin])
        return rail_pin.pin, _rail_direction(intent, decorator.rail)

    for relation in relations:
        if isinstance(relation, (PullToRailRelation, ShuntToRailRelation)):
            continue

        if isinstance(relation, AnchorSideRelation):
            node_net = _net_for_endpoint(intent, relation.anchor).name
            members: list[PinEndpoint] = []
            cluster_members: list[NodeClusterMemberConstraint] = []
            for reference in relation.subjects:
                if reference in primary_subjects:
                    raise ValueError(f"{reference} has multiple primary placements")
                primary_subjects.add(reference)
                node_pin = _pin_on_net(intent, reference, node_net)
                far_pin, extend = decorate(reference, node_net)
                members.append(node_pin)
                cluster_members.append(
                    NodeClusterMemberConstraint(
                        reference=reference,
                        pin=node_pin.pin,
                        far_pin=far_pin,
                        extend_toward=extend,
                    )
                )

            if len(cluster_members) == 1:
                member = cluster_members[0]
                placements.append(
                    PinRelationConstraint(
                        reference=member.reference,
                        pin=member.pin,
                        anchor=relation.anchor,
                        side=relation.side,
                        distance_steps=relation.distance_steps,
                        far_pin=member.far_pin,
                        extend_toward=member.extend_toward,
                        source_kind=relation.kind,
                    )
                )
            else:
                placements.append(
                    NodeClusterConstraint(
                        anchor=relation.anchor,
                        side=relation.side,
                        distance_steps=relation.distance_steps,
                        members=tuple(cluster_members),
                    )
                )
            node_connections.append(
                NodeConnectionConstraint(
                    net=node_net,
                    anchor=relation.anchor,
                    members=tuple(members),
                    shared_node=len(cluster_members) > 1,
                )
            )
            continue

        if isinstance(relation, BranchRelation):
            if relation.subject in primary_subjects:
                raise ValueError(
                    f"{relation.subject} has multiple primary placements"
                )
            primary_subjects.add(relation.subject)
            node_net = _net_for_endpoint(intent, relation.anchor).name
            node_pin = _pin_on_net(intent, relation.subject, node_net)
            far_pin, extend = decorate(relation.subject, node_net)
            if far_pin is None and relation.extend is not None:
                # nothing points this part along a rail; the relation says which way its other pin goes
                others = [
                    endpoint
                    for net in intent.all_nets
                    for endpoint in net.endpoints
                    if endpoint.reference == relation.subject and endpoint != node_pin
                ]
                if len(others) == 1:
                    far_pin, extend = others[0].pin, relation.extend
            resolved_side: RelationSideSpec = relation.side
            if resolved_side == "auto" and extend is not None:
                resolved_side = extend
            placements.append(
                PinRelationConstraint(
                    reference=relation.subject,
                    pin=node_pin.pin,
                    anchor=relation.anchor,
                    side=resolved_side,
                    distance_steps=relation.distance_steps,
                    far_pin=far_pin,
                    extend_toward=extend,
                    source_kind=relation.kind,
                )
            )
            node_connections.append(
                NodeConnectionConstraint(
                    net=node_net,
                    anchor=relation.anchor,
                    members=(node_pin,),
                )
            )
            continue

        if isinstance(relation, SeriesRelation):
            if relation.subject in primary_subjects:
                raise ValueError(
                    f"{relation.subject} has multiple primary placements"
                )
            primary_subjects.add(relation.subject)
            first_net = _net_for_endpoint(intent, relation.first).name
            second_net = _net_for_endpoint(intent, relation.second).name
            pin_a = _pin_on_net(intent, relation.subject, first_net)
            pin_b = _pin_on_net(intent, relation.subject, second_net)
            if pin_a == pin_b:
                raise ValueError(
                    f"{relation.subject} series endpoints resolve to one pin"
                )
            placements.append(
                SeriesRelationConstraint(
                    reference=relation.subject,
                    pin_a=pin_a.pin,
                    pin_b=pin_b.pin,
                    anchor_a=relation.first,
                    anchor_b=relation.second,
                    side=relation.side,
                    distance_steps=relation.distance_steps,
                )
            )
            continue

        if isinstance(relation, ParallelBankRelation):
            near_endpoints: list[PinEndpoint] = []
            far_endpoints: list[PinEndpoint] = []
            near_pins: list[str] = []
            far_pins: list[str] = []
            for reference, anchor in zip(
                relation.subjects,
                relation.anchors,
                strict=True,
            ):
                if reference in primary_subjects:
                    raise ValueError(
                        f"{reference} has multiple primary placements"
                    )
                primary_subjects.add(reference)
                anchor_net = _net_for_endpoint(intent, anchor).name
                if anchor_net != relation.near_rail:
                    raise ValueError(
                        f"{anchor.token} is on {anchor_net}, expected "
                        f"{relation.near_rail}"
                    )
                near_pin = _pin_on_net(intent, reference, relation.near_rail)
                far_pin = _pin_on_net(intent, reference, relation.far_rail)
                near_endpoints.append(near_pin)
                far_endpoints.append(far_pin)
                near_pins.append(near_pin.pin)
                far_pins.append(far_pin.pin)

            far_direction = _rail_direction(intent, relation.far_rail)
            placements.append(
                ParallelBankConstraint(
                    references=relation.subjects,
                    near_pins=tuple(near_pins),
                    far_pins=tuple(far_pins),
                    anchors=relation.anchors,
                    side=relation.side,
                    distance_steps=relation.distance_steps,
                    far_direction=far_direction,
                )
            )
            add_power(
                f"{relation.group_prefix}:near",
                relation.near_rail,
                [*near_endpoints, *relation.anchors],
                _rail_direction(intent, relation.near_rail),
            )
            add_power(
                f"{relation.group_prefix}:far",
                relation.far_rail,
                far_endpoints,
                far_direction,
            )
            continue

        raise TypeError(f"Unsupported schematic relation: {type(relation)!r}")

    unused_decorators = sorted(set(decorators) - primary_subjects)
    if unused_decorators:
        raise ValueError(
            "rail decorators require a placement relation for: "
            + ", ".join(unused_decorators)
        )

    def placement_subjects(
        constraint: PlacementConstraint,
    ) -> set[str]:
        if isinstance(constraint, PinRelationConstraint):
            return {constraint.reference}
        if isinstance(constraint, NodeClusterConstraint):
            return {member.reference for member in constraint.members}
        if isinstance(constraint, SeriesRelationConstraint):
            return {constraint.reference}
        if isinstance(constraint, ParallelBankConstraint):
            return set(constraint.references)
        raise TypeError(f"Unsupported placement constraint: {constraint!r}")

    def placement_anchors(
        constraint: PlacementConstraint,
    ) -> tuple[PinEndpoint, ...]:
        if isinstance(constraint, PinRelationConstraint):
            return (constraint.anchor,)
        if isinstance(constraint, NodeClusterConstraint):
            return (constraint.anchor,)
        if isinstance(constraint, SeriesRelationConstraint):
            return (constraint.anchor_a, constraint.anchor_b)
        if isinstance(constraint, ParallelBankConstraint):
            return constraint.anchors
        raise TypeError(f"Unsupported placement constraint: {constraint!r}")

    def placement_side(
        constraint: PlacementConstraint,
    ) -> RelationSideSpec:
        return constraint.side

    adjacency: dict[int, set[int]] = {
        index: set() for index in range(len(placements))
    }
    subjects_by_index = [
        placement_subjects(constraint) for constraint in placements
    ]
    anchors_by_index = [
        placement_anchors(constraint) for constraint in placements
    ]
    for first in range(len(placements)):
        for second in range(first + 1, len(placements)):
            first_subjects = subjects_by_index[first]
            second_subjects = subjects_by_index[second]
            first_anchors = anchors_by_index[first]
            second_anchors = anchors_by_index[second]
            linked = (
                any(
                    anchor.reference in second_subjects
                    for anchor in first_anchors
                )
                or any(
                    anchor.reference in first_subjects
                    for anchor in second_anchors
                )
                or bool(
                    {anchor.token for anchor in first_anchors}
                    & {anchor.token for anchor in second_anchors}
                )
            )
            if linked:
                adjacency[first].add(second)
                adjacency[second].add(first)

    placement_groups: list[PlacementGroupConstraint] = []
    unseen = set(range(len(placements)))
    group_number = 0
    while unseen:
        seed = min(unseen)
        stack = [seed]
        component: set[int] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            unseen.discard(current)
            stack.extend(adjacency[current] - component)

        component_subjects = set().union(
            *(subjects_by_index[index] for index in component)
        )
        root_anchors = _dedupe_endpoints(
            [
                anchor
                for index in sorted(component)
                for anchor in anchors_by_index[index]
                if anchor.reference not in component_subjects
            ]
        )
        root_sides = [
            placement_side(placements[index])
            for index in sorted(component)
            if any(
                anchor in root_anchors
                for anchor in anchors_by_index[index]
            )
        ]
        concrete_sides = [
            side for side in root_sides if side != "auto"
        ]
        if not root_anchors or not concrete_sides:
            continue
        side = concrete_sides[0]
        if any(candidate != side for candidate in concrete_sides):
            raise ValueError(
                "A connected relation group cannot have conflicting root sides"
            )
        root_placement_indexes = tuple(
            index
            for index in sorted(component)
            if any(
                anchor in root_anchors
                for anchor in anchors_by_index[index]
            )
        )
        placement_groups.append(
            PlacementGroupConstraint(
                name=f"group_{group_number}",
                placement_indexes=tuple(sorted(component)),
                root_placement_indexes=root_placement_indexes,
                references=tuple(sorted(component_subjects)),
                root_anchors=root_anchors,
                side=side,
            )
        )
        group_number += 1

    power_groups = tuple(
        PowerGroupConstraint(
            name=group_name,
            net=net,
            endpoints=_dedupe_endpoints(endpoints),
            direction=direction,
        )
        for (group_name, net, direction), endpoints in grouped_power.items()
    )
    return CompiledSchematicRelations(
        placements=tuple(placements),
        placement_groups=tuple(placement_groups),
        power_groups=power_groups,
        node_connections=tuple(node_connections),
    )
