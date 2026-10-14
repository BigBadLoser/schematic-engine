import threading
import time
import unittest

from backend.kicad.mcp_client import McpHttpClient


class McpConcurrencyTests(unittest.TestCase):
    def test_project_context_bundles_do_not_interleave(self) -> None:
        client = McpHttpClient()
        calls: list[tuple[str, str]] = []
        calls_lock = threading.Lock()
        active_root = {"value": ""}

        def fake_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
            if name == "kicad_set_project":
                active_root["value"] = str(arguments["project_dir"])
                time.sleep(0.02)
            with calls_lock:
                calls.append((active_root["value"], name))
            return {}

        client._tool = fake_tool  # type: ignore[method-assign]
        client.list_tools = lambda refresh=False: set()  # type: ignore[method-assign]

        threads = [
            threading.Thread(target=client.read_project, args=("project-a",)),
            threading.Thread(target=client.read_project, args=("project-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        roots = [root for root, _ in calls]
        first_b = roots.index("project-b") if "project-b" in roots else len(roots)
        self.assertTrue(all(root == "project-a" for root in roots[:first_b]))
        self.assertTrue(all(root == "project-b" for root in roots[first_b:]))
