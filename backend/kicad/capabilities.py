from __future__ import annotations

from shutil import which
from typing import Any


MCP_TOOL_CAPABILITIES = {
    "kicad_get_project_info": "mcp_pro_project_read",
    "sch_get_symbols": "mcp_pro_schematic_symbols_read",
    "sch_get_connectivity_graph": "mcp_pro_schematic_connectivity_read",
    "sch_list_sheets": "mcp_pro_schematic_hierarchy_read",
    "sch_get_circuit_ir": "mcp_pro_schematic_ir_read",
    "pcb_get_board_summary": "mcp_pro_board_summary_read",
    "pcb_get_footprints": "mcp_pro_footprints_read",
}


def executable_available(name: str) -> bool:
    return which(name) is not None


def current_capabilities(runtime: dict[str, Any] | None = None) -> list[dict[str, object]]:
    runtime = runtime or {}
    advertised = set(runtime.get("tools", []))
    successful = set(runtime.get("successful_tools", []))
    connected = bool(runtime.get("connected"))

    capabilities: list[dict[str, object]] = [
        {
            "operation": "project_metadata_read",
            "implemented": True,
            "backend": "local_file_discovery",
            "access": "read",
            "proof_level": "INTERNAL_VALIDATED",
            "requires_gui": False,
            "supports_headless": True,
            "known_limitations": "Root schematic selection follows project-name convention; ambiguous projects fail closed.",
            "kicad_version": "file-independent",
        },
        {
            "operation": "project_document_revision_read",
            "implemented": True,
            "backend": "local_file_hash",
            "access": "read",
            "proof_level": "INTERNAL_VALIDATED",
            "requires_gui": False,
            "supports_headless": True,
            "known_limitations": "Recursively hashes KiCad project/schematic/board documents, including child sheets.",
            "kicad_version": "file-independent",
        },
        {
            "operation": "semantic_stale_reference_detection",
            "implemented": False,
            "backend": "semantic_design",
            "access": "read",
            "proof_level": "UNVERIFIED",
            "requires_gui": False,
            "supports_headless": True,
            "known_limitations": "Stable symbol KIID capture is not implemented yet; STALE cannot be distinguished from fallback-ref replacement.",
            "kicad_version": "10.x",
        },
        {
            "operation": "mcp_pro_protocol_handshake",
            "implemented": connected,
            "backend": "mcp_pro",
            "access": "read",
            "proof_level": "RUNTIME_VERIFIED" if connected else "UNVERIFIED",
            "requires_gui": False,
            "supports_headless": True,
            "known_limitations": (
                f"Negotiated MCP {runtime.get('protocol_version')}."
                if connected
                else "No successful MCP initialize/tools-list handshake in this process."
            ),
            "kicad_version": "10.x",
        },
    ]

    for tool_name, operation in MCP_TOOL_CAPABILITIES.items():
        advertised_tool = tool_name in advertised
        verified = tool_name in successful
        capabilities.append(
            {
                "operation": operation,
                "implemented": verified,
                "available": advertised_tool,
                "backend": "mcp_pro",
                "access": "read",
                "proof_level": (
                    "LIVE_KICAD_READBACK_VERIFIED"
                    if verified
                    else "RUNTIME_DISCOVERED"
                    if advertised_tool
                    else "UNVERIFIED"
                ),
                "requires_gui": None,
                "supports_headless": None,
                "known_limitations": (
                    "Tool succeeded in the current process."
                    if verified
                    else "Advertised by MCP Pro but not yet exercised in this process."
                    if advertised_tool
                    else "Tool has not been discovered from the connected MCP server."
                ),
                "kicad_version": "10.x",
            }
        )

    cli_available = executable_available("kicad-cli")
    capabilities.append(
        {
            "operation": "kicad_cli_available",
            "implemented": cli_available,
            "backend": "kicad_cli",
            "access": "read",
            "proof_level": "RUNTIME_DISCOVERED" if cli_available else "UNVERIFIED",
            "requires_gui": False,
            "supports_headless": cli_available,
            "known_limitations": (
                "Executable is on PATH; individual ERC/DRC/export capabilities still require E2E proof."
                if cli_available
                else "kicad-cli is not available on PATH."
            ),
            "kicad_version": "10.x",
        }
    )
    return capabilities
