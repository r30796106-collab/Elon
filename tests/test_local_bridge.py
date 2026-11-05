import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from engine.local_bridge import LocalBridgeServer
from security.policy import SecurityPolicy


TOKEN = "ELON_TEST_TOKEN_12345678901234567890"


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event, data):
        self.events.append((event, data))


class FakeEngine:
    def __init__(self, root):
        self.root = Path(root)
        self.workspace = self.root / "workspace"
        self.backups = self.root / "backups"

        self.workspace.mkdir()
        self.backups.mkdir()

        self.policy = SecurityPolicy(self.root)
        self.audit = FakeAudit()
        self.tasks = {}

        self.run_registered_tests = Mock(
            return_value={
                "success": True,
                "stdout": "OK",
                "stderr": "",
            }
        )

    def get_task(self, task_id):
        return self.tasks.get(task_id)


class LocalBridgeTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.temp.name)

        (self.engine.workspace / "sample.py").write_text(
            "value = 42\n",
            encoding="utf-8",
        )

        # Find an available local port.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        self.bridge = LocalBridgeServer(
            self.engine,
            TOKEN,
            port=port,
        )

        self.thread = threading.Thread(
            target=self.bridge.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.bridge.shutdown()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, token=None, body=None,
                content_type="application/json"):

        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.bridge.port,
            timeout=5,
        )

        headers = {}

        if token is not None:
            headers["Authorization"] = "Bearer " + token

        if body is not None:
            headers["Content-Type"] = content_type

        connection.request(
            method,
            path,
            body=body,
            headers=headers,
        )

        response = connection.getresponse()
        status = response.status
        raw = response.read()

        connection.close()

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}

        return status, data

    def test_bridge_binds_to_localhost(self):
        self.assertEqual(
            self.bridge.server.server_address[0],
            "127.0.0.1",
        )

    def test_rejects_unauthenticated_health_request(self):
        status, data = self.request("GET", "/health")

        self.assertEqual(status, 401)
        self.assertFalse(data["success"])

    def test_accepts_authenticated_health_request(self):
        status, data = self.request(
            "GET",
            "/health",
            token=TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "running")

    def test_rejects_wrong_token(self):
        status, _ = self.request(
            "GET",
            "/health",
            token="WRONG_TOKEN_123456789012345678901234",
        )

        self.assertEqual(status, 401)

    def test_tools_endpoint_hides_test_execution(self):
        status, data = self.request(
            "GET",
            "/tools",
            token=TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertIn("list_workspace_files", data["tools"])
        self.assertNotIn("run_registered_tests", data["tools"])

    def test_executes_allowed_read_only_tool(self):
        body = json.dumps({
            "tool": "list_workspace_files",
            "arguments": {},
        })

        status, data = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["success"])
        self.assertIn("sample.py", str(data["result"]))

    def test_blocks_registered_test_execution(self):
        body = json.dumps({
            "tool": "run_registered_tests",
            "arguments": {"test_name": "core"},
        })

        status, data = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
        )

        self.assertEqual(status, 403)
        self.assertFalse(data["success"])
        self.engine.run_registered_tests.assert_not_called()

    def test_rejects_extra_request_fields(self):
        body = json.dumps({
            "tool": "list_workspace_files",
            "arguments": {},
            "command": "whoami",
        })

        status, _ = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
        )

        self.assertEqual(status, 400)

    def test_blocks_path_traversal(self):
        body = json.dumps({
            "tool": "read_workspace_file",
            "arguments": {"path": "../outside.py"},
        })

        status, data = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
        )

        self.assertEqual(status, 400)
        self.assertFalse(data["success"])

    def test_rejects_invalid_content_type(self):
        body = json.dumps({
            "tool": "list_workspace_files",
            "arguments": {},
        })

        status, _ = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
            content_type="text/plain",
        )

        self.assertEqual(status, 415)

    def test_rejects_oversized_request(self):
        body = b"x" * (LocalBridgeServer.MAX_REQUEST_BYTES + 1)

        status, data = self.request(
            "POST",
            "/tools/execute",
            token=TOKEN,
            body=body,
        )

        self.assertEqual(status, 413)
        self.assertFalse(data["success"])

    def test_rejects_short_token(self):
        with self.assertRaises(ValueError):
            LocalBridgeServer(
                self.engine,
                "short",
                port=8766,
            )


if __name__ == "__main__":
    unittest.main()
