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
from itertools import combinations
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

    # Round-trip check comes first when the item is unambiguously a
    # re-open / after-reload verification. Without this, a plan whose
    # `what` reads "HAPPY: re-open the just-edited reward — switched
    # DigitalContentType ..." would match the edit-and-switch regex
    # (because "edited" + "switched" both appear) and demand 3
    # screenshots, even though no save action happens in this item.
    # The signal is past-tense verbs like "just-edited" / "switched"
    # combined with a re-open verb, OR explicit hard-reload wording.
    if _ROUND_TRIP_RE.search(combined):
        return "round-trip"

    # Most-specific first among save-action buckets: edit-and-switch
    # is a strict superset of happy-save (it changes a field and
    # persists) so it must match before happy-save.
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


# v0.6.8: identical-screenshot lint. Originally only checked negative
# items (v0.6.6 t-007/008/009 bug: three byte-identical pre-submit
# blank forms claiming three different error states).
#
# v0.7.5 extension: applies to ALL chrome-devtools items, not just
# negatives, AND uses MD5 (not just byte size) for the comparison.
# The v0.7.4 PR-1126 run shipped 11 screenshot files across 5
# different chrome items (t-005..t-009) where 7 of them shared the
# same MD5 (a viewport-top capture taken before scrollIntoView
# completed for any of the asserted fields). Byte size alone wouldn't
# catch ALL cases — different page chrome can produce same byte size
# coincidentally — but MD5 is decisive.
#
# The floor exists so tiny legitimately-shared stubs (an empty PNG
# sentinel) don't trip the check.
_IDENTICAL_MIN_BYTES = 50 * 1024


def _primary_screenshot_path(result_item: dict) -> str | None:
    """Return the primary screenshot's path-as-string for byte-size
    comparison. Prefers v0.6.4 ``screenshots[0].path``; falls back
    to legacy ``screenshot_ref``. Returns None when neither is set.

    The "primary" is the first list entry intentionally — the v0.6.4
    contract puts the asserted artifact at index 0 for single-shot
    buckets (negative, render-check); for multi-shot buckets the
    pre-state lives at 0, which is the legitimate place a "blank
    form" can show up. We only flag identical *negative* primaries
    because for negatives, the asserted artifact IS the rendered
    error.
    """
    ss = result_item.get("screenshots")
    if isinstance(ss, list):
        for s in ss:
            if not isinstance(s, dict):
                continue
            p = s.get("path")
            if isinstance(p, str) and p.strip():
                return p
            # Skip malformed entries — keep looking for a valid path.
        # All entries malformed or empty.
    legacy = result_item.get("screenshot_ref")
    if isinstance(legacy, str) and legacy.strip():
        return legacy
    return None


def _resolve_screenshot_size(
    run_dir: Path | None, ref: str
) -> int | None:
    """Resolve a screenshot reference to a byte size on disk.

    Mirrors ``render_item_artifacts._normalize`` resolution: absolute
    path → repo-root-relative → run_dir/screenshots/<basename>. Returns
    None if no resolution exists or the resolved path is missing.
    """
    if not ref:
        return None
    ref_path = Path(ref)
    candidates: list[Path] = []
    if ref_path.is_absolute():
        candidates.append(ref_path)
    else:
        if run_dir is not None:
            candidates.append(run_dir / "screenshots" / ref_path.name)
            candidates.append(run_dir / ref_path.name)
        candidates.append(Path.cwd() / ref_path)
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c.stat().st_size
        except OSError:
            continue
    return None


def _md5_of(path: Path) -> str | None:
    """Compute MD5 of a file's bytes. Returns None on read error."""
    import hashlib
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, FileNotFoundError):
        return None


def _resolve_screenshot_path(
    run_dir: Path | None, ref: str
) -> Path | None:
    """Resolve a screenshot ref to an absolute Path that exists.

    Mirrors ``_resolve_screenshot_size``'s candidate list but returns
    the Path so the caller can compute MD5 / read content / etc.
    """
    if not ref or run_dir is None:
        return None
    ref_path = Path(ref)
    candidates: list[Path] = []
    if ref_path.is_absolute():
        candidates.append(ref_path)
    else:
        candidates.append(run_dir / "screenshots" / ref_path.name)
        candidates.append(run_dir / ref_path.name)
        candidates.append(Path.cwd() / ref_path)
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c
        except OSError:
            continue
    return None


def _collect_md5_index(
    plan: dict,
    results: dict,
    run_dir: Path,
) -> dict[str, list[tuple[str, int, Path]]]:
    """Walk every chrome-devtools item's screenshots and return a map
    md5 → list of (item_id, screenshot_idx, path) tuples. Files below
    the byte-size floor are skipped (legitimate tiny stubs shouldn't
    trip the check). Used by both within-item and cross-item lints.
    """
    by_id_results = {it["id"]: it for it in results.get("items", [])
                     if isinstance(it, dict) and "id" in it}
    chrome_item_ids = [
        item["id"] for item in plan.get("items", [])
        if isinstance(item, dict) and "id" in item
        and item.get("tool") == "chrome-devtools"
    ]
    by_md5: dict[str, list[tuple[str, int, Path]]] = {}
    for item_id in chrome_item_ids:
        result = by_id_results.get(item_id)
        if result is None or result.get("status") not in ("pass", "fail"):
            continue
        refs: list[tuple[int, str]] = []
        ss = result.get("screenshots")
        if isinstance(ss, list):
            for idx, s in enumerate(ss):
                if isinstance(s, dict):
                    p = s.get("path")
                    if isinstance(p, str) and p.strip():
                        refs.append((idx, p))
        legacy = result.get("screenshot_ref")
        if not refs and isinstance(legacy, str) and legacy.strip():
            refs.append((0, legacy))
        for idx, ref in refs:
            path = _resolve_screenshot_path(run_dir, ref)
            if path is None:
                continue
            try:
                if path.stat().st_size < _IDENTICAL_MIN_BYTES:
                    continue
            except OSError:
                continue
            md5 = _md5_of(path)
            if md5 is None:
                continue
            by_md5.setdefault(md5, []).append((item_id, idx, path))
    return by_md5


def _check_within_item_identical_md5(
    plan: dict,
    results: dict,
    run_dir: Path | None,
) -> list[str]:
    """v0.7.6 HARD violation: a single item's `screenshots[]` contains
    2+ entries with the same MD5. The before/after pair claimed by
    label/focus is actually before/before — the test captured the
    same image twice and labeled them differently.

    This is a hard violation because for a single item, multiple
    identical screenshots can never represent multiple distinct
    asserted states. Cross-item duplication (separate function below)
    can be legitimate (a render-check + a post-empty-save can both
    show the same blank form).
    """
    if run_dir is None:
        return []
    by_id_results = {it["id"]: it for it in results.get("items", [])
                     if isinstance(it, dict) and "id" in it}
    violations: list[str] = []
    for item in plan.get("items", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        if item.get("tool") != "chrome-devtools":
            continue
        result = by_id_results.get(item["id"])
        if result is None or result.get("status") not in ("pass", "fail"):
            continue
        ss = result.get("screenshots")
        if not isinstance(ss, list) or len(ss) < 2:
            continue
        # Resolve each entry to (idx, md5).
        md5_by_idx: list[tuple[int, str]] = []
        for idx, s in enumerate(ss):
            if not isinstance(s, dict):
                continue
            p = s.get("path")
            if not (isinstance(p, str) and p.strip()):
                continue
            path = _resolve_screenshot_path(run_dir, p)
            if path is None:
                continue
            try:
                if path.stat().st_size < _IDENTICAL_MIN_BYTES:
                    continue
            except OSError:
                continue
            md5 = _md5_of(path)
            if md5 is None:
                continue
            md5_by_idx.append((idx, md5))
        # Cluster by md5; any cluster size >= 2 within this item is a
        # hard violation.
        clusters: dict[str, list[int]] = {}
        for idx, md5 in md5_by_idx:
            clusters.setdefault(md5, []).append(idx)
        for md5, idxs in clusters.items():
            if len(idxs) < 2:
                continue
            joined = ", ".join(f"[{i}]" for i in idxs)
            violations.append(
                f"{item['id']}: screenshots {joined} within the same "
                f"item share MD5 {md5}. The before/after pair claimed "
                f"by your labels is actually before/before — the "
                f"executor took the same screenshot twice. Re-shoot "
                f"the AFTER state at the moment the asserted change "
                f"is on-screen, using element-scoped take_screenshot "
                f"(uid parameter from take_snapshot)."
            )
    return violations


# Cross-item cluster size that escalates from "advisory" to a WARN
# violation. v0.7.4 PR-#1126 had a 7-share cluster — the audit pattern
# this exists to surface. Clusters of 2 or 3 are common when distinct
# items legitimately assert on the same visual state (render-check +
# after-empty-save + after-reload-empty all look like "form with empty
# inputs"), so the noise floor is set at 4.
_CROSS_ITEM_WARN_THRESHOLD = 4


def _check_cross_item_md5_cluster(
    plan: dict,
    results: dict,
    run_dir: Path | None,
) -> list[str]:
    """v0.7.6 WARN-level violation (advisory, NOT pipeline-aborting):
    when a cluster of ``_CROSS_ITEM_WARN_THRESHOLD`` or more
    screenshots across DIFFERENT chrome items share the same MD5,
    surface it. Clusters of size 2-3 across different items are
    common in legitimate runs (multiple items naturally agree on
    "form with empty inputs") so they don't fire.

    Each violation is prefixed with `WARN ` so the orchestrator /
    proctor_run.py can distinguish from hard violations and decide
    whether to block the pipeline. The format keeps the cluster
    listing identical to v0.7.5 so reviewers see the same evidence.
    """
    if run_dir is None:
        return []
    by_md5 = _collect_md5_index(plan, results, run_dir)
    violations: list[str] = []
    for md5, hits in by_md5.items():
        # Restrict to cross-item duplication — within-item is handled
        # by the within-item check above (HARD).
        unique_items = {h[0] for h in hits}
        if len(unique_items) < 2:
            continue
        if len(hits) < _CROSS_ITEM_WARN_THRESHOLD:
            continue
        members = ", ".join(f"{h[0]}[{h[1]}]" for h in hits)
        first_path = hits[0][2]
        try:
            size = first_path.stat().st_size
        except OSError:
            size = -1
        violations.append(
            f"WARN {members}: {len(hits)} screenshots across "
            f"{len(unique_items)} chrome items share MD5 {md5} "
            f"({size} bytes). This matches the v0.7.4 PR-#1126 "
            f"viewport-top-collision pattern; the executor likely "
            f"took the same screenshot for multiple asserted states "
            f"instead of capturing each one individually. Inspect "
            f"the screenshots; re-shoot any that don't show distinct "
            f"asserted content using element-scoped take_screenshot "
            f"(uid parameter from take_snapshot)."
        )
    return violations


def _check_identical_screenshots(
    plan: dict,
    results: dict,
    run_dir: Path | None,
) -> list[str]:
    """v0.7.6 facade — combines within-item HARD violations and
    cross-item WARN violations. Kept as a single entry point so
    proctor_run.py's existing call site doesn't need to change; the
    WARN-prefixed entries are still pipeline-aborting today but the
    prefix means a future proctor_run.py upgrade can distinguish."""
    return (
        _check_within_item_identical_md5(plan, results, run_dir)
        + _check_cross_item_md5_cluster(plan, results, run_dir)
    )


def _check_legacy_screenshot_ref(plan: dict, results: dict) -> list[str]:
    """v0.7.5+: chrome-devtools items must NOT use the legacy
    ``screenshot_ref`` singular field. The v0.6.4+ ``screenshots: [{
    path, label, focus}]`` array is mandatory.

    The PR-1126 v0.7.4 run shipped every chrome item with just
    ``screenshot_ref`` and a bare path — no ``label``, no ``focus``,
    no way for the reviewer to tell what the screenshot was supposed
    to show. The count-based contract caught insufficient count for
    save/round-trip but render-check items satisfied legacy count=1
    and slipped through.

    Returns violation strings for any chrome item where
    ``screenshots`` is missing AND ``screenshot_ref`` is set.
    """
    by_id_results = {it["id"]: it for it in results.get("items", [])
                     if isinstance(it, dict) and "id" in it}
    violations: list[str] = []
    for item in plan.get("items", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        if item.get("tool") != "chrome-devtools":
            continue
        result = by_id_results.get(item["id"])
        if result is None or result.get("status") not in ("pass", "fail"):
            continue
        # New shape present → pass.
        if isinstance(result.get("screenshots"), list) and result["screenshots"]:
            continue
        # Legacy shape present without new shape → reject.
        legacy = result.get("screenshot_ref")
        if isinstance(legacy, str) and legacy.strip():
            violations.append(
                f"{item['id']}: chrome-devtools result uses legacy "
                f"`screenshot_ref` singular field instead of the "
                f"v0.6.4+ `screenshots: [{{path, label, focus}}]` "
                f"array. Reviewers can't tell what the screenshot is "
                f"supposed to show without `label` + `focus`. "
                f"Re-emit with the new shape — see "
                f"agents/pr-test-executor.md 'Result-field shape'."
            )
    return violations


def check(
    plan: dict,
    results: dict,
    run_dir: Path | str | None = None,
) -> list[str]:
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

    When ``run_dir`` is provided (v0.6.8+), additionally scan negative
    items for byte-identical primary screenshots — the t-007/008/009
    signature of "fetch() submit screenshotted the pre-submit form
    instead of the rendered error".
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
    # v0.7.5: identical-screenshot lint across ALL chrome items.
    # (v0.6.8 was negative-only.) Run after the count check so a
    # count-deficient item is reported once for the primary failure
    # (no screenshot) without also being flagged by this comparison
    # (it has no resolvable file anyway).
    rd: Path | None
    if isinstance(run_dir, str):
        rd = Path(run_dir)
    else:
        rd = run_dir
    violations.extend(
        _check_identical_screenshots(plan, results, rd)
    )
    # v0.7.5: legacy `screenshot_ref` singular field is forbidden for
    # chrome items. Force the v0.6.4+ `screenshots: [...]` array.
    violations.extend(_check_legacy_screenshot_ref(plan, results))
    return violations


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", required=True,
                   help="Path to test-plan.json")
    p.add_argument("--results", required=True,
                   help="Path to test-results.json")
    p.add_argument("--run-dir", default=None,
                   help=("Path to the run directory (enables the "
                         "v0.6.8 identical-negative-screenshot "
                         "byte-size lint). Optional — when omitted, "
                         "only the count-based contract is enforced."))
    args = p.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    results = json.loads(Path(args.results).read_text())
    run_dir = Path(args.run_dir) if args.run_dir else None
    violations = check(plan, results, run_dir=run_dir)
    for v in violations:
        sys.stdout.write(v + "\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(_main())
