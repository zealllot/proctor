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
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def fetch_diff(arg: PRArg) -> str:
    """Return raw unified diff via ``gh pr diff``."""
    cmd = ["gh", "pr", "diff", str(arg.number)]
    if arg.repo:
        cmd += ["-R", arg.repo]
    return subprocess.check_output(cmd, text=True)
