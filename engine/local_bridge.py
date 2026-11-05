import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalBridgeServer:
    """
    Authenticated, localhost-only HTTP bridge.
    Exposes read-only registered tools; never exposes arbitrary shell access.
    """

    HOST = "127.0.0.1"
    MAX_REQUEST_BYTES = 8 * 1024
    MAX_RESPONSE_BYTES = 300 * 1024

    ALLOWED_TOOLS = {
        "list_workspace_files",
        "read_workspace_file",
        "scan_project",
        "analyze_project_impact",
        "build_project_context",
    }

    def __init__(self, engine, token, port=8765):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Bridge token must contain at least 32 characters")

        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("Port must be an integer")

        if not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")

        from engine.tool_registry import ToolRegistry

        self.engine = engine
        self.registry = ToolRegistry(engine)
        self.token = token

        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ELON-LocalBridge"
            sys_version = ""

            def log_message(self, format_string, *args):
                # Do not log headers or authorization tokens.
                return

            def _send(self, status, payload):
                raw = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

                if len(raw) > owner.MAX_RESPONSE_BYTES:
                    status = 500
                    raw = b'{"success":false,"error":"Response too large"}'

                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self):
                supplied = self.headers.get("Authorization", "")
                expected = "Bearer " + owner.token
                return hmac.compare_digest(supplied, expected)

            def _reject_auth(self):
                self._send(401, {
                    "success": False,
                    "error": "Authentication required",
                })

            def _audit(self, event, details):
                owner.engine.audit.append(event, details)

            def do_GET(self):
                if not self._authorized():
                    self._reject_auth()
                    return

                if self.path == "/health":
                    self._audit("LOCAL_BRIDGE_HEALTH", {"success": True})
                    self._send(200, {
                        "success": True,
                        "service": "ELON Local Bridge",
                        "status": "running",
                        "host": owner.HOST,
                    })
                    return

                if self.path == "/tools":
                    tools = {
                        name: sorted(owner.registry.tools[name])
                        for name in sorted(owner.ALLOWED_TOOLS)
                        if name in owner.registry.tools
                    }

                    self._audit("LOCAL_BRIDGE_TOOLS_LISTED", {
                        "count": len(tools),
                    })
                    self._send(200, {
                        "success": True,
                        "tools": tools,
                    })
                    return

                self._send(404, {
                    "success": False,
                    "error": "Endpoint not found",
                })

            def do_POST(self):
                if not self._authorized():
                    self._reject_auth()
                    return

                if self.path != "/tools/execute":
                    self._send(404, {
                        "success": False,
                        "error": "Endpoint not found",
                    })
                    return

                if self.headers.get_content_type() != "application/json":
                    self._send(415, {
                        "success": False,
                        "error": "Content-Type must be application/json",
                    })
                    return

                length_header = self.headers.get("Content-Length")

                try:
                    length = int(length_header)
                except (TypeError, ValueError):
                    self._send(411, {
                        "success": False,
                        "error": "Valid Content-Length required",
                    })
                    return

                if length <= 0 or length > owner.MAX_REQUEST_BYTES:
                    self._send(413, {
                        "success": False,
                        "error": "Request body size is invalid or too large",
                    })
                    return

                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete request body")

                    request = json.loads(raw.decode("utf-8"))

                    if not isinstance(request, dict):
                        raise ValueError("Request must be a JSON object")

                    if set(request) != {"tool", "arguments"}:
                        raise ValueError("Unexpected request fields")

                    tool = request["tool"]
                    arguments = request["arguments"]

                    if not isinstance(tool, str) or tool not in owner.ALLOWED_TOOLS:
                        raise PermissionError("Tool is not enabled on the bridge")

                    if not isinstance(arguments, dict):
                        raise ValueError("Arguments must be a JSON object")

                    result = owner.registry.execute(tool, arguments)

                    owner._audit("LOCAL_BRIDGE_TOOL", {
                        "tool": tool,
                        "success": True,
                    })

                    self._send(200, {
                        "success": True,
                        "tool": tool,
                        "result": result,
                    })

                except PermissionError as error:
                    owner._audit("LOCAL_BRIDGE_REJECTED", {
                        "reason": str(error)[:200],
                    })
                    self._send(403, {
                        "success": False,
                        "error": str(error),
                    })

                except Exception as error:
                    owner._audit("LOCAL_BRIDGE_REJECTED", {
                        "reason": type(error).__name__,
                    })
                    self._send(400, {
                        "success": False,
                        "error": str(error)[:200],
                    })

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server((self.HOST, port), Handler)

    def _audit(self, event, details):
        self.engine.audit.append(event, details)

    @property
    def port(self):
        return self.server.server_address[1]

    def serve_forever(self):
        self.server.serve_forever(poll_interval=0.5)

    def close(self):
        self.server.server_close()

    def shutdown(self):
        self.server.shutdown()
        self.close()
