"""JSON schema validators for PRoctor stage contracts.

Each ``validate_*`` raises ``SchemaError`` with a precise message when its
input deviates from the expected shape. Validators check structural
correctness and enumeration constraints; they do not check semantic
correctness (e.g. whether a SHA actually exists in the repo).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Template syntax for cross-item data flow (v0.3.25+):
#   {{<item_id>.<output_key>}}    e.g. {{t-007.created_id}}
# - item_id matches the existing id format ([A-Za-z0-9_-]+ — same chars
#   the planner already uses for t-001 / fix-step-2 / etc.)
# - output_key must be a valid identifier ([A-Za-z_][A-Za-z0-9_]*) so
#   downstream items can use a stable, shell-safe reference.
# Whitespace inside braces is tolerated so plan authors can write
# `{{ t-005.created_id }}` for readability.
_TEMPLATE_RE = re.compile(
    r"\{\{\s*([A-Za-z0-9_-]+)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)
_PRODUCES_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

VALID_CATEGORIES = {
    "frontend", "api", "schema", "infra",
    "mobile", "cli", "e2e-flow", "docs",
}
VALID_RISK = {"low", "medium", "high"}
VALID_TOOL = {"chrome-devtools", "bash", "curl", "lint-only", "skip"}
VALID_STATUS = {"pass", "fail", "skipped"}

# Optional categorization for negative-path test items so the planner can
# balance coverage across distinct failure-mode kinds instead of stacking
# four near-identical validation rejects. Happy-path items leave this
# unset.
VALID_ERROR_TYPE = {
    "validation",       # form / input validator rejects bad data
    "permission",       # role-based access check fires
    "network",          # upstream / API failure path
    "state-conflict",   # concurrent edit / duplicate submit / stale data
    "not-found",        # 404 / missing-record handling
    "auth",             # not-logged-in / session-expired
}

# Auth types accepted in .pr-test.yml's auth.type field. Add new values
# here when adding support for other login mechanisms (oauth, magic
# link, etc.); each new value needs a corresponding flow in the
# executing-pr-tests skill.
VALID_AUTH_TYPES = {"form_with_totp"}

# Required form selectors when auth.type == "form_with_totp". Each value
# must be a CSS selector that uniquely identifies the relevant DOM node
# on the consumer's actual login page.
_FORM_TOTP_SELECTOR_KEYS = {"email", "password", "totp", "submit"}


class SchemaError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _require_keys(obj: dict, keys: set, label: str) -> None:
    missing = keys - set(obj.keys())
    _require(not missing, f"{label}: missing keys {sorted(missing)}")


def validate_change_map(cm: dict) -> None:
    _require(isinstance(cm, dict), "ChangeMap: must be a dict")
    _require_keys(cm, {"pr", "hunks", "categories_present"}, "ChangeMap")

    pr = cm["pr"]
    _require_keys(pr, {"number", "head_sha", "base_sha", "url"}, "ChangeMap.pr")
    _require(isinstance(pr["number"], int) and pr["number"] > 0,
             "ChangeMap.pr.number must be a positive int")

    _require(isinstance(cm["hunks"], list), "ChangeMap.hunks must be a list")
    for i, h in enumerate(cm["hunks"]):
        _require_keys(h, {"file", "category", "risk", "summary"},
                      f"ChangeMap.hunks[{i}]")
        _require(h["category"] in VALID_CATEGORIES,
                 f"ChangeMap.hunks[{i}].category {h['category']!r} not in {VALID_CATEGORIES}")
        _require(h["risk"] in VALID_RISK,
                 f"ChangeMap.hunks[{i}].risk {h['risk']!r} not in {VALID_RISK}")
        # `impact_radius` (v0.3.24+) optional — list of caller files
        # discovered by grep-based import-graph analysis. Items in the
        # list are repo-relative paths. Empty list means "analyzed and
        # found no callers"; omitted means "did not analyze" (e.g. docs
        # hunk). The planner reads this to add regression items for
        # high-impact changes.
        if "impact_radius" in h and h["impact_radius"] is not None:
            _require(isinstance(h["impact_radius"], list),
                     f"ChangeMap.hunks[{i}].impact_radius must be a list of file paths")
            for j, p in enumerate(h["impact_radius"]):
                _require(isinstance(p, str) and p.strip(),
                         f"ChangeMap.hunks[{i}].impact_radius[{j}] must be a non-empty string")
                _require(p != h["file"],
                         f"ChangeMap.hunks[{i}].impact_radius[{j}] cannot reference the changed file itself")
        # `impact_radius_truncated` (v0.3.28+) — True when the helper
        # found MORE qualifying callers than top_n. Signals to the
        # planner that the visible 10 don't represent the full blast
        # radius and risk should auto-upgrade to high. Boolean only.
        if "impact_radius_truncated" in h and h["impact_radius_truncated"] is not None:
            _require(isinstance(h["impact_radius_truncated"], bool),
                     f"ChangeMap.hunks[{i}].impact_radius_truncated must be a bool")

    _require(isinstance(cm["categories_present"], list),
             "ChangeMap.categories_present must be a list")
    for c in cm["categories_present"]:
        _require(c in VALID_CATEGORIES,
                 f"ChangeMap.categories_present contains unknown {c!r}")

    # Optional pr_context (added in v0.1.11): the analyzer surfaces the
    # PR's title/body/links so the planner can use documented requirements
    # as test inputs. Old ChangeMaps without this field are still valid.
    if "pr_context" in cm:
        ctx = cm["pr_context"]
        _require(isinstance(ctx, dict), "ChangeMap.pr_context must be a dict if present")
        if "title" in ctx:
            _require(isinstance(ctx["title"], str),
                     "ChangeMap.pr_context.title must be a string")
        if "body" in ctx:
            _require(isinstance(ctx["body"], str),
                     "ChangeMap.pr_context.body must be a string")
        if "links" in ctx:
            _require(isinstance(ctx["links"], list),
                     "ChangeMap.pr_context.links must be a list")
        if "requirement_hints" in ctx:
            _require(isinstance(ctx["requirement_hints"], list),
                     "ChangeMap.pr_context.requirement_hints must be a list")
        # directives: user-provided HTML-comment overrides extracted from
        # the PR body. Added in v0.2.3. All sub-fields optional.
        if "directives" in ctx:
            d = ctx["directives"]
            _require(isinstance(d, dict),
                     "ChangeMap.pr_context.directives must be a dict if present")
            for list_key in ("skip_paths", "skip_categories", "focus_paths"):
                if list_key in d:
                    _require(isinstance(d[list_key], list),
                             f"ChangeMap.pr_context.directives.{list_key} must be a list")
            if "max_items" in d:
                _require(isinstance(d["max_items"], int) and d["max_items"] > 0,
                         "ChangeMap.pr_context.directives.max_items must be a positive int")


def validate_test_plan(tp: dict) -> None:
    _require(isinstance(tp, dict), "TestPlan: must be a dict")
    _require_keys(tp, {"items"}, "TestPlan")
    _require(isinstance(tp["items"], list), "TestPlan.items must be a list")

    # Structured journeys (v0.3.28+) — optional top-level array.
    # Each entry is {id, goal, terminal_state}; items reference via
    # `journey_id`. Reporter groups by id (not by free-form `journey`
    # string), so slight name drift between two items can't split a
    # single user-flow into two report sections. The v0.3.23 loose
    # `journey: "<name>"` string still validates (legacy mode) but
    # the planner skill prefers `journey_id`.
    journey_ids: set[str] = set()
    if "journeys" in tp and tp["journeys"] is not None:
        _require(isinstance(tp["journeys"], list),
                 "TestPlan.journeys must be a list if present")
        for i, j in enumerate(tp["journeys"]):
            _require(isinstance(j, dict),
                     f"TestPlan.journeys[{i}] must be a dict")
            _require_keys(j, {"id", "goal", "terminal_state"},
                          f"TestPlan.journeys[{i}]")
            for k in ("id", "goal", "terminal_state"):
                _require(isinstance(j[k], str) and j[k].strip(),
                         f"TestPlan.journeys[{i}].{k} must be a non-empty string")
            _require(j["id"] not in journey_ids,
                     f"TestPlan.journeys[{i}].id {j['id']!r} duplicates an earlier entry")
            journey_ids.add(j["id"])

    seen_ids: set[str] = set()
    for i, item in enumerate(tp["items"]):
        _require_keys(item, {"id", "category", "what", "how", "tool", "risk", "depends_on"},
                      f"TestPlan.items[{i}]")
        _require(item["id"] not in seen_ids,
                 f"TestPlan.items[{i}].id {item['id']!r} duplicated")
        seen_ids.add(item["id"])
        _require(item["category"] in VALID_CATEGORIES,
                 f"TestPlan.items[{i}].category {item['category']!r} invalid")
        _require(item["risk"] in VALID_RISK,
                 f"TestPlan.items[{i}].risk {item['risk']!r} invalid")
        _require(item["tool"] in VALID_TOOL,
                 f"TestPlan.items[{i}].tool {item['tool']!r} invalid")
        _require(isinstance(item["depends_on"], list),
                 f"TestPlan.items[{i}].depends_on must be a list")
        for dep in item["depends_on"]:
            _require(dep != item["id"],
                     f"TestPlan.items[{i}] cannot depend on itself")
        # `as_account` (v0.3.0+) optional — references which configured
        # admin account to run this item under. When omitted, the
        # executor defaults to auth.accounts[0]. Cross-referencing
        # against the actual account list happens via
        # validate_test_plan_account_refs(plan, cfg).
        if "as_account" in item and item["as_account"] is not None:
            _require(isinstance(item["as_account"], str) and item["as_account"].strip(),
                     f"TestPlan.items[{i}].as_account must be a non-empty string if set")
        # `rationale` (v0.3.18+) optional — one-paragraph explanation of
        # WHY the planner generated this item for THIS diff (which hunk
        # it targets, which acceptance criterion it checks, which risk
        # it mitigates). Shown in the report under "Why this test" so
        # the developer can audit whether the planner's reasoning makes
        # sense. Missing rationale just means that section is omitted
        # for backward compat with older plans.
        if "rationale" in item and item["rationale"] is not None:
            _require(isinstance(item["rationale"], str) and item["rationale"].strip(),
                     f"TestPlan.items[{i}].rationale must be a non-empty string if set")
        # `preconditions` (v0.3.22+) optional — explicit description of
        # the test's required starting state. Separated from `how:` so
        # the executor knows what to set up *before* the test action
        # versus what to execute as the test action itself. Examples:
        #   "Logged in as developer; DB has one published category."
        #   "No existing reward named 'fixture-test-image'."
        # Missing means "no special setup beyond what auth + setup
        # already arrange".
        if "preconditions" in item and item["preconditions"] is not None:
            _require(isinstance(item["preconditions"], str) and item["preconditions"].strip(),
                     f"TestPlan.items[{i}].preconditions must be a non-empty string if set")
        # `verify_precondition_via` (v0.3.29+) optional — a shell
        # command the executor runs BEFORE dispatching the subagent.
        # Non-zero exit → item marked `skipped` with
        # `reason: "precondition-not-met"` so the reviewer can tell
        # "environment didn't match the assumed state" apart from
        # "the change under test is broken". The command may contain
        # `{{<id>.<key>}}` templates which get substituted just like
        # `how:` / `preconditions` before execution.
        if "verify_precondition_via" in item and item["verify_precondition_via"] is not None:
            _require(isinstance(item["verify_precondition_via"], str)
                     and item["verify_precondition_via"].strip(),
                     f"TestPlan.items[{i}].verify_precondition_via must be a "
                     f"non-empty string if set")
        # `error_type` (v0.3.22+) optional — categorization for negative
        # items so the planner can spread coverage across distinct
        # failure-mode classes instead of repeating the same validation
        # check in four shapes. Happy-path items must leave this unset.
        if "error_type" in item and item["error_type"] is not None:
            _require(item["error_type"] in VALID_ERROR_TYPE,
                     f"TestPlan.items[{i}].error_type {item['error_type']!r} not in {sorted(VALID_ERROR_TYPE)}")
        # `journey` (v0.3.23+) optional — name of the user journey this
        # item belongs to. Items grouped by journey in the report so a
        # reviewer can see "the Create-Image-Reward flow has 4 items,
        # all passed". Free-form short string, kebab/snake_case
        # recommended. Items not part of a journey omit this.
        if "journey" in item and item["journey"] is not None:
            _require(isinstance(item["journey"], str) and item["journey"].strip(),
                     f"TestPlan.items[{i}].journey must be a non-empty string if set")
        # `journey_id` (v0.3.28+) — preferred over the loose `journey`
        # string. References a top-level journeys[].id so two items
        # can't accidentally split into separate report groups due to
        # whitespace / pluralization / typo drift in the name.
        if "journey_id" in item and item["journey_id"] is not None:
            _require(isinstance(item["journey_id"], str) and item["journey_id"].strip(),
                     f"TestPlan.items[{i}].journey_id must be a non-empty string if set")
            _require(item["journey_id"] in journey_ids,
                     f"TestPlan.items[{i}].journey_id {item['journey_id']!r} "
                     f"not in TestPlan.journeys ({sorted(journey_ids)})")
            # Mixing both forms is ambiguous — reporter would have to
            # decide which to display. Force the planner to pick one.
            _require(item.get("journey") is None,
                     f"TestPlan.items[{i}]: set EITHER journey OR journey_id, "
                     f"not both (got journey={item.get('journey')!r}, "
                     f"journey_id={item['journey_id']!r})")
        # `data_from` (v0.3.23+) optional list of item IDs — declares
        # that THIS item's test state depends on the LISTED items
        # having successfully produced their effect (e.g. created a
        # record this item now edits). Stronger than depends_on, which
        # only orders execution:
        #   - depends_on: t-007 must finish before t-008 runs
        #   - data_from:  t-008 must be SKIPPED if t-007 fail/skipped
        # The executor enforces the skip; the reporter renders it as
        # "skipped (upstream failed)" with the dep chain visible.
        if "data_from" in item and item["data_from"] is not None:
            _require(isinstance(item["data_from"], list),
                     f"TestPlan.items[{i}].data_from must be a list of item IDs")
            for j, src in enumerate(item["data_from"]):
                _require(isinstance(src, str) and src.strip(),
                         f"TestPlan.items[{i}].data_from[{j}] must be a non-empty string")
                _require(src != item["id"],
                         f"TestPlan.items[{i}] cannot pull data_from itself")
        # `produces` (v0.3.25+) optional list of output key names this
        # item promises to capture and return to the executor's run
        # context. Downstream items reference these via
        # `{{<this_id>.<key>}}` templates in their `how:` /
        # `preconditions`. Keys must be valid identifiers (used in
        # shell-ish substitution).
        if "produces" in item and item["produces"] is not None:
            _require(isinstance(item["produces"], list),
                     f"TestPlan.items[{i}].produces must be a list of output key names")
            seen_keys: set[str] = set()
            for j, k in enumerate(item["produces"]):
                _require(isinstance(k, str) and k.strip(),
                         f"TestPlan.items[{i}].produces[{j}] must be a non-empty string")
                _require(_PRODUCES_KEY_RE.match(k),
                         f"TestPlan.items[{i}].produces[{j}] {k!r} must match "
                         f"[A-Za-z_][A-Za-z0-9_]* (used as a {{{{id.key}}}} substitution token)")
                _require(k not in seen_keys,
                         f"TestPlan.items[{i}].produces[{j}] {k!r} duplicates an earlier key")
                seen_keys.add(k)

    # Second pass: every depends_on / data_from entry must reference a
    # known id. data_from sources should also be in depends_on (data
    # dependency implies ordering dependency — auto-validate so the
    # planner can't accidentally race).
    by_id = {it["id"]: it for it in tp["items"]}
    for i, item in enumerate(tp["items"]):
        for dep in item["depends_on"]:
            _require(dep in seen_ids,
                     f"TestPlan.items[{i}].depends_on references unknown id {dep!r}")
        for src in (item.get("data_from") or []):
            _require(src in seen_ids,
                     f"TestPlan.items[{i}].data_from references unknown id {src!r}")
            _require(src in item["depends_on"],
                     f"TestPlan.items[{i}] declares data_from={src!r} but doesn't "
                     f"list it in depends_on — data dependency requires execution ordering")
        # Third check (v0.3.25+): every {{<id>.<key>}} template in
        # `how:` / `preconditions` must point to a real upstream item,
        # be listed in this item's data_from (so the run-context will
        # be populated when this item runs), and the upstream must
        # declare `produces: [..., <key>, ...]`. Without this, the
        # template would silently render as literal `{{...}}` at
        # runtime and the test would fail in a confusing way.
        for field_name in ("how", "preconditions", "verify_precondition_via"):
            text = item.get(field_name)
            if not isinstance(text, str):
                continue
            for m in _TEMPLATE_RE.finditer(text):
                ref_id, ref_key = m.group(1), m.group(2)
                tpl = f"{{{{{ref_id}.{ref_key}}}}}"
                _require(ref_id in seen_ids,
                         f"TestPlan.items[{i}].{field_name}: template {tpl} "
                         f"references unknown item id {ref_id!r}")
                _require(ref_id in (item.get("data_from") or []),
                         f"TestPlan.items[{i}].{field_name}: template {tpl} "
                         f"requires {ref_id!r} in this item's data_from "
                         f"(consuming upstream state must be declared explicitly)")
                producer_keys = by_id[ref_id].get("produces") or []
                _require(ref_key in producer_keys,
                         f"TestPlan.items[{i}].{field_name}: template {tpl} "
                         f"references key {ref_key!r} but item {ref_id!r} does not "
                         f"declare produces=[..., {ref_key!r}, ...]")


def validate_test_results(tr: dict) -> None:
    _require(isinstance(tr, dict), "TestResults: must be a dict")
    _require_keys(tr, {"items", "summary"}, "TestResults")
    _require(isinstance(tr["items"], list), "TestResults.items must be a list")

    counts = {"pass": 0, "fail": 0, "skipped": 0}
    for i, item in enumerate(tr["items"]):
        # Required: id, status, evidence (the executor's outcome summary).
        # Optional rich-report fields (added in v0.1.12):
        #   - command: the actual shell / browser command executed
        #   - output_excerpt: a relevant snippet of stdout/stderr (≤ 4 KB)
        #   - logs_ref: path inside .proctor/runs/<run-id>/ to a log file
        #   - screenshot_ref: path to a screenshot for chrome-devtools items
        _require_keys(item, {"id", "status", "evidence"},
                      f"TestResults.items[{i}]")
        _require(item["status"] in VALID_STATUS,
                 f"TestResults.items[{i}].status {item['status']!r} invalid")
        for opt_str in ("command", "output_excerpt", "logs_ref",
                        "screenshot_ref", "screenshot_focus"):
            # Treat explicit `null` the same as omitted — the executor
            # often emits `"screenshot_ref": null` for non-chrome-devtools
            # items, and that's not a schema violation.
            if item.get(opt_str) is not None:
                _require(isinstance(item[opt_str], str),
                         f"TestResults.items[{i}].{opt_str} must be a string if present")
        # `outputs` (v0.3.25+) — captured data from this item's run,
        # keyed by name as declared in the matching TestPlan item's
        # `produces` array. Downstream items reference these via
        # `{{<this_id>.<key>}}` templates which the executor substitutes
        # in-place before dispatching the dependent subagent. Values
        # MUST be strings (downstream uses them in shell / URL / DOM
        # contexts where a string is the only sane unit).
        if item.get("outputs") is not None:
            _require(isinstance(item["outputs"], dict),
                     f"TestResults.items[{i}].outputs must be an object if present")
            for k, v in item["outputs"].items():
                _require(isinstance(k, str) and _PRODUCES_KEY_RE.match(k),
                         f"TestResults.items[{i}].outputs: key {k!r} must match "
                         f"[A-Za-z_][A-Za-z0-9_]*")
                _require(isinstance(v, str) and v != "",
                         f"TestResults.items[{i}].outputs[{k!r}]: value must be a "
                         f"non-empty string (got {v!r})")
        counts[item["status"]] += 1

    summary = tr["summary"]
    _require_keys(summary, {"total", "pass", "fail", "skipped"}, "TestResults.summary")
    _require(summary["total"] == len(tr["items"]),
             f"TestResults.summary.total {summary['total']} != items count {len(tr['items'])}")
    for k in counts:
        _require(summary[k] == counts[k],
                 f"TestResults.summary.{k} {summary[k]} != actual {counts[k]}")


def validate_fix_pr_ref(ref) -> None:
    if ref is None:
        return
    _require(isinstance(ref, dict), "FixPRRef: must be dict or None")
    _require_keys(ref, {"number", "url", "branch", "covers"}, "FixPRRef")
    _require(isinstance(ref["covers"], list) and len(ref["covers"]) > 0,
             "FixPRRef.covers must be non-empty list")


# ---------------------------------------------------------------------------
# .pr-test.yml validation (v0.3.0+) + .pr-test.local.yml overlay loading.
# ---------------------------------------------------------------------------

def validate_pr_test_config(cfg: dict) -> None:
    """Validate the merged `.pr-test.yml` (+ optional `.pr-test.local.yml`
    overlay). Auth block is optional — when omitted, PRoctor runs in the
    legacy "no-login" mode (driven by the consumer's `setup:` block, which
    might start a fresh server with bypassed auth). When auth is present,
    every required sub-key is enforced strictly so consumers get loud
    feedback instead of a silent misconfiguration mid-run."""
    _require(isinstance(cfg, dict), ".pr-test.yml: must be a mapping")

    auth = cfg.get("auth")
    if auth is None:
        return  # legacy mode, nothing more to check at config level
    _require(isinstance(auth, dict), ".pr-test.yml.auth: must be a mapping")
    _require_keys(auth, {"type", "login_url", "selectors", "accounts"},
                  ".pr-test.yml.auth")
    _require(auth["type"] in VALID_AUTH_TYPES,
             f".pr-test.yml.auth.type {auth['type']!r} not in {sorted(VALID_AUTH_TYPES)}")

    if auth["type"] == "form_with_totp":
        sel = auth["selectors"]
        _require(isinstance(sel, dict), ".pr-test.yml.auth.selectors: must be a mapping")
        missing = _FORM_TOTP_SELECTOR_KEYS - set(sel.keys())
        _require(not missing,
                 f".pr-test.yml.auth.selectors: missing keys {sorted(missing)}")
        for k in _FORM_TOTP_SELECTOR_KEYS:
            _require(isinstance(sel[k], str) and sel[k].strip(),
                     f".pr-test.yml.auth.selectors.{k}: must be a non-empty string")

    accs = auth["accounts"]
    _require(isinstance(accs, list) and len(accs) > 0,
             ".pr-test.yml.auth.accounts: must be a non-empty list")
    seen_names: set[str] = set()
    for i, a in enumerate(accs):
        label = f".pr-test.yml.auth.accounts[{i}]"
        _require(isinstance(a, dict), f"{label}: must be a mapping")
        _require_keys(a, {"name"}, label)
        _require(isinstance(a["name"], str) and a["name"].strip(),
                 f"{label}.name: must be a non-empty string")
        _require(a["name"] not in seen_names,
                 f"{label}.name {a['name']!r} duplicates an earlier account; "
                 "names must be unique")
        seen_names.add(a["name"])
        # Each credential field accepts EITHER an inline value OR a *_env
        # pointer (env var name). Exactly one form per field is required —
        # mixing both is an error so the consumer doesn't accidentally
        # think the env var is being used when the inline value wins.
        for field in ("email", "password", "totp_seed"):
            inline = a.get(field)
            env_key = a.get(f"{field}_env")
            _require(
                (inline is not None) ^ (env_key is not None),
                f"{label}: must set exactly one of {field!r} or {field}_env, "
                f"not both / not neither (got "
                f"{field}={inline!r}, {field}_env={env_key!r})"
            )
            if inline is not None:
                _require(isinstance(inline, str) and inline.strip(),
                         f"{label}.{field}: inline value must be a non-empty string")
            else:
                _require(isinstance(env_key, str) and env_key.strip(),
                         f"{label}.{field}_env: must be a non-empty string")


def validate_test_plan_account_refs(plan: dict, cfg: dict) -> None:
    """When `cfg.auth.accounts` is set, every plan item's optional
    `as_account` field must reference a real account name. This catches
    typos like `as_account: editer` early instead of mid-execution."""
    auth = (cfg or {}).get("auth")
    if not auth:
        return
    valid = {a["name"] for a in auth["accounts"]}
    for i, item in enumerate(plan.get("items", [])):
        if "as_account" in item and item["as_account"] is not None:
            _require(item["as_account"] in valid,
                     f"TestPlan.items[{i}].as_account {item['as_account']!r} "
                     f"not in auth.accounts ({sorted(valid)})")


# ---------------------------------------------------------------------------
# Config loader: .pr-test.yml + optional .pr-test.local.yml overlay.
# ---------------------------------------------------------------------------

def _deep_merge_overlay(base: dict, overlay: dict) -> dict:
    """Merge `overlay` over `base`. Dicts merge key-by-key recursively;
    lists in overlay REPLACE lists in base (we deliberately do not
    element-merge `accounts` — that would mix dev-only env vars with
    test-env ones and produce confusing silent partial overrides).
    Scalars in overlay win."""
    out = dict(base)
    for k, v in overlay.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge_overlay(out[k], v)
        else:
            out[k] = v
    return out


def load_config(repo_root: str | os.PathLike[str] = ".") -> dict:
    """Load the merged PRoctor config for the repo at `repo_root`.

    Reads `.pr-test.yml`; if `.pr-test.local.yml` exists, deep-merges it
    on top so per-developer overrides (different `base_url`, different
    secret env var names) take effect without touching the committed file.

    Returns the merged config dict. Does NOT validate — callers should
    pipe the result through `validate_pr_test_config` before use."""
    import yaml  # local import — schema.py shouldn't pay yaml cost unless config is loaded

    root = Path(repo_root)
    base_path = root / ".pr-test.yml"
    if not base_path.exists():
        raise FileNotFoundError(f".pr-test.yml not found under {root}")
    base = yaml.safe_load(base_path.read_text()) or {}

    local_path = root / ".pr-test.local.yml"
    if local_path.exists():
        overlay = yaml.safe_load(local_path.read_text()) or {}
        return _deep_merge_overlay(base, overlay)
    return base
