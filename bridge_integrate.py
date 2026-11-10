from pathlib import Path
from datetime import datetime
import shutil

root = Path.cwd()
main_path = root / "main.py"
bridge_path = root / "engine" / "local_bridge.py"

main = main_path.read_text()
bridge = bridge_path.read_text()

def replace_once(source, old, new, label):
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected 1 match, found {count}. No files changed."
        )
    return source.replace(old, new, 1)

main = replace_once(
    main, "import json\n",
    "import json\nimport os\n", "os import"
)

main = replace_once(
    main,
    "from engine.tool_registry import ToolRegistry\n",
    "from engine.tool_registry import ToolRegistry\n"
    "from engine.local_bridge import LocalBridgeServer\n",
    "bridge import"
)

main = replace_once(
    main,
    '    sub.add_parser("health")\n',
    '    sub.add_parser("health")\n\n'
    '    bridge_command = sub.add_parser("bridge")\n'
    '    bridge_command.add_argument("--port", type=int, default=8765)\n',
    "bridge parser"
)

main = replace_once(
    main,
    '    try:\n        if args.command == "create":\n',
    '    try:\n'
    '        if args.command == "bridge":\n'
    '            token = os.environ.get("ELON_BRIDGE_TOKEN", "")\n'
    '            if len(token) < 32:\n'
    '                raise RuntimeError("ELON_BRIDGE_TOKEN must contain at least 32 characters.")\n'
    '            server = LocalBridgeServer(engine, token, port=args.port)\n'
    '            print(f"ELON LOCAL BRIDGE: http://127.0.0.1:{server.port}")\n'
    '            print("Authentication: Bearer token from environment")\n'
    '            print("Access: localhost only; read-only tools")\n'
    '            print("Stop with CTRL+C")\n'
    '            try:\n'
    '                server.serve_forever()\n'
    '            except KeyboardInterrupt:\n'
    '                print("\\nELON Local Bridge stopped.")\n'
    '            finally:\n'
    '                server.close()\n'
    '        elif args.command == "create":\n',
    "bridge dispatch"
)

bridge = replace_once(
    bridge,
    '    def shutdown(self):\n'
    '        self.server.shutdown()\n'
    '        self.server.server_close()\n',
    '    def close(self):\n'
    '        self.server.server_close()\n'
    '\n'
    '    def shutdown(self):\n'
    '        self.server.shutdown()\n'
    '        self.close()\n',
    "safe close method"
)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_dir = root / "backups"
backup_dir.mkdir(exist_ok=True)

main_backup = backup_dir / f"main.py.pre_local_bridge_{stamp}.bak"
bridge_backup = backup_dir / f"local_bridge.py.pre_close_{stamp}.bak"

shutil.copy2(main_path, main_backup)
shutil.copy2(bridge_path, bridge_backup)

try:
    main_path.write_text(main)
    bridge_path.write_text(bridge)
except Exception:
    shutil.copy2(main_backup, main_path)
    shutil.copy2(bridge_backup, bridge_path)
    raise

print("LOCAL BRIDGE INTEGRATION: SUCCESS")
print("Main backup:", main_backup)
print("Bridge backup:", bridge_backup)
print("autonomous_controller.py: NOT MODIFIED")
