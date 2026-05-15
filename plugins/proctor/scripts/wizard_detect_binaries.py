"""Detect Go entry-point binaries the consumer repo declares under
``cmd/`` (plus the optional root ``main.go``) and classify each as
``http-server`` / ``daemon`` / ``one-shot`` / ``unknown`` based on
the patterns its ``main.go`` source contains.

## Why this exists (v0.7.7)

v0.7.6 e2e against mcd-website PR #1126 found a real gap. The
project has multiple ``cmd/*/main.go`` binaries:

- root ``main.go`` — the HTTP server users hit at ``/admin``.
- ``cmd/mcd-daemon/main.go`` — a 1-minute ticker that re-publishes
  banners / categories / etc. to S3.
- ``cmd/mcd-publisher/main.go`` — one-shot republisher (CLI tool).
- ``cmd/mcd-sitemap/main.go`` — one-shot sitemap generator.

PRoctor's local-setup ran ``go run .`` which starts the HTTP server
but NOT mcd-daemon. So when a PR claimed "Published JSON
include_tags/exclude_tags are arrays of trimmed tokens", admin
save → daemon publishes → published-JSON-on-S3 never happened
during a PRoctor run — the daemon wasn't there to publish anything.
The planner had nothing to assert against and had to lint-only the
output format.

The fix is to bring up ALL the project's daemons (HTTP server +
publish loops + workers) in local setup so the system runs as it
normally would for a real developer. Then PRoctor's planner can
plan a plain ``curl <published-url>`` item that waits for the
ticker to fire.

To make that work, the wizard needs to TELL the user "here are
your project's binaries; which ones should PRoctor start during
setup?". That selection question needs a candidate list with
heuristic classifications so the user can quickly say "yes, start
that daemon; no, that one's a one-shot CLI".

This module produces that list. The wizard surfaces it as a
multi-select AskUserQuestion in /proctor:proctor-init (fresh mode).

## Output shape

```jsonc
{
  "candidates": [
    {
      "path": "main.go",
      "binary_name": "<repo-name>",   // root main.go → repo basename
      "looks_like": "http-server",
      "evidence": ["http.ListenAndServe"]
    },
    {
      "path": "cmd/mcd-daemon/main.go",
      "binary_name": "mcd-daemon",
      "looks_like": "daemon",
      "evidence": ["time.NewTicker"]
    },
    ...
  ]
}
```

`evidence` lists the patterns that matched, so the wizard's
question text can quote them ("looks like: daemon — ticker/job
loop detected") and the user trusts the classification.

## Classifier patterns

Conservative — when nothing matches, mark ``unknown`` rather than
forcing a guess. The wizard's question UI still includes
``unknown`` entries (the user might know better than the
heuristics) but defaults them to NOT-preselected.

- ``http-server`` — HTTP listener pattern:
  ``http.ListenAndServe`` / ``http.Server`` / ``router.Run`` /
  ``router.ListenAndServe`` / ``fasthttp`` / ``gin.New`` /
  ``echo.New``.
- ``daemon`` — ticker / cron / async worker pattern:
  ``time.Tick`` / ``time.NewTicker`` / ``cron.AddFunc`` /
  ``cron.New`` / ``RunJob`` / ``workerqueue`` /
  ``sync.WaitGroup.*Wait``.
- ``one-shot`` — short file (<200 lines), no server / no ticker
  pattern. Typical CLI utilities (sitemap generators, republishers,
  migration tools). Best to NOT start in setup; the user runs them
  on-demand.
- ``unknown`` — none of the above matched and the file is non-
  trivial in size. Classifier punts; user decides.

Order matters: ``http-server`` checked first so an HTTP server
that ALSO has a ticker (rare but possible — health-check beacon
inside the same binary) classifies as http-server (the user-facing
role wins). Daemon-only binaries don't expose HTTP listeners by
definition, so the check order is unambiguous in practice.

## CLI

```
python3 wizard_detect_binaries.py --repo-root <path>
```

Stdout: JSON object as above. Always exits 0 — empty candidate
list (no binaries detected) is a valid result (the wizard skips
the daemon-selection step in that case).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# Patterns checked in priority order — first match wins.
# Listed as (pattern, label, classification).
_HTTP_SERVER_PATTERNS = [
    (r"\bhttp\.ListenAndServe\b", "http.ListenAndServe"),
    (r"\bhttp\.Server\b", "http.Server"),
    (r"\brouter\.(?:Run|ListenAndServe)\b", "router.Run/ListenAndServe"),
    (r"\bfasthttp\b", "fasthttp"),
    (r"\bgin\.New\b", "gin.New"),
    (r"\becho\.New\b", "echo.New"),
]

_DAEMON_PATTERNS = [
    (r"\btime\.Tick\b", "time.Tick"),
    (r"\btime\.NewTicker\b", "time.NewTicker"),
    (r"\bcron\.AddFunc\b", "cron.AddFunc"),
    (r"\bcron\.New\b", "cron.New"),
    (r"\bRunJob\b", "RunJob"),
    (r"\bworkerqueue\b", "workerqueue"),
    (r"\bsync\.WaitGroup\b[\s\S]*?\.Wait\(\)", "sync.WaitGroup.Wait"),
]

# Lines under which we treat a "no server / no daemon" binary as a
# one-shot CLI rather than `unknown`. Empirically, sitemap
# generators / republishers / migration tools live in <200 lines of
# main.go. Larger binaries that fit no pattern are more likely
# misclassified than legitimately one-shot.
_ONE_SHOT_LINE_THRESHOLD = 200


def _matches(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    """Return the labels of every pattern that matched the text.
    Empty list = no match."""
    hits: list[str] = []
    for pat, label in patterns:
        if re.search(pat, text):
            hits.append(label)
    return hits


def _classify(content: str) -> tuple[str, list[str]]:
    """Return ``(looks_like, evidence)`` for a main.go's source.

    Priority: http-server > daemon > one-shot > unknown. See module
    docstring for rationale on the ordering.
    """
    http_hits = _matches(content, _HTTP_SERVER_PATTERNS)
    if http_hits:
        return "http-server", http_hits
    daemon_hits = _matches(content, _DAEMON_PATTERNS)
    if daemon_hits:
        return "daemon", daemon_hits
    line_count = content.count("\n") + 1
    if line_count < _ONE_SHOT_LINE_THRESHOLD:
        return "one-shot", [f"short ({line_count} lines), no ticker/server"]
    return "unknown", []


def _binary_name_for(path: Path, repo_root: Path) -> str:
    """Derive the binary's directory name for use in pidfile / log
    naming. ``cmd/<X>/main.go`` → ``X``; root ``main.go`` →
    ``repo_root.name`` (repo basename)."""
    rel = path.relative_to(repo_root)
    parts = rel.parts
    if len(parts) == 1 and parts[0] == "main.go":
        return repo_root.name
    if len(parts) >= 2 and parts[0] == "cmd":
        return parts[-2]
    # Fallback: use the parent directory name.
    return rel.parent.name or repo_root.name


def detect_binaries(repo_root: Path) -> list[dict]:
    """Return the candidate list for the wizard.

    Walks ``cmd/*/main.go`` (skipping vendored / node_modules paths)
    PLUS the root ``main.go`` if present. Classifies each by
    reading its source. Sort order: root main.go first, then
    cmd/* alphabetically — stable across runs.
    """
    candidates: list[dict] = []

    # Root main.go — emit first if present.
    root_main = repo_root / "main.go"
    if root_main.is_file():
        try:
            content = root_main.read_text(errors="replace")
        except OSError:
            content = ""
        looks_like, evidence = _classify(content)
        candidates.append({
            "path": "main.go",
            "binary_name": _binary_name_for(root_main, repo_root),
            "looks_like": looks_like,
            "evidence": evidence,
        })

    # cmd/*/main.go — walk one level under cmd/.
    cmd_dir = repo_root / "cmd"
    if cmd_dir.is_dir():
        # Sort directory names so output is stable.
        for sub in sorted(cmd_dir.iterdir()):
            if not sub.is_dir():
                continue
            # Skip vendored / build-cache style dirs that snuck in.
            if sub.name.startswith(".") or sub.name in {"vendor", "node_modules"}:
                continue
            main_file = sub / "main.go"
            if not main_file.is_file():
                continue
            try:
                content = main_file.read_text(errors="replace")
            except OSError:
                content = ""
            looks_like, evidence = _classify(content)
            candidates.append({
                "path": str(main_file.relative_to(repo_root)),
                "binary_name": _binary_name_for(main_file, repo_root),
                "looks_like": looks_like,
                "evidence": evidence,
            })

    return candidates


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repo-root",
        default=".",
        help="Consumer repo root (default: cwd). The script scans "
             "this directory's root main.go + cmd/*/main.go.",
    )
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    candidates = detect_binaries(repo_root)

    sys.stdout.write(json.dumps({"candidates": candidates}, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
