from __future__ import annotations

import argparse
from pathlib import Path

from .adapters import CodexAdapter, DeepSeekAdapter
from .models import TaskSpec
from .orchestrator import Orchestrator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run",))
    parser.add_argument("task_id")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--audit-root", default=None)
    args = parser.parse_args()
    audit_root = args.audit_root or (
        Path(args.repo).resolve().parent / ".autolab" / "runs"
    )
    result = Orchestrator(args.repo, audit_root).run(
        TaskSpec(args.task_id, f"tasks/{args.task_id}.md"),
        DeepSeekAdapter(),
        CodexAdapter(),
    )
    print(result.state.value)


if __name__ == "__main__":
    main()
