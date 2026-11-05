from pathlib import Path
from providers.base import AIProvider


class ManualProvider(AIProvider):
    name = "manual"

    def __init__(self, root):
        self.root = Path(root).resolve()

    def prepare(self, task):
        project = self.root / "workspace"
        output = project / f"AI_REQUEST_{task['id']}.md"

        readme = self.root / "README.md"
        policy = self.root / "security" / "POLICY.md"

        context = readme.read_text() if readme.exists() else ""
        rules = policy.read_text() if policy.exists() else ""

        prompt = f"""# ELON AI DEVELOPMENT REQUEST

## Task ID
{task['id']}

## Task
{task['title']}

## Description
{task['description']}

## Project
ELON autonomous AI developer.

## Project context
{context}

## Security rules
{rules}

## Your instructions

Act as ELON's senior software engineer.

1. Analyze the task.
2. Identify the files that need changes.
3. Break the task into small steps.
4. Identify technical risks.
5. Propose complete implementation files.
6. Include automated tests.
7. Never request secrets or credentials.
8. Never propose unrestricted shell execution.
9. Never perform deployment or financial actions.

For this first response, return a development plan only.
Do not modify files or claim that tests have passed.

Use this response structure:

1. Task understanding
2. Proposed architecture
3. Files to create or modify
4. Implementation steps
5. Security considerations
6. Testing strategy
7. Expected outcome
"""

        output.write_text(prompt, encoding="utf-8")

        return output
