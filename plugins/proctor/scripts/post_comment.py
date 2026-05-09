"""Post a comment to a PR. If the body exceeds GitHub's 65 KB limit,
upload the long body as a gist and post a short summary that links to
it.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# Conservative threshold; GitHub's hard limit is ~65,536 chars.
MAX_INLINE = 60_000


def post(*, pr_number: int, repo: Optional[str], body: str,
         summary_for_gist: Optional[str] = None) -> None:
    repo_args = ["-R", repo] if repo else []
    if len(body) <= MAX_INLINE:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as f:
            f.write(body); path = f.name
        try:
            subprocess.check_output(
                ["gh", "pr", "comment", str(pr_number), "--body-file", path]
                + repo_args, text=True
            )
        finally:
            Path(path).unlink(missing_ok=True)
        return

    # Long body → gist.
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as f:
        f.write(body); gist_path = f.name
    try:
        gist_out = subprocess.check_output(
            ["gh", "gist", "create", "--filename",
             f"proctor-pr{pr_number}-report.md", gist_path],
            text=True,
        )
    finally:
        Path(gist_path).unlink(missing_ok=True)

    gist_url = gist_out.strip().splitlines()[-1].strip()
    short = summary_for_gist or "PRoctor report"
    inline_body = f"{short}\n\nFull report (too long for a comment): {gist_url}"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as f:
        f.write(inline_body); path = f.name
    try:
        subprocess.check_output(
            ["gh", "pr", "comment", str(pr_number), "--body-file", path]
            + repo_args, text=True
        )
    finally:
        Path(path).unlink(missing_ok=True)
