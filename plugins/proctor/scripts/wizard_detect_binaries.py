"""Detect Go entry-point binaries the consumer repo declares under
``cmd/`` (plus the optional root ``main.go``) and classify each as
``serves-http`` / ``runs-loop`` / ``runs-once`` / ``unknown`` based on
the source patterns its ``main.go`` contains.

## Why this exists (v0.7.7+)

v0.7.6 e2e against mcd-website PR #1126 found a real gap. The
project has multiple ``cmd/*/main.go`` binaries:

- root ``main.go`` — the HTTP server users hit at ``/admin``.
- ``cmd/<X>-daemon/main.go`` — a 1-minute ticker that re-publishes
  banners / categories / etc. to S3.
- ``cmd/<X>-publisher/main.go`` — one-shot republisher (CLI tool).
- ``cmd/<X>-sitemap/main.go`` — one-shot sitemap generator.

PRoctor's local-setup ran ``go run .`` which starts the HTTP server
but NOT the long-running supplementary binary that re-publishes on a
tick. When a PR claimed "Published JSON include_tags/exclude_tags
are arrays of trimmed tokens", admin save → supplementary binary
publishes → published-JSON-on-S3 never happened during a PRoctor
run — the supplementary binary wasn't there to publish anything.
The planner had nothing to assert against and had to lint-only the
output format.

The fix is to bring up ALL the project's binaries (HTTP server +
long-running supplementary binaries) in local setup so the system
runs as it normally would for a real developer. Then PRoctor's
planner can plan a plain ``curl <published-url>`` item that waits
for the ticker to fire.

To make that work, the wizard needs to TELL the user "here are
your project's binaries; which ones should PRoctor start during
setup?". That selection question needs a candidate list with
heuristic classifications + concrete evidence so the user can
quickly say "yes, that's a long-running loop I want started; no,
that's a one-shot CLI".

This module produces that list. The wizard surfaces it as a
multi-select AskUserQuestion in /proctor:proctor-init.

## v0.7.9 — neutral terminology

v0.7.8 and earlier used the labels ``http-server`` / ``daemon`` /
``one-shot`` / ``unknown``. "daemon" is a consumer-specific noun
(mcd-website ships a binary called ``mcd-daemon`` — the label
matched the binary name and made the classification look
authoritative). Different consumers use different category names:
``sidekiq``, ``celery-worker``, ``cron``, ``scheduler``,
``pubsub-listener``, etc. v0.7.9 renames the categories to neutral
structural descriptions:

- ``serves-http`` — the binary's primary purpose is serving HTTP.
- ``runs-loop`` — the binary's primary purpose is a long-running
  loop (ticker, cron, worker queue, scheduler).
- ``runs-once`` — short binary with no loop pattern; CLI utility.
- ``unknown`` — none of the above; the user decides.

The classifier logic is unchanged from v0.7.8; only the labels
are renamed. Downstream code that reads ``looks_like`` should
accept both v0.7.9 labels (preferred) and v0.7.8 legacy labels
(for backward-compat with cached wizard JSON).

## Output shape

```jsonc
{
  "candidates": [
    {
      "path": "main.go",
      "binary_name": "<repo-name>",   // root main.go → repo basename
      "looks_like": "serves-http",
      "evidence": ["matches 'http.ListenAndServe'"]
    },
    {
      "path": "cmd/<X>-daemon/main.go",
      "binary_name": "<X>-daemon",
      "looks_like": "runs-loop",
      "evidence": [
        "matches 'time.NewTicker'",
        "matches 'utils.RunJob(' (×15)"
      ]
    },
    ...
  ]
}
```

The ``evidence`` field lists the patterns the file matched (with
match counts where useful), so the wizard's user-facing AskUser
prompt can quote them ("matches 'time.NewTicker' — runs-loop
pattern") and the user trusts the classification. v0.7.9 adds an
explicit note when ``serves-http`` patterns were ALSO present but
``runs-loop`` precedence applied (e.g. a binary with 15 ticker
goroutines AND a tail-end ``/health-check`` HTTP listener — the
ticker work is the primary purpose).

## Classifier patterns

Conservative — when nothing matches, mark ``unknown`` rather than
forcing a guess. The wizard's question UI still includes
``unknown`` entries (the user might know better than the
heuristics) but defaults them to NOT-preselected.

- ``serves-http`` — HTTP listener pattern:
  ``http.ListenAndServe`` / ``http.Server`` / ``router.Run`` /
  ``router.ListenAndServe`` / ``fasthttp`` / ``gin.New`` /
  ``echo.New`` / ``<pkg>.ListenAndServe[TLS]?`` (any package
  exposing a ListenAndServe method — appkit's
  ``server.ListenAndServe``, etc.). A short file (<200 lines) that
  matches this pattern is still ``serves-http`` (the root
  ``main.go`` thin-wrapper case).
- ``runs-loop`` — ticker / scheduled work / async worker pattern:
  ``time.Tick`` / ``time.NewTicker`` / ``cron.AddFunc`` /
  ``cron.New`` / ``RunJob`` / ``workerqueue`` /
  ``sync.WaitGroup.*Wait``.
- ``runs-once`` — short file (<200 lines), no server / no loop
  pattern. Typical CLI utilities (sitemap generators, republishers,
  migration tools). Best to NOT start in setup; the user runs them
  on-demand.
- ``unknown`` — none of the above matched and the file is non-
  trivial in size. Classifier punts; user decides.

Order matters (v0.7.8+): ``runs-loop`` checked FIRST so a binary
with BOTH a long-running loop AND an HTTP admin / health-check
endpoint classifies as ``runs-loop`` — the long-running
side-effect-emitting work is the primary purpose; the HTTP
listener is auxiliary. The evidence field surfaces this with an
explicit "ALSO matches ... — runs-loop precedence applied" line
so the user understands the heuristic.

## CLI

```
python3 wizard_detect_binaries.py --repo-root <path>
```

Stdout: JSON object as above. Always exits 0 — empty candidate
list (no binaries detected) is a valid result (the wizard skips
the supplementary-binaries step in that case).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# Patterns checked in priority order — first match wins.
# Listed as (regex, evidence_label).
#
# v0.7.8: broadened the ListenAndServe regex to catch any
# `<pkg>.ListenAndServe[TLS]?` call. mcd-website's root main.go
# uses `server.ListenAndServe(config.Config.HTTP, ...)` — appkit's
# wrapper. v0.7.7's regex only matched the literal `http.ListenAndServe`
# / `router.ListenAndServe` and missed appkit-style wrappers, so a
# 29-line root main.go got classified as `runs-once`.
_HTTP_SERVER_PATTERNS = [
    (r"\bhttp\.ListenAndServe\b", "http.ListenAndServe"),
    (r"\bhttp\.Server\b", "http.Server"),
    (r"\brouter\.(?:Run|ListenAndServe)\b", "router.Run/ListenAndServe"),
    (r"\bfasthttp\b", "fasthttp"),
    (r"\bgin\.New\b", "gin.New"),
    (r"\becho\.New\b", "echo.New"),
    # Generic `<lowercase-pkg>.ListenAndServe[TLS]?` — catches
    # `server.ListenAndServe`, `proxy.ListenAndServe`, etc. Excludes
    # the http/router/fasthttp matches above (they're more specific
    # and emit a cleaner evidence label). The `[a-z]` start prevents
    # matching constants / structs (CamelCase / UPPER_CASE).
    (r"\b[a-z][A-Za-z0-9_]*\.ListenAndServe(?:TLS)?\b", "<pkg>.ListenAndServe"),
]

_RUNS_LOOP_PATTERNS = [
    (r"\btime\.Tick\b", "time.Tick"),
    (r"\btime\.NewTicker\b", "time.NewTicker"),
    (r"\bcron\.AddFunc\b", "cron.AddFunc"),
    (r"\bcron\.New\b", "cron.New"),
    (r"\bRunJob\b", "RunJob"),
    (r"\bworkerqueue\b", "workerqueue"),
    (r"\bsync\.WaitGroup\b[\s\S]*?\.Wait\(\)", "sync.WaitGroup.Wait"),
]

# Lines under which we treat a "no server / no loop" binary as a
# `runs-once` CLI rather than `unknown`. Empirically, sitemap
# generators / republishers / migration tools live in <200 lines of
# main.go. Larger binaries that fit no pattern are more likely
# misclassified than legitimately one-shot.
_RUNS_ONCE_LINE_THRESHOLD = 200


def _match_with_counts(
    text: str, patterns: list[tuple[str, str]]
) -> list[tuple[str, int]]:
    """Return (label, count) tuples for every pattern that matched.
    Labels appear in input order; counts are the number of regex hits
    (capped at a reasonable display value — 999 is plenty)."""
    hits: list[tuple[str, int]] = []
    for pat, label in patterns:
        n = len(re.findall(pat, text))
        if n > 0:
            hits.append((label, min(n, 999)))
    return hits


def _format_evidence(
    label_counts: list[tuple[str, int]]
) -> list[str]:
    """Render evidence entries as 'matches X' or 'matches X (×N)' so
    the wizard's prompt can quote them. v0.7.9 makes the prefix
    explicit ('matches ...') instead of bare labels — the user-facing
    AskUser prompt shows them next to file paths and the prefix
    disambiguates pattern names from filenames."""
    out: list[str] = []
    for label, n in label_counts:
        if n == 1:
            out.append(f"matches '{label}'")
        else:
            out.append(f"matches '{label}' (×{n})")
    return out


def _dedup_http_evidence(
    hits: list[tuple[str, int]]
) -> list[tuple[str, int]]:
    """When a specific http label (http.ListenAndServe /
    router.Run/ListenAndServe) is present, drop the generic
    ``<pkg>.ListenAndServe`` fallback. Specific labels read cleaner
    in the user-facing prompt."""
    labels = {h[0] for h in hits}
    specific_present = bool(
        labels & {"http.ListenAndServe", "router.Run/ListenAndServe"}
    )
    if not specific_present:
        return hits
    return [(l, n) for (l, n) in hits if l != "<pkg>.ListenAndServe"]


def _classify(content: str) -> tuple[str, list[str]]:
    """Return ``(looks_like, evidence)`` for a main.go's source.

    Priority (v0.7.8+): runs-loop > serves-http > runs-once > unknown.

    ``runs-loop`` trumps ``serves-http`` when BOTH patterns are
    present in the same file. The motivating real-world case is
    ``cmd/mcd-daemon/main.go`` on mcd-website — 15 publish-on-tick
    goroutines + a tail-end 4-line ``/health-check`` HTTP listener.
    v0.7.7 classified it as ``http-server`` (first-match-wins on the
    http-server list); v0.7.8 fixed by checking the loop list first.
    A long-running side-effect job is the file's primary purpose;
    the HTTP listener is auxiliary (admin / health-check).

    When BOTH patterns match, ``evidence`` includes an explicit
    "ALSO matches '<http-pattern>' — runs-loop precedence applied"
    line so the user sees the heuristic in action rather than a
    silent verdict.
    """
    loop_hits = _match_with_counts(content, _RUNS_LOOP_PATTERNS)
    http_hits = _dedup_http_evidence(
        _match_with_counts(content, _HTTP_SERVER_PATTERNS)
    )
    if loop_hits:
        evidence = _format_evidence(loop_hits)
        if http_hits:
            # Note the precedence so the user understands why this is
            # classified as a loop and not as an HTTP server.
            http_labels = ", ".join(
                f"'{lbl}'" for (lbl, _) in http_hits
            )
            evidence.append(
                f"ALSO matches {http_labels} — runs-loop precedence applied"
            )
        return "runs-loop", evidence
    if http_hits:
        return "serves-http", _format_evidence(http_hits)
    line_count = content.count("\n") + 1
    if line_count < _RUNS_ONCE_LINE_THRESHOLD:
        return "runs-once", [f"short ({line_count} lines), no runs-loop pattern"]
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
