import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data):
        raw = json.dumps(
            data, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    def append(self, event, details):
        previous = "GENESIS"

        if self.path.exists():
            lines = self.path.read_text().splitlines()
            if lines:
                previous = json.loads(lines[-1])["hash"]

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details,
            "previous_hash": previous
        }

        record["hash"] = self.digest(record)

        with self.path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

        return record["hash"]

    def verify(self):
        if not self.path.exists():
            return True

        previous = "GENESIS"

        for line in self.path.read_text().splitlines():
            record = json.loads(line)
            supplied_hash = record.pop("hash")

            if record["previous_hash"] != previous:
                return False

            if self.digest(record) != supplied_hash:
                return False

            previous = supplied_hash

        return True
