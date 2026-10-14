"""How much room placement keeps around each symbol, and how routing treats the wires already on the sheet.

A ``LayoutPolicy`` is what the server is told (``sch_set_layout_clearances``) before a build and what the router and the
terminal planner read while it runs. It is pure: no I/O, no KiCad, no knowledge of any particular circuit.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any

# The margin, around a symbol's body and pin tips, that placement keeps clear. The router keeps 1.5 grids
# (1.905 mm) away from every symbol and needs 4.445 mm from a pin tip to a foreign body, so 1.27 mm was as tight
# as placement can usefully go and 3.81 mm is loose enough that a symbol rarely needs more.
# Widening this to 2.54 (`density-1`) doubled the routed corners on three demos: the margin is also the router's and the
# terminal planner's obstacle box, so wires detoured round every part. Room between parts comes from the layout spacing.
TIGHT_MARGIN_MM = 1.27
LOOSE_MARGIN_MM = 3.81
MARGIN_STEP_MM = 1.27


@dataclass(frozen=True)
class LayoutPolicy:
    """How placement sizes symbols, and how routing treats the wires already on the sheet.

    ``legacy`` is the geometry the engine shipped with: MCP Pro's fixed 20.32 x 15.24 mm minimum box for every
    symbol. ``tight`` sizes a symbol to its body and pin tips plus a margin, per reference, so a symbol that a
    later stage struggled with can be given more room without loosening the rest of the sheet.
    """

    mode: str = "legacy"
    default_mm: float = TIGHT_MARGIN_MM
    margins: tuple[tuple[str, float], ...] = ()
    stub_obstacles: bool = False
    # Power terminals measure symbols by body and pin tips rather than by MCP Pro's box. A tight layout always
    # does; the legacy layout does not unless asked, which is how the guarded fallback is built.
    tight_terminals: bool = False
    # A part with no declared relation is placed from the relation its nets imply (``relation_inference``). A placement
    # that cannot be solved with those relations is tried again without them.
    infer_relations: bool = True
    # The symbols no relation places are put where ``placement_refinement`` chooses. A placement a later stage cannot
    # live with is tried again as the layout was compacted before the refinement existed.
    refine: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("legacy", "tight"):
            raise ValueError(f"Unknown layout mode: {self.mode!r}")
        if self.default_mm < 0 or any(value < 0 for _ref, value in self.margins):
            raise ValueError("Layout margins cannot be negative")

    @classmethod
    def legacy(cls) -> LayoutPolicy:
        return cls(mode="legacy", default_mm=1.27)  # the verified baseline keeps its margin

    @classmethod
    def tight(cls, default_mm: float = TIGHT_MARGIN_MM) -> LayoutPolicy:
        return cls(mode="tight", default_mm=default_mm, tight_terminals=True)

    @classmethod
    def legacy_with_tight_terminals(cls) -> LayoutPolicy:
        """MCP Pro's symbol boxes for placement, body-and-pin-tip bounds for the power terminals."""
        return cls(mode="legacy", default_mm=1.27, tight_terminals=True)

    def margin(self, reference: str) -> float:
        return dict(self.margins).get(reference, self.default_mm)

    def _with_margins(self, changes: Mapping[str, float]) -> LayoutPolicy:
        merged = {**dict(self.margins), **changes}
        # A margin equal to the default carries no information; dropping it keeps equal policies equal.
        kept = tuple(sorted((ref, round(value, 4)) for ref, value in merged.items()
                            if abs(value - self.default_mm) > 1e-9))
        return replace(self, margins=kept)

    def loosened(
        self,
        references: Collection[str],
        *,
        step_mm: float = MARGIN_STEP_MM,
        ceiling_mm: float = LOOSE_MARGIN_MM,
    ) -> LayoutPolicy | None:
        """Give the named symbols more room, or the whole sheet when none are named. None when at the ceiling."""
        if self.mode != "tight":
            return None
        if references:
            changes = {
                ref: min(ceiling_mm, self.margin(ref) + step_mm)
                for ref in references if self.margin(ref) < ceiling_mm - 1e-9
            }
            return self._with_margins(changes) if changes else None
        if self.default_mm >= ceiling_mm - 1e-9:
            return None
        raised = min(ceiling_mm, self.default_mm + step_mm)
        shifted = {ref: max(value, raised) for ref, value in self.margins}
        return replace(self, default_mm=raised, margins=tuple(sorted(
            (ref, value) for ref, value in shifted.items() if abs(value - raised) > 1e-9
        )))

    def tightened(
        self,
        references: Collection[str],
        *,
        step_mm: float = MARGIN_STEP_MM,
        floor_mm: float = 0.0,
    ) -> LayoutPolicy | None:
        """Take room away from the named symbols, for a placement that found no free position."""
        if self.mode != "tight" or not references:
            return None
        changes = {
            ref: max(floor_mm, self.margin(ref) - step_mm)
            for ref in references if self.margin(ref) > floor_mm + 1e-9
        }
        return self._with_margins(changes) if changes else None

    def with_stub_obstacles(self, enabled: bool) -> LayoutPolicy:
        return replace(self, stub_obstacles=enabled)

    def without_inference(self) -> LayoutPolicy | None:
        """The same layout with only the relations the design declared, or None when that is already so."""
        return replace(self, infer_relations=False) if self.infer_relations else None

    def without_refinement(self) -> LayoutPolicy | None:
        """The same layout with the free symbols compacted instead of refined, or None when that is already so."""
        return replace(self, refine=False) if self.refine else None

    def as_arguments(self) -> dict[str, Any]:
        """The arguments of the ``sch_set_layout_clearances`` tool."""
        return {
            "mode": self.mode,
            "default_mm": self.default_mm,
            "by_reference": dict(self.margins),
            "tight_terminals": self.tight_terminals,
        }

    def describe(self) -> dict[str, Any]:
        return {
            **self.as_arguments(), "stub_obstacles": self.stub_obstacles, "infer_relations": self.infer_relations,
            "refine": self.refine,
        }
