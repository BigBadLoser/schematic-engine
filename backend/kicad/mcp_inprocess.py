"""The MCP client's interface, served by the extended server object in this process.

``McpInProcessClient`` is ``McpHttpClient`` with the transport replaced: ``initialize``, ``tools/list`` and
``tools/call`` go straight to the FastMCP server that ``build_extended_server`` returns, with no HTTP and no server
process. Everything above the transport (tool allow-lists, write guard, context switch, result parsing, the error
types and messages) is inherited, so callers see what they see over HTTP.

The server's coroutines run on one dedicated event-loop thread for the life of the client. That works from plain
code and from inside a running loop alike, and keeps loop-bound state (locks, limiters) on a single loop.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import McpConnectionError, McpHttpClient

DEFAULT_PROFILE = "schematic_authoring"
DEFAULT_MODE = "write"


@dataclass
class McpInProcessClient(McpHttpClient):
    profile: str = DEFAULT_PROFILE
    mode: str = DEFAULT_MODE
    endpoint: str = "inprocess://extended-server"
    _server: Any = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    # --- the event-loop thread ---------------------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            self._thread = threading.Thread(target=run, name="mcp-inprocess", daemon=True)
            self._thread.start()
            ready.wait()
            self._loop = loop
            atexit.register(self.close)
        return self._loop

    def _run(self, coroutine: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._ensure_loop())
        try:
            return future.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise McpConnectionError(f"Could not reach MCP Pro at {self.endpoint}: timed out") from error

    def close(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            if not loop.is_running():
                loop.close()

    # --- transport ---------------------------------------------------------------------------------------------

    def _initialize_locked(self) -> dict[str, Any]:
        if self._initialized:
            return self._server_info
        from .mcp_extended_server import build_extended_server

        import kicad_mcp.config as mcp_config

        os.environ["KICAD_MCP_OPERATING_MODE"] = self.mode  # what the server's main() does with --mode
        mcp_config._config = None  # the config singleton reads the environment once: start it as a fresh server would
        self._server = build_extended_server(self.profile)
        self._server_info = {"name": "kicad-mcp-pro (in-process)", "profile": self.profile, "mode": self.mode}
        self._initialized = True
        return self._server_info

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._initialize_locked()
            params = params or {}
            if method == "tools/list":
                tools = self._run(self._server.list_tools())
                return {"tools": [tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools]}
            if method == "tools/call":
                return self._call_tool(str(params["name"]), dict(params.get("arguments") or {}))
            raise McpConnectionError(f"MCP Pro error: unsupported in-process method {method}")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._run(self._server.call_tool(name, arguments))
        except McpConnectionError:
            raise
        except Exception as error:  # the HTTP layer turns an exception inside a tool into an error result
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        if hasattr(result, "model_dump"):
            return dict(result.model_dump(by_alias=True, exclude_none=True, mode="json"))
        raise McpConnectionError(f"MCP Pro error: unexpected in-process result {type(result).__name__}")
