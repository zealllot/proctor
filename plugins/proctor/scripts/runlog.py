"""Run-id derivation and structured single-line logging for PRoctor."""

from __future__ import annotations

import hashlib
import sys
from typing import Any


def make_run_id(*, pr_number: int, head_sha: str, started_at_iso: str) -> str:
    short_sha = head_sha[:7]
    h = hashlib.sha1(
        f"{pr_number}|{head_sha}|{started_at_iso}".encode()
    ).hexdigest()[:8]
    return f"pr{pr_number}-{short_sha}-{h}"


def log_line(stage: str, phase: str, **fields: Any) -> None:
    """Emit one structured log line: ``[proctor:<stage>] <phase> k=v k=v``.

    Goes to stdout so it lands in the Claude Code transcript without needing
    a separate logger.
    """
    parts = [f"[proctor:{stage}]", phase]
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    print(" ".join(parts), file=sys.stdout)
