"""Empirical-grounding validator for executor results.

Real run on v0.6.1 showed the main AI (executing inline rather than
via per-item subagent dispatch) classifying 3 items as
`status=skipped, reason=precondition-not-met` with evidence that
was pure code-inspection reasoning — no actual attempt, no captured
stderr, no observed failure. The user's env had been fixed but the
AI carried stale session memory about a prior chacha20poly1305
error and preemptively skipped without empirical verification.

This script enforces a discipline that prose alone hasn't:

  status=skipped + reason=precondition-not-met (or reason=environment)
  ⇒ evidence MUST contain empirical-attempt evidence:
     - a `command:` field showing what was actually run, OR
     - a captured-output marker in evidence (`exit=`, `HTTP `,
       `stderr:`, `error response:`, etc.)
     - OR an explicit "(no attempt performed because... <reason>)"
       disclaimer that surfaces the gap to the human reviewer.

Run after each executor subagent returns. Emits warnings; doesn't
mutate the result. The executing-pr-tests skill's Step 4 calls this
script and includes the warnings in the run's evidence chain so the
reporter renders them visibly.

Usage:
    python3 validate_item_result.py < result.json
    # or
    python3 validate_item_result.py --result-file path/to/result.json

Exit 0 always (warnings, not errors). stdout = warnings (one per line);
empty = clean.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Markers that indicate the executor actually ATTEMPTED the action
# and captured a real response. Any one of these in evidence text
# (case-insensitive) is enough to satisfy the empirical-grounding
# rule.
_OBSERVED_MARKERS = [
    r"\bexit(?:\s+code)?\s*[:=]\s*\d",  # exit 1 / exit code: 137
    r"\bHTTP\s+\d{3}\b",                # HTTP 500
    r"\bstderr\s*:",                    # stderr: ...
    r"\bstdout\s*:",
    r"\bresponse body\b",
    r"\bgot\s+(?:error|response)",
    r"\berror\s+response\b",
    r"\bnavigated to\b",
    r"\bsnapshot\b.*\bshows\b",
    r"\bDOM\s+(?:snapshot|state)\b",
    r"\bserver returned\b",
    r"\bcurl\s+returned\b",
    # v0.6.3: removed `\battempt(?:ed|s)?\b.*\b(?:fail|error|...)` —
    # too loose. Code-inspection prose routinely says "X attempts to
    # call Y and fails BEFORE backend handling" (descriptive future-
    # tense, NOT a captured attempt). Subagent acceptance test on the
    # user's actual v0.6.1 t-005 evidence proved this false-negatived
    # the exact bug we shipped to catch. The other markers above
    # cover legitimate captures (exit/HTTP/stderr/server returned/
    # curl returned/DOM snapshot); we don't need a "attempted..."
    # alias.
    # Explicit "no attempt because..." disclaimer that surfaces the gap
    r"\bno attempt performed\b",
    r"\bnot attempted\b",
    r"\bdid not attempt\b",
]

_OBSERVED_RE = re.compile("|".join(_OBSERVED_MARKERS), re.IGNORECASE)

# Reasons that require empirical grounding to claim. Code-inspection-
# only "the environment can't possibly support this" without an actual
# attempt is the bug this script catches.
_EMPIRICAL_REQUIRED_REASONS = {
    "precondition-not-met",
    "environment",
    "data-template-missing",  # also requires observed missing key
}

# Reasons that are inherently propagated from upstream items —
# empirical grounding is on the UPSTREAM, not this item.
_PROPAGATED_REASONS_PREFIX = ("data-dep-failed", )


def check(item: dict) -> list[str]:
    """Return a list of warning strings for this item result. Empty
    list = clean."""
    warnings: list[str] = []
    status = item.get("status")
    reason = (item.get("reason") or "")
    evidence = (item.get("evidence") or "")
    command = (item.get("command") or "")

    if status != "skipped":
        return warnings

    # Propagated skips don't need their own empirical grounding —
    # the upstream item carries it.
    if any(reason.startswith(p) for p in _PROPAGATED_REASONS_PREFIX):
        return warnings

    if reason not in _EMPIRICAL_REQUIRED_REASONS:
        return warnings

    # The actual check: evidence must show empirical grounding OR
    # there must be a non-empty command field (proves something was
    # invoked).
    if command.strip():
        return warnings
    if _OBSERVED_RE.search(evidence):
        return warnings

    warnings.append(
        f"{item.get('id', '<unknown>')}: status=skipped "
        f"reason={reason!r} but evidence appears to be code-inspection "
        f"reasoning (no captured exit/HTTP/stderr/snapshot markers, "
        f"no `command:` field). The executor must ATTEMPT the action "
        f"and observe a real failure before classifying as "
        f"precondition-not-met. Code-inspection-only skip is "
        f"forbidden per the pr-test-executor agent contract. "
        f"Re-run this item; if it actually fails, the captured "
        f"output becomes the empirical grounding."
    )
    return warnings


def check_results(results: dict) -> list[str]:
    """Check every item in a TestResults JSON. Returns a flat list."""
    warnings: list[str] = []
    for item in results.get("items", []):
        warnings.extend(check(item))
    return warnings


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--result-file", default=None,
        help="Path to a single-item result JSON (the subagent's "
             "direct output). If omitted, reads from stdin.",
    )
    p.add_argument(
        "--results-file", default=None,
        help="Path to a full TestResults JSON. If set, checks every "
             "item.",
    )
    args = p.parse_args()

    warnings: list[str] = []
    if args.results_file:
        results = json.loads(Path(args.results_file).read_text())
        warnings = check_results(results)
    elif args.result_file:
        item = json.loads(Path(args.result_file).read_text())
        warnings = check(item)
    else:
        text = sys.stdin.read().strip()
        if not text:
            return 0
        data = json.loads(text)
        if isinstance(data, dict) and "items" in data:
            warnings = check_results(data)
        elif isinstance(data, dict):
            warnings = check(data)

    for w in warnings:
        sys.stdout.write(w + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
