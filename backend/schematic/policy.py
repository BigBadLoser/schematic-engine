from __future__ import annotations

from dataclasses import dataclass

from .intent import SchematicIntent


@dataclass(frozen=True)
class SchematicAuthoringPolicy:
    """Translate companion semantic intent into MCP Pro design intent.

    MCP Pro is the sole schematic author and layout engine. This policy may
    enrich MCP's semantic inputs, but it must not derive coordinates, route
    order, block placement, or other companion-owned geometry.
    """

    functional_spacing_mm: float = 12.7

    def mcp_design_intent(self, intent: SchematicIntent) -> dict[str, object]:
        roles = {
            symbol.reference: (symbol.role or "").lower()
            for symbol in intent.symbols
        }
        payload: dict[str, object] = {
            "critical_nets": [
                net.name for net in intent.all_nets if net.is_critical
            ],
            "functional_spacing_mm": self.functional_spacing_mm,
        }

        role_fields = {
            "connector_refs": {
                "connector",
                "input_connector",
                "output_connector",
            },
            "power_tree_refs": {"power", "regulator", "power_source"},
            "analog_refs": {"analog"},
            "digital_refs": {"digital", "mcu", "logic"},
            "sensor_cluster_refs": {"sensor"},
        }
        for field_name, accepted in role_fields.items():
            refs = [
                ref
                for ref, role in roles.items()
                if role in accepted
            ]
            if refs:
                payload[field_name] = refs

        grouped: dict[str, list[str]] = {}
        for hint in intent.placement_hints:
            if hint.kind != "decouples":
                continue
            ic_ref = hint.target.split(".", 1)[0]
            grouped.setdefault(ic_ref, []).append(hint.subject)
        if grouped:
            payload["decoupling_pairs"] = [
                {
                    "ic_ref": ic_ref,
                    "cap_refs": cap_refs,
                    "max_distance_mm": 4.0,
                }
                for ic_ref, cap_refs in grouped.items()
            ]

        return payload


DEFAULT_AUTHORING_POLICY = SchematicAuthoringPolicy()
