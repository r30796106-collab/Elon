import json
import secrets
import socket
import threading
import urllib.request

from engine.task_engine import TaskEngine
from engine.local_bridge import LocalBridgeServer

# Find an available unprivileged port.
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

engine = TaskEngine(".")
token = secrets.token_urlsafe(32)

server = LocalBridgeServer(engine, token, port=port)
thread = threading.Thread(target=server.serve_forever, daemon=True)

try:
    thread.start()
    base = f"http://127.0.0.1:{server.port}"

    def get_json(path):
        request = urllib.request.Request(
            base + path,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    status, health = get_json("/health")
    print("HEALTH HTTP:", status)
    print("HEALTH STATUS:", health.get("status"))

    status, tools = get_json("/tools")
    print("TOOLS HTTP:", status)
    print("AVAILABLE TOOLS:")
    for name in tools.get("tools", []):
        print("-", name)

    assert health.get("status") == "running"
    assert status == 200

    print("\nACTUAL TASKENGINE SMOKE TEST: PASS")

finally:
    if thread.is_alive():
        server.shutdown()
        thread.join(timeout=5)
    server.close()
