"""JSON schema validators for PRoctor stage contracts.

Each ``validate_*`` raises ``SchemaError`` with a precise message when its
input deviates from the expected shape. Validators check structural
correctness and enumeration constraints; they do not check semantic
correctness (e.g. whether a SHA actually exists in the repo).
"""

from __future__ import annotations

VALID_CATEGORIES = {
    "frontend", "api", "schema", "infra",
    "mobile", "cli", "e2e-flow", "docs",
}
VALID_RISK = {"low", "medium", "high"}
VALID_TOOL = {"chrome-devtools", "bash", "curl", "lint-only", "skip"}
VALID_STATUS = {"pass", "fail", "skipped"}


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

    _require(isinstance(cm["categories_present"], list),
             "ChangeMap.categories_present must be a list")
    for c in cm["categories_present"]:
        _require(c in VALID_CATEGORIES,
                 f"ChangeMap.categories_present contains unknown {c!r}")


def validate_test_plan(tp: dict) -> None:
    _require(isinstance(tp, dict), "TestPlan: must be a dict")
    _require_keys(tp, {"items"}, "TestPlan")
    _require(isinstance(tp["items"], list), "TestPlan.items must be a list")

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

    # Second pass: every depends_on entry must reference a known id.
    for i, item in enumerate(tp["items"]):
        for dep in item["depends_on"]:
            _require(dep in seen_ids,
                     f"TestPlan.items[{i}].depends_on references unknown id {dep!r}")


def validate_test_results(tr: dict) -> None:
    _require(isinstance(tr, dict), "TestResults: must be a dict")
    _require_keys(tr, {"items", "summary"}, "TestResults")
    _require(isinstance(tr["items"], list), "TestResults.items must be a list")

    counts = {"pass": 0, "fail": 0, "skipped": 0}
    for i, item in enumerate(tr["items"]):
        # logs_ref is optional: in headless CI runs the executor reports
        # inline and may not produce a per-item log file. id/status/evidence
        # are required for the report stage to render anything useful.
        _require_keys(item, {"id", "status", "evidence"},
                      f"TestResults.items[{i}]")
        _require(item["status"] in VALID_STATUS,
                 f"TestResults.items[{i}].status {item['status']!r} invalid")
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
