"""Circuit-diverse semantic regression fixtures.

These are data, not layout modes. The engine must not import this module or
switch algorithms based on fixture identity, device prefix or library name.
"""
from __future__ import annotations

from backend.schematic import (
    FunctionalBlockIntent, NetIntent, PinEndpoint, SchematicIntent, SymbolIntent,
    branch, pull_to_rail, shunt_to_rail,
)


def _p(reference: str, pin: str) -> PinEndpoint:
    return PinEndpoint(reference, pin)


def _flag(reference: str) -> SymbolIntent:
    # Electrical source declaration for KiCad ERC, not a layout exception.
    return SymbolIntent(
        reference, "power", "PWR_FLAG", "PWR_FLAG",
        role="power_source", authoring_auxiliary=True,
    )


def rc_filter() -> SchematicIntent:
    return SchematicIntent(
        symbols=(
            SymbolIntent("J1", "Connector_Generic", "Conn_01x02", "INPUT", role="input_connector"),
            SymbolIntent("R1", "Device", "R", "10k"),
            SymbolIntent("C1", "Device", "C", "100nF"),
            SymbolIntent("J2", "Connector_Generic", "Conn_01x02", "OUTPUT", role="output_connector"),
            _flag("#FLG1"),
        ),
        nets=(
            NetIntent("VIN", (_p("J1", "1"), _p("R1", "1")), routing="wire"),
            NetIntent("VOUT", (_p("R1", "2"), _p("C1", "1"), _p("J2", "1")), routing="wire"),
            NetIntent("GND", (_p("J1", "2"), _p("C1", "2"), _p("J2", "2"), _p("#FLG1", "1")),
                      kind="ground", routing="label", scope="global",
                      drivers=(_p("#FLG1", "1"),), expected_driver_count=1),
        ),
        relations=(
            branch("C1", anchor=_p("R1", "2"), side="below"),
            shunt_to_rail("C1", node=_p("R1", "2"), rail="GND"),
        ),
        blocks=(
            FunctionalBlockIntent("filter", ("R1", "C1"), inputs=("VIN",), outputs=("VOUT",)),
            FunctionalBlockIntent("source", ("J1", "#FLG1"), outputs=("VIN",)),
            FunctionalBlockIntent("load", ("J2",), inputs=("VOUT",)),
        ),
    )


def noninverting_amplifier() -> SchematicIntent:
    # LM741 is a single-unit amplifier. Offset-null pins are intentionally NC.
    return SchematicIntent(
        symbols=(
            SymbolIntent("U1", "Amplifier_Operational", "LM741", "LM741"),
            SymbolIntent("R1", "Device", "R", "10k"),
            SymbolIntent("R2", "Device", "R", "100k"),
            SymbolIntent("J1", "Connector_Generic", "Conn_01x02", "INPUT", role="input_connector"),
            SymbolIntent("J2", "Connector_Generic", "Conn_01x02", "OUTPUT", role="output_connector"),
            _flag("#FLG1"), _flag("#FLG2"), _flag("#FLG3"),
        ),
        nets=(
            NetIntent("IN", (_p("J1", "1"), _p("U1", "3")), routing="wire"),
            NetIntent("OUT", (_p("U1", "6"), _p("R2", "2"), _p("J2", "1")), routing="wire"),
            NetIntent("FB", (_p("U1", "2"), _p("R1", "2"), _p("R2", "1")), routing="wire"),
            NetIntent("GND", (_p("R1", "1"), _p("J1", "2"), _p("J2", "2"), _p("#FLG1", "1")),
                      kind="ground", routing="label", scope="global",
                      drivers=(_p("#FLG1", "1"),), expected_driver_count=1),
            NetIntent("VCC", (_p("U1", "7"), _p("#FLG2", "1")),
                      kind="power", routing="label", scope="global",
                      drivers=(_p("#FLG2", "1"),), expected_driver_count=1),
            NetIntent("VEE", (_p("U1", "4"), _p("#FLG3", "1")),
                      kind="power", routing="label", scope="global", polarity="negative",
                      drivers=(_p("#FLG3", "1"),), expected_driver_count=1),
        ),
        no_connects=(_p("U1", "1"), _p("U1", "5"), _p("U1", "8")),
        relations=(
            branch("R1", anchor=_p("U1", "2"), side="below"),
            pull_to_rail("R1", node=_p("U1", "2"), rail="GND"),
        ),
        blocks=(
            FunctionalBlockIntent("amplification", ("U1", "R1", "R2"), inputs=("IN",), outputs=("OUT",)),
            FunctionalBlockIntent("input", ("J1", "#FLG1", "#FLG2", "#FLG3"), outputs=("IN",)),
            FunctionalBlockIntent("output", ("J2",), inputs=("OUT",)),
        ),
    )


def linear_regulator() -> SchematicIntent:
    return SchematicIntent(
        symbols=(
            SymbolIntent("U1", "Regulator_Linear", "AMS1117-3.3", "AMS1117-3.3"),
            SymbolIntent("C1", "Device", "C", "10uF"),
            SymbolIntent("C2", "Device", "C", "10uF"),
            SymbolIntent("J1", "Connector_Generic", "Conn_01x02", "INPUT", role="input_connector"),
            SymbolIntent("J2", "Connector_Generic", "Conn_01x02", "OUTPUT", role="output_connector"),
            _flag("#FLG1"), _flag("#FLG2"),
        ),
        nets=(
            NetIntent("VIN", (_p("J1", "1"), _p("U1", "3"), _p("C1", "1"), _p("#FLG1", "1")),
                      kind="power", routing="label", scope="global",
                      drivers=(_p("#FLG1", "1"),), expected_driver_count=1),
            NetIntent("+3V3", (_p("U1", "2"), _p("C2", "1"), _p("J2", "1")),
                      kind="power", routing="label", scope="global",
                      drivers=(_p("U1", "2"),), expected_driver_count=1),
            NetIntent("GND", (_p("U1", "1"), _p("C1", "2"), _p("C2", "2"), _p("J1", "2"), _p("J2", "2"), _p("#FLG2", "1")),
                      kind="ground", routing="label", scope="global",
                      drivers=(_p("#FLG2", "1"),), expected_driver_count=1),
        ),
        relations=(
            branch("C1", anchor=_p("U1", "3"), side="left"),
            branch("C2", anchor=_p("U1", "2"), side="right"),
            shunt_to_rail("C1", node=_p("U1", "3"), rail="GND"),
            shunt_to_rail("C2", node=_p("U1", "2"), rail="GND"),
        ),
        blocks=(
            FunctionalBlockIntent("regulation", ("U1", "C1", "C2"), inputs=("VIN",), outputs=("+3V3",)),
            FunctionalBlockIntent("input", ("J1", "#FLG1", "#FLG2"), outputs=("VIN",)),
            FunctionalBlockIntent("output", ("J2",), inputs=("+3V3",)),
        ),
    )


def i2c_sensor() -> SchematicIntent:
    """An I2C temperature sensor on a four-pin connector, with its pull-ups, bypass capacitor and address pins to ground.

    No relations are declared: where every part goes is left to what the netlist implies.
    """
    return SchematicIntent(
        symbols=(
            SymbolIntent("U1", "Sensor_Temperature", "LM75B", "LM75B"),
            SymbolIntent("R1", "Device", "R", "4.7k"),
            SymbolIntent("R2", "Device", "R", "4.7k"),
            SymbolIntent("C1", "Device", "C", "100nF"),
            SymbolIntent("J1", "Connector_Generic", "Conn_01x04", "I2C", role="input_connector"),
            _flag("#FLG1"), _flag("#FLG2"),
        ),
        nets=(
            NetIntent("SDA", (_p("J1", "3"), _p("U1", "1"), _p("R1", "2")), routing="wire"),
            NetIntent("SCL", (_p("J1", "4"), _p("U1", "2"), _p("R2", "2")), routing="wire"),
            NetIntent("+3V3", (_p("J1", "1"), _p("U1", "8"), _p("R1", "1"), _p("R2", "1"), _p("C1", "1"), _p("#FLG1", "1")),
                      kind="power", routing="label", scope="global",
                      drivers=(_p("#FLG1", "1"),), expected_driver_count=1),
            NetIntent("GND", (_p("J1", "2"), _p("U1", "4"), _p("U1", "5"), _p("U1", "6"), _p("U1", "7"), _p("C1", "2"),
                              _p("#FLG2", "1")),
                      kind="ground", routing="label", scope="global",
                      drivers=(_p("#FLG2", "1"),), expected_driver_count=1),
        ),
        no_connects=(_p("U1", "3"),),
        blocks=(
            FunctionalBlockIntent("sensor", ("U1", "R1", "R2", "C1"), inputs=("SDA", "SCL")),
            FunctionalBlockIntent("connector", ("J1", "#FLG1", "#FLG2"), outputs=("SDA", "SCL")),
        ),
    )


def led_switch() -> SchematicIntent:
    """A transistor that switches an LED from a control line, with its base resistor, base pull-down and LED resistor.

    No relations are declared.
    """
    return SchematicIntent(
        symbols=(
            SymbolIntent("J1", "Connector_Generic", "Conn_01x03", "SUPPLY / CTRL", role="input_connector"),
            SymbolIntent("Q1", "Transistor_BJT", "2N3904", "2N3904"),
            SymbolIntent("R1", "Device", "R", "1k"),
            SymbolIntent("R2", "Device", "R", "10k"),
            SymbolIntent("R3", "Device", "R", "330"),
            SymbolIntent("D1", "Device", "LED", "LED"),
            _flag("#FLG1"), _flag("#FLG2"),
        ),
        nets=(
            NetIntent("CTRL", (_p("J1", "3"), _p("R1", "1")), routing="wire"),
            NetIntent("BASE", (_p("R1", "2"), _p("Q1", "2"), _p("R2", "1")), routing="wire"),
            NetIntent("LED_A", (_p("R3", "2"), _p("D1", "2")), routing="wire"),
            NetIntent("SINK", (_p("D1", "1"), _p("Q1", "3")), routing="wire"),
            NetIntent("+5V", (_p("J1", "1"), _p("R3", "1"), _p("#FLG1", "1")),
                      kind="power", routing="label", scope="global",
                      drivers=(_p("#FLG1", "1"),), expected_driver_count=1),
            NetIntent("GND", (_p("J1", "2"), _p("Q1", "1"), _p("R2", "2"), _p("#FLG2", "1")),
                      kind="ground", routing="label", scope="global",
                      drivers=(_p("#FLG2", "1"),), expected_driver_count=1),
        ),
        blocks=(
            FunctionalBlockIntent("switch", ("Q1", "R1", "R2", "R3", "D1"), inputs=("CTRL",)),
            FunctionalBlockIntent("connector", ("J1", "#FLG1", "#FLG2"), outputs=("CTRL",)),
        ),
    )


FIXTURES = {
    "rc_filter": rc_filter,
    "noninverting_amplifier": noninverting_amplifier,
    "linear_regulator": linear_regulator,
}

# Circuits that declare no relations at all: how the engine places a design it was given only as a netlist.
INFERENCE_FIXTURES = {
    "i2c_sensor": i2c_sensor,
    "led_switch": led_switch,
}


# KiCad's own demo circuits, rebuilt from their netlists (see schematic_demo_circuits): human-drawn originals to compare with
try:
    from scripts.schematic_demo_circuits import DEMO_CIRCUITS as DEMO_FIXTURES
except ImportError:  # the data is generated: `sandbox.py demos --write`
    DEMO_FIXTURES = {}

# Other authors' sheets (a corpus of public KiCad projects), rebuilt from their netlists the same way: `sandbox.py corpus --write`
try:
    from scripts.schematic_corpus_circuits import CORPUS_CIRCUITS as CORPUS_FIXTURES
except ImportError:
    CORPUS_FIXTURES = {}


def build_fixture(name: str) -> SchematicIntent:
    try:
        builder = FIXTURES.get(name) or INFERENCE_FIXTURES.get(name) or DEMO_FIXTURES.get(name) or CORPUS_FIXTURES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown schematic fixture: {name}") from exc
    return builder()
