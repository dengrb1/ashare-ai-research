"""Small subprocess boundary shared by long-lived queue consumers."""

from __future__ import annotations

import subprocess
import sys


def execute_isolated(kind: str, job_id: str) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "ashare_ai.orchestration.run_job", kind, job_id],
        check=False,
    )
    return completed.returncode
