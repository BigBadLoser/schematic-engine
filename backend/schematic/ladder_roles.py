"""Classify schematic parts into signal chains, rails, attachments and rungs.

Ground nets are ground rails; positive or unspecified power nets are supply
rails; negative power nets are negative rails. Authoring auxiliaries and power
flag symbols do not take part in classification. References beginning with V
or I are sources; Simulation_SPICE symbols named for independent V/I or
behavioral and controlled B/E/F/G/H sources are also sources. Sources and input connectors seed
the path first. Only when neither exists do the highest-pin-count holders of
declared input nets seed the path. The shortest simple path passes through the
highest-pin-count eligible part. Two-pin parts hanging from a rail are not path
candidates unless they are sources or connectors. Declared outputs and output
connectors supply sinks; without one, the farthest reachable part is the sink.
Equal choices use reference order, and an unconnected block falls back to its
hub as a one-part chain. Blocks connected by output/input interfaces are
ordered in signal flow.

Two-pin parts connecting a block hub's inbound and output nets are feedback
links, including when those nets are adjacent. Other two-pin parts between
non-adjacent chain nets are feedback; parts between trunks or a trunk and a
chain net are rungs. Signal nets with three or more endpoint pins are trunks.
Two-pin parts hanging between one rail and one signal net attach to the earliest
chain host on that signal, or the highest-pin-count non-chain host. Multi-pin
parts sharing a signal through pins with the same name or number form pairs.
A hub (a chain part with six or more pins: a controller) fans its pins into lanes. A lane is the run of two-pin
parts, left unclassified by the rules above, that hang in series from one of the hub's pins that is neither a rail
nor a chain net: the parts on the pin's net, then the parts on the net beyond them, in the order they connect. Such a
part is a lane part and no longer in ``other``. Unclassified parts go in ``other``. These rules use only intent and
supplied pin counts so layout code can place a ladder without knowing circuit names.
Sorting every choice keeps the result stable when callers supply symbols,
nets, or blocks in a new order.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Literal, Mapping

from .designators import is_source
from .intent import FunctionalBlockIntent, NetIntent, SchematicIntent


@dataclass(frozen=True)
class Attachment:
    host: str
    net: str
    rail: str
    side: Literal["ground", "supply"]


@dataclass(frozen=True)
class Rung:
    net_a: str
    net_b: str


@dataclass(frozen=True)
class Feedback:
    net_from: str
    net_to: str


@dataclass(frozen=True)
class Lane:
    """The series parts hung from one pin of a hub, in the order they connect, and the nets they run through.

    ``nets`` starts with the pin's net and holds the net beyond each part, so it is one longer than ``series``."""
    hub: str
    pin: str
    net: str
    series: tuple[str, ...]
    nets: tuple[str, ...]


@dataclass(frozen=True)
class Roles:
    rails: dict[str, str]
    trunks: tuple[str, ...]
    chains: dict[str, tuple[str, ...]]
    attachments: dict[str, Attachment]
    rungs: dict[str, Rung]
    feedback: dict[str, Feedback]
    pairs: tuple[tuple[str, str], ...]
    other: tuple[str, ...]
    lanes: dict[str, Lane] = field(default_factory=dict)      # "<hub>.<pin>" -> the lane from that pin


HUB_PINS = 6          # a chain part with this many pins or more fans its pins into lanes


def _is_auxiliary(symbol: object) -> bool:
    return bool(getattr(symbol, "authoring_auxiliary", False)) or (
        getattr(symbol, "library", "") == "power"
        and getattr(symbol, "symbol_name", "") == "PWR_FLAG"
    )


def _is_source(reference: str, symbol: object) -> bool:
    return is_source(reference, getattr(symbol, "library", ""), getattr(symbol, "symbol_name", ""),
                     getattr(symbol, "role", None))


Labels = Mapping[str, Mapping[str, "set[str] | frozenset[str]"]]      # part -> neighbour -> the nets that join them


def _route(
    graph: Mapping[str, set[str]], labels: Labels | None, start: str, end: str, *,
    entry: str | None = None, strict: bool = True, avoid: frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The shortest route ``(parts, the net of each step)`` from ``start`` to ``end``, by breadth-first search.

    With ``labels`` and ``strict`` a part is left by a different net than it was entered by (``entry`` is the net the
    start was entered by): a part that shares one net with both its neighbours is not a stage in the path.  Parts in
    ``avoid`` are not used."""
    if start == end:
        return (start,), ()
    use_nets = strict and labels is not None
    first = (start, entry if use_nets else None)
    queue = deque([first])
    previous: dict = {first: None}
    seen = {start}
    while queue:
        state = queue.popleft()
        current, came_by = state
        for neighbor in sorted(graph.get(current, ())):
            if neighbor in avoid and neighbor != end:
                continue
            if use_nets:
                options = sorted(n for n in labels.get(current, {}).get(neighbor, ()) if n != came_by)
            else:
                options = [None]
            for net in options:
                nxt = (neighbor, net)
                if nxt in previous or (not use_nets and neighbor in seen):
                    continue
                previous[nxt] = (state, net)
                seen.add(neighbor)
                if neighbor == end:
                    parts, nets, node = [], [], nxt
                    while previous[node] is not None:
                        parts.append(node[0])
                        nets.append(previous[node][1])
                        node = previous[node][0]
                    parts.append(node[0])
                    return tuple(reversed(parts)), tuple(reversed(nets))
                queue.append(nxt)
    return None


def _shortest_path(graph: Mapping[str, set[str]], start: str, end: str, labels: Labels | None = None) -> tuple[str, ...] | None:
    """The shortest path of parts.  Among the paths as short as the shortest, with ``labels`` the one that leaves each part by
    another net than it came in by is preferred (a longer path is never taken for it: parts that all meet on one net
    are one hop apart, and a chain through a net is the short way)."""
    plain = _route(graph, labels, start, end, strict=False)
    if plain is None or labels is None:
        return plain[0] if plain else None
    strict = _route(graph, labels, start, end, strict=True)
    return strict[0] if strict is not None and len(strict[0]) == len(plain[0]) else plain[0]


def _through_by_flow(graph: Mapping[str, set[str]], start: str, hub: str, end: str) -> tuple[str, ...] | None:
    """The shortest simple path from ``start`` to ``end`` through ``hub``, exactly.

    It is two paths from the hub, one to each end, that share no part but the hub, and as short together as can be: a
    minimum-cost flow of value two from the hub to a sink joined to both ends, every part split into an in and an out node
    of capacity one (the hub's two), every hop costing one, found by two augmentations along Bellman-Ford shortest paths
    in the residual graph.  Polynomial, so no search over simple paths (exponential on a dense graph) is needed."""
    if start == end:
        return (start,) if hub == start else None
    nodes = sorted(set(graph) | {n for others in graph.values() for n in others} | {start, hub, end})
    index: dict[object, int] = {}
    for name in nodes:
        index[("in", name)] = len(index)
        index[("out", name)] = len(index)
    sink = len(index)
    edges: list[list[int]] = []          # to, capacity left, cost, original capacity
    adjacent: list[list[int]] = [[] for _ in range(sink + 1)]

    def add(u: int, v: int, capacity: int, cost: int) -> None:
        adjacent[u].append(len(edges))
        edges.append([v, capacity, cost, capacity])
        adjacent[v].append(len(edges))
        edges.append([u, 0, -cost, 0])

    for name in nodes:
        add(index[("in", name)], index[("out", name)], 2 if name == hub else 1, 0)
        for other in sorted(graph.get(name, ())):
            add(index[("out", name)], index[("in", other)], 1, 1)
    add(index[("out", start)], sink, 1, 0)
    add(index[("out", end)], sink, 1, 0)
    source = index[("out", hub)]
    for _round in range(2):
        distance = {source: 0}
        came: dict[int, int] = {}
        queue = deque([source])
        waiting = {source}
        while queue:                       # Bellman-Ford with a queue: costs are negative on the residual edges
            u = queue.popleft()
            waiting.discard(u)
            for edge_id in adjacent[u]:
                v, capacity, cost, _original = edges[edge_id]
                if capacity > 0 and distance[u] + cost < distance.get(v, 1 << 30):
                    distance[v] = distance[u] + cost
                    came[v] = edge_id
                    if v not in waiting:
                        waiting.add(v)
                        queue.append(v)
        if sink not in came:
            return None
        node = sink
        while node != source:
            edge_id = came[node]
            edges[edge_id][1] -= 1
            edges[edge_id ^ 1][1] += 1
            node = edges[edge_id ^ 1][0]
    names = {value: key[1] for key, value in index.items() if key[0] == "in"}
    flow: dict[int, list[int]] = {}
    for edge_id in range(0, len(edges), 2):       # forward edges carrying flow, by where they leave from
        to, left, _cost, original = edges[edge_id]
        for _unit in range(original - left):
            flow.setdefault(edges[edge_id ^ 1][0], []).append(to)
    legs: list[list[str]] = []
    for _unit in range(2):
        leg, node = [hub], source
        while node != sink:
            node = flow[node].pop(0)
            if node in names:
                leg.append(names[node])
        legs.append(leg)
    to_start = next((leg for leg in legs if leg[-1] == start), None)
    to_end = next((leg for leg in legs if leg[-1] == end and leg is not to_start), None)
    if to_start is None or to_end is None:
        return None
    return tuple(reversed(to_start)) + tuple(to_end[1:])


def _first_through(graph: Mapping[str, set[str]], start: str, hub: str, end: str, best: tuple[str, ...]) -> tuple[str, ...]:
    """The first of the shortest simple paths through the hub when neighbours are taken in sorted order (the path the
    exhaustive breadth-first search over simple paths used to return), built step by step: a neighbour is taken when the
    rest of a path of the optimal length (``best``) can still be found without the parts already used."""
    path = [start]
    while path[-1] != end:
        taken = set(path)
        for neighbor in sorted(graph.get(path[-1], ())):
            if neighbor in taken:
                continue
            visited = hub in taken or neighbor == hub
            if neighbor == end:
                if visited and len(path) + 1 == len(best):
                    path.append(neighbor)
                    break
                continue
            rest = {u: {v for v in others if v not in taken} for u, others in graph.items() if u not in taken}
            if visited:
                tail = _route(rest, None, neighbor, end, strict=False)
                tail = tail[0] if tail else None
            else:
                tail = _through_by_flow(rest, neighbor, hub, end)
            if tail is not None and len(path) + len(tail) == len(best):
                path.append(neighbor)
                break
        else:
            return best
    return tuple(path)


def _shortest_path_through(
    graph: Mapping[str, set[str]], start: str, hub: str, end: str, labels: Labels | None = None,
) -> tuple[str, ...] | None:
    """The shortest simple path from start to end that visits hub (None when there is none).

    The length is the exact optimum (``_through_by_flow``).  Among paths of that length, with ``labels`` one that leaves each
    part by another net than it was entered by is preferred (two shortest paths joined, when they share no part); else the
    first in the order of the sorted neighbours."""
    best = _through_by_flow(graph, start, hub, end)
    if best is None:
        return None
    if labels is not None:
        inbound = _route(graph, labels, start, hub, strict=True)
        if inbound is not None:
            outbound = _route(graph, labels, hub, end, entry=inbound[1][-1] if inbound[1] else None, strict=True,
                              avoid=frozenset(inbound[0]) - {hub})
            if outbound is not None:
                joined = inbound[0] + outbound[0][1:]
                if len(set(joined)) == len(joined) and len(joined) == len(best):
                    return joined
    return _first_through(graph, start, hub, end, best)


def _chain_for_block(
    block: FunctionalBlockIntent,
    symbols: Mapping[str, object],
    nets: tuple[NetIntent, ...],
    rails: Mapping[str, str],
    pin_counts: Mapping[str, int],
) -> tuple[tuple[tuple[str, ...], ...], dict[str, tuple[str, str]]]:
    """The block's chains (the main path first, then one per further disjoint path from its inputs to its outputs) and the
    bypass parts that become feedback."""
    members = tuple(sorted(ref for ref in block.members if ref in symbols and not _is_auxiliary(symbols[ref])))
    if not members:
        return (), {}
    member_set = set(members)
    endpoints_by_net = {
        net.name: tuple(sorted(ep.reference for ep in net.endpoints if ep.reference in member_set and not _is_auxiliary(symbols[ep.reference])))
        for net in nets
        if net.name not in rails
    }
    graph: dict[str, set[str]] = {ref: set() for ref in members}
    labels: dict[str, dict[str, set[str]]] = {ref: {} for ref in members}
    for net_name, refs in endpoints_by_net.items():
        for ref in refs:
            graph[ref].update(other for other in refs if other != ref)
            for other in refs:
                if other != ref:
                    labels[ref].setdefault(other, set()).add(net_name)

    def hangs_on_rail(ref: str) -> bool:
        memberships = [net for net in nets for ep in net.endpoints if ep.reference == ref]
        rail_pins = sum(net.name in rails for net in memberships)
        signal_pins = sum(net.name not in rails for net in memberships)
        # a two-pin part between a rail and a signal net, or a clamp: a part of three pins or more with one pin on a signal net
        # and the rest on rails (a dual diode on a node), which hangs from that net's wire like a support
        return signal_pins == 1 and ((pin_counts.get(ref) == 2 and rail_pins == 1)
                                     or (pin_counts.get(ref, 0) >= 3 and rail_pins >= 2))

    chain_candidates = tuple(
        ref for ref in members
        if not hangs_on_rail(ref)
        or _is_source(ref, symbols[ref])
        or getattr(symbols[ref], "role", None) in {"input_connector", "output_connector"}
    ) or members
    seeds = {
        ref for ref in members
        if _is_source(ref, symbols[ref])
        or getattr(symbols[ref], "role", None) == "input_connector"
    }
    if not seeds:
        for net_name in block.inputs:
            holders = [ref for ref in endpoints_by_net.get(net_name, ()) if ref in chain_candidates]
            if holders:
                highest_pin_count = max(int(pin_counts.get(ref, 0)) for ref in holders)
                seeds.update(ref for ref in holders if int(pin_counts.get(ref, 0)) == highest_pin_count)
    if not seeds:
        seeds.add(chain_candidates[0])

    hub = min(chain_candidates, key=lambda ref: (-int(pin_counts.get(ref, 0)), ref))

    input_nets = [name for name in block.inputs if hub in endpoints_by_net.get(name, ())]
    if not input_nets:
        inbound_routes = [
            path for seed in sorted(seeds)
            if (path := _shortest_path(graph, seed, hub, labels)) is not None and len(path) > 1
        ]
        if inbound_routes:
            inbound_path = min(inbound_routes, key=lambda path: (len(path), path))
            previous = inbound_path[-2]
            input_nets = sorted(
                name for name, refs in endpoints_by_net.items()
                if previous in refs and hub in refs
            )
    output_nets = [name for name in block.outputs if hub in endpoints_by_net.get(name, ())]
    bypasses: dict[str, tuple[str, str]] = {}
    if input_nets and output_nets:
        for ref in members:
            if ref == hub or pin_counts.get(ref) != 2:
                continue
            if _is_source(ref, symbols[ref]) or getattr(symbols[ref], "role", None) in {"input_connector", "output_connector"}:
                continue
            part_nets = {
                net.name for net in nets
                if net.name not in rails and any(ep.reference == ref for ep in net.endpoints)
            }
            shared_inputs = sorted(part_nets & set(input_nets))
            shared_outputs = sorted(part_nets & set(output_nets))
            if shared_inputs and shared_outputs and shared_inputs[0] != shared_outputs[0]:
                bypasses[ref] = (shared_inputs[0], shared_outputs[0])
    path_candidates = tuple(ref for ref in chain_candidates if ref not in bypasses) or chain_candidates
    path_graph = {
        ref: {other for other in graph[ref] if other not in bypasses}
        for ref in members if ref not in bypasses
    }

    sinks: set[str] = set()
    for net_name in block.outputs:
        holders = [
            ref for ref in endpoints_by_net.get(net_name, ())
            if ref not in bypasses
            and (not hangs_on_rail(ref) or ref in seeds or getattr(symbols[ref], "role", None) == "output_connector")
        ]
        connectors = [ref for ref in holders if getattr(symbols[ref], "role", None) == "output_connector"]
        if connectors:
            sinks.update(connectors)
        elif holders:
            highest_pin_count = max(int(pin_counts.get(ref, 0)) for ref in holders)
            sinks.update(ref for ref in holders if int(pin_counts.get(ref, 0)) == highest_pin_count)
    sinks.update(ref for ref in members if getattr(symbols[ref], "role", None) == "output_connector")

    if sinks:
        sink_distances = {}
        for ref in sinks:
            distances = [
                len(path) - 1
                for seed in sorted(seeds)
                if ref in path_graph and (path := _shortest_path(path_graph, seed, ref, labels)) is not None
            ]
            if distances:
                sink_distances[ref] = min(distances)
        if sink_distances:
            farthest_output = max(sink_distances.values())
            sinks = {ref for ref, distance in sink_distances.items() if distance == farthest_output}
            connectors = {ref for ref in sinks if getattr(symbols[ref], "role", None) == "output_connector"}
            if connectors:
                sinks = connectors
            elif hub in sinks:
                sinks = {hub}
        else:
            sinks = set()

    # In the absence of an explicit sink, use the most distant member from its
    # nearest seed. Ties select the lowest reference.
    if not sinks:
        distances: dict[str, int] = {}
        for ref in path_candidates:
            paths = [_shortest_path(path_graph, seed, ref, labels) for seed in sorted(seeds)] if ref in path_graph else []
            lengths = [len(path) - 1 for path in paths if path is not None]
            if lengths:
                distances[ref] = min(lengths)
        farthest = max(distances.values(), default=0)
        sinks = {min(ref for ref, distance in distances.items() if distance == farthest)} if distances else {hub}

    candidates: list[tuple[str, ...]] = []
    for seed in sorted(seeds):
        for sink in sorted(sinks):
            if seed not in path_graph or sink not in path_graph or hub not in path_graph:
                continue
            path = _shortest_path_through(path_graph, seed, hub, sink, labels)
            if path is not None:
                candidates.append(path)
    if not candidates:
        # A disconnected block has no valid through-path. Keep its structural
        # hub as a one-part chain and leave disconnected parts for other rules.
        return ((hub,),), bypasses
    main = min(candidates, key=lambda path: (len(path), path))

    # Parts the main path leaves, that join an input net of the block to an output net (the other three gate resistors of an
    # H-bridge, each between a header pin and a gate), are further paths: one chain each, as parallel rows.
    chains = [main]
    input_set = {name for name in block.inputs if name in endpoints_by_net}
    output_set = {name for name in block.outputs if name in endpoints_by_net}
    if input_set and output_set:
        left = {
            ref for ref in members
            if ref not in main and ref not in bypasses and (
                not hangs_on_rail(ref) or _is_source(ref, symbols[ref])
                or getattr(symbols[ref], "role", None) in {"input_connector", "output_connector"})
        }
        while True:
            starts = [ref for ref in sorted(left) if any(ref in endpoints_by_net[name] for name in input_set)]
            ends = {ref for ref in left if any(ref in endpoints_by_net[name] for name in output_set)}
            found = []
            for start in starts:
                for end in sorted(ends):
                    path = _shortest_path({ref: graph[ref] & left for ref in left}, start, end, labels)
                    if path is not None:
                        found.append(path)
            if not found:
                break
            extra = min(found, key=lambda path: (len(path), path))
            chains.append(extra)
            left -= set(extra)
    # Multi-pin parts (a switch, a transistor: not the two-pin supports that become attachments, rungs and feedback) the chains
    # leave that enter by an input net of the block and meet a chain part by a net of their own (the battery
    # switch beside the USB diode, both feeding the pass transistor) are further chains, as parallel rows: an input
    # path that joins the main path where it meets a part of it, not one that reaches an output.
    if input_set:
        placed_refs = {ref for chain in chains for ref in chain}
        while True:
            left = {
                ref for ref in members
                if ref not in placed_refs and ref not in bypasses and pin_counts.get(ref, 0) >= 3
            }
            starts = [ref for ref in sorted(left) if any(ref in endpoints_by_net[name] for name in input_set)]
            goals = {ref for ref in left if any(
                ref in refs and set(refs) & placed_refs for name, refs in endpoints_by_net.items() if name not in input_set)}
            found = []
            for start in starts:
                for goal in sorted(goals):
                    path = _shortest_path({ref: graph[ref] & left for ref in left}, start, goal, labels)
                    if path is not None:
                        found.append(path)
            if not found:
                break
            extra = min(found, key=lambda path: (len(path), path))
            chains.append(extra)
            placed_refs |= set(extra)
    return tuple(chains), bypasses


def classify(intent: SchematicIntent, pin_counts: Mapping[str, int]) -> Roles:
    """Derive stable structural roles from intent, excluding authoring power flags."""
    all_symbols = {symbol.reference: symbol for symbol in intent.symbols}
    symbols = {ref: symbol for ref, symbol in all_symbols.items() if not _is_auxiliary(symbol)}
    nets = tuple(sorted(intent.all_nets, key=lambda net: net.name))
    rails = {
        net.name: ("ground" if net.kind == "ground" else "negative" if net.polarity == "negative" else "supply")
        for net in nets if net.kind in {"ground", "power"}
    }

    chains: dict[str, tuple[str, ...]] = {}
    bypass_feedback: dict[str, Feedback] = {}
    blocks_by_name = {block.name: block for block in intent.blocks}
    block_order = _block_flow_order(intent.blocks)
    def block_rails(block: FunctionalBlockIntent) -> dict[str, str]:
        # A rail the block declares among its inputs or outputs is a chain net for it, not a rail.
        declared = {name for name in (*block.inputs, *block.outputs) if rails.get(name) in {"supply", "negative"}}
        return {name: kind for name, kind in rails.items() if name not in declared}

    block_of_chain: dict[str, str] = {}
    for block in block_order:
        block_chains, bypasses = _chain_for_block(block, symbols, nets, block_rails(block), pin_counts)
        for index, chain in enumerate(block_chains, start=1):
            name = block.name if len(block_chains) == 1 else f"{block.name}#{index}"
            chains[name] = chain
            block_of_chain[name] = block.name
        for ref, (net_from, net_to) in bypasses.items():
            bypass_feedback[ref] = Feedback(net_from, net_to)
    # A multi-pin part no chain took (the low-side switch of a half bridge, a block member the path left out) and that shares a signal net with a chain part is
    # a chain of its own, as a block of one: left in ``other`` it would stand apart from the circuit it belongs to.  A clamp
    # (one signal pin, the rest on rails) is not: it hangs from its node (below).
    chained = {ref for chain in chains.values() for ref in chain}
    for ref in sorted(symbols):
        count = pin_counts.get(ref, 0)
        if ref in chained or not 3 <= count < HUB_PINS:
            continue
        joined = [net for net in nets if any(ep.reference == ref for ep in net.endpoints)]
        signal_pins = sum(1 for net in joined if net.name not in rails for ep in net.endpoints if ep.reference == ref)
        rail_pins = sum(1 for net in joined if net.name in rails for ep in net.endpoints if ep.reference == ref)
        if signal_pins == 1 and rail_pins >= 2:
            continue
        if any(ep.pin == mine.pin and ep.reference != ref and pin_counts.get(ep.reference, 0) >= 3
               for net in joined if net.name not in rails for mine in net.endpoints if mine.reference == ref
               for ep in net.endpoints):
            continue            # a pair member (same pin of two multi-pin parts on one net) is the pair rule's
        if any(ep.reference in chained for net in joined if net.name not in rails for ep in net.endpoints):
            chains[ref] = (ref,)
            block_of_chain[ref] = ref
            blocks_by_name[ref] = FunctionalBlockIntent(ref, (ref,))
    chain_refs = {ref for chain in chains.values() for ref in chain}
    chain_ref_order = {ref: index for index, ref in enumerate(ref for chain in chains.values() for ref in chain)}
    net_refs = {
        net.name: {ep.reference for ep in net.endpoints if ep.reference in symbols}
        for net in nets
    }
    chain_net_order: list[str] = []
    for block_name in chains:
        chain = chains[block_name]
        block = blocks_by_name[block_of_chain[block_name]]
        block_member_set = set(block.members)
        local_rails = block_rails(block)
        first, last = chain[0], chain[-1]
        for net_name in block.inputs:
            refs = net_refs.get(net_name, set())
            if net_name not in local_rails and first in refs and net_name not in chain_net_order:
                chain_net_order.append(net_name)
        for left, right in zip(chain, chain[1:]):
            shared_nets = []
            for net in nets:
                refs = {ep.reference for ep in net.endpoints}
                if net.name not in local_rails and left in refs and right in refs and refs & block_member_set:
                    shared_nets.append(net.name)
            if shared_nets:
                preferred = [name for name in block.outputs if name in shared_nets]
                if not preferred:
                    preferred = [name for name in block.inputs if name in shared_nets]
                selected = min(preferred or shared_nets)
                if selected not in chain_net_order:
                    chain_net_order.append(selected)
        for net_name in block.outputs:
            refs = net_refs.get(net_name, set())
            if net_name not in local_rails and last in refs and net_name not in chain_net_order:
                chain_net_order.append(net_name)
    net_symbols = {
        net.name: tuple(sorted({ep.reference for ep in net.endpoints if ep.reference in symbols}))
        for net in nets
    }
    # The spec also calls for trunk order by first appearance along a chain.
    # Include qualifying nets encountered at any chain member, including a
    # one-member chain's local fanout nets.
    trunk_order: list[str] = []
    for chain in chains.values():
        for ref in chain:
            for net in nets:
                if net.kind == "signal" and sum(ep.reference in symbols for ep in net.endpoints) >= 3 and ref in net_symbols[net.name] and net.name not in trunk_order:
                    trunk_order.append(net.name)
    all_trunk_names = {
        net.name for net in nets
        if net.kind == "signal" and sum(ep.reference in symbols for ep in net.endpoints) >= 3
    }
    trunk_order.extend(sorted(all_trunk_names - set(trunk_order)))
    trunks = tuple(trunk_order)
    trunk_set = set(trunks)
    chain_nets = set(chain_net_order)
    chain_rails = chain_nets & set(rails)
    chain_net_position = {name: index for index, name in enumerate(chain_net_order)}

    attachments: dict[str, Attachment] = {}
    rungs: dict[str, Rung] = {}
    feedback: dict[str, Feedback] = {}
    pair_set: set[tuple[str, str]] = set()
    consumed: set[str] = set(chain_refs)

    # A tap: a one-pin part (a test point, a single-pin connector) on a signal net that other pins share.  It hangs from that
    # net's wire, its pin facing the wire, the attachment of a part with no rail (``rail`` is empty).
    for ref in sorted(symbols):
        if ref in consumed or pin_counts.get(ref) != 1:
            continue
        joined = [net for net in nets if any(ep.reference == ref for ep in net.endpoints)]
        if len(joined) != 1 or joined[0].name in rails or len(set(net_symbols[joined[0].name]) - {ref}) < 1:
            continue
        candidates = [candidate for candidate in net_symbols[joined[0].name] if candidate in chain_refs]
        if candidates:
            host = min(candidates, key=lambda candidate: (chain_ref_order[candidate], candidate))
        else:
            others = [candidate for candidate in net_symbols[joined[0].name] if candidate != ref]
            host = min(others, key=lambda candidate: (-int(pin_counts.get(candidate, 0)), candidate))
        attachments[ref] = Attachment(host, joined[0].name, "", "supply")
        consumed.add(ref)

    for ref in sorted(symbols):
        if ref in consumed or pin_counts.get(ref, 0) < 2:
            continue
        memberships = [(net, ep.pin) for net in nets for ep in net.endpoints if ep.reference == ref]
        rail_memberships = [(net, pin) for net, pin in memberships if net.name in rails]
        nonrail_memberships = [(net, pin) for net, pin in memberships if net.name not in rails]
        if pin_counts.get(ref) != 2:
            # a clamp (one signal pin, the others on rails) hangs from its signal net; the supply rail is the one it names
            if len(nonrail_memberships) != 1 or len(rail_memberships) < 2 or ref in chain_refs:
                continue
            rail_memberships = [min(rail_memberships, key=lambda m: (rails[m[0].name] != "supply", m[0].name))]
        if len(rail_memberships) == 2 and sum(net.name in chain_rails for net, _ in rail_memberships) == 1:
            # A part between a chain rail and a true rail (an output capacitor) hangs on the chain rail.
            signal_net, rail_net = (m[0] for m in sorted(rail_memberships, key=lambda m: m[0].name not in chain_rails))
            rail_memberships, nonrail_memberships = [(rail_net, "")], [(signal_net, "")]
        if len(rail_memberships) == 1 and len(nonrail_memberships) == 1:
            rail_net, _ = rail_memberships[0]
            signal_net, _ = nonrail_memberships[0]
            candidates = [candidate for candidate in net_symbols[signal_net.name] if candidate in chain_refs]
            if candidates:
                host = min(candidates, key=lambda candidate: (chain_ref_order[candidate], candidate))
            else:
                candidates = [candidate for candidate in net_symbols[signal_net.name] if candidate != ref]
                if not candidates:
                    continue
                host = min(candidates, key=lambda candidate: (-int(pin_counts.get(candidate, 0)), candidate))
            attachments[ref] = Attachment(host, signal_net.name, rail_net.name, "ground" if rails[rail_net.name] in {"ground", "negative"} else "supply")
            consumed.add(ref)

    for ref in sorted(symbols):
        if ref in consumed or pin_counts.get(ref) != 2:
            continue
        if ref in bypass_feedback:
            feedback[ref] = bypass_feedback[ref]
            consumed.add(ref)
            continue
        memberships = [net for net in nets for ep in net.endpoints if ep.reference == ref]
        if len(memberships) != 2 or any(net.name in rails for net in memberships):
            continue
        names = [net.name for net in memberships]
        a, b = names
        if a != b and a in chain_nets and b in chain_nets and abs(chain_net_position[a] - chain_net_position[b]) > 1:
            feedback[ref] = Feedback(a, b)
            consumed.add(ref)
        elif a != b and ((a in trunk_set and b in trunk_set) or (a in trunk_set and b in chain_nets) or (b in trunk_set and a in chain_nets)):
            rungs[ref] = Rung(a, b)
            consumed.add(ref)

    # A pair uses two multi-pin parts that connect to the same signal through
    # pins with the same number/name, as in a transistor emitter pair.
    for net in nets:
        if net.name in rails:
            continue
        grouped: dict[str, set[str]] = {}
        for ep in net.endpoints:
            if ep.reference in symbols and pin_counts.get(ep.reference, 0) >= 3:
                grouped.setdefault(ep.pin, set()).add(ep.reference)
        for refs in grouped.values():
            ordered = sorted(refs)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    pair_set.add((left, right))
    for pair in sorted(pair_set):
        consumed.update(pair)

    roles = Roles(
        rails={name: kind for name, kind in sorted(rails.items()) if name not in chain_rails},
        trunks=trunks,
        chains=chains,
        attachments=dict(sorted(attachments.items())),
        rungs=dict(sorted(rungs.items())),
        feedback=dict(sorted(feedback.items())),
        pairs=tuple(sorted(pair_set)),
        other=tuple(sorted(set(symbols) - consumed)),
    )
    endpoints = {net.name: tuple((ep.reference, ep.pin) for ep in net.endpoints if ep.reference in symbols) for net in nets}
    return attach_to_trunks(assign_lanes(roles, endpoints, pin_counts), endpoints, pin_counts)


def assign_lanes(roles: Roles, endpoints: Mapping[str, tuple[tuple[str, str], ...]], pin_counts: Mapping[str, int]) -> Roles:
    """The roles with the lanes of every hub found, and the parts they take out of ``other``.

    ``endpoints`` maps each net to its ``(reference, pin)`` pairs. A hub is a chain part with ``HUB_PINS`` pins or more.
    A hub pin starts a lane when its net is not a rail and not a chain net (one a chain part shares with the next) and
    every other pin on it belongs to a part that is not a chain part: a net that reaches the chain is wired to it, not
    drawn as a lane. From the pin's net the lane takes the lowest-referenced two-pin ``other`` part with both nets off the
    rails, then does the same from the net beyond it, until none is left; a part whose far net reaches the chain (and no
    pin of the hub) is not taken, and a part is in one lane only, the pins taken in order. Parts that join the hub's other
    lanes are lane parts like the rest (the layout draws them as spans)."""
    chain_parts = {ref for chain in roles.chains.values() for ref in chain}
    chains = list(roles.chains.values())
    neighbours = [pair for chain in chains for pair in zip(chain, chain[1:])]
    neighbours += [(left[-1], right[0]) for left, right in zip(chains, chains[1:]) if left and right]
    chain_nets = {
        net for left, right in neighbours for net, pins in endpoints.items()
        if net not in roles.rails and any(r == left for r, _p in pins) and any(r == right for r, _p in pins)
    }
    free = {ref for ref in roles.other if pin_counts.get(ref) == 2}
    nets_of: dict[str, list[str]] = {}
    for net, pins in endpoints.items():
        for ref, _pin in pins:
            if ref in free and net not in nets_of.setdefault(ref, []):
                nets_of[ref].append(net)
    lanes: dict[str, Lane] = {}
    taken: set[str] = set()
    for hub in sorted(ref for ref in chain_parts if pin_counts.get(ref, 0) >= HUB_PINS):
        hub_pins = sorted((pin, net) for net, pins in endpoints.items() for ref, pin in pins if ref == hub)
        for pin, net in hub_pins:
            if net in roles.rails or net in chain_nets or any(r in chain_parts and r != hub for r, _p in endpoints[net]):
                continue
            series: list[str] = []
            run = [net]
            while True:
                found = [ref for ref in sorted(free - taken) if net in nets_of.get(ref, ())
                         and len(nets_of[ref]) == 2 and not any(n in roles.rails for n in nets_of[ref])]
                if not found:
                    break
                ref = far = None
                for candidate in found:        # the lowest-referenced part that can go on this lane
                    beyond = next(n for n in nets_of[candidate] if n != net)
                    if beyond in run or candidate in series:
                        continue
                    if not any(r == hub for r, _p in endpoints[beyond]) and any(r in chain_parts for r, _p in endpoints[beyond]):
                        continue            # the net beyond runs to the chain elsewhere: the part is not drawn on this lane
                    ref, far = candidate, beyond
                    break
                if ref is None:
                    break
                taken.add(ref)
                series.append(ref)
                run.append(far)
                net = far
            if series:
                lanes[f"{hub}.{pin}"] = Lane(hub, pin, run[0], tuple(series), tuple(run))
    if not lanes:
        return roles
    return replace(roles, lanes=lanes, other=tuple(ref for ref in roles.other if ref not in taken))


def attach_to_trunks(roles: Roles, endpoints: Mapping[str, tuple[tuple[str, str], ...]], pin_counts: Mapping[str, int]) -> Roles:
    """The roles with the two-pin parts left in ``other`` that stand between a trunk net and a dead-end net made rungs.

    A dead-end net is one that only the part and one chain part share (a pull-up on a hub's output, a resistor on a gate;
    a lane has already taken the parts that run on from a hub pin out to nets of their own).  Such a part is a rung between the two
    nets, its far pin landing on the wired trunk (a part hung against a power rail is an attachment, one against a trunk
    that is wired is a rung).  A part with a pin on a rail (a support, a supply, a bypass) is not one."""
    chain_parts = {ref for chain in roles.chains.values() for ref in chain}
    holders = {net: {ref for ref, _pin in pins} for net, pins in endpoints.items()}
    rungs = dict(roles.rungs)
    taken: set[str] = set()
    for ref in sorted(roles.other):
        if pin_counts.get(ref) != 2:
            continue
        nets = sorted({net for net, pins in endpoints.items() if any(r == ref for r, _p in pins)})
        if len(nets) != 2 or any(net in roles.rails for net in nets):
            continue
        for dead, trunk in (nets, nets[::-1]):
            if (trunk in roles.trunks and dead not in roles.trunks and len(holders[dead]) == 2
                    and any(h in chain_parts for h in holders[dead])):
                rungs[ref] = Rung(dead, trunk)
                taken.add(ref)
                break
    if not taken:
        return roles
    return replace(roles, rungs=dict(sorted(rungs.items())), other=tuple(ref for ref in roles.other if ref not in taken))


def _block_flow_order(blocks: tuple[FunctionalBlockIntent, ...]) -> tuple[FunctionalBlockIntent, ...]:
    """Topologically order declared block interfaces, with name-stable ties."""
    by_name = {block.name: block for block in blocks}
    edges = {block.name: set() for block in blocks}
    indegree = {block.name: 0 for block in blocks}
    for left in blocks:
        for right in blocks:
            if left.name != right.name and set(left.outputs) & set(right.inputs):
                if right.name not in edges[left.name]:
                    edges[left.name].add(right.name)
                    indegree[right.name] += 1
    # A block that passes nothing on (an indicator, a load) waits until the blocks that do are placed: it cannot
    # stand between a producer and the blocks that take its output.
    def rank(name: str) -> tuple[bool, str]:
        return (not by_name[name].outputs, name)

    # Blocks in a dependency cycle (strongly connected) are ordered among themselves by rank, as one unit: the cycle is
    # broken there and the acyclic order is kept everywhere else.
    reach = {name: {name} for name in edges}
    changed = True
    while changed:
        changed = False
        for name in edges:
            for after in edges[name]:
                if not reach[after] <= reach[name]:
                    reach[name] |= reach[after]
                    changed = True
    component = {name: frozenset(o for o in reach[name] if name in reach[o]) for name in edges}
    units = sorted(set(component.values()), key=lambda unit: min(rank(n) for n in unit))
    unit_edges = {unit: {component[after] for name in unit for after in edges[name]} - {unit} for unit in units}
    unit_in = {unit: 0 for unit in units}
    for after_set in unit_edges.values():
        for after in after_set:
            unit_in[after] += 1

    def unit_key(unit):
        return min(rank(n) for n in unit)

    ready = sorted((unit for unit, degree in unit_in.items() if degree == 0), key=unit_key)
    ordered: list[str] = []
    while ready:
        unit = ready.pop(0)
        ordered.extend(sorted(unit, key=rank))
        for after in sorted(unit_edges[unit], key=unit_key):
            unit_in[after] -= 1
            if unit_in[after] == 0:
                ready.append(after)
                ready.sort(key=unit_key)
    return tuple(by_name[name] for name in ordered)
