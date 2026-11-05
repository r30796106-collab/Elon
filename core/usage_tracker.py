
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


class UsageTracker:
    INPUT_USD_PER_MILLION = 0.25
    OUTPUT_USD_PER_MILLION = 1.50
    DEFAULT_USD_INR = 90.0
    MONTHLY_BUDGET_INR = 500.0
    WARNING_INR = 400.0

    def __init__(self):
        self.directory = Path.home() / ".config/elon"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "usage.json"
        self.usd_inr = float(os.environ.get(
            "ELON_USD_INR", self.DEFAULT_USD_INR
        ))

    def _month(self):
        return datetime.now().strftime("%Y-%m")

    def _read(self):
        if not self.path.exists():
            return {"month": self._month(), "estimated_inr": 0.0,
                    "requests": 0, "input_tokens": 0, "output_tokens": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("month") != self._month():
                return {"month": self._month(), "estimated_inr": 0.0,
                        "requests": 0, "input_tokens": 0, "output_tokens": 0}
            return data
        except (OSError, ValueError, TypeError):
            raise RuntimeError(
                "Usage file is unreadable. Inspect ~/.config/elon/usage.json"
            )

    def _write(self, data):
        fd, temp_name = tempfile.mkstemp(
            prefix=".usage_", dir=str(self.directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def estimate(self, input_tokens, output_tokens):
        usd = (
            input_tokens * self.INPUT_USD_PER_MILLION
            + output_tokens * self.OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        return usd * self.usd_inr

    def check_budget(self, prompt, max_output_tokens):
        data = self._read()
        estimated_input = max(1, (len(prompt.encode("utf-8")) + 2) // 3)
        reserve = self.estimate(estimated_input, max_output_tokens) * 1.20

        if data["estimated_inr"] + reserve > self.MONTHLY_BUDGET_INR:
            raise RuntimeError(
                "ELON monthly estimated-cost limit reached. "
                "Check: python main.py usage"
            )

    def record(self, model, input_tokens, output_tokens):
        data = self._read()
        cost = self.estimate(input_tokens, output_tokens)
        data["estimated_inr"] = round(
            float(data.get("estimated_inr", 0)) + cost, 6
        )
        data["requests"] = int(data.get("requests", 0)) + 1
        data["input_tokens"] = int(data.get("input_tokens", 0)) + input_tokens
        data["output_tokens"] = int(data.get("output_tokens", 0)) + output_tokens
        data["last_model"] = model
        data["updated_at"] = datetime.now().astimezone().isoformat()
        self._write(data)

    def report(self):
        data = self._read()
        spent = float(data["estimated_inr"])
        budget = self.MONTHLY_BUDGET_INR
        percent = min(100.0, spent / budget * 100)
        filled = min(25, int(percent / 4))
        bar = "#" * filled + "-" * (25 - filled)

        lines = [
            "ELON MONTHLY AI SPENDING",
            f"Month: {data['month']}",
            f"Estimated paid-tier equivalent: Rs {spent:.4f}",
            f"Monthly limit: Rs {budget:.2f}",
            f"Remaining estimated budget: Rs {max(0, budget-spent):.4f}",
            f"Meter: [{bar}] {percent:.2f}%",
            "Scale: Rs 1 -> Rs 500",
            f"Requests: {data['requests']}",
            f"Input tokens: {data['input_tokens']}",
            f"Output tokens: {data['output_tokens']}",
            f"USD/INR estimate: {self.usd_inr}",
            "Billing note: This is a local estimate, not Google's invoice."
        ]
        if spent >= budget:
            lines.append("STATUS: LIMIT REACHED")
        elif spent >= self.WARNING_INR:
            lines.append("STATUS: WARNING - Rs 400 threshold reached")
        else:
            lines.append("STATUS: WITHIN LOCAL LIMIT")
        return "\n".join(lines)
