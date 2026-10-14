from __future__ import annotations

from typing import Any

from backend.kicad.netlist import Endpoint, SchematicNetlist

from .intent import SchematicIntent


def collapse_units(intent: SchematicIntent) -> SchematicIntent:
    """The intent with every unit placeable (``U5:2``) named by its part (``U5``): the netlist's identity is the part's.

    An intent with no unit placeable comes back as it was."""
    from dataclasses import replace

    from .designators import split_unit
    from .intent import PinEndpoint

    if not any(split_unit(symbol.reference)[1] is not None for symbol in intent.symbols):
        return intent

    def whole(endpoint: PinEndpoint) -> PinEndpoint:
        return PinEndpoint(split_unit(endpoint.reference)[0], endpoint.pin)

    symbols, seen = [], set()
    for symbol in intent.symbols:
        part = split_unit(symbol.reference)[0]
        if part not in seen:
            seen.add(part)
            symbols.append(replace(symbol, reference=part))
    nets = tuple(replace(net, endpoints=tuple(whole(e) for e in net.endpoints), drivers=tuple(whole(e) for e in net.drivers),
                         sinks=tuple(whole(e) for e in net.sinks)) for net in intent.nets)
    connections = tuple(replace(c, source=whole(c.source), target=whole(c.target)) for c in intent.connections)
    return SchematicIntent(symbols=tuple(symbols), nets=nets, connections=connections,
                           no_connects=tuple(whole(e) for e in intent.no_connects))


def verify_intent_netlist(intent: SchematicIntent, netlist: SchematicNetlist) -> dict[str, Any]:
    """Require exact pin equivalence classes, independent of generated net names.

    KiCad omits virtual power symbols/flags from exported component netlists.
    Their effect is still checked through the physical pins they connect.
    Unspecified physical pins may be isolated, but may not acquire connections.
    """
    intent = collapse_units(intent)
    intent_virtual_refs = {
        symbol.reference for symbol in intent.symbols if symbol.library == "power"
    }
    unexpectedly_virtual = (netlist.virtual_references & set(intent.references)) - intent_virtual_refs
    if unexpectedly_virtual:
        raise RuntimeError("Netlist verification physical components became virtual: " + ", ".join(sorted(unexpectedly_virtual)))
    virtual_refs = netlist.virtual_references | intent_virtual_refs
    expected_refs = set(intent.references) - virtual_refs
    missing_refs = expected_refs - netlist.references
    if missing_refs:
        raise RuntimeError("Netlist verification missing components: " + ", ".join(sorted(missing_refs)))

    actual_groups = [
        frozenset(endpoint for endpoint in members if endpoint[0] not in virtual_refs)
        for members in netlist.nets.values()
    ]
    actual_by_pin = {
        endpoint: members for members in actual_groups for endpoint in members
    }
    expected_groups: set[frozenset[Endpoint]] = set()
    failures: list[str] = []

    def describe(endpoints: set[Endpoint] | frozenset[Endpoint]) -> str:
        return ", ".join(f"{reference}.{pin}" for reference, pin in sorted(endpoints))

    for net in intent.all_nets:
        expected = frozenset(
            (endpoint.reference, endpoint.pin)
            for endpoint in net.endpoints
            if endpoint.reference not in virtual_refs
        )
        if not expected:
            continue
        expected_groups.add(expected)
        absent = expected - netlist.pins
        if absent:
            failures.append(f"{net.name}: missing pins {describe(absent)}")
            continue
        unrouted = expected - actual_by_pin.keys()
        if unrouted:
            failures.append(f"{net.name}: missing net endpoints {describe(unrouted)}")
            continue
        observed = {actual_by_pin[endpoint] for endpoint in expected}
        if len(observed) != 1:
            failures.append(f"{net.name}: split across {len(observed)} actual nets")
        extras = set().union(*observed) - expected
        if extras:
            failures.append(f"{net.name}: short/unexpected endpoints {describe(extras)}")

    for endpoint in intent.no_connects:
        if endpoint.reference in virtual_refs:
            continue
        pin = (endpoint.reference, endpoint.pin)
        if pin not in netlist.pins:
            failures.append(f"no-connect {endpoint.token}: missing pin")
        elif len(actual_by_pin.get(pin, frozenset())) > 1:
            failures.append(f"no-connect {endpoint.token}: connected to other pins")

    for actual in actual_groups:
        if len(actual) > 1 and actual not in expected_groups:
            failures.append("unexpected physical connection: " + describe(actual))
    if failures:
        raise RuntimeError("Schematic netlist does not match intent: " + " ; ".join(failures))
    return {
        "status": "PASS",
        "source": "kicad_cli_kicadxml",
        "verified_net_count": len(expected_groups),
        "verified_no_connect_count": len(intent.no_connects),
        "physical_component_count": len(expected_refs),
        "virtual_references": sorted(virtual_refs),
    }
