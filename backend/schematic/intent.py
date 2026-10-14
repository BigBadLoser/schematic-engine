from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .relations import SchematicRelation


NetKind = Literal["signal", "power", "ground", "clock", "bus"]
NetRouting = Literal["auto", "wire", "label"]
NetScope = Literal["local", "global", "hierarchical"]
LabelKind = NetScope
NetImportance = Literal["low", "normal", "high", "critical"]
BlockPlacement = Literal["auto", "functional", "linear", "star", "grid"]
BlockPresentationIntent = Literal["auto", "inline", "frame", "sheet", "hierarchical"]
PlacementKind = Literal[
    "left_of",
    "right_of",
    "above",
    "below",
    "near",
    "align_x",
    "align_y",
    "align_with",
    "edge",
    "decouples",
    "protects",
    "belongs_to",
]
PlacementStrength = Literal["preferred", "required"]
BlockRelationKind = Literal["left_of", "right_of", "above", "below"]


@dataclass(frozen=True)
class SymbolIntent:
    """Electrical symbol identity and semantic role; no layout coordinates."""

    reference: str
    library: str
    symbol_name: str
    value: str
    footprint: str = ""
    role: str | None = None
    # Authoring auxiliaries participate electrically but need not be floorplanned
    # as ordinary components. This is explicit intent, not a library-name rule.
    properties: dict[str, str] = field(default_factory=dict)
    authoring_auxiliary: bool = False

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("reference is required")
        if not self.library.strip():
            raise ValueError("library is required")
        if not self.symbol_name.strip():
            raise ValueError("symbol_name is required")

    def to_mcp(self) -> dict[str, object]:
        # Semantic-only fields such as role deliberately stay above the MCP
        # symbol schema unless MCP Pro explicitly exposes them.
        payload: dict[str, object] = {
            "reference": self.reference,
            "library": self.library,
            "symbol_name": self.symbol_name,
            "value": self.value,
            "footprint": self.footprint,
        }
        if self.properties:
            payload["properties"] = dict(self.properties)
        return payload


@dataclass(frozen=True)
class PinEndpoint:
    reference: str
    pin: str

    def __post_init__(self) -> None:
        if not self.reference.strip() or not self.pin.strip():
            raise ValueError("pin endpoint requires reference and pin")

    @property
    def token(self) -> str:
        return f"{self.reference}.{self.pin}"


@dataclass(frozen=True)
class NetIntent:
    """Electrical net semantics independent of physical wire geometry."""

    name: str
    endpoints: tuple[PinEndpoint, ...]
    kind: NetKind = "signal"
    signal_class: str | None = None
    routing: NetRouting = "auto"
    scope: NetScope = "local"
    # Compatibility alias retained while callers migrate to scope.
    label_kind: LabelKind | None = None
    expose_label: bool = False
    drivers: tuple[PinEndpoint, ...] = ()
    sinks: tuple[PinEndpoint, ...] = ()
    expected_driver_count: int | None = None
    bus: str | None = None
    fanout_hint: int | None = None
    importance: NetImportance = "normal"
    critical: bool = False
    # Which side of ground a supply rail sits on. The data, never the name: a rail called "-5V" says so in its polarity.
    polarity: Literal["positive", "negative"] | None = None

    @property
    def hangs(self) -> bool:
        """A negative supply rail's symbol hangs from its pin, upside down, like a ground symbol."""
        return self.kind == "power" and self.polarity == "negative"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("net name is required")
        if not self.endpoints:
            raise ValueError("net must contain at least one endpoint")
        if len(set(self.endpoints)) != len(self.endpoints):
            raise ValueError(f"net {self.name!r} contains duplicate endpoints")
        endpoint_set = set(self.endpoints)
        for role, endpoints in (("driver", self.drivers), ("sink", self.sinks)):
            missing = set(endpoints) - endpoint_set
            if missing:
                raise ValueError(
                    f"net {self.name!r} {role} endpoints must also appear in endpoints"
                )
        if self.expected_driver_count is not None and self.expected_driver_count < 0:
            raise ValueError("expected_driver_count must be non-negative")
        if self.fanout_hint is not None and self.fanout_hint < 0:
            raise ValueError("fanout_hint must be non-negative")

    @property
    def effective_scope(self) -> NetScope:
        return self.label_kind or self.scope

    @property
    def is_critical(self) -> bool:
        return self.critical or self.importance == "critical"


@dataclass(frozen=True)
class ConnectionIntent:
    """Backward-compatible local point-to-point connection."""

    net_name: str
    source: PinEndpoint
    target: PinEndpoint

    def __post_init__(self) -> None:
        if not self.net_name.strip():
            raise ValueError("net_name is required")
        if self.source == self.target:
            raise ValueError("connection endpoints must differ")

    def to_net(self) -> NetIntent:
        return NetIntent(
            name=self.net_name,
            endpoints=(self.source, self.target),
            routing="wire",
        )


@dataclass(frozen=True)
class PlacementHint:
    """Coordinate-free relationship consumed by composition policy when possible."""

    kind: PlacementKind
    subject: str
    target: str
    strength: PlacementStrength = "preferred"

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.target.strip():
            raise ValueError("placement hint subject and target are required")
        if self.kind in {
            "left_of",
            "right_of",
            "above",
            "below",
            "near",
            "align_x",
            "align_y",
            "align_with",
        } and self.subject == self.target:
            raise ValueError("placement hint subject and target must differ")
        if self.kind == "edge" and self.target not in {"input", "output", "top", "bottom"}:
            raise ValueError("edge hint target must be input, output, top, or bottom")


@dataclass(frozen=True)
class BlockPlacementHint:
    """Coordinate-free relationship between functional blocks."""

    kind: BlockRelationKind
    subject: str
    target: str
    strength: PlacementStrength = "preferred"

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.target.strip():
            raise ValueError("block placement subject and target are required")
        if self.subject == self.target:
            raise ValueError("block placement subject and target must differ")


@dataclass(frozen=True)
class FunctionalBlockIntent:
    """Semantic grouping/composition object independent of final coordinates."""

    name: str
    members: tuple[str, ...]
    label: str | None = None
    role: str | None = None
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    local_power_rails: tuple[str, ...] = ()
    presentation: BlockPresentationIntent = "auto"
    placement: BlockPlacement = "auto"
    parent: str | None = None
    children: tuple[str, ...] = ()
    repeated_instance_of: str | None = None
    channel: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("functional block name is required")
        if not self.members:
            raise ValueError("functional block must contain at least one reference")
        if len(set(self.members)) != len(self.members):
            raise ValueError("functional block members must be unique")
        if len(set(self.children)) != len(self.children):
            raise ValueError("functional block children must be unique")
        if self.name in self.children:
            raise ValueError("functional block cannot contain itself as a child")


@dataclass(frozen=True)
class SchematicIntent:
    symbols: tuple[SymbolIntent, ...]
    nets: tuple[NetIntent, ...] = ()
    connections: tuple[ConnectionIntent, ...] = ()
    blocks: tuple[FunctionalBlockIntent, ...] = ()
    placement_hints: tuple[PlacementHint, ...] = ()
    block_placement_hints: tuple[BlockPlacementHint, ...] = ()
    no_connects: tuple[PinEndpoint, ...] = ()
    # Optional reusable, coordinate-free layout relations provided by the design.
    # Fixture-specific relations must never be loaded by a workflow implementation.
    relations: tuple[SchematicRelation, ...] = ()

    def __post_init__(self) -> None:
        refs = [item.reference for item in self.symbols]
        if len(refs) != len(set(refs)):
            raise ValueError("symbol references must be unique")
        known = set(refs)

        net_names = [net.name for net in self.all_nets]
        if len(net_names) != len(set(net_names)):
            raise ValueError("net names must be unique")

        net_by_endpoint: dict[PinEndpoint, str] = {}
        for net in self.all_nets:
            for endpoint in net.endpoints:
                if endpoint.reference not in known:
                    raise ValueError(
                        f"net {net.name!r} references unknown symbol "
                        f"{endpoint.reference!r}"
                    )
                prior_net = net_by_endpoint.get(endpoint)
                if prior_net is not None:
                    raise ValueError(
                        f"endpoint {endpoint.token} belongs to multiple nets: "
                        f"{prior_net!r} and {net.name!r}"
                    )
                net_by_endpoint[endpoint] = net.name

        if len(set(self.no_connects)) != len(self.no_connects):
            raise ValueError("no-connect endpoints must be unique")
        connected_endpoints = {
            endpoint
            for net in self.all_nets
            for endpoint in net.endpoints
        }
        for endpoint in self.no_connects:
            if endpoint.reference not in known:
                raise ValueError(
                    f"no-connect references unknown symbol: {endpoint.reference}"
                )
            if endpoint in connected_endpoints:
                raise ValueError(
                    f"no-connect endpoint is also connected to a net: {endpoint.token}"
                )

        block_names = [block.name for block in self.blocks]
        if len(block_names) != len(set(block_names)):
            raise ValueError("functional block names must be unique")
        block_name_set = set(block_names)
        net_name_set = set(net_names)

        for block in self.blocks:
            missing = sorted(set(block.members) - known)
            if missing:
                raise ValueError(
                    f"functional block {block.name!r} references unknown symbols: "
                    + ", ".join(missing)
                )
            if block.parent == block.name:
                raise ValueError(
                    f"functional block {block.name!r} cannot parent itself"
                )
            if block.parent is not None and block.parent not in block_name_set:
                raise ValueError(
                    f"functional block {block.name!r} references unknown parent "
                    f"{block.parent!r}"
                )
            if block.repeated_instance_of == block.name:
                raise ValueError(
                    f"functional block {block.name!r} cannot repeat itself"
                )
            if (
                block.repeated_instance_of is not None
                and block.repeated_instance_of not in block_name_set
            ):
                raise ValueError(
                    f"functional block {block.name!r} references unknown repeated "
                    f"instance source {block.repeated_instance_of!r}"
                )
            missing_children = sorted(set(block.children) - block_name_set)
            if missing_children:
                raise ValueError(
                    f"functional block {block.name!r} references unknown children: "
                    + ", ".join(missing_children)
                )
            for interface_name in block.inputs + block.outputs + block.local_power_rails:
                if interface_name not in net_name_set:
                    raise ValueError(
                        f"functional block {block.name!r} references unknown net "
                        f"{interface_name!r}"
                    )

        for hint in self.block_placement_hints:
            if hint.subject not in block_name_set:
                raise ValueError(
                    f"block placement references unknown subject block: {hint.subject}"
                )
            if hint.target not in block_name_set:
                raise ValueError(
                    f"block placement references unknown target block: {hint.target}"
                )

        hierarchy_edges: dict[str, set[str]] = {
            block.name: set(block.children) for block in self.blocks
        }
        for block in self.blocks:
            if block.parent is not None:
                hierarchy_edges[block.parent].add(block.name)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(block_name: str) -> None:
            if block_name in visiting:
                raise ValueError("functional block hierarchy contains a cycle")
            if block_name in visited:
                return
            visiting.add(block_name)
            for child_name in hierarchy_edges[block_name]:
                visit(child_name)
            visiting.remove(block_name)
            visited.add(block_name)

        for block_name in block_names:
            visit(block_name)

        for hint in self.placement_hints:
            if hint.subject not in known:
                raise ValueError(
                    f"placement hint references unknown subject symbol: {hint.subject}"
                )
            if hint.kind in {
                "left_of",
                "right_of",
                "above",
                "below",
                "near",
                "align_x",
                "align_y",
                "align_with",
            } and hint.target not in known:
                raise ValueError(
                    f"placement hint references unknown target symbol: {hint.target}"
                )
            if hint.kind == "belongs_to" and hint.target not in block_name_set:
                raise ValueError(
                    f"belongs_to hint references unknown functional block: {hint.target}"
                )
            if hint.kind == "protects" and hint.target not in known | net_name_set:
                raise ValueError(
                    f"protects hint references unknown symbol/net: {hint.target}"
                )
            if hint.kind == "decouples":
                target_ref = hint.target.split(".", 1)[0]
                if target_ref not in known:
                    raise ValueError(
                        f"decouples hint references unknown target symbol: {target_ref}"
                    )

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(item.reference for item in self.symbols)

    @property
    def all_nets(self) -> tuple[NetIntent, ...]:
        return self.nets + tuple(connection.to_net() for connection in self.connections)

    def block_for_reference(self, reference: str) -> FunctionalBlockIntent | None:
        for block in self.blocks:
            if reference in block.members:
                return block
        return None

