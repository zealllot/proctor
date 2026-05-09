"""Wrap ``gh`` for PR metadata + diff retrieval.

The CLI-friendly entry points are ``parse_pr_arg`` (string → structured) and
``fetch_pr`` (structured → JSON dict). ``fetch_pr`` shells out to ``gh``;
network I/O happens here, nowhere else.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

_URL_RE = re.compile(
    r"^https?://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<num>\d+)/?$"
)


@dataclass(frozen=True)
class PRArg:
    number: int
    repo: Optional[str]   # "owner/name" or None for current repo


def parse_pr_arg(s: str) -> PRArg:
    s = s.strip()
    if s.isdigit():
        return PRArg(number=int(s), repo=None)
    m = _URL_RE.match(s)
    if m:
        return PRArg(number=int(m.group("num")), repo=m.group("repo"))
    raise ValueError(f"unparseable PR argument: {s!r}")


def fetch_pr(arg: PRArg) -> dict:
    """Return PR metadata as a dict via ``gh pr view --json``.

    Network I/O. Raises subprocess.CalledProcessError on gh failure.
    """
    cmd = ["gh", "pr", "view", str(arg.number), "--json",
           "number,headRefOid,baseRefOid,url,headRefName,baseRefName,title,body,author"]
    if arg.repo:
        cmd += ["-R", arg.repo]
    out = _gh_with_retry(cmd)
    return json.loads(out)


def fetch_diff(arg: PRArg) -> str:
    """Return raw unified diff via ``gh pr diff``."""
    cmd = ["gh", "pr", "diff", str(arg.number)]
    if arg.repo:
        cmd += ["-R", arg.repo]
    return _gh_with_retry(cmd)


import time as _time


def _gh_with_retry(cmd: list[str], *, attempts: int = 3) -> str:
    """Run a ``gh`` command with bounded retry on secondary rate limit (429)."""
    for i in range(attempts):
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "")
            if "rate limit" not in stderr.lower() or i == attempts - 1:
                raise
            wait_s = min(60 * (2 ** i), 300)
            _time.sleep(wait_s)
    raise RuntimeError("unreachable")
