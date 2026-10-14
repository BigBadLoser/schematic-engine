"""Reference designator conventions the engine relies on."""
from __future__ import annotations

import re

# KiCad's Simulation_SPICE library gives voltage sources the designator V and current sources I.
_SOURCE_PREFIX = re.compile(r"^[VI]\d", re.IGNORECASE)


def is_source_reference(reference: str) -> bool:
    """A voltage or current source: where a circuit meets the outside, like a connector, not a part that supports another."""
    return bool(_SOURCE_PREFIX.match(reference))


# Symbols of KiCad's Simulation_SPICE library that are sources: independent voltage and current sources and the
# controlled and behavioural ones.  (Not every name starting with V or I is one: IBIS_DEVICE, VOLTMETER_DIFF.)
_SPICE_SOURCES = frozenset({
    "V", "VAM", "VDC", "VEXP", "VPULSE", "VPWL", "VSFFM", "VSIN", "VTRNOISE", "VTRRANDOM",
    "I", "IAM", "IDC", "IEXP", "IPULSE", "IPWL", "ISFFM", "ISIN", "ITRNOISE", "ITRRANDOM",
    "BSOURCE", "ESOURCE", "FSOURCE", "GSOURCE", "HSOURCE", "VCVS", "VCCS", "CCVS", "CCCS",
})


def is_source(reference: str, library: str = "", symbol_name: str = "", role: str | None = None) -> bool:
    """Whether a part is a voltage or current source (or a battery), by the strongest evidence there is.

    In order: an explicit ``role`` that says source (``power_source``); then the symbol's identity (a Simulation_SPICE
    source, a ``Device:Battery`` symbol); then the reference designator, whose prefix letters are taken whole: exactly V or I
    before the digits (``V1``, ``I2``, not ``IC1`` or ``VR1``).  A bare name such as ``VBIAS`` is the weakest evidence and
    counts as nothing.  ``library`` may be a library id (``Library:Symbol``) when ``symbol_name`` is not given."""
    if "source" in (role or "").lower():
        return True
    if not symbol_name and ":" in library:
        library, symbol_name = library.split(":", 1)
    library_name = library.split(":", 1)[0]
    if library_name == "Simulation_SPICE" and symbol_name.upper() in _SPICE_SOURCES:
        return True
    if library_name == "Device" and symbol_name.lower().startswith("battery"):
        return True
    return is_source_reference(reference)


UNIT_SEPARATOR = ":"


def unit_reference(reference: str, unit: int) -> str:
    """The placeable of one unit of a multi-unit part: ``U5`` unit 2 is ``U5:2``."""
    return f"{reference}{UNIT_SEPARATOR}{unit}"


def split_unit(placeable: str) -> tuple[str, int | None]:
    """The part a placeable belongs to and its unit: ``"U5:2"`` is ``("U5", 2)``, a plain reference ``(reference, None)``.

    The netlist identity of a unit placeable is its part's reference; only the layout and the build tell the units apart."""
    reference, separator, unit = placeable.rpartition(UNIT_SEPARATOR)
    if separator and reference and unit.isdigit():
        return reference, int(unit)
    return placeable, None
