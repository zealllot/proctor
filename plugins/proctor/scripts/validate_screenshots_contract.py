"""Per-item-type screenshot-count contract validator (v0.6.5+).

The v0.6.4 release shipped a richer `screenshots: [{path, label, focus}]`
field and an executor-agent prose contract for per-item-type minimum
screenshot counts (render-check 1, negative 1, happy-save 2, round-
trip 2, edit-and-switch 3). Production runs continued to ship single-
shot t-006 evidence — the prose discipline did not survive contact
with the executor's turn model. This script enforces the same
contract mechanically by reading the TestPlan + TestResults and
flagging per-item-type screenshot-count violations as schema errors
(exit non-zero), making the contract impossible to bypass without
either fixing the result or hand-editing it (which the reviewer sees).

Wired into ``proctor_run.py`` between the EXECUTED → REPORTED
transition, alongside ``validate_test_results``. A failure here
aborts the pipeline before the report is rendered, so the developer
sees the gap before the run is "complete" rather than discovering
useless screenshots in the published report.

## Item-type inference

Heuristics over (TestPlan item) → (one of the agent's item-type
buckets). Each item maps to exactly one bucket.

1. ``tool != "chrome-devtools"``  → ``not-chrome-devtools`` (no
   screenshot requirement; we don't enforce screenshots on lint /
   bash / curl items).
2. ``error_type`` is set (validation / permission / network /
   state-conflict / not-found / auth)  →  ``negative`` (≥1).
3. ``what:`` (case-insensitive) mentions ``edit``+``switch`` together,
   or ``change ... type``, or matches the literal v0.6.4-doc
   anti-pattern ``"edit reward, switch X from A to B"``  →
   ``edit-and-switch`` (≥3).
4. ``what:`` mentions ``round-trip`` / ``re-open`` / ``reload`` /
   ``persist`` / ``round trip`` (NOT happy-save) →
   ``round-trip`` (≥2).
5. ``what:`` starts with ``HAPPY`` (the planner convention), or
   mentions a saving verb (``create``+``save``, ``save succeeds``,
   ``persist``, ``submit``) → ``happy-save`` (≥2).
6. Default → ``render-check`` (≥1).

The classifier is intentionally over-inclusive on the higher
minimums — false positives (a render-check incorrectly classified as
happy-save and demanding 2 screenshots) are a smaller harm than
false negatives (the t-006 case: an edit-and-switch ships with 1
screenshot and the reviewer doesn't see the change). The executor
can always provide MORE screenshots than the minimum.

## Output

stdout: one violation per line, prefixed with the item id, in the
form: ``<id>: expected ≥<N> screenshots for <bucket>, got <M>``.
Empty stdout = clean.

Exit code: 0 if clean, 1 if any violation. The pipeline aborts on
exit 1; the run dir's pipeline-state.json keeps step=executed so the
fix is iterable (developer retries the executor stage rather than
restarting from scratch).

## Usage

::

    python3 validate_screenshots_contract.py \\
        --plan path/to/test-plan.json \\
        --results path/to/test-results.json

For pytest convenience, the module also exposes ``classify_item`` and
``check`` as importable callables. ``check(plan, results)`` returns
a list of violation strings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Per-bucket minimum screenshot counts, mirroring the table in
# plugins/proctor/agents/pr-test-executor.md (v0.6.4+ contract).
_MIN_COUNTS = {
    "render-check": 1,
    "negative": 1,
    "happy-save": 2,
    "round-trip": 2,
    "edit-and-switch": 3,
}

# Buckets that have a fixed screenshot count requirement enforced
# here. "not-chrome-devtools" is exempt.
ENFORCED_BUCKETS = set(_MIN_COUNTS.keys())

# Regex patterns for the `what:` field, ordered most-specific first
# (we return the FIRST matching bucket so over-inclusive patterns
# don't shadow the precise ones). All matched case-insensitively
# against the item's ``what`` (plus its ``how`` for the multi-stage
# edit-and-switch pattern that the agent docs describe as "change a
# field value on existing record, save").
_EDIT_AND_SWITCH_RE = re.compile(
    r"(?:\bedit\b.*\bswitch\b|"
    r"\bswitch\b.*\btype\b.*\bfrom\b.*\bto\b|"
    r"\bchange\b.*\btype\b.*\bfrom\b.*\bto\b)",
    re.IGNORECASE | re.DOTALL,
)
_ROUND_TRIP_RE = re.compile(
    r"(?:\bround[-\s]?trip\b|"
    r"\bre[-\s]?open\b.*\b(?:reload|hard[-\s]?reload|persist|verify)\b|"
    r"\bafter\b.*\b(?:reload|hard[-\s]?reload)\b|"
    r"\bhard[-\s]?reload\b)",
    re.IGNORECASE | re.DOTALL,
)
# Happy-save is intentionally broad — "create/save", "submit",
# "save succeeds" — because the executor's contract wants 2
# screenshots for ANY state-change-with-persist flow. The planner
# also writes ``HAPPY:`` as a literal prefix in plans we've seen.
_HAPPY_SAVE_RE = re.compile(
    r"(?:^\s*HAPPY\b|"
    r"\bsave\s+succeeds?\b|"
    r"\bcreate\b.*\b(?:save|persist|succeed)\b|"
    r"\bsubmit\b.*\b(?:succeed|accept|persist)\b)",
    re.IGNORECASE | re.DOTALL,
)


def classify_item(item: dict) -> str:
    """Return the item-type bucket for a single TestPlan item.

    Returns one of: ``not-chrome-devtools``, ``render-check``,
    ``negative``, ``happy-save``, ``round-trip``, ``edit-and-switch``.

    The bucket determines the minimum screenshot count enforced by
    ``check`` against the matching TestResults item. ``classify_item``
    is a pure function of the plan item — it doesn't peek at the
    results — so reviewers can read a plan and predict which items
    will be screenshot-enforced.
    """
    tool = item.get("tool")
    if tool != "chrome-devtools":
        return "not-chrome-devtools"

    if item.get("error_type"):
        return "negative"

    what = (item.get("what") or "").strip()
    how = (item.get("how") or "").strip()
    combined = f"{what}\n{how}"

    # Most-specific first: edit-and-switch is a strict superset of
    # round-trip (it ends with a re-open) so it must match first.
    if _EDIT_AND_SWITCH_RE.search(combined):
        return "edit-and-switch"
    if _HAPPY_SAVE_RE.search(combined) and _ROUND_TRIP_RE.search(combined):
        # An item that combines happy-save AND round-trip wording in
        # the SAME entry (rare but seen in compact plans) is treated
        # as a round-trip — both buckets demand 2, so the answer is
        # the same.
        return "round-trip"
    if _ROUND_TRIP_RE.search(combined):
        return "round-trip"
    if _HAPPY_SAVE_RE.search(combined):
        return "happy-save"
    return "render-check"


def _count_screenshots(result_item: dict) -> int:
    """Count valid screenshot entries for a result item.

    Prefers v0.6.4 ``screenshots: [{path, label, focus}]`` list when
    present and well-formed. Falls back to legacy
    ``screenshot_ref`` (counts as 1 if non-empty).
    """
    ss = result_item.get("screenshots")
    if isinstance(ss, list):
        # Only count list entries that are dicts with the three
        # required non-empty keys (matches schema.py's enforcement).
        valid = 0
        for s in ss:
            if not isinstance(s, dict):
                continue
            if not all(
                isinstance(s.get(k), str) and s.get(k, "").strip()
                for k in ("path", "label", "focus")
            ):
                continue
            valid += 1
        return valid
    legacy = result_item.get("screenshot_ref")
    if isinstance(legacy, str) and legacy.strip():
        return 1
    return 0


def check(plan: dict, results: dict) -> list[str]:
    """Return a list of violation strings. Empty = contract satisfied.

    A violation is reported when:
      - The plan item is chrome-devtools, AND
      - The plan item's bucket is in ``ENFORCED_BUCKETS``, AND
      - The matching results item's status is ``pass`` or ``fail``
        (skipped items are exempt — there's no evidence to capture
        when the test didn't run), AND
      - The matching results item's screenshot count is below the
        bucket's minimum.

    Items present in the plan but absent from results are NOT flagged
    here (that's a separate execution-completeness check). Items
    present in results without a matching plan entry are skipped
    (planner-vs-results drift is also outside this script's scope).
    """
    violations: list[str] = []
    by_id_results = {it["id"]: it for it in results.get("items", [])
                     if isinstance(it, dict) and "id" in it}
    for item in plan.get("items", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        bucket = classify_item(item)
        if bucket not in ENFORCED_BUCKETS:
            continue
        result = by_id_results.get(item["id"])
        if result is None:
            # Missing result — not this script's problem.
            continue
        if result.get("status") not in ("pass", "fail"):
            # Skipped items can't produce screenshots; the empirical-
            # grounding check (validate_item_result.py) is the right
            # tool for those.
            continue
        n = _count_screenshots(result)
        need = _MIN_COUNTS[bucket]
        if n < need:
            violations.append(
                f"{item['id']}: expected >={need} screenshots for "
                f"{bucket} item, got {n}. The v0.6.4+ executor "
                f"contract (agents/pr-test-executor.md) requires "
                f"this minimum so the screenshot(s) carry the "
                f"evidence the test asserts on. See the per-item-"
                f"type matrix in the agent doc."
            )
    return violations


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", required=True,
                   help="Path to test-plan.json")
    p.add_argument("--results", required=True,
                   help="Path to test-results.json")
    args = p.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    results = json.loads(Path(args.results).read_text())
    violations = check(plan, results)
    for v in violations:
        sys.stdout.write(v + "\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(_main())
