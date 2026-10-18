"""Controlled test parent: one pure offline child, no model or broker access."""

import sys
import time
from pathlib import Path

from qf_platform.supervisor import WorkerSupervisor

supervisor = WorkerSupervisor(Path(sys.argv[1]), domains=("assistant-tasks",))
supervisor.start()
print(supervisor.snapshot()["assistant-tasks"]["pid"], flush=True)
try:
    time.sleep(60)
finally:
    supervisor.close()
