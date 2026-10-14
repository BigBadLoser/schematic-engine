import json
import unittest
from unittest.mock import patch

from backend.kicad.mcp_client import McpHttpClient, decode_mcp_response


class FakeResponse:
    def __init__(self, payload: dict | None, headers: dict[str, str] | None = None) -> None:
        self._body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class McpClientTests(unittest.TestCase):
    def test_decodes_streamable_http_sse_payload(self) -> None:
        payload = b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        decoded = decode_mcp_response(payload)
        self.assertTrue(decoded["result"]["ok"])

    @patch("backend.kicad.mcp_client.urlopen")
    def test_probe_initializes_session_and_lists_tools(self, urlopen: object) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "kicad-mcp-pro", "version": "test"},
                    },
                },
                {"MCP-Session-Id": "session-1"},
            ),
            FakeResponse(None),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {"name": "kicad_set_project"},
                            {"name": "sch_get_symbols"},
                        ]
                    },
                }
            ),
        ]
        probe = McpHttpClient().probe()
        self.assertEqual(probe["session_id"], "session-1")
        self.assertEqual(
            probe["tools"], ["kicad_set_project", "sch_get_symbols"]
        )

    @patch("backend.kicad.mcp_client.urlopen")
    def test_probe_reports_missing_required_tools(self, urlopen: object) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "kicad-mcp-pro", "version": "test"},
                    },
                }
            ),
            FakeResponse(None),
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "kicad_set_project"}]},
                }
            ),
        ]
        probe = McpHttpClient().probe({"kicad_set_project", "pcb_get_footprints"})
        self.assertEqual(probe["missing_required_tools"], ["pcb_get_footprints"])


    def test_rejects_tool_outside_read_only_allowlist(self) -> None:
        from backend.kicad.mcp_client import McpConnectionError

        client = McpHttpClient()
        with self.assertRaisesRegex(McpConnectionError, "not allowed"):
            client._tool("sch_add_symbol", {})


class EndpointPortTests(unittest.TestCase):
    def test_default_port_is_3334(self) -> None:
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("KICAD_AI_MCP_PORT", None)
            self.assertEqual(McpHttpClient().endpoint, "http://127.0.0.1:3334/mcp")

    def test_port_comes_from_environment(self) -> None:
        with patch.dict("os.environ", {"KICAD_AI_MCP_PORT": "3336"}):
            self.assertEqual(McpHttpClient().endpoint, "http://127.0.0.1:3336/mcp")
            self.assertEqual(McpHttpClient(endpoint="http://x/mcp").endpoint, "http://x/mcp")
