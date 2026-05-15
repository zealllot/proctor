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

## v0.7.7+ — supplementary-binary output coverage

The v0.7.6 e2e against mcd-website PR #1126 found that when a
project ships multiple ``cmd/*/main.go`` binaries (HTTP server +
long-running supplementary binary that re-publishes on a tick),
PRoctor's local setup only ran ``go run .`` which started the
HTTP server but NOT the supplementary binary. PRs describing
"published JSON includes trimmed tokens" never got verified at
runtime because the binary that does the publishing wasn't
running. The planner shipped a lint-only item ("source-level:
SplitTags calls Trim") and the runtime gap was invisible.

The v0.7.7+ fix has two halves:

1. The /proctor:proctor-init wizard now detects ``cmd/*/main.go``
   binaries and prompts the user to include their long-running
   supplementary binaries in ``.proctor/local.yml setup:`` — see
   ``scripts/wizard_detect_binaries.py``.
2. This module's
   ``missing-runtime-verify-when-supplementary-binary-present``
   rule (v0.7.9; renamed from ``...-when-daemon-present``) fires
   when ``setup_context.supplementary_binaries_running`` is
   non-empty AND the diff touches a file reachable from any of
   those binaries AND PR body mentions output keywords
   (publish/JSON/endpoint/output/serialize) AND the plan has no
   ``tool: bash`` / ``curl`` item that curls against the binary's
   output URL.

v0.7.9 renamed the ``setup_context`` keys for neutrality
(``daemons_running`` → ``supplementary_binaries_running``,
``daemon_touched`` → ``supplementary_binary_touched``). The
v0.7.7/v0.7.8 keys remain accepted as aliases so plans persisted
under the old names keep working.

Combined, the wizard half makes runtime verification POSSIBLE
(the supplementary binary is in setup, so it publishes), and the
lint half makes the planner ACCOUNTABLE for using that capability
when the PR shape calls for it.
"""

from __future__ import annotations

import re
from typing import Iterable

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
#
# v0.3.36 expansion: a faithful careful planner reached for "persist"
# / "submitted" / "uploaded" past-tense variants and tripped the
# coverage warning (which is built on this same list). Added the
# common synonyms so the lint matches what real careful prose looks
# like instead of forcing a vocabulary the planner has to memorize.
_WRITE_PHRASES = [
    r"\bsave\b", r"\bsaving\b", r"\bsaves\b", r"\bsaved\b",
    r"\bcreate\b", r"\bcreating\b", r"\bcreates\b", r"\bcreated\b",
    r"\bupdate\b", r"\bupdating\b", r"\bupdates\b", r"\bupdated\b",
    r"\bsubmit\b", r"\bsubmitting\b", r"\bsubmits\b", r"\bsubmitted\b",
    r"\bedit\b", r"\bediting\b", r"\bedits\b", r"\bedited\b",
    r"\bpublish\b", r"\bpublishing\b", r"\bpublishes\b", r"\bpublished\b",
    r"\bupload\b", r"\buploading\b", r"\buploads\b", r"\buploaded\b",
    r"\bpersist\b", r"\bpersisting\b", r"\bpersists\b", r"\bpersisted\b",
    r"\binsert\b", r"\binserting\b", r"\binserts\b", r"\binserted\b",
    r"\bstore\b", r"\bstoring\b", r"\bstores\b", r"\bstored\b",
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


# Stopwords filtered out of token-overlap comparisons (Fix C1). Kept
# small so project-specific noun phrases like "DigitalContent",
# "DCT", "tags" survive — those carry signal that "the" / "is" do not.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at",
    "by", "with", "from", "is", "are", "was", "were", "be", "been",
    "being", "this", "that", "these", "those", "it", "its", "as",
    "but", "if", "then", "than", "so", "do", "does", "did", "have",
    "has", "had", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "not", "no", "yes", "any", "all", "some",
    "i", "we", "you", "they", "he", "she", "them", "us", "me", "my",
    "your", "our", "their", "his", "her",
    # Common test-prose verbs that aren't content tokens
    "verify", "check", "assert", "ensure", "make", "test", "tests",
    "tested", "testing", "case", "cases",
}

# Token regex used by Fix C1 (token overlap) and Fix C2 (new symbol
# detection). Permits alphanumerics + underscore — matches identifiers
# and CamelCase phrases when used to extract words from prose text we
# lowercase first.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

# Criterion-style lines inside a linked-content excerpt or PR body.
# Conservative on purpose — only lines whose intent is clearly an
# acceptance criterion get extracted. Matches:
#   - markdown task list bullets: "- [ ] X" / "* [ ] X"
#   - sentences starting with "must" / "should" / "verify that" /
#     "the system shall" / "X is required"
_CRITERION_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[-*]\s*\[[ x]\]\s+(?P<bullet>.+)"
    r"|(?P<must>(?:must|should|verify that|the system shall)\b.+)"
    r")",
    re.IGNORECASE,
)

# Symbol-extraction regexes for Fix C2 (new-symbol-not-exercised).
# Each pattern walks added lines (`+...`) of a unified diff and pulls
# the new top-level symbol name. Conservative — over-extraction would
# fire false coverage warnings; under-extraction silently lets bugs
# through. The recall target: any user-callable surface added by the
# diff (a function/class/method/constant the rest of the codebase can
# reference by name). Local variables / unexported helpers are out of
# scope.
_SYMBOL_PATTERNS = [
    # Go
    re.compile(r"^\+\s*func\s+(?:\([^)]*\)\s+)?([A-Z][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\+\s*type\s+([A-Z][A-Za-z0-9_]*)\s+"),
    # Python — top-level (no leading indent) only, exclude `_name`
    re.compile(r"^\+(?!\s)def\s+([A-Za-z][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\+(?!\s)class\s+([A-Za-z][A-Za-z0-9_]*)\s*[(:]"),
    # JS/TS exports
    re.compile(r"^\+\s*export\s+(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\+\s*export\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<{]"),
    re.compile(r"^\+\s*export\s+const\s+([A-Za-z_][A-Za-z0-9_]*)\s*="),
    re.compile(r"^\+\s*export\s+default\s+(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    # Ruby
    re.compile(r"^\+\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\b"),
]


def _tokenize(text: str) -> set[str]:
    """Extract content tokens from prose. Lowercases, filters stopwords,
    drops tokens with fewer than 3 chars (one-letter / "ok" / "id"
    noise). Returns a set so overlap math is symmetric."""
    if not text:
        return set()
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= 3 and t.lower() not in _STOPWORDS
    }


def _extract_criteria_from_text(text: str) -> list[str]:
    """Extract bullet/must-style criteria lines from prose. Each
    returned string is the bullet's content (without the leading
    `- [ ]` or `must ` marker). Used for both PR body
    requirement_hints and linked-content excerpts."""
    if not text:
        return []
    out: list[str] = []
    for line in text.splitlines():
        m = _CRITERION_LINE_RE.match(line)
        if not m:
            continue
        body = m.group("bullet") or m.group("must")
        if body and body.strip():
            out.append(body.strip())
    return out


def _gather_criteria(change_map: dict | None) -> list[tuple[str, str]]:
    """Return (source_label, criterion_text) tuples from the change
    map's requirement_hints + linked_content + comments. Used by Fix
    C1 to compute coverage."""
    if not change_map:
        return []
    ctx = change_map.get("pr_context") or {}
    out: list[tuple[str, str]] = []
    for hint in (ctx.get("requirement_hints") or []):
        if isinstance(hint, str) and hint.strip():
            out.append(("pr-body", hint.strip()))
    for lc in (ctx.get("linked_content") or []):
        if not isinstance(lc, dict):
            continue
        if not lc.get("fetched"):
            continue
        excerpt = lc.get("excerpt") or ""
        src = lc.get("source_type") or "linked"
        for crit in _extract_criteria_from_text(excerpt):
            out.append((f"linked:{src}", crit))
    for c in (ctx.get("comments") or []):
        if not isinstance(c, dict):
            continue
        body = c.get("body") or ""
        for crit in _extract_criteria_from_text(body):
            out.append(("pr-comment", crit))
    return out


def _excused_inputs(plan: dict) -> set[str]:
    """Return the set of lowercased criterion / symbol strings the
    planner's coverage audit explicitly listed in `gaps[]`. Fix C1
    and C2 skip flagging these so the lint doesn't double-warn the
    reviewer about a gap the planner already surfaced."""
    audit = (plan or {}).get("planner_coverage_audit") or {}
    gaps = audit.get("gaps") if isinstance(audit, dict) else None
    if not isinstance(gaps, list):
        return set()
    out: set[str] = set()
    for g in gaps:
        if not isinstance(g, dict):
            continue
        for key in ("input", "criterion", "symbol"):
            v = g.get(key)
            if isinstance(v, str) and v.strip():
                out.add(v.strip().lower())
    return out


def _extract_new_symbols(diff_text: str) -> list[str]:
    """Walk added lines of a unified diff and return new top-level
    symbol names. De-duplicates, preserves first-seen order."""
    if not diff_text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat in _SYMBOL_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
            break
    return out


def _item_corpus(item: dict) -> str:
    """Combine an item's prose fields into a single search corpus for
    coverage / symbol-mention checks. Includes what + how + rationale
    + preconditions so the analyst's intent in any of those fields
    counts as coverage."""
    parts: list[str] = []
    for k in ("what", "how", "rationale", "preconditions"):
        v = item.get(k)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def _pr_body_coverage_warnings(
    plan: dict, change_map: dict | None
) -> list[str]:
    """Fix C1: emit a warning per PR-body / linked-content / comment
    criterion that no plan item covers. Two thresholds — coverage
    counts when an item's corpus overlaps with the criterion's tokens
    on EITHER:
      - ≥ 50% of the criterion's content tokens, OR
      - ≥ 3 named tokens
    (both fire together when present; either alone qualifies). Items
    with `tool=skip` count as coverage — the planner explicitly
    surfaced the criterion as a gap, which is the lint's whole point.
    """
    criteria = _gather_criteria(change_map)
    if not criteria:
        return []
    items = plan.get("items") or []
    excused = _excused_inputs(plan)
    warnings: list[str] = []
    for source, criterion in criteria:
        if criterion.lower() in excused:
            continue
        crit_tokens = _tokenize(criterion)
        if not crit_tokens:
            continue
        covered_by: list[str] = []
        for it in items:
            item_tokens = _tokenize(_item_corpus(it))
            if not item_tokens:
                continue
            overlap = crit_tokens & item_tokens
            half_cover = len(overlap) >= max(1, len(crit_tokens) // 2)
            named_cover = len(overlap) >= 3
            if half_cover or named_cover:
                covered_by.append(it.get("id", "?"))
        if not covered_by:
            warnings.append(
                f"pr-body-coverage: criterion {criterion!r} (source: "
                f"{source}) is not covered by any plan item. Add an "
                f"item whose what:/how:/rationale: contains the "
                f"criterion's key terms, or list it in "
                f"planner_coverage_audit.gaps[] with a reason."
            )
    return warnings


# Keywords that signal the PR is talking about a supplementary
# binary's produced output (published JSON, serialized payload, HTTP
# response from an output endpoint). When the diff touches code
# reachable from a supplementary binary AND the PR body uses one of
# these vocabularies, the v0.7.7+ rule expects a runtime
# curl-against-output-URL item.
_SUPPLEMENTARY_OUTPUT_KEYWORDS_RE = re.compile(
    r"\b(?:publish(?:ed|ing|es)?|publis[s]?h|serializ(?:ed|es?|ing|ation)|"
    r"output|endpoint|JSON|payload|emit(?:ted|s|ting)?|writ(?:es?|ten|ing)\s+to\s+S3|"
    r"upload(?:ed|s|ing)?\s+to\s+S3|render(?:ed|s|ing)?\s+(?:JSON|response))\b",
    re.IGNORECASE,
)

# Patterns identifying items that DO verify supplementary-binary
# output at runtime — used to decide whether the rule has been
# satisfied. We accept either a curl against a URL-shaped value OR a
# bash item whose how:/what: explicitly mentions polling for
# binary-published output.
_RUNTIME_OUTPUT_VERIFY_RE = re.compile(
    r"\bcurl\b|\bwget\b|\bhttp(?:s)?://|"
    r"\bpoll(?:s|ed|ing)?\s+for\s+(?:publish|output|JSON|S3)|"
    r"\bawait\s+(?:publish|output)|"
    r"\bwait\s+for\s+(?:ticker|daemon|publish|loop)",
    re.IGNORECASE,
)


def _missing_runtime_verify_when_supplementary_binary_present_warnings(
    plan: dict,
    change_map: dict | None,
    setup_context: dict | None,
) -> list[str]:
    """v0.7.7+: when the project runs a supplementary binary in local
    setup AND the diff touches code reachable from that binary AND
    the PR body mentions output-producing keywords (publish / JSON /
    endpoint / serialize / output), the plan MUST include at least
    one bash / curl item that verifies the binary's output at
    runtime.

    The check is the lint half of the "supplementary binaries are in
    setup, plan a real verify item" v0.7.7 contract. Without it,
    plans silently fall back to lint-only items asserting
    source-level facts ("SplitTags is called from RebuildBanners")
    when the actual bug — would the published JSON come out
    trimmed? — never gets runtime-exercised.

    Inputs (v0.7.9 keys; v0.7.7/v0.7.8 keys remain accepted as
    aliases for backward-compat):

    - ``setup_context.supplementary_binaries_running`` — list of
      binary names the planner parsed from ``.proctor/local.yml`` /
      ``.proctor/config.yml`` ``setup:`` block.
      (v0.7.7/v0.7.8 alias: ``daemons_running``.)
    - ``setup_context.supplementary_binary_touched`` — list of
      binary names whose code path the diff touches (planner-
      computed; the SKILL prose tells the planner to grep
      cmd/<name>/main.go imports vs. diff-changed files).
      (v0.7.7/v0.7.8 alias: ``daemon_touched``.)
    - ``change_map.pr_context.body`` — PR body prose, scanned for
      output-keyword mentions.
    - ``plan.items[]`` — scanned for any bash / curl item whose
      how/what contains a curl-against-URL pattern.

    Rule fires only when all three conditions are met. False
    positives are kept low by requiring the explicit ``touched``
    list rather than guessing from diff paths — the planner already
    knows which binaries are worth verifying because the SKILL
    prose walks it through that mapping.
    """
    if not setup_context or not isinstance(setup_context, dict):
        return []
    # v0.7.9 keys with v0.7.7/v0.7.8 fallbacks for back-compat.
    running = (
        setup_context.get("supplementary_binaries_running")
        or setup_context.get("daemons_running")
        or []
    )
    touched = (
        setup_context.get("supplementary_binary_touched")
        or setup_context.get("daemon_touched")
        or []
    )
    if not running or not touched:
        return []

    touched_in_setup = [d for d in touched if d in running]
    if not touched_in_setup:
        return []

    body = ""
    if change_map and isinstance(change_map, dict):
        ctx = change_map.get("pr_context") or {}
        if isinstance(ctx, dict):
            body = ctx.get("body") or ""
    if not body or not _SUPPLEMENTARY_OUTPUT_KEYWORDS_RE.search(body):
        return []

    items = plan.get("items") or []
    for it in items:
        if it.get("tool") not in ("bash", "curl"):
            continue
        corpus = _item_corpus(it)
        if _RUNTIME_OUTPUT_VERIFY_RE.search(corpus):
            return []

    bins_str = ", ".join(repr(d) for d in touched_in_setup)
    return [
        f"missing-runtime-verify-when-supplementary-binary-present: "
        f"binary/binaries {bins_str} are in local setup AND touched "
        f"by the diff, AND the PR body mentions output "
        f"(publish/JSON/endpoint/serialize/output keywords), BUT "
        f"the plan has no bash/curl item that curls against the "
        f"binary's output URL or polls for its published output. "
        f"Add a runtime verify item that waits for the binary's "
        f"loop (ticker / scheduler / cron) and asserts the "
        f"published output reflects the PR's stated change. If the "
        f"output URL is genuinely unverifiable in your environment, "
        f"plan a tool='skip' item with reason explaining the gap so "
        f"the report makes it visible — don't silently lint-only."
    ]


# v0.7.7/v0.7.8 callers may still reference the old name. Keep an
# alias so internal callers — and any consumer-side scripts that
# imported it — don't break.
_missing_runtime_verify_when_daemon_present_warnings = (
    _missing_runtime_verify_when_supplementary_binary_present_warnings
)


def _new_symbol_not_exercised_warnings(
    plan: dict, diff_text: str | None
) -> list[str]:
    """Fix C2: emit a warning per new diff symbol that only
    `lint-only` items mention. Items with `tool != "lint-only"` that
    mention the symbol name in what/how/rationale count as
    exercising. Items whose tool is `skip` are NOT counted as
    exercising — they're explicit gaps."""
    if not diff_text:
        return []
    symbols = _extract_new_symbols(diff_text)
    if not symbols:
        return []
    items = plan.get("items") or []
    excused = _excused_inputs(plan)
    warnings: list[str] = []
    for sym in symbols:
        if sym.lower() in excused:
            continue
        # Word-boundary match, case-sensitive — symbols are
        # identifiers, casing matters.
        sym_re = re.compile(rf"\b{re.escape(sym)}\b")
        lint_only_hits: list[str] = []
        runtime_hits: list[str] = []
        for it in items:
            if not sym_re.search(_item_corpus(it)):
                continue
            tool = it.get("tool")
            if tool == "lint-only":
                lint_only_hits.append(it.get("id", "?"))
            elif tool in ("chrome-devtools", "bash", "curl"):
                runtime_hits.append(it.get("id", "?"))
        if runtime_hits:
            continue  # exercised
        if lint_only_hits:
            warnings.append(
                f"new-symbol-not-exercised: new symbol {sym!r} is only "
                f"mentioned by lint-only items ({', '.join(lint_only_hits)}). "
                f"Add a runtime item (chrome-devtools / bash / curl) "
                f"that actually exercises {sym!r}, or list it in "
                f"planner_coverage_audit.gaps[] with a reason."
            )
    return warnings


def check(
    plan: dict,
    change_map: dict | None = None,
    diff_text: str | None = None,
    setup_context: dict | None = None,
) -> list[str]:
    """Return a list of warning strings. Empty list = clean plan.

    Warnings are formatted ``<item_id>: <message>`` so the orchestrator
    can print them as a bullet list. Order: combined-happy-negative
    warnings first, missing-round-trip warnings second, plan-level
    coverage warnings last, all sorted by item id (or empty id for
    plan-level) for stable output.

    v0.7.6+: when ``change_map`` is provided, the pr-body-coverage
    check fires for criteria from pr_context.requirement_hints /
    linked_content / comments that no plan item covers. When
    ``diff_text`` is provided, new-symbol-not-exercised fires for
    diff symbols only lint-checked, never runtime-exercised. Both
    inputs are optional — when absent, the v0.7.5 behavior is
    preserved (backward compat).

    v0.7.7+ (v0.7.9 renamed): when ``setup_context`` is provided,
    the ``missing-runtime-verify-when-supplementary-binary-present``
    check fires when a supplementary binary is in setup AND touched
    by the diff AND the PR body mentions output keywords AND no
    runtime curl item exists. The parameter shape (v0.7.9) is::

        {
            "supplementary_binaries_running": [...],
            "supplementary_binary_touched":    [...],
        }

    The v0.7.7/v0.7.8 key names (``daemons_running`` /
    ``daemon_touched``) are still accepted as aliases for
    backward-compat. Absent: no-op."""
    items = plan.get("items") or []
    combined_warnings: list[str] = []
    missing_roundtrip_warnings: list[str] = []
    coverage_warnings: list[str] = []

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
        # v0.3.36: skip items that are themselves reload siblings —
        # phrasings like "re-open saved Image" / "assert created
        # record visible in list" / "verify updated field round-trips"
        # contain past-tense write verbs (saved/created/updated) only
        # as nouns referring to the upstream write. They shouldn't
        # need their OWN reload sibling. Detect: what: contains a
        # reload phrase AND the item has data_from set (i.e. it's
        # explicitly downstream of another item).
        if _RE_RELOAD.search(what) and (it.get("data_from") or []):
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

    # 3. All-negative plan: 2+ negative items and ZERO happy-path
    #    items doing a write action. Fires on the failure mode the
    #    planner falls into when it rationalizes "happy path is
    #    deferred because backend dep" — ending up with N validator
    #    rejects and no save coverage at all. The user has flagged
    #    this exact pattern across multiple PRoctor runs.
    negative_count = sum(
        1 for it in items
        if it.get("error_type")
        or _RE_NEG.search((it.get("what") or ""))
    )
    happy_write_count = sum(
        1 for it in items
        if it.get("tool") == "chrome-devtools"
        and not it.get("error_type")
        and _RE_WRITE.search((it.get("what") or ""))
    )
    if negative_count >= 2 and happy_write_count == 0:
        coverage_warnings.append(
            f"plan-coverage: {negative_count} negative items but 0 "
            f"chrome-devtools items whose what: contains a recognized "
            f"happy-path write verb (save/create/update/submit/edit/"
            f"publish/upload/persist/insert/store, present or past "
            f"tense). PRs that add new fields/forms need AT LEAST ONE "
            f"happy item that fills the form with valid input and "
            f"asserts the record persisted. If you DO have happy "
            f"items but used a different verb, rephrase using one of "
            f"the recognized verbs so this lint and the report's "
            f"happy-vs-negative grouping count them correctly. If "
            f"backend dependencies block the full save flow, plan "
            f"the item anyway with tool=\"skip\" and reason="
            f"\"backend-dep-not-deployed\" so the gap is visible in "
            f"the report, not silently absent from the plan."
        )

    # v0.7.6 new checks — independent of the v0.7.5 plan-internal
    # ones above. Each is no-op when its input is absent (backward
    # compat).
    pr_body_warnings = _pr_body_coverage_warnings(plan, change_map)
    new_symbol_warnings = _new_symbol_not_exercised_warnings(plan, diff_text)
    # v0.7.7 new check (v0.7.9 renamed). Same pattern, no-op when
    # setup_context absent.
    supplementary_verify_warnings = (
        _missing_runtime_verify_when_supplementary_binary_present_warnings(
            plan, change_map, setup_context,
        )
    )

    combined_warnings.sort()
    missing_roundtrip_warnings.sort()
    coverage_warnings.sort()
    pr_body_warnings.sort()
    new_symbol_warnings.sort()
    supplementary_verify_warnings.sort()
    return (
        combined_warnings
        + missing_roundtrip_warnings
        + coverage_warnings
        + pr_body_warnings
        + new_symbol_warnings
        + supplementary_verify_warnings
    )


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
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any warnings fired (default is exit 0 always).",
    )
    p.add_argument(
        "--change-map",
        default=None,
        help=("Optional path to change-map.json. When provided, the "
              "pr-body-coverage check fires for criteria the plan "
              "doesn't cover."),
    )
    p.add_argument(
        "--diff",
        default=None,
        help=("Optional path to diff.patch (unified diff). When "
              "provided, the new-symbol-not-exercised check fires for "
              "new top-level symbols only lint-checked by the plan."),
    )
    p.add_argument(
        "--setup-context",
        default=None,
        help=("Optional path to setup-context.json with shape "
              "{supplementary_binaries_running: [...], "
              "supplementary_binary_touched: [...]} (v0.7.9; the "
              "v0.7.7/v0.7.8 keys daemons_running / daemon_touched "
              "are accepted as aliases). When provided AND both "
              "lists overlap AND the PR body mentions output "
              "keywords, the missing-runtime-verify-when-"
              "supplementary-binary-present rule fires if the plan "
              "has no bash/curl item against the binary's output "
              "URL."),
    )
    args = p.parse_args()

    plan = json.load(sys.stdin)
    change_map = None
    if args.change_map:
        cm_path = Path(args.change_map)
        if cm_path.exists():
            try:
                change_map = json.loads(cm_path.read_text())
            except json.JSONDecodeError as e:
                sys.stderr.write(
                    f"warning: --change-map at {args.change_map} could "
                    f"not be parsed ({e}); skipping pr-body-coverage "
                    f"check.\n"
                )
    diff_text = None
    if args.diff:
        diff_path = Path(args.diff)
        if diff_path.exists():
            try:
                diff_text = diff_path.read_text()
            except OSError as e:
                sys.stderr.write(
                    f"warning: --diff at {args.diff} could not be read "
                    f"({e}); skipping new-symbol-not-exercised check.\n"
                )
    setup_context = None
    if args.setup_context:
        sc_path = Path(args.setup_context)
        if sc_path.exists():
            try:
                setup_context = json.loads(sc_path.read_text())
            except json.JSONDecodeError as e:
                sys.stderr.write(
                    f"warning: --setup-context at {args.setup_context} "
                    f"could not be parsed ({e}); skipping "
                    f"missing-runtime-verify-when-supplementary-"
                    f"binary-present check.\n"
                )
    warnings = check(
        plan,
        change_map=change_map,
        diff_text=diff_text,
        setup_context=setup_context,
    )
    for w in warnings:
        sys.stdout.write(w + "\n")
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
