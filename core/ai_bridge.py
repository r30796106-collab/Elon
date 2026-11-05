from pathlib import Path
from providers.manual_provider import ManualProvider


class AIBridge:
    MAX_RESPONSE_BYTES = 100 * 1024

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.provider = ManualProvider(self.root)

    def prepare_task(self, engine, task_id):
        task = engine.get_task(task_id)

        if not task:
            raise ValueError("Task not found")

        if task["status"] != "pending":
            raise ValueError("Only pending tasks can be prepared")

        output = self.provider.prepare(task)

        engine.audit.append(
            "AI_REQUEST_PREPARED",
            {
                "task_id": task_id,
                "provider": self.provider.name,
                "file": str(output.relative_to(self.root))
            }
        )

        return output

    def ingest_response(self, engine, task_id, response_path):
        task = engine.get_task(task_id)

        if not task:
            raise ValueError("Task not found")

        if task["status"] != "pending":
            raise ValueError("Only pending tasks can receive a plan")

        relative = Path(response_path)

        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Response path must stay inside workspace")

        if not relative.parts:
            raise ValueError("Response path is empty")

        source = self.workspace / relative

        # Reject symlinks in every path component.
        current = self.workspace
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symbolic links are not permitted")

        try:
            source = source.resolve(strict=True)
            source.relative_to(self.workspace)
        except (OSError, ValueError):
            raise ValueError("Response file must exist inside workspace")

        if not source.is_file():
            raise ValueError("Response must be a regular file")

        size = source.stat().st_size
        if size == 0:
            raise ValueError("Response file is empty")
        if size > self.MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds 100 KB limit")

        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError("Response must be UTF-8 text")

        if not content.strip():
            raise ValueError("Response contains no readable text")

        output = self.workspace / f"AI_PLAN_{task_id}.md"

        # Never silently overwrite an earlier plan.
        try:
            with output.open("x", encoding="utf-8") as file:
                file.write(content)
        except FileExistsError:
            raise ValueError(
                f"Plan already exists: {output.name}. "
                "Review or rename it before ingesting another response."
            )

        engine.audit.append(
            "AI_RESPONSE_INGESTED",
            {
                "task_id": task_id,
                "source": str(source.relative_to(self.root)),
                "plan": str(output.relative_to(self.root)),
                "bytes": size,
                "executed": False
            }
        )

        return output
