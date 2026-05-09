"""Per-PR mutex implemented via the GitHub label ``proctor:running``.

Two concurrent ``/proctor`` invocations on the same PR must not both
proceed. ``acquire`` is best-effort: a label-based lock has a TOCTOU
window, but is good enough for this domain (concurrent invocations are
rare and human-driven).
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

LABEL = "proctor:running"


def _repo_args(repo: Optional[str]) -> list[str]:
    return ["-R", repo] if repo else []


def acquire(*, pr_number: int, repo: Optional[str]) -> bool:
    """Add the lock label. Return False if already present (lock held)."""
    out = subprocess.check_output(
        ["gh", "pr", "view", str(pr_number), "--json", "labels"] + _repo_args(repo),
        text=True,
    )
    data = json.loads(out)
    labels = [lab["name"] for lab in data.get("labels", [])] if isinstance(data, dict) else \
             [lab["name"] for lab in data] if isinstance(data, list) else []
    if LABEL in labels:
        return False
    subprocess.check_call(
        ["gh", "pr", "edit", str(pr_number), "--add-label", LABEL] + _repo_args(repo)
    )
    return True


def release(*, pr_number: int, repo: Optional[str]) -> None:
    """Remove the lock label. Idempotent — ignore "label does not exist"."""
    try:
        subprocess.check_call(
            ["gh", "pr", "edit", str(pr_number), "--remove-label", LABEL]
            + _repo_args(repo)
        )
    except subprocess.CalledProcessError:
        # Already not present, or label undefined in repo — fine.
        pass
