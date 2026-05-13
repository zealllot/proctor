"""Heuristic plan-quality lints that the orchestrator surfaces at the
approval gate.

After 28 patch releases of "you must include happy paths" and "you
must verify persistence after writes", the planner still cut corners
in v0.3.29 — most recently emitting items like::

    t-008  Create reward type=Image: missing asset rejected; with
           asset, save succeeds

which combines a negative and a happy-path assertion into one item
(report can only show one status — the happy half gets silently
lost), AND has no sibling item that re-opens the saved record to
verify the data round-tripped through the persistence layer. The
schema and skills can't catch this at validation time because the
patterns are stylistic, not structural.

This module ships the safety net: a regex-based linter the
orchestrator runs on the plan BEFORE the approval gate. Warnings are
ADVISORY — they don't block the run, but they show up next to the
plan table so the human reviewer can choose to re-prompt the planner
before approving.

Two checks today:

1. **Combined happy/negative item.** ``what:`` contains both
   success-phrasing and rejection-phrasing.
2. **Write without round-trip sibling.** ``what:`` is a write action
   (save/create/update/submit/edit/publish), is NOT a negative item
   (``error_type`` unset), and no other item references this one via
   ``data_from`` with a ``what:`` describing re-opening / round-trip /
   reload.

Conservative by design — false positives make the approval gate noisy
and reviewers learn to ignore warnings. The two patterns are tuned to
fire on the exact phrasings the planner has produced in practice.
"""

from __future__ import annotations

import re

# Phrases that signal a success outcome. Tense-agnostic (saves /
# saved / saving all match) because the planner uses all of them.
_HAPPY_PHRASES = [
    r"\bsucceed(?:s|ed)?\b",
    r"\bsave(?:s|d)?\b",
    r"\bcreate(?:s|d)?\b",
    r"\bupdate(?:s|d)?\b",
    r"\bworks?\b",
    r"\bpasses?\b",
    r"\baccepted\b",
    r"\bpersist(?:s|ed)?\b",
]
_RE_HAPPY = re.compile("|".join(_HAPPY_PHRASES), re.IGNORECASE)

# Phrases that signal a rejection / failure outcome. Word boundaries
# matter — `\bvalid\b` would match the BENIGN "valid URL"; we use
# `\binvalid\b` only.
_NEGATIVE_PHRASES = [
    r"\brejected?\b",
    r"\brejects?\b",
    r"\binvalid\b",
    r"\berror\b",
    r"\bfail(?:s|ed)?\b",
    r"\bdeny\b", r"\bdenied?\b",
    r"\bforbidden\b",
    r"\bmissing\b",
    r"\bunauthor(?:ized|ised)\b",
]
_RE_NEG = re.compile("|".join(_NEGATIVE_PHRASES), re.IGNORECASE)

# Write-action verbs in `what:`. Distinct from happy phrases — a
# "passes test suite" item is happy but not a write.
_WRITE_PHRASES = [
    r"\bsave\b", r"\bsaving\b", r"\bsaves\b",
    r"\bcreate\b", r"\bcreating\b", r"\bcreates\b",
    r"\bupdate\b", r"\bupdating\b", r"\bupdates\b",
    r"\bsubmit\b", r"\bsubmitting\b", r"\bsubmits\b",
    r"\bedit\b", r"\bediting\b", r"\bedits\b",
    r"\bpublish\b", r"\bpublishing\b", r"\bpublishes\b",
    r"\bupload\b", r"\buploading\b", r"\buploads\b",
]
_RE_WRITE = re.compile("|".join(_WRITE_PHRASES), re.IGNORECASE)

# Round-trip / reload / re-open phrasing that a sibling item is
# expected to use after a write item.
_RELOAD_PHRASES = [
    r"\bre-?open(?:s|ed|ing)?\b",
    r"\bround[- ]?trip(?:s|ed|ping)?\b",
    r"\breload(?:s|ed|ing)?\b",
    r"\bre-?render(?:s|ed|ing)?\b",
    r"\bload(?:s|ed|ing)?\s+back\b",
    r"\bload(?:s|ed|ing)?\s+correctly\b",
    r"\bnavigate(?:s|d)?\s+back\b",
    r"\bdetail\s+page\b",
    r"\b(?:all\s+)?fields\s+round-?trip\b",
    r"\bappear(?:s|ed|ing)?\b",
    r"\bvisible\s+in\s+(?:the\s+)?[\w/\-]*\s*list\b",
    r"\bvisible\s+in\s+list\b",
]
_RE_RELOAD = re.compile("|".join(_RELOAD_PHRASES), re.IGNORECASE)


def check(plan: dict) -> list[str]:
    """Return a list of warning strings. Empty list = clean plan.

    Warnings are formatted ``<item_id>: <message>`` so the orchestrator
    can print them as a bullet list. Order: combined-happy-negative
    warnings first, then missing-round-trip warnings, both sorted by
    item id for stable output."""
    items = plan.get("items") or []
    combined_warnings: list[str] = []
    missing_roundtrip_warnings: list[str] = []

    for it in items:
        what = it.get("what") or ""
        if not isinstance(what, str):
            continue

        # 1. Combined happy + negative phrasing.
        #
        # Skip items that explicitly declare `error_type` — those are
        # intentional negative items and combined phrasings like
        # "form submit rejected when field missing" are expected.
        # The check fires only for items that LOOK like they're trying
        # to do both happy and negative in one shot (no error_type set
        # but `what:` contains both vocabularies).
        if it.get("error_type"):
            continue
        if _RE_HAPPY.search(what) and _RE_NEG.search(what):
            combined_warnings.append(
                f"{it['id']}: what: combines happy and negative phrasing "
                f"({what!r}). Split into two items — the report can only "
                f"show one status per item, so the happy half gets lost "
                f"when bundled."
            )

    # 2. Write items without a sibling reload-and-verify.
    # Build the sibling index once: for each item, who references it
    # via data_from?
    referenced_by: dict[str, list[dict]] = {}
    for it in items:
        for src in (it.get("data_from") or []):
            referenced_by.setdefault(src, []).append(it)

    for it in items:
        what = it.get("what") or ""
        # Skip negative items — they don't persist anything to reload.
        if it.get("error_type"):
            continue
        # Skip lint-only / bash / curl items — round-trip is a UI
        # concept relevant to chrome-devtools writes only.
        if it.get("tool") not in (None, "chrome-devtools"):
            continue
        if not _RE_WRITE.search(what):
            continue

        siblings = referenced_by.get(it["id"], [])
        has_reload_sibling = any(
            _RE_RELOAD.search(s.get("what") or "")
            for s in siblings
        )
        if has_reload_sibling:
            continue

        missing_roundtrip_warnings.append(
            f"{it['id']}: write action ({what!r}) has no sibling item "
            f"asserting round-trip data loading via data_from. Add a "
            f"follow-up item that re-opens the saved record (or "
            f"navigates back to the list) and asserts all submitted "
            f"fields are visible."
        )

    combined_warnings.sort()
    missing_roundtrip_warnings.sort()
    return combined_warnings + missing_roundtrip_warnings


def _main() -> int:
    """CLI form: read plan JSON from stdin, emit one warning per line.

    Default exit code is 0 in both clean and warn cases (advisory mode
    — the orchestrator decides what to do with the output).

    With ``--strict`` (v0.3.32+), exit code is 1 when ANY warnings
    fired. This lets the orchestrator's hard-gate retry loop branch
    cleanly on exit code without parsing stdout:

        if ! python3 plan_smells.py --strict < plan.json; then
            # regenerate plan with warnings as feedback
        fi
    """
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser()
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any warnings fired (default is exit 0 always).",
    )
    args = p.parse_args()

    plan = json.load(sys.stdin)
    warnings = check(plan)
    for w in warnings:
        sys.stdout.write(w + "\n")
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
