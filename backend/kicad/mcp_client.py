from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from threading import RLock
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class McpConnectionError(RuntimeError):
    pass


LEGACY_PROTOCOL_VERSION = "2025-11-25"
CLIENT_INFO = {"name": "kicad-ai-engineering-companion", "version": "0.1.0"}

# kicad_set_project changes MCP Pro's active context only; it does not mutate KiCad files.
CONTEXT_TOOLS = {"kicad_set_project"}
GUARDED_WRITE_TOOLS = {
    "sch_add_symbol",
    "sch_add_wire",
    "sch_build_circuit",
    "sch_auto_place_functional",
    "project_set_design_intent",
    "sch_reload",
    "sch_add_pin_labels",
    "sch_auto_place_symbols",
    "sch_move_relative",
    "sch_move_block_relative",
    "sch_move_block_to_layout_offset",
    "sch_move_symbol",
    "sch_translate_references_to_margin",
    "sch_add_pin_power_terminals",
    "sch_route_pin_bus",
    "sch_place_pin_at_offset",
    "sch_add_pin_power_symbols",
    "sch_place_pin_relation",
    "sch_place_series_relation",
    "sch_place_parallel_bank",
    "sch_route_pin_rail",
    "sch_place_node_cluster",
    "sch_place_relation_band",
    "sch_route_relation_connection",
    "sch_set_layout_clearances",
    "sch_add_pin_net_labels",
    "sch_add_wire_labels",
    "sch_add_text",
    "sch_place_fields",
    "sch_attach_power_flags",
    "sch_set_symbol_mirror",
    "sch_move_unit",
    "sch_align_relative_pins",
    "sch_frame_functional_blocks",
    "sch_mark_no_connect_pins",
    "sch_modify_field_orientation",
    "sch_route_wire_between_pins",
    "sch_add_missing_junctions",
    "sch_fix_readability",
    "sch_set_sheet_size",
    "sch_align_to_grid",
    "sch_straighten_wires",
    "sch_resolve_label_overlaps",
    "sch_normalize_power_orientation",
    "sch_normalize_text_sizes",
}

READ_ONLY_TOOLS = {
    "kicad_get_project_info",
    "pcb_get_board_summary",
    "pcb_get_footprints",
    "sch_get_symbols",
    "sch_choose_orientations",
    "sch_symbol_geometry",
    "sch_measure_functional_blocks",
    "sch_plan_relation_lanes",
    "sch_get_connectivity_graph",
    "sch_list_sheets",
    "sch_get_circuit_ir",
    "sch_visual_qa",
    "sch_cosmetic_score",
    "schematic_connectivity_gate",
    "run_erc",
}

# Minimum MCP Pro surface required by the current read-only companion.
# Hierarchy/circuit-IR tools remain optional enrichment so older compatible
# MCP Pro releases can still be inspected.
COMPANION_REQUIRED_TOOLS = {
    "kicad_set_project",
    "kicad_get_project_info",
    "pcb_get_board_summary",
    "pcb_get_footprints",
    "sch_get_symbols",
    "sch_get_connectivity_graph",
}

SCHEMATIC_WORKFLOW_REQUIRED_TOOLS = {
    "kicad_set_project",
    "kicad_get_project_info",
    "sch_get_symbols",
    "sch_choose_orientations",
    "sch_get_connectivity_graph",
    "sch_build_circuit",
    "sch_auto_place_functional",
    "sch_auto_place_symbols",
    "sch_move_relative",
    "sch_mark_no_connect_pins",
    "sch_route_wire_between_pins",
    "sch_add_pin_labels",
    "sch_add_missing_junctions",
    "sch_modify_field_orientation",
    "sch_fix_readability",
    "sch_visual_qa",
    "sch_cosmetic_score",
}


def decode_mcp_response(payload: bytes) -> dict[str, Any]:
    """Decode either JSON or a Streamable-HTTP SSE response body."""
    text = payload.decode("utf-8").strip()
    if not text:
        return {}
    if not text.startswith("data:"):
        return json.loads(text)

    messages: list[dict[str, Any]] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                messages.append(json.loads("\n".join(current)))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line.removeprefix("data:").lstrip())
    if current:
        messages.append(json.loads("\n".join(current)))
    if not messages:
        return {}
    # Tool calls are request/response in this client; the last JSON-RPC message is the response.
    return messages[-1]


def default_endpoint() -> str:
    """MCP Pro endpoint; the port is KICAD_AI_MCP_PORT when set, else 3334."""
    return f"http://127.0.0.1:{os.getenv('KICAD_AI_MCP_PORT') or 3334}/mcp"


@dataclass
class McpHttpClient:
    endpoint: str = field(default_factory=lambda: default_endpoint())
    timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("KICAD_AI_MCP_TIMEOUT_SECONDS", "30")
        )
    )
    protocol_version: str = LEGACY_PROTOCOL_VERSION
    _request_id: int = 0
    _initialized: bool = False
    _session_id: str | None = None
    _server_info: dict[str, Any] = field(default_factory=dict)
    _tools: set[str] = field(default_factory=set)
    _successful_tools: set[str] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                session_id = response.headers.get("MCP-Session-Id")
                if session_id:
                    self._session_id = session_id
                return decode_mcp_response(response.read())
        except HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(error)
            raise McpConnectionError(
                f"MCP Pro HTTP {error.code} at {self.endpoint}: {detail or error.reason}"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise McpConnectionError(f"Could not reach MCP Pro at {self.endpoint}: {error}") from error

    def initialize(self) -> dict[str, Any]:
        """Negotiate the supported legacy initialize/session lane used by this local client."""
        with self._lock:
            return self._initialize_locked()

    def _initialize_locked(self) -> dict[str, Any]:
        if self._initialized:
            return self._server_info
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        }
        response: dict[str, Any] | None = None
        for attempt in range(6):
            try:
                response = self._post(payload)
                break
            except McpConnectionError as error:
                # A companion-owned MCP process is started asynchronously so the
                # desktop window can appear immediately. Retry only transient local
                # connection failures during the first handshake.
                if "Could not reach MCP Pro" not in str(error) or attempt == 5:
                    raise
                sleep(0.25)
        assert response is not None
        if "error" in response:
            raise McpConnectionError(f"MCP Pro initialize failed: {response['error']}")
        result = response.get("result", {})
        negotiated = result.get("protocolVersion")
        if negotiated:
            self.protocol_version = str(negotiated)
        self._server_info = dict(result.get("serverInfo", {}))
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return self._server_info

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._initialize_locked()
            self._request_id += 1
            response = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            if "error" in response:
                raise McpConnectionError(f"MCP Pro error: {response['error']}")
            return dict(response.get("result", {}))

    def list_tools(self, refresh: bool = False) -> set[str]:
        if self._tools and not refresh:
            return set(self._tools)
        tools: set[str] = set()
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            for tool in result.get("tools", []):
                name = tool.get("name")
                if name:
                    tools.add(str(name))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools = tools
        return set(tools)

    def _tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ONLY_TOOLS | CONTEXT_TOOLS:
            raise McpConnectionError(f"Tool is not allowed by the read-only adapter: {name}")
        available = self.list_tools()
        if name not in available:
            raise McpConnectionError(
                f"MCP Pro does not advertise required tool: {name}. "
                "The companion requires a broader inspection profile than MCP Pro's "
                "bounded default profile; use profile 'critic' in readonly mode."
            )
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            content = result.get("content", [{"text": "MCP tool failed"}])
            raise McpConnectionError(content[0].get("text", "MCP tool failed"))
        self._successful_tools.add(name)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload = dict(structured)
            content = result.get("content", [])
            if isinstance(content, list):
                texts = [
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("text")
                ]
                if texts:
                    payload.setdefault("_content_text", "\n".join(texts))
            return payload
        return {"content": result.get("content", [])}

    @staticmethod
    def schematic_writes_enabled() -> bool:
        value = os.environ.get("KICAD_AI_ENABLE_SCHEMATIC_WRITES", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _guarded_write_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.schematic_writes_enabled():
            raise McpConnectionError(
                "Schematic writes are disabled. Set KICAD_AI_ENABLE_SCHEMATIC_WRITES=1 "
                "before launching the companion to opt in."
            )
        if name not in GUARDED_WRITE_TOOLS:
            raise McpConnectionError(f"Tool is not allowed by the guarded write adapter: {name}")
        available = self.list_tools()
        if name not in available:
            raise McpConnectionError(
                f"MCP Pro does not advertise guarded write tool: {name}. "
                "The MCP runtime must be started in write mode with a profile that exposes it."
            )
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            content = result.get("content", [{"text": "MCP write tool failed"}])
            raise McpConnectionError(content[0].get("text", "MCP write tool failed"))
        self._successful_tools.add(name)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload = dict(structured)
            content = result.get("content", [])
            if isinstance(content, list):
                texts = [
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("text")
                ]
                if texts:
                    payload.setdefault("_content_text", "\n".join(texts))
            return payload
        return {"content": result.get("content", [])}

    def call_schematic_write_tool(
        self,
        root: str,
        *,
        sch_file: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._tool(
                "kicad_set_project",
                {"project_dir": root, "sch_file": sch_file},
            )
            return self._guarded_write_tool(name, arguments)

    def call_schematic_read_tool(
        self,
        root: str,
        *,
        sch_file: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._tool(
                "kicad_set_project",
                {"project_dir": root, "sch_file": sch_file},
            )
            return self._tool(name, arguments)

    def add_schematic_symbol(
        self,
        root: str,
        *,
        sch_file: str,
        library: str,
        symbol_name: str,
        x_mm: float,
        y_mm: float,
        reference: str,
        value: str,
        footprint: str = "",
        rotation: int = 0,
        unit: int = 1,
    ) -> dict[str, Any]:
        # Keep the context switch and write in one critical section because MCP Pro
        # stores one active project context server-side.
        with self._lock:
            self._tool(
                "kicad_set_project",
                {"project_dir": root, "sch_file": sch_file},
            )
            return self._guarded_write_tool(
                "sch_add_symbol",
                {
                    "library": library,
                    "symbol_name": symbol_name,
                    "x_mm": x_mm,
                    "y_mm": y_mm,
                    "reference": reference,
                    "value": value,
                    "footprint": footprint,
                    "rotation": rotation,
                    "snap_to_grid": True,
                    "unit": unit,
                },
            )

    def add_schematic_wire(
        self,
        root: str,
        *,
        sch_file: str,
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
    ) -> dict[str, Any]:
        with self._lock:
            self._tool(
                "kicad_set_project",
                {"project_dir": root, "sch_file": sch_file},
            )
            return self._guarded_write_tool(
                "sch_add_wire",
                {
                    "x1_mm": x1_mm,
                    "y1_mm": y1_mm,
                    "x2_mm": x2_mm,
                    "y2_mm": y2_mm,
                    "snap_to_grid": True,
                },
            )

    def probe(self, required_tools: set[str] | None = None) -> dict[str, Any]:
        server = self.initialize()
        tools = self.list_tools()
        required = required_tools or set()
        missing = sorted(required - tools)
        return {
            "server_info": server,
            "protocol_version": self.protocol_version,
            "session_id": self._session_id,
            "tools": sorted(tools),
            "missing_required_tools": missing,
        }

    def runtime_state(self) -> dict[str, Any]:
        return {
            "server_info": dict(self._server_info),
            "protocol_version": self.protocol_version if self._initialized else None,
            "tools": sorted(self._tools),
            "successful_tools": sorted(self._successful_tools),
            "connected": self._initialized,
            "schematic_writes_enabled": self.schematic_writes_enabled(),
        }

    def read_project(
        self,
        root: str,
        *,
        pcb_file: str | None = None,
        sch_file: str | None = None,
    ) -> dict[str, Any]:
        # MCP Pro stores one active project context server-side. Serialize the
        # entire context-switch + read bundle so concurrent HTTP requests cannot
        # mix different projects.
        with self._lock:
            return self._read_project_locked(root, pcb_file=pcb_file, sch_file=sch_file)

    def _read_project_locked(
        self,
        root: str,
        *,
        pcb_file: str | None = None,
        sch_file: str | None = None,
    ) -> dict[str, Any]:
        project_args: dict[str, Any] = {"project_dir": root}
        if pcb_file:
            project_args["pcb_file"] = pcb_file
        if sch_file:
            project_args["sch_file"] = sch_file
        self._tool("kicad_set_project", project_args)

        read: dict[str, Any] = {"project": self._tool("kicad_get_project_info", {})}
        if pcb_file:
            read["board_summary"] = self._tool("pcb_get_board_summary", {})
            read["footprints"] = self._tool("pcb_get_footprints", {})
        if sch_file:
            read["symbols"] = self._tool("sch_get_symbols", {})
            read["connectivity"] = self._tool("sch_get_connectivity_graph", {})
            if "sch_list_sheets" in self.list_tools():
                read["sheets"] = self._tool("sch_list_sheets", {})
            if "sch_get_circuit_ir" in self.list_tools():
                read["circuit_ir"] = self._tool("sch_get_circuit_ir", {})
        return read


def make_client(**kwargs: Any) -> McpHttpClient:
    """The engine's MCP client: in-process when KICAD_AI_MCP_INPROCESS=1, else HTTP to ``default_endpoint()``."""
    if os.environ.get("KICAD_AI_MCP_INPROCESS", "").strip() == "1":
        from .mcp_inprocess import McpInProcessClient

        return McpInProcessClient(**kwargs)
    return McpHttpClient(**kwargs)
