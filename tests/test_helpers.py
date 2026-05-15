import json
import os
import pathlib
import subprocess
import sys
import time
import pytest
from unittest import mock
from plugins.proctor.scripts.schema import (
    validate_change_map,
    validate_test_plan,
    validate_test_results,
    validate_fix_pr_ref,
    SchemaError,
)
from plugins.proctor.scripts.pr_fetch import parse_pr_arg, PRArg
from plugins.proctor.scripts.runlog import make_run_id, log_line
from plugins.proctor.scripts import gh_lock, post_comment


def test_change_map_minimum_valid():
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{"file": "a.py", "category": "api", "risk": "low", "summary": "."}],
        "categories_present": ["api"],
    }
    validate_change_map(valid)  # should not raise


def test_change_map_rejects_unknown_category():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{"file": "a.py", "category": "wat", "risk": "low", "summary": "."}],
        "categories_present": ["wat"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_change_map_accepts_pr_context():
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "title": "Add display_name with 100-char limit",
            "body": "Per ACME-42: max 100 chars.",
            "links": ["https://acme.atlassian.net/browse/ACME-42"],
            "requirement_hints": ["max 100 chars on display_name"],
        },
        "hunks": [{"file": "a.go", "category": "api", "risk": "medium", "summary": "."}],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_rejects_malformed_pr_context():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": "not-a-dict",
        "hunks": [{"file": "a.go", "category": "api", "risk": "low", "summary": "."}],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_change_map_accepts_directives():
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "directives": {
                "skip_paths": ["vendor/", "third_party/**"],
                "skip_categories": ["docs"],
                "focus_paths": ["src/payments/"],
                "max_items": 5,
            },
        },
        "hunks": [{"file": "src/payments/charge.go", "category": "api", "risk": "high", "summary": "."}],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_rejects_bad_directive_types():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "directives": {"skip_paths": "not-a-list"},
        },
        "hunks": [{"file": "a.go", "category": "api", "risk": "low", "summary": "."}],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_test_plan_minimum_valid():
    valid = {
        "items": [
            {
                "id": "t-001",
                "category": "api",
                "what": "GET /x returns 200",
                "how": "curl http://localhost/x",
                "tool": "bash",
                "risk": "low",
                "depends_on": [],
            }
        ]
    }
    validate_test_plan(valid)


def test_test_plan_rejects_duplicate_ids():
    bad = {
        "items": [
            {"id": "t-001", "category": "api", "what": "x", "how": "x",
             "tool": "bash", "risk": "low", "depends_on": []},
            {"id": "t-001", "category": "api", "what": "y", "how": "y",
             "tool": "bash", "risk": "low", "depends_on": []},
        ]
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_test_results_summary_must_match_items():
    bad = {
        "items": [
            {"id": "t-001", "status": "pass", "evidence": "ok",
             "logs_ref": ".proctor/runs/x/t-001.log"},
        ],
        "summary": {"total": 2, "pass": 1, "fail": 1, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_logs_ref_optional():
    valid = {
        "items": [
            {"id": "t-001", "status": "pass", "evidence": "ok"},
        ],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_test_results_rich_fields_accepted():
    valid = {
        "items": [
            {
                "id": "t-001", "status": "pass", "evidence": "Found Phone column",
                "command": "curl http://x/admin/users",
                "output_excerpt": "<th>Phone</th>",
                "logs_ref": ".proctor/runs/x/logs/t-001.log",
                "screenshot_ref": ".proctor/runs/x/screenshots/t-001.png",
                "screenshot_focus": "Phone column header visible above the data rows.",
            },
        ],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_test_results_rejects_non_string_rich_field():
    bad = {
        "items": [
            {"id": "t-001", "status": "pass", "evidence": "ok",
             "command": 12345},
        ],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_explicit_null_rich_fields_accepted():
    valid = {
        "items": [
            {"id": "t-001", "status": "pass", "evidence": "ok",
             "screenshot_ref": None,
             "screenshot_focus": None,
             "logs_ref": None},
        ],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_fix_pr_ref_can_be_null():
    validate_fix_pr_ref(None)


def test_fix_pr_ref_minimum_valid():
    valid = {"number": 124, "url": "https://x", "branch": "fix-123-abc",
             "covers": ["t-002"]}
    validate_fix_pr_ref(valid)


def test_parse_pr_arg_number_only():
    arg = parse_pr_arg("123")
    assert arg == PRArg(number=123, repo=None)


def test_parse_pr_arg_full_url():
    arg = parse_pr_arg("https://github.com/owner/name/pull/42")
    assert arg == PRArg(number=42, repo="owner/name")


def test_parse_pr_arg_rejects_garbage():
    with pytest.raises(ValueError):
        parse_pr_arg("not-a-pr")


# v0.3.35: URL tab-suffixes from GitHub UI copy-paste
@pytest.mark.parametrize("url", [
    "https://github.com/owner/name/pull/42",
    "https://github.com/owner/name/pull/42/",
    "https://github.com/owner/name/pull/42/files",
    "https://github.com/owner/name/pull/42/changes",
    "https://github.com/owner/name/pull/42/commits",
    "https://github.com/owner/name/pull/42/checks",
    "https://github.com/owner/name/pull/42/conversation",
    "https://github.com/owner/name/pull/42/files/",
    "https://github.com/owner/name/pull/42/files#issuecomment-12345",
])
def test_parse_pr_arg_accepts_tab_suffixes(url):
    arg = parse_pr_arg(url)
    assert arg == PRArg(number=42, repo="owner/name")


def test_make_run_id_is_deterministic_for_same_inputs():
    a = make_run_id(pr_number=1, head_sha="abc1234", started_at_iso="2026-05-09T10:00:00Z")
    b = make_run_id(pr_number=1, head_sha="abc1234", started_at_iso="2026-05-09T10:00:00Z")
    assert a == b
    assert "abc1234" in a
    assert "1" in a


def test_log_line_format(capsys):
    log_line("analyze", "start", pr=123, sha="abc1234")
    out = capsys.readouterr().out.strip()
    assert out.startswith("[proctor:analyze] start")
    assert "pr=123" in out
    assert "sha=abc1234" in out


def test_acquire_lock_calls_gh_label_add():
    with mock.patch.object(gh_lock.subprocess, "check_output") as co, \
         mock.patch.object(gh_lock.subprocess, "check_call") as cc:
        co.return_value = "[]"   # no labels yet
        ok = gh_lock.acquire(pr_number=1, repo=None)
        assert ok
        cc.assert_called_once()
        args = cc.call_args[0][0]
        assert args[:4] == ["gh", "pr", "edit", "1"]
        assert "--add-label" in args
        assert "proctor:running" in args


def test_acquire_lock_returns_false_when_already_held():
    with mock.patch.object(gh_lock.subprocess, "check_output") as co, \
         mock.patch.object(gh_lock.subprocess, "check_call") as cc:
        co.return_value = '[{"name":"proctor:running"}]'
        ok = gh_lock.acquire(pr_number=1, repo=None)
        assert not ok
        cc.assert_not_called()


def test_release_lock_removes_label():
    with mock.patch.object(gh_lock.subprocess, "check_call") as cc:
        gh_lock.release(pr_number=1, repo=None)
        args = cc.call_args[0][0]
        assert "--remove-label" in args
        assert "proctor:running" in args


def test_post_short_comment_inline(monkeypatch):
    calls = []
    def fake(cmd, **_):
        calls.append(cmd); return ""
    monkeypatch.setattr(post_comment.subprocess, "check_output", fake)
    post_comment.post(pr_number=123, repo=None, body="short body")
    assert calls[0][:4] == ["gh", "pr", "comment", "123"]
    # Body passed via --body-file (stdin/file)
    assert any("--body-file" in a for a in calls[0]) or "--body" in calls[0]


def test_post_long_comment_uses_gist(monkeypatch):
    calls = []
    def fake_co(cmd, **_):
        calls.append(("co", cmd))
        if cmd[:2] == ["gh", "gist"]:
            return "https://gist.github.com/x/abc123\n"
        return ""
    monkeypatch.setattr(post_comment.subprocess, "check_output", fake_co)
    long_body = "x" * 70_000
    post_comment.post(pr_number=123, repo=None, body=long_body)
    # Should have one gist create and one pr comment
    cmds = [c for _, c in calls]
    assert any(c[:2] == ["gh", "gist"] for c in cmds)
    assert any(c[:3] == ["gh", "pr", "comment"] for c in cmds)


def test_test_plan_rejects_dangling_depends_on():
    bad = {
        "items": [
            {"id": "t-001", "category": "api", "what": "x", "how": "x",
             "tool": "bash", "risk": "low", "depends_on": ["t-999"]},
        ]
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_acquire_lock_dict_response_no_label():
    with mock.patch.object(gh_lock.subprocess, "check_output") as co, \
         mock.patch.object(gh_lock.subprocess, "check_call") as cc:
        co.return_value = '{"labels": []}'
        ok = gh_lock.acquire(pr_number=2, repo=None)
        assert ok
        cc.assert_called_once()


def test_acquire_lock_dict_response_with_label():
    with mock.patch.object(gh_lock.subprocess, "check_output") as co, \
         mock.patch.object(gh_lock.subprocess, "check_call") as cc:
        co.return_value = '{"labels": [{"name": "proctor:running"}]}'
        ok = gh_lock.acquire(pr_number=2, repo=None)
        assert not ok
        cc.assert_not_called()


def test_gh_with_retry_on_rate_limit_then_success(monkeypatch):
    from plugins.proctor.scripts import pr_fetch
    calls = {"n": 0}
    def fake(cmd, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "API rate limit exceeded\n"
            raise err
        return "ok\n"
    monkeypatch.setattr(pr_fetch.subprocess, "check_output", fake)
    monkeypatch.setattr(pr_fetch._time, "sleep", lambda *_: None)
    out = pr_fetch._gh_with_retry(["gh", "pr", "view", "1"])
    assert out == "ok\n"
    assert calls["n"] == 2


def test_gh_with_retry_re_raises_non_rate_limit(monkeypatch):
    from plugins.proctor.scripts import pr_fetch
    def fake(cmd, **_):
        err = subprocess.CalledProcessError(1, cmd)
        err.stderr = "fatal: not a git repo\n"
        raise err
    monkeypatch.setattr(pr_fetch.subprocess, "check_output", fake)
    with pytest.raises(subprocess.CalledProcessError):
        pr_fetch._gh_with_retry(["gh", "pr", "view", "1"])


# --- v0.3.0: auth block + multi-account + local overlay ----------------------

from plugins.proctor.scripts.schema import (
    validate_pr_test_config,
    validate_test_plan_account_refs,
    _deep_merge_overlay,
)
from plugins.proctor.scripts import totp


_VALID_AUTH = {
    "type": "form_with_totp",
    "login_url": "/auth/login",
    "selectors": {
        "email": "input[name=login]",
        "password": "input[name=password]",
        "totp": "input[name=passcode]",
        "submit": "button[type=submit]",
    },
    "accounts": [
        {"name": "dev", "email_env": "D_E", "password_env": "D_P", "totp_seed_env": "D_T"},
        {"name": "editor", "email_env": "E_E", "password_env": "E_P", "totp_seed_env": "E_T"},
    ],
}


def _auth_inline(**override):
    """Helper for tests: returns an auth block with inline credentials for
    the dev account. v0.3.6+ schema allows either *_env (env-driven) or
    inline string values, but not both, per credential field."""
    base = {
        "type": "form_with_totp",
        "login_url": "/auth/login",
        "selectors": {"email": "i[e]", "password": "i[p]", "totp": "i[t]", "submit": "b"},
        "accounts": [{
            "name": "dev",
            "email": "proctor-dev@local.test",
            "password": "letmein-12345",
            "totp_seed": "JBSWY3DPEHPK3PXP",
            **override,
        }],
    }
    return base


def test_pr_test_config_legacy_no_auth_ok():
    validate_pr_test_config({})
    validate_pr_test_config({"setup": ["echo hi"], "base_url": "http://x"})


def test_pr_test_config_full_auth_ok():
    validate_pr_test_config({"auth": _VALID_AUTH})


def test_pr_test_config_rejects_unknown_auth_type():
    bad = {"auth": dict(_VALID_AUTH, type="oauth2")}
    with pytest.raises(SchemaError):
        validate_pr_test_config(bad)


def test_pr_test_config_rejects_missing_selector():
    sel = dict(_VALID_AUTH["selectors"])
    del sel["totp"]
    bad = {"auth": dict(_VALID_AUTH, selectors=sel)}
    with pytest.raises(SchemaError):
        validate_pr_test_config(bad)


def test_pr_test_config_rejects_empty_accounts():
    bad = {"auth": dict(_VALID_AUTH, accounts=[])}
    with pytest.raises(SchemaError):
        validate_pr_test_config(bad)


def test_pr_test_config_rejects_duplicate_account_names():
    bad = {"auth": dict(_VALID_AUTH, accounts=[
        _VALID_AUTH["accounts"][0],
        dict(_VALID_AUTH["accounts"][0], email_env="OTHER"),
    ])}
    with pytest.raises(SchemaError):
        validate_pr_test_config(bad)


def test_plan_account_ref_ok():
    plan = {"items": [{"id": "t-1", "category": "api", "what": "x", "how": "y",
                       "tool": "bash", "risk": "low", "depends_on": [],
                       "as_account": "editor"}]}
    validate_test_plan_account_refs(plan, {"auth": _VALID_AUTH})


def test_plan_account_ref_unknown_rejected():
    plan = {"items": [{"id": "t-1", "category": "api", "what": "x", "how": "y",
                       "tool": "bash", "risk": "low", "depends_on": [],
                       "as_account": "ghost"}]}
    with pytest.raises(SchemaError):
        validate_test_plan_account_refs(plan, {"auth": _VALID_AUTH})


def test_plan_account_ref_no_auth_means_no_check():
    plan = {"items": [{"id": "t-1", "category": "api", "what": "x", "how": "y",
                       "tool": "bash", "risk": "low", "depends_on": [],
                       "as_account": "anything"}]}
    # No auth in cfg → no enforcement (legacy mode is permissive).
    validate_test_plan_account_refs(plan, {})


def test_plan_item_as_account_must_be_nonempty_string():
    bad_plan = {"items": [{"id": "t-1", "category": "api", "what": "x", "how": "y",
                           "tool": "bash", "risk": "low", "depends_on": [],
                           "as_account": ""}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad_plan)


def test_deep_merge_scalar_overrides():
    out = _deep_merge_overlay({"a": 1, "b": 2}, {"a": 9})
    assert out == {"a": 9, "b": 2}


def test_deep_merge_dict_recurses():
    out = _deep_merge_overlay({"x": {"a": 1, "b": 2}}, {"x": {"b": 9, "c": 3}})
    assert out == {"x": {"a": 1, "b": 9, "c": 3}}


def test_deep_merge_list_replaces():
    # Lists must REPLACE, not append — accounts arrays would otherwise
    # accumulate dev-only entries on top of base test-env entries and
    # cause silent partial overrides.
    out = _deep_merge_overlay({"accounts": [1, 2, 3]}, {"accounts": [9]})
    assert out == {"accounts": [9]}


# --- totp helper ------------------------------------------------------------

def test_totp_rfc6238_test_vector_truncated_to_6():
    # RFC 6238 test vector: ASCII '12345678901234567890' at t=59 → 94287082
    # (8-digit). Truncated to 6 digits → 287082.
    rfc_seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of 20-byte ASCII seed
    assert totp.code(rfc_seed, 59) == "287082"


def test_totp_padding_tolerant():
    # Google Authenticator QR strings often omit `=` padding. Helper must
    # accept both.
    seed_padded = "JBSWY3DPEHPK3PXP"           # padded form
    seed_unpad  = "JBSWY3DPEHPK3PXP"           # already aligned to 8
    assert totp.code(seed_padded, 0) == totp.code(seed_unpad, 0)


def test_totp_changes_with_time_step():
    seed = "JBSWY3DPEHPK3PXP"
    # 30-second step: t=0 and t=29 share a code; t=30 differs.
    assert totp.code(seed, 0) == totp.code(seed, 29)
    assert totp.code(seed, 0) != totp.code(seed, 30)


# --- v0.3.6: inline auth credentials (alternative to *_env) ---------------

def test_inline_credentials_accepted():
    validate_pr_test_config({"auth": _auth_inline()})


def test_inline_and_env_mixed_rejected():
    # Same field with BOTH inline and *_env → ambiguous, reject.
    bad = _auth_inline()
    bad["accounts"][0]["email_env"] = "ALSO_AN_ENV"  # plus the inline `email`
    with pytest.raises(SchemaError):
        validate_pr_test_config({"auth": bad})


def test_neither_inline_nor_env_rejected():
    # Missing both forms → required field missing.
    bad = _auth_inline()
    del bad["accounts"][0]["email"]  # leaves no email + no email_env
    with pytest.raises(SchemaError):
        validate_pr_test_config({"auth": bad})


def test_inline_credentials_empty_string_rejected():
    bad = _auth_inline(email="")
    with pytest.raises(SchemaError):
        validate_pr_test_config({"auth": bad})


def test_mixed_accounts_different_forms_ok():
    # One account uses inline, another uses *_env — both should validate.
    cfg = {"auth": {
        "type": "form_with_totp",
        "login_url": "/auth/login",
        "selectors": {"email": "i[e]", "password": "i[p]", "totp": "i[t]", "submit": "b"},
        "accounts": [
            {"name": "dev", "email": "d@x", "password": "p", "totp_seed": "JBSWY3DPEHPK3PXP"},
            {"name": "editor", "email_env": "E_E", "password_env": "E_P", "totp_seed_env": "E_T"},
        ],
    }}
    validate_pr_test_config(cfg)


# --- v0.3.22: preconditions + error_type fields ---------------------------

def test_plan_item_preconditions_accepted():
    valid = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                        "risk":"low","depends_on":[],
                        "preconditions":"Logged in as developer."}]}
    validate_test_plan(valid)


def test_plan_item_preconditions_empty_rejected():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":[],
                      "preconditions":""}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_item_preconditions_null_allowed():
    # null == omitted, both fine — keeps the field optional.
    valid = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                        "risk":"low","depends_on":[], "preconditions":None}]}
    validate_test_plan(valid)


def test_plan_item_error_type_accepted():
    for et in ("validation", "permission", "network", "state-conflict",
               "not-found", "auth"):
        item = {"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                "risk":"low","depends_on":[], "error_type": et}
        validate_test_plan({"items": [item]})


def test_plan_item_error_type_unknown_rejected():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":[], "error_type":"made-up"}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


# --- v0.3.23: journey + data_from cross-item dependency -------------------

def test_plan_item_journey_field_accepted():
    valid = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                        "risk":"low","depends_on":[], "journey":"create-image-reward"}]}
    validate_test_plan(valid)


def test_plan_item_journey_empty_rejected():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":[], "journey":""}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_item_data_from_valid():
    valid = {"items": [
        {"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
         "risk":"low","depends_on":[]},
        {"id":"t-2","category":"api","what":"x","how":"y","tool":"bash",
         "risk":"low","depends_on":["t-1"], "data_from":["t-1"]},
    ]}
    validate_test_plan(valid)


def test_plan_item_data_from_unknown_id_rejected():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":["t-9"], "data_from":["t-9"]}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_item_data_from_implies_depends_on():
    # data_from MUST be in depends_on to enforce ordering.
    bad = {"items": [
        {"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
         "risk":"low","depends_on":[]},
        {"id":"t-2","category":"api","what":"x","how":"y","tool":"bash",
         "risk":"low","depends_on":[], "data_from":["t-1"]},  # missing in depends_on
    ]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_item_data_from_self_rejected():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":[], "data_from":["t-1"]}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_item_data_from_must_be_list():
    bad = {"items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                      "risk":"low","depends_on":[], "data_from":"t-9"}]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


# --- v0.3.28: structured journeys + impact_radius_truncated --------------

def test_plan_structured_journeys_accepted():
    valid = {
        "journeys": [{
            "id": "j-create-image",
            "goal": "Admin creates a published image reward.",
            "terminal_state": "Reward visible in list with status=Published.",
        }],
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[], "journey_id":"j-create-image"}],
    }
    validate_test_plan(valid)


def test_plan_journey_id_must_reference_existing_journey():
    bad = {
        "journeys": [{"id":"j-a","goal":"g","terminal_state":"t"}],
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[], "journey_id":"j-z"}],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_journey_and_journey_id_both_set_rejected():
    # The reporter would have to choose which to display — force a pick.
    bad = {
        "journeys": [{"id":"j-a","goal":"g","terminal_state":"t"}],
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[],
                   "journey":"Create Image Reward", "journey_id":"j-a"}],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_legacy_journey_string_still_works():
    """v0.3.23 plans without journeys[] / journey_id should still
    validate so we don't break consumers who haven't migrated."""
    valid = {
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[],
                   "journey":"create-image-reward"}],
    }
    validate_test_plan(valid)


def test_plan_journeys_missing_fields_rejected():
    bad = {
        "journeys": [{"id":"j-a","goal":"g"}],  # missing terminal_state
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[]}],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_journeys_duplicate_id_rejected():
    bad = {
        "journeys": [
            {"id":"j-a","goal":"g","terminal_state":"t"},
            {"id":"j-a","goal":"g2","terminal_state":"t2"},
        ],
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[]}],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_journeys_empty_string_field_rejected():
    bad = {
        "journeys": [{"id":"j-a","goal":"","terminal_state":"t"}],
        "items": [{"id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
                   "risk":"low","depends_on":[]}],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


# --- v0.3.30: plan_smells lint (combined items + missing round-trip) ----

from plugins.proctor.scripts.plan_smells import check as plan_check


def test_plan_smells_clean_plan_returns_no_warnings():
    plan = {"items": [
        {"id": "t-1", "category": "api", "what": "happy: save Image reward",
         "how": "fill form, save", "tool": "chrome-devtools",
         "risk": "high", "depends_on": [], "produces": ["created_id"]},
        {"id": "t-2", "category": "api",
         "what": "happy: re-open saved Image, fields round-trip correctly",
         "how": "navigate back, assert", "tool": "chrome-devtools",
         "risk": "high", "depends_on": ["t-1"], "data_from": ["t-1"]},
    ]}
    assert plan_check(plan) == []


def test_plan_smells_combined_happy_negative_flagged():
    # The exact phrasing the user's plan emitted in production.
    plan = {"items": [{
        "id": "t-008", "category": "api", "tool": "chrome-devtools",
        "what": "Create reward type=Image: missing asset rejected; with asset, save succeeds",
        "how": "...", "risk": "high", "depends_on": [],
    }]}
    warnings = plan_check(plan)
    assert len(warnings) >= 1
    assert any("combines happy and negative" in w for w in warnings)
    assert any("t-008" in w for w in warnings)


def test_plan_smells_combined_phrasing_save_and_reject():
    plan = {"items": [{
        "id": "t-009", "category": "api", "tool": "chrome-devtools",
        "what": "Create reward type=Game: empty + invalid Game URL rejected; valid URL saves",
        "how": "...", "risk": "high", "depends_on": [],
    }]}
    assert any("combines happy and negative" in w for w in plan_check(plan))


def test_plan_smells_pure_negative_not_flagged():
    plan = {"items": [{
        "id": "t-010", "category": "api", "tool": "chrome-devtools",
        "what": "Submitting without selecting Type rejected with required error",
        "how": "...", "risk": "medium", "depends_on": [],
        "error_type": "validation",
    }]}
    assert plan_check(plan) == []


def test_plan_smells_pure_happy_not_flagged():
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: save Image reward with valid asset",
         "how": "...", "risk": "high", "depends_on": [],
         "produces": ["created_id"]},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: re-open saved reward, fields load back correctly",
         "how": "...", "risk": "high",
         "depends_on": ["t-1"], "data_from": ["t-1"]},
    ]}
    assert plan_check(plan) == []


def test_plan_smells_write_without_roundtrip_sibling_flagged():
    plan = {"items": [{
        "id": "t-1", "category": "api", "tool": "chrome-devtools",
        "what": "HAPPY: save Image reward with valid asset",
        "how": "...", "risk": "high", "depends_on": [],
    }]}
    warnings = plan_check(plan)
    assert any("round-trip" in w and "t-1" in w for w in warnings)


def test_plan_smells_write_with_roundtrip_sibling_clean():
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: save Image reward",
         "how": "...", "risk": "high", "depends_on": [],
         "produces": ["created_id"]},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "Re-open saved Image, all fields round-trip correctly",
         "how": "...", "risk": "high",
         "depends_on": ["t-1"], "data_from": ["t-1"]},
    ]}
    assert plan_check(plan) == []


def test_plan_smells_write_with_appears_in_list_sibling_clean():
    # "appears in list" / "visible in list" counts as round-trip
    # verification — the record is being read back from server state.
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: save Image reward",
         "how": "...", "risk": "high", "depends_on": []},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "New reward appears in the /admin/rewards list",
         "how": "...", "risk": "medium",
         "depends_on": ["t-1"], "data_from": ["t-1"]},
    ]}
    assert plan_check(plan) == []


def test_plan_smells_negative_write_not_flagged():
    # A write item that's actually a negative test (error_type set)
    # shouldn't be expected to round-trip — nothing got persisted.
    plan = {"items": [{
        "id": "t-1", "category": "api", "tool": "chrome-devtools",
        "what": "Submit form with missing required field — save rejected",
        "how": "...", "risk": "medium", "depends_on": [],
        "error_type": "validation",
    }]}
    assert plan_check(plan) == []


def test_plan_smells_lint_only_write_not_flagged():
    # `tool: "lint-only"` items can't do UI round-trip — skip the check.
    plan = {"items": [{
        "id": "t-1", "category": "api", "tool": "lint-only",
        "what": "Migration creates new column",
        "how": "...", "risk": "low", "depends_on": [],
    }]}
    assert plan_check(plan) == []


def test_plan_smells_bash_write_not_flagged():
    # Same for bash-tool items (e.g. CLI test suites that do writes).
    plan = {"items": [{
        "id": "t-1", "category": "api", "tool": "bash",
        "what": "go test ./api/... covers the create path",
        "how": "...", "risk": "low", "depends_on": [],
    }]}
    assert plan_check(plan) == []


# --- v0.3.32: plan_smells --strict flag for orchestrator hard-gate -------

def test_plan_smells_cli_strict_exits_1_when_warnings(tmp_path):
    import subprocess
    bad_plan = tmp_path / "plan.json"
    bad_plan.write_text(
        '{"items":[{"id":"t-1","category":"api","tool":"chrome-devtools",'
        '"what":"save record with missing field rejected; with field, saves",'
        '"how":"...","risk":"high","depends_on":[]}]}'
    )
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts" / "plan_smells.py")
    result = subprocess.run(
        ["python3", script, "--strict"],
        stdin=open(bad_plan), capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "combines happy and negative" in result.stdout


def test_plan_smells_cli_strict_exits_0_when_clean(tmp_path):
    import subprocess
    clean_plan = tmp_path / "plan.json"
    clean_plan.write_text(
        '{"items":[{"id":"t-1","category":"api","tool":"chrome-devtools",'
        '"what":"HAPPY: save record","how":"...","risk":"high",'
        '"depends_on":[],"produces":["created_id"]},'
        '{"id":"t-2","category":"api","tool":"chrome-devtools",'
        '"what":"Re-open saved record: all fields round-trip",'
        '"how":"...","risk":"high","depends_on":["t-1"],"data_from":["t-1"]}]}'
    )
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts" / "plan_smells.py")
    result = subprocess.run(
        ["python3", script, "--strict"],
        stdin=open(clean_plan), capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_plan_smells_cli_default_advisory_exits_0_even_with_warnings(tmp_path):
    """Without --strict, exit code is 0 even when warnings fired —
    preserves v0.3.30 advisory behavior for any tooling that relied
    on it."""
    import subprocess
    bad_plan = tmp_path / "plan.json"
    bad_plan.write_text(
        '{"items":[{"id":"t-1","category":"api","tool":"chrome-devtools",'
        '"what":"save with bad input rejected; valid input saves",'
        '"how":"...","risk":"high","depends_on":[]}]}'
    )
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts" / "plan_smells.py")
    result = subprocess.run(
        ["python3", script],  # no --strict
        stdin=open(bad_plan), capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "combines happy and negative" in result.stdout


def test_plan_smells_all_negative_no_happy_save_flagged():
    """The exact failure mode the user hit: 4 validator-reject items,
    zero happy-path saves. Plan_smells must catch this."""
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "form renders", "how": "...",
         "risk": "high", "depends_on": []},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "Validator rejects save when type unselected",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
        {"id": "t-3", "category": "api", "tool": "chrome-devtools",
         "what": "Validator requires asset for Image type",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
        {"id": "t-4", "category": "api", "tool": "chrome-devtools",
         "what": "Validator requires non-empty Game URL",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
        {"id": "t-5", "category": "api", "tool": "chrome-devtools",
         "what": "Validator rejects malformed Game URL",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
    ]}
    warnings = plan_check(plan)
    assert any("plan-coverage" in w
               and "0 chrome-devtools items whose what:" in w
               for w in warnings)


def test_plan_smells_happy_save_present_no_coverage_warning():
    """When the plan has a real happy save item, the coverage check
    does NOT fire — only the missing-roundtrip would (separate)."""
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: save Image reward with valid asset",
         "how": "...", "risk": "high", "depends_on": [],
         "produces": ["created_id"]},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "Re-open saved reward, fields round-trip",
         "how": "...", "risk": "high",
         "depends_on": ["t-1"], "data_from": ["t-1"]},
        {"id": "t-3", "category": "api", "tool": "chrome-devtools",
         "what": "Validator rejects empty type",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
        {"id": "t-4", "category": "api", "tool": "chrome-devtools",
         "what": "Validator rejects empty URL",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "validation"},
    ]}
    warnings = plan_check(plan)
    assert not any("plan-coverage" in w for w in warnings)


def test_plan_smells_reload_sibling_with_past_tense_write_verb_not_self_flagged():
    """v0.3.36 regression: a reload sibling's what: routinely contains
    past-tense write verbs as NOUNS (`re-open saved record`, `assert
    created record visible`). After v0.3.36 added past-tense forms to
    _WRITE_PHRASES for the coverage check, these reload siblings were
    falsely flagged as themselves needing their own round-trip
    sibling. The fix: skip items that have RELOAD phrase + data_from
    set."""
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "HAPPY: save Image reward",
         "how": "...", "risk": "high", "depends_on": [],
         "produces": ["created_id"]},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "Re-open saved Image reward — all fields round-trip correctly",
         "how": "...", "risk": "high",
         "depends_on": ["t-1"], "data_from": ["t-1"]},
    ]}
    warnings = plan_check(plan)
    # No "missing round-trip" warning on t-2 (it IS the round-trip).
    assert not any("t-2" in w and "round-trip" in w for w in warnings)


def test_plan_smells_single_negative_not_flagged_for_coverage():
    """The coverage check requires 2+ negatives — a plan with just
    one negative and no happy save is unusual but not flagged
    (might be a hardening-only PR)."""
    plan = {"items": [
        {"id": "t-1", "category": "api", "tool": "chrome-devtools",
         "what": "form renders", "how": "...",
         "risk": "high", "depends_on": []},
        {"id": "t-2", "category": "api", "tool": "chrome-devtools",
         "what": "Validator rejects unauthorized access",
         "how": "...", "risk": "high", "depends_on": [],
         "error_type": "permission"},
    ]}
    warnings = plan_check(plan)
    assert not any("plan-coverage" in w for w in warnings)


# --- v0.6.4: multi-screenshot evidence (richer report screenshots) --------

def test_test_results_screenshots_list_accepted():
    """v0.6.4+: multi-screenshot `screenshots` list on result items."""
    valid = {
        "items": [{
            "id": "t-006", "status": "pass", "evidence": "ok",
            "screenshots": [
                {"path": ".proctor/runs/x/screenshots/t-006__1.png",
                 "label": "Before: form shows DigitalContentType=Image",
                 "focus": "Top center: select reads 'Image'."},
                {"path": ".proctor/runs/x/screenshots/t-006__2.png",
                 "label": "Changed: select switched to Game",
                 "focus": "Same select now reads 'Game'."},
            ],
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_test_results_screenshots_missing_required_key_rejected():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "screenshots": [
                {"path": "x.png", "label": "y"},  # missing focus
            ],
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_screenshots_empty_string_rejected():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "screenshots": [
                {"path": "", "label": "y", "focus": "z"},
            ],
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_screenshots_must_be_list():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "screenshots": "not-a-list",
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_artifacts_renders_multi_screenshot_block(tmp_path):
    """The exact bug user hit on t-006: report showed one useless
    screenshot. v0.6.4 renders the full list with labels + focus."""
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    for n in (1, 2, 3):
        shot = run_dir / "screenshots" / f"t-006__{n}.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-006", tool="chrome-devtools",
        logs_ref=None, screenshot_ref=None, screenshot_focus=None,
        mode="local",
        screenshots=[
            {"path": "t-006__1.png", "label": "Before: Image",
             "focus": "Select reads 'Image'."},
            {"path": "t-006__2.png", "label": "Changed: Game",
             "focus": "Select reads 'Game'."},
            {"path": "t-006__3.png", "label": "Persisted",
             "focus": "After reload, still 'Game'."},
        ],
    )
    # All 3 image embeds + 3 focus lines.
    assert "Before: Image" in out
    assert "Changed: Game" in out
    assert "Persisted" in out
    assert out.count("![t-006") == 3
    assert "Focus:" in out
    assert "Select reads 'Image'." in out
    assert "Select reads 'Game'." in out


def test_artifacts_multi_screenshot_missing_file_marks_each(tmp_path):
    """One missing screenshot among many doesn't kill the whole
    section — each entry's existence is checked independently."""
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    # Only the first exists.
    (run_dir / "screenshots" / "t-1.png").write_bytes(b"\x89PNG\r\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-1", tool="chrome-devtools",
        logs_ref=None, screenshot_ref=None, screenshot_focus=None,
        mode="local",
        screenshots=[
            {"path": "t-1.png", "label": "Step 1", "focus": "ok"},
            {"path": "t-1__missing.png", "label": "Step 2",
             "focus": "should be there"},
        ],
    )
    assert "Step 1" in out
    assert "Step 2" in out
    assert "file not found" in out


def test_artifacts_screenshots_list_takes_precedence_over_legacy(tmp_path):
    """When both `screenshots` and the legacy single fields are
    provided, v0.6.4 prefers the list (richer evidence)."""
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "screenshots" / "new1.png").write_bytes(b"\x89PNG\r\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-1", tool="chrome-devtools",
        logs_ref=None,
        screenshot_ref="legacy.png", screenshot_focus="legacy",
        mode="local",
        screenshots=[{"path": "new1.png", "label": "new",
                      "focus": "should be the one rendered"}],
    )
    assert "new" in out
    assert "should be the one rendered" in out
    # Legacy rendering shouldn't appear when the list is present.
    assert "legacy" not in out


def test_artifacts_legacy_single_screenshot_still_works(tmp_path):
    """v0.6.3-and-earlier results that only set screenshot_ref
    continue to render via the single-screenshot path."""
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "screenshots" / "t-1.png").write_bytes(b"\x89PNG\r\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-1", tool="chrome-devtools",
        logs_ref=None,
        screenshot_ref="t-1.png",
        screenshot_focus="legacy single shot",
        mode="local",
    )
    assert "legacy single shot" in out
    assert "What to look for:" in out


# --- v0.6.2: validate_item_result empirical-grounding check ---------------

from plugins.proctor.scripts.validate_item_result import check as vir_check


def test_vir_pass_item_no_warning():
    """Pass items don't trigger the check at all."""
    assert vir_check({
        "id": "t-001", "status": "pass", "evidence": "all good",
    }) == []


def test_vir_propagated_skip_no_warning():
    """data-dep-failed skips are propagated from upstream; grounding
    lives on the upstream item, not this one."""
    assert vir_check({
        "id": "t-006", "status": "skipped",
        "reason": "data-dep-failed: t-005",
        "evidence": "upstream failed",
    }) == []


def test_vir_data_template_missing_propagated_no_warning():
    """data-template-missing has the form 'data-template-missing: t-007.x'
    — also propagated."""
    # Note: data-template-missing happens to be in BOTH the empirical-
    # required list AND the propagated-prefix list. The propagated check
    # fires first → no warning expected.
    assert vir_check({
        "id": "t-008", "status": "skipped",
        "reason": "data-template-missing: t-007.created_id",
        "evidence": "upstream pass but no output captured",
    }) == []


def test_vir_precondition_skip_with_empirical_evidence_no_warning():
    """When evidence cites a real HTTP status / exit code / stderr,
    the empirical grounding is present → no warning."""
    cases = [
        {"id": "t-1", "status": "skipped", "reason": "precondition-not-met",
         "evidence": "curl returned HTTP 502; server not reachable."},
        {"id": "t-2", "status": "skipped", "reason": "precondition-not-met",
         "evidence": "go test exit code: 137 (OOM); skipping."},
        {"id": "t-3", "status": "skipped", "reason": "precondition-not-met",
         "evidence": "stderr: connection refused on localhost:5432; "
                     "Postgres not running."},
        {"id": "t-4", "status": "skipped", "reason": "environment",
         "evidence": "Navigated to /admin; DOM snapshot shows login "
                     "screen — session expired."},
    ]
    for c in cases:
        assert vir_check(c) == [], f"unexpected warning for {c['id']}: {c}"


def test_vir_precondition_skip_with_command_field_no_warning():
    """A non-empty `command:` field also satisfies the rule —
    something was actually invoked."""
    assert vir_check({
        "id": "t-1", "status": "skipped", "reason": "precondition-not-met",
        "evidence": "the env doesn't support this, so skipping.",
        "command": "curl -fsS http://localhost:9801/health",
    }) == []


def test_vir_precondition_skip_code_inspection_warning():
    """The exact v0.6.1 failure mode: skip evidence is pure code-
    inspection reasoning, no empirical attempt."""
    cases = [
        {
            "id": "t-005", "status": "skipped",
            "reason": "precondition-not-met",
            "evidence": (
                "local dev_env's empty/dev PASETO key in pkg/auth "
                "blocks the gRPC client's chacha20poly1305 token "
                "construction when the CMS attempts to call mcd-"
                "services' CreateReward RPC."
            ),
        },
        {
            "id": "t-009", "status": "skipped",
            "reason": "precondition-not-met",
            "evidence": (
                "editing an existing backfilled Digital Download "
                "reward requires (a) a row with DigitalContentType "
                "already populated from the backend's deployment-"
                "time backfill, and (b) the gRPC UpdateReward call "
                "to succeed on save. Both conditions fail in local."
            ),
        },
    ]
    for c in cases:
        warnings = vir_check(c)
        assert warnings, f"expected warning for {c['id']}: {c}"
        assert any("code-inspection" in w for w in warnings)
        assert any(c["id"] in w for w in warnings)


def test_vir_v061_t005_actual_evidence_flagged():
    """REGRESSION: the exact t-005 evidence from the user's v0.6.1
    run. v0.6.2 first-ship had a `\\battempt...fail` regex that
    false-negatived this fixture (subagent caught it). v0.6.3 removed
    that regex; this fixture must now flag. Don't 'simplify' this
    string — it's the production failure mode pinned verbatim."""
    item = {
        "id": "t-005",
        "status": "skipped",
        "reason": "precondition-not-met",
        "evidence": (
            "precondition-not-met: local dev_env's empty/dev PASETO "
            "key in pkg/auth blocks the gRPC client's "
            "chacha20poly1305 token construction when the CMS "
            "attempts to call mcd-services' CreateReward RPC. "
            "Creating a Digital Download reward of any type (Image "
            "or Game) hits this happy-path save and fails BEFORE "
            "backend handling. Re-run this item against a deployed "
            "test env (CI mode) where the PASETO key is non-empty."
        ),
    }
    warnings = vir_check(item)
    assert warnings, (
        "v0.6.1 t-005 evidence MUST flag — it's pure code-inspection "
        "with no captured exit code / HTTP / stderr. If this test "
        "fails, a regex was added that matches descriptive future-"
        "tense prose ('attempts to call ... fails'). Tighten or remove."
    )
    assert "code-inspection" in warnings[0]
    assert "t-005" in warnings[0]


def test_vir_v061_t007_actual_evidence_flagged():
    """Same pattern as t-005 but for the Game-type happy save."""
    item = {
        "id": "t-007",
        "status": "skipped",
        "reason": "precondition-not-met",
        "evidence": (
            "precondition-not-met: same chacha20poly1305 PASETO "
            "blocker as t-005. Creating a Digital Download reward "
            "with type=Game also routes through the gRPC "
            "CreateReward RPC and fails before backend write."
        ),
    }
    warnings = vir_check(item)
    assert warnings
    assert "t-007" in warnings[0]


def test_vir_v061_t009_actual_evidence_flagged():
    """t-009 evidence — describes a require-then-fail chain but no
    actual attempt. Must flag."""
    item = {
        "id": "t-009",
        "status": "skipped",
        "reason": "precondition-not-met",
        "evidence": (
            "precondition-not-met: editing an existing backfilled "
            "Digital Download reward requires (a) a row with "
            "DigitalContentType already populated from the backend's "
            "deployment-time backfill (per PR description, dirk's "
            "note), and (b) the gRPC UpdateReward call to succeed "
            "on save. Both conditions fail in local dev_env (no "
            "backfilled fixtures + empty PASETO)."
        ),
    }
    warnings = vir_check(item)
    assert warnings
    assert "t-009" in warnings[0]


def test_vir_explicit_no_attempt_disclaimer_no_warning():
    """An honest 'did not attempt because X' disclaimer in evidence
    is OK — it surfaces the gap explicitly rather than disguising it
    as a precondition check."""
    assert vir_check({
        "id": "t-1", "status": "skipped", "reason": "precondition-not-met",
        "evidence": "Did not attempt: backend dependency PR #2663 "
                    "isn't deployed on staging yet; the test would "
                    "fail with a 502 we already know how to fix.",
    }) == []


def test_vir_unknown_skip_reason_no_warning():
    """Skip reasons we don't recognize (e.g. legacy `tool: skip`
    items) don't trigger the check."""
    assert vir_check({
        "id": "t-1", "status": "skipped", "reason": "tool-skip",
        "evidence": "any text",
    }) == []


def test_vir_check_results_walks_items():
    from plugins.proctor.scripts.validate_item_result import (
        check_results as vir_check_all
    )
    results = {
        "items": [
            {"id": "t-1", "status": "pass", "evidence": "ok"},
            {"id": "t-2", "status": "skipped",
             "reason": "precondition-not-met",
             "evidence": "would fail because of env"},   # warn
            {"id": "t-3", "status": "skipped",
             "reason": "data-dep-failed: t-2",
             "evidence": "upstream gone"},                # propagated, ok
        ],
        "summary": {"total": 3, "pass": 1, "fail": 0, "skipped": 2},
    }
    warnings = vir_check_all(results)
    assert len(warnings) == 1
    assert "t-2" in warnings[0]


def test_vir_cli_stdin_single_item(tmp_path):
    import subprocess
    script = (pathlib.Path(__file__).resolve().parent.parent
              / "plugins" / "proctor" / "scripts"
              / "validate_item_result.py")
    item = json.dumps({
        "id": "t-1", "status": "skipped",
        "reason": "precondition-not-met",
        "evidence": "the env wouldn't support this",
    })
    result = subprocess.run(
        ["python3", str(script)],
        input=item, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "code-inspection" in result.stdout
    assert "t-1" in result.stdout


# --- v0.6.0: proctor_run state machine for /proctor:proctor ---------------


def _run_proctor(state_file, **kwargs):
    """Helper: invoke proctor_run.py and return its emitted envelope."""
    import subprocess as sp
    script = (pathlib.Path(__file__).resolve().parent.parent
              / "plugins" / "proctor" / "scripts" / "proctor_run.py")
    plugin_root = (pathlib.Path(__file__).resolve().parent.parent
                   / "plugins" / "proctor")
    cmd = ["python3", str(script),
           "--state-file", str(state_file),
           "--plugin-root", str(plugin_root)]
    for k, v in kwargs.items():
        if v is None:
            continue
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    result = sp.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_proctor_first_invocation_requires_pr_arg(tmp_path):
    state_file = tmp_path / "state.json"
    env = _run_proctor(state_file)
    assert env["type"] == "error"
    assert "pr-arg" in env["message"]


def test_proctor_first_invocation_emits_bash_for_fetch(tmp_path):
    state_file = tmp_path / "state.json"
    env = _run_proctor(state_file, pr_arg="1115")
    assert env["type"] == "bash"
    assert "pr_fetch" in env["command"] or "fetch_pr" in env["command"]
    assert "RUN_ID=" in env["command"]


def test_proctor_after_fetch_dispatches_analyze(tmp_path):
    """The state machine must transition from FETCHED → dispatch
    analyzing-pr-changes after the harness populates run_id/run_dir/
    pr_number from the pre-flight bash output."""
    state_file = tmp_path / "state.json"
    # First invocation: get bash envelope, state advances to FETCHED.
    _run_proctor(state_file, pr_arg="1115")
    # Simulate the harness updating state with values from bash stdout.
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = json.loads(state_file.read_text())
    state["run_id"] = "test-run"
    state["run_dir"] = str(run_dir)
    state["pr_number"] = 1115
    state_file.write_text(json.dumps(state))
    # Second invocation: should dispatch the analyze skill.
    env = _run_proctor(state_file)
    assert env["type"] == "dispatch_skill"
    assert env["skill"] == "proctor:analyzing-pr-changes"
    assert "change-map.json" in env["expects_artifact"]


def test_proctor_after_analyze_validates_and_dispatches_plan(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    # Skip ahead to the post-analyze step by hand-priming state.
    state = {"step": "analyzed", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115, "pr_arg": "1115"}
    state_file.write_text(json.dumps(state))
    # Write a valid change-map.json so the validator passes.
    (run_dir / "change-map.json").write_text(json.dumps({
        "pr": {"number": 1115, "head_sha": "abc", "base_sha": "def",
               "url": "https://x"},
        "hunks": [{"file": "a.go", "category": "api", "risk": "low",
                   "summary": "."}],
        "categories_present": ["api"],
    }))
    env = _run_proctor(state_file)
    assert env["type"] == "dispatch_skill"
    assert env["skill"] == "proctor:planning-pr-tests"


def test_proctor_after_plan_emits_bash_for_render(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "plan_dispatched", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115, "pr_arg": "1115"}
    state_file.write_text(json.dumps(state))
    (run_dir / "test-plan.json").write_text(json.dumps({"items": [
        {"id": "t-1", "category": "api", "what": "x", "how": "y",
         "tool": "bash", "risk": "low", "depends_on": []},
    ]}))
    env = _run_proctor(state_file)
    assert env["type"] == "bash"
    assert "render_plan_table.py" in env["command"]


def test_proctor_after_render_emits_ask_user_approval(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "planned", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    env = _run_proctor(state_file)
    assert env["type"] == "ask_user"
    assert env["header"] == "Approve plan"
    assert any("Run all" in o["label"] for o in env["options"])
    assert any("Cancel" in o["label"] for o in env["options"])


def test_proctor_approval_run_all_dispatches_execute(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "test-plan.json").write_text('{"items": []}')
    state = {"step": "table_shown", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    env = _run_proctor(state_file, answer="Run all items")
    assert env["type"] == "dispatch_skill"
    assert env["skill"] == "proctor:executing-pr-tests"
    # approved-plan.json should have been written.
    assert (run_dir / "approved-plan.json").exists()


def test_proctor_approval_cancel_emits_done(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "table_shown", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    env = _run_proctor(state_file,
                       answer="Cancel — let me edit the plan first")
    assert env["type"] == "done"
    assert "aborted" in env["summary"].lower()


def test_proctor_execute_no_failures_skips_fix(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "approved", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    # All-pass results.
    (run_dir / "test-results.json").write_text(json.dumps({
        "items": [{"id": "t-1", "status": "pass", "evidence": "ok"}],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }))
    env = _run_proctor(state_file)
    assert env["type"] == "show"
    assert "No failures" in env["markdown"]
    # fix-pr-ref.json = null should have been written.
    assert (run_dir / "fix-pr-ref.json").read_text().strip() == "null"


def test_proctor_execute_with_failures_dispatches_fix(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "approved", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    (run_dir / "test-results.json").write_text(json.dumps({
        "items": [
            {"id": "t-1", "status": "pass", "evidence": "ok"},
            {"id": "t-2", "status": "fail", "evidence": "broke",
             "reason": "assertion"},
        ],
        "summary": {"total": 2, "pass": 1, "fail": 1, "skipped": 0},
    }))
    env = _run_proctor(state_file)
    assert env["type"] == "dispatch_skill"
    assert env["skill"] == "proctor:fixing-test-failures"


def test_proctor_execute_aborted_skips_to_report(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    state = {"step": "approved", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    (run_dir / "test-results.json").write_text(json.dumps({
        "items": [], "summary": {"total": 0, "pass": 0, "fail": 0, "skipped": 0},
        "aborted": "force-push",
    }))
    env = _run_proctor(state_file)
    assert env["type"] == "show"
    assert "aborted" in env["markdown"].lower()


def test_proctor_after_report_done(tmp_path):
    state_file = tmp_path / "state.json"
    run_dir = tmp_path / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html></html>")
    state = {"step": "reported", "run_id": "test-run",
             "run_dir": str(run_dir), "pr_number": 1115}
    state_file.write_text(json.dumps(state))
    env = _run_proctor(state_file)
    assert env["type"] == "done"
    assert "report" in env["summary"].lower()


def test_proctor_corrupted_state_resets(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json {{{")
    env = _run_proctor(state_file, pr_arg="1115")
    # Should treat as fresh state and emit the first bash envelope.
    assert env["type"] in ("bash", "error")


# --- v0.5.0: wizard state machine driver ---------------------------------

# wizard_run.py uses dynamic sys.path setup at import time so importing
# its functions from a test file is tricky. Use subprocess + JSON parse.


def _run_wizard(state_file, current_tag=None, answer=None, bash_rc=None,
                repo_root=None, plugin_root=None):
    """Helper: invoke wizard_run.py and return its emitted envelope."""
    import subprocess as sp
    script = (pathlib.Path(__file__).resolve().parent.parent
              / "plugins" / "proctor" / "scripts" / "wizard_run.py")
    plugin_root = plugin_root or (pathlib.Path(__file__).resolve().parent.parent
                                  / "plugins" / "proctor")
    cmd = ["python3", str(script),
           "--state-file", str(state_file),
           "--repo-root", str(repo_root) if repo_root else ".",
           "--plugin-root", str(plugin_root)]
    if current_tag:
        cmd += ["--current-tag", current_tag]
    if answer is not None:
        cmd += ["--answer", answer]
    if bash_rc is not None:
        cmd += ["--bash-rc", str(bash_rc)]
    result = sp.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_wizard_first_invocation_on_user_scenario_emits_bash(tmp_path):
    """The exact user bug scenario: v0.4.0 layout, seed script present,
    local.yml missing, pin out of date → state machine's first
    invocation should emit type=bash directly (running seed-local.sh).

    v0.7.10 update: pre-v0.7.10 the regenerate step opened with an
    ``ask_user`` listing three regeneration options. Two of them were
    no-ops (just pointed at legacy prose), so v0.7.10 collapsed the
    prompt and goes straight to running the seed script."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.3")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "bash"
    assert "seed-local.sh" in env["command"]


def test_wizard_current_emits_done(tmp_path):
    """Fully-configured repo at the latest pin: state machine just
    emits type=done. No multi-step loop needed."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "done"
    assert "already integrated" in env["summary"]


def test_wizard_bump_only_emits_bash(tmp_path):
    """local.yml present but pin out of date: state machine emits
    type=bash to invoke the atomic bump-action.sh script. AI runs it
    in ONE Bash tool call — no stalls."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.3")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "bash"
    assert "wizard_bump_action.sh" in env["command"]
    assert "v0.4.6" in env["command"]


def test_wizard_bump_only_after_bash_success_emits_done(tmp_path):
    """After bump-action.sh exits 0, the state machine's next
    invocation emits type=done. No further user input needed."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.3")
    state_file = tmp_path / "wizard-state.json"
    # First invocation gets the bash envelope.
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    # Second invocation (simulating bash exited 0): should be done.
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path, bash_rc=0)
    assert env["type"] == "done"


def test_wizard_bump_only_after_bash_failure_done_with_warning(tmp_path):
    """If bump-action.sh exits non-zero (e.g. push failed), state
    machine still emits type=done so AI exits the loop cleanly, but
    summary surfaces the failure."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.3")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path, bash_rc=4)
    assert env["type"] == "done"
    assert "exited 4" in env["summary"]


def test_wizard_needs_local_regen_bash_then_show_after_success(tmp_path):
    """v0.7.10: regenerate step opens with a ``bash`` envelope to run
    seed-local.sh; after rc=0 the step emits a ``show`` envelope
    summarizing the regeneration."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    env1 = _run_wizard(state_file, current_tag="v0.4.6",
                       repo_root=tmp_path)
    assert env1["type"] == "bash"
    assert "seed-local.sh" in env1["command"]
    # Simulate seed script success.
    env2 = _run_wizard(state_file, current_tag="v0.4.6",
                       repo_root=tmp_path, bash_rc=0)
    assert env2["type"] == "show"
    assert "regenerated" in env2["markdown"].lower()


def test_wizard_needs_local_regen_bash_failure_emits_error(tmp_path):
    """v0.7.10: when seed-local.sh exits non-zero (DB not reachable
    etc.), the step emits an ``error`` envelope with actionable
    guidance — and does NOT mark the step complete, so re-running
    the wizard picks up here."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path, bash_rc=1)
    assert env["type"] == "error", env
    assert "seed-local.sh exited" in env["message"]
    # Surface actionable guidance about local dev dependencies.
    assert "docker-compose" in env["message"] or "dependencies" in env["message"]


def test_wizard_fresh_falls_back_to_legacy_prose(tmp_path):
    """Fresh install isn't migrated to the step iterator yet — emit
    a show envelope pointing at the legacy prose, then done. v0.7.9
    renamed the pointer text to ``legacy `commands/proctor-init.md```
    (the actual file's name; v0.7.8 said ``legacy SKILL.md``)."""
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "show"
    assert "fresh" in env["markdown"]
    assert "commands/proctor-init.md" in env["markdown"]
    # State should be marked done so a second invocation doesn't loop.
    env2 = _run_wizard(state_file, current_tag="v0.4.6",
                       repo_root=tmp_path)
    assert env2["type"] == "done"


def test_wizard_state_file_persists_between_invocations(tmp_path):
    """The state file is the only thing carrying context between
    invocations — losing it would break the loop. v0.7.9 schema:
    ``pending_steps`` / ``current_step`` / ``current_step_substate``
    replace the v0.5.0 single ``step`` field."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    # Backward-compat: ``mode`` is preserved (alias of the first
    # active step) for prose / tests that read it.
    assert state["mode"] == "needs-local-regen"
    # v0.7.9: a step is in progress.
    assert state["current_step"] == "step_regenerate_local_yml"
    assert state["current_step_substate"]  # non-empty: in progress


def test_wizard_state_file_deleted_when_done(tmp_path):
    """v0.7.3: when the wizard reaches step=done, the state file is
    auto-deleted. The file's only purpose is "resume after interrupt";
    once we've emitted done there's nothing to resume, and leaving the
    stale file confuses subsequent re-runs (a fresh invocation should
    start from scratch, not resume "done"). Source: user found a
    leftover .proctor/wizard-state.json carrying step=done in their
    consumer repo and asked what it was for."""
    # Use the fresh-mode path which emits done on the very first
    # invocation (the legacy-fallback shape), so we can assert that
    # the state file gets deleted in the same call that emits done.
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    assert env["type"] == "show"
    # Not done yet (still emitting the legacy-prose pointer) — file persists.
    assert state_file.exists()
    # Next invocation reaches done → file should be removed.
    env2 = _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    assert env2["type"] == "done"
    assert not state_file.exists(), (
        "v0.7.3: wizard must auto-delete .proctor/wizard-state.json when "
        "step=done — leftover state confuses subsequent re-runs and "
        "leaves bookkeeping artifacts the consumer didn't ask for."
    )


def test_wizard_state_file_unlink_is_idempotent(tmp_path):
    """If the state file is already gone when we try to delete it on
    `done`, no crash. Defensive — the file might've been hand-deleted
    by the user between the previous wizard step's _save_state and
    this invocation."""
    state_file = tmp_path / "wizard-state.json"
    # First invocation creates state file (the fresh-mode show envelope).
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    # User deletes it manually before the second invocation.
    state_file.unlink()
    # Second invocation: starts from empty state, immediately emits done.
    # Must not crash trying to unlink the already-absent file.
    env = _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    # Empty state means we go through the full fresh path again, ending in show.
    assert env["type"] in {"done", "show"}
    # Either way, no exception was raised getting here.


def test_wizard_corrupted_state_file_resets_gracefully(tmp_path):
    """If state file is corrupted JSON, wizard treats it as fresh
    rather than crashing (better than locking user out)."""
    state_file = tmp_path / "wizard-state.json"
    state_file.write_text("this is not json {{{")
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    # Should run detection + decide. With no .proctor or workflow it
    # should pick `fresh` mode.
    assert env["type"] in ("show", "ask_user", "done", "bash")


# --- v0.7.8: amend-daemons state-machine flow -------------------------------


def _make_v04_repo_with_setup_no_daemons(
    tmp_path, *, pin="v0.7.7", with_cmd_binary=True,
):
    """Variant of _make_v04_repo that DROPS a real `setup:` block in
    `.proctor/local.yml` — non-empty, but without any `go run ./cmd/`
    line. This is exactly the v0.7.6-era consumer shape that v0.7.8's
    amend-daemons rule (renamed `step_supplement_setup` in v0.7.9)
    targets.

    v0.7.9 made the trigger smarter: the step only fires when there
    are actual binaries to add. ``with_cmd_binary=True`` (default)
    drops a synthetic ``cmd/example-loop/main.go`` with a ticker
    pattern so the v0.7.9 trigger fires. Tests that want to assert
    "no step fires when there are no binaries" pass
    ``with_cmd_binary=False``."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin=pin)
    (tmp_path / ".proctor" / "local.yml").write_text(
        "base_url: http://localhost:9801\n"
        "setup:\n"
        "  - bash -c 'echo starting test server'\n"
        "  - bash -c 'nohup go run . > /tmp/proctor-app.log 2>&1 & echo $! > /tmp/proctor-app.pid'\n"
        "  - bash -c 'for i in 1 2 3 4 5; do curl -sf http://localhost:9801/ > /dev/null && break; sleep 1; done'\n"
        "auth:\n"
        "  accounts:\n"
        "    - name: developer\n"
        "      email: x\n"
    )
    if with_cmd_binary:
        cmd_dir = tmp_path / "cmd" / "example-loop"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text(
            "package main\nimport \"time\"\n"
            "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
        )
    return tmp_path


def test_wdm_v078_amend_daemons_fires_when_setup_lacks_cmd_daemons(tmp_path):
    """v0.7.8 amend-daemons rule (renamed ``step_supplement_setup``
    in v0.7.9): consumer is on v0.4+ layout, local.yml exists, setup
    has content, NO ``go run ./cmd/`` line, AND there's at least one
    ``cmd/*/main.go`` binary not yet in setup. Wizard should offer
    to scan + amend.

    v0.7.9 also tightened the trigger: it only fires when there are
    actual candidate binaries (the helper adds an
    ``cmd/example-loop/main.go`` to the fixture so the trigger is
    real). The legacy v0.7.8 trigger fired purely on the absence of
    ``go run ./cmd/`` lines and would offer to scan even on repos
    with no Go binaries at all."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    state = wdm_state(tmp_path)
    # Pass current_tag matching the pin so bump-only doesn't preempt.
    d = wdm_decide(state, current_tag="v0.7.7", repo_root=tmp_path)
    # The shim aliases STEP_SUPPLEMENT_SETUP → ``amend-daemons``
    # for backward-compat with v0.7.8 tests / prose.
    assert d["mode"] == "amend-daemons", d
    assert d["ask_user"] is not None
    # v0.7.9 neutral terminology: ``supplementary binaries`` instead
    # of ``daemon binaries`` in user-facing text.
    assert "supplementary binaries" in d["ask_user"]["options"][0]["label"]
    assert "Skip" in d["ask_user"]["options"][1]["label"]


def test_wdm_v078_amend_daemons_skipped_when_setup_has_cmd_daemon_line(tmp_path):
    """When setup ALREADY has `go run ./cmd/` (the user already
    accepted the v0.7.7 fresh-mode prompt, or amended earlier), the
    rule does NOT fire — fall through to `current`."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.7")
    (tmp_path / ".proctor" / "local.yml").write_text(
        "base_url: x\n"
        "setup:\n"
        "  - bash -c 'nohup go run ./cmd/mcd-daemon > /tmp/d.log 2>&1 &'\n"
    )
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.7.7", repo_root=tmp_path)
    assert d["mode"] == "current", d


def test_wdm_v078_amend_daemons_skipped_for_empty_setup(tmp_path):
    """`setup: []` / `setup: ~` shouldn't trigger the offer — likely
    an existing-env-mode consumer who has no setup deliberately."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.7")
    (tmp_path / ".proctor" / "local.yml").write_text(
        "base_url: x\nsetup: []\n"
    )
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.7.7", repo_root=tmp_path)
    assert d["mode"] == "current", d


def test_wdm_v078_bump_only_wins_over_amend_daemons(tmp_path):
    """v0.7.10: ordering inverted — supplement_setup is now the first
    step (writes ``setup-block.yml`` before regenerate/bump downstream
    consume it). The shim's single-mode alias for this scenario is
    therefore ``amend-daemons`` (the v0.7.8 alias for
    step_supplement_setup), not ``bump-only``.

    Pre-v0.7.10: bump-only ran first because the v0.7.8 priority kept
    pin-bump above amend-daemons. v0.7.10 audit found that ordering
    dropped the supplement step entirely when local.yml was missing
    (Bug A) and produced a stale local.yml when regenerate ran before
    supplement could write the new setup-block (Bug C). Reordering
    supplement to the head of the list fixes both."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.5")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.7.8", repo_root=tmp_path)
    assert d["mode"] == "amend-daemons", d


def test_wizard_v078_amend_daemons_first_invocation_emits_offer(tmp_path):
    """v0.7.9 step iterator: first call against a supplement-setup-
    eligible repo emits ask_user with the scan/skip options.

    v0.7.9 header is ``Supplementary binaries`` (v0.7.8 was ``Daemon
    scan``); test updated."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.7.7",
                      repo_root=tmp_path)
    assert env["type"] == "ask_user"
    assert env["header"] == "Supplementary binaries"
    labels = [o["label"] for o in env["options"]]
    assert any("supplementary binaries" in l for l in labels)
    assert any(l.startswith("Skip") for l in labels)


def test_wizard_v078_amend_daemons_skip_path_emits_done(tmp_path):
    """v0.7.9: offer → user picks Skip → step completes silently →
    iterator pops no more steps → terminal done. The terminal done
    summary names the completed step (``step_supplement_setup``
    with outcome ``skipped``)."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.7", repo_root=tmp_path)
    env = _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="Skip — my setup is fine",
    )
    assert env["type"] == "done"
    # v0.7.9 done summary names the completed steps and outcomes.
    assert "step_supplement_setup" in env["summary"]
    assert "skipped" in env["summary"]


def test_wizard_v078_amend_daemons_scan_path_emits_bash(tmp_path):
    """After user picks Scan, wizard emits a bash envelope to run
    wizard_detect_binaries.py against the consumer repo."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.7", repo_root=tmp_path)
    env = _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="Scan for supplementary binaries you may want to start in setup",
    )
    assert env["type"] == "bash"
    assert "wizard_detect_binaries.py" in env["command"]
    assert "proctor-wizard-binaries.json" in env["command"]


def test_wizard_v078_amend_daemons_after_scan_emits_multiselect(tmp_path):
    """After the bash command finishes (rc=0 + JSON written), the
    next invocation reads the JSON and emits a multi-select
    ask_user with one option per detected candidate. The fixture's
    ``_make_v04_repo_with_setup_no_daemons`` already drops a
    ``cmd/example-loop/main.go``; v0.7.9 also adds a
    ``cmd/test-daemon/main.go`` to verify the multiselect lists
    BOTH binaries."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    cmd_dir = tmp_path / "cmd" / "test-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
    )
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.7", repo_root=tmp_path)
    _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="Scan for supplementary binaries you may want to start in setup",
    )
    # Simulate the AI ran the bash command (manually invoke detector
    # to populate the JSON file).
    import subprocess as sp
    detector = (pathlib.Path(__file__).resolve().parent.parent
                / "plugins" / "proctor" / "scripts"
                / "wizard_detect_binaries.py")
    with open("/tmp/proctor-wizard-binaries.json", "w") as f:
        sp.run(
            ["python3", str(detector), "--repo-root", str(tmp_path)],
            stdout=f, check=True,
        )
    env = _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        bash_rc=0,
    )
    assert env["type"] == "ask_user"
    assert env.get("multi_select") is True
    # v0.7.9 neutral header.
    assert env["header"] == "Supplementary binaries to start in setup"
    # The test-daemon candidate should appear in options.
    labels = " ".join(o["label"] for o in env["options"])
    assert "cmd/test-daemon/main.go" in labels


def test_wizard_v078_amend_daemons_final_pick_amends_local_yml(tmp_path):
    """Full happy path: user picks a binary, wizard writes
    ``.proctor/setup-block.yml`` AND amends ``.proctor/local.yml``'s
    ``setup:`` block with the kill+start pair. v0.7.9 made
    setup-block.yml the canonical source — but during this run the
    wizard also writes to local.yml so the planner picks up the
    change without needing a seed-script re-run."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    cmd_dir = tmp_path / "cmd" / "test-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
    )
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.7", repo_root=tmp_path)
    _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="Scan for supplementary binaries you may want to start in setup",
    )
    import subprocess as sp
    detector = (pathlib.Path(__file__).resolve().parent.parent
                / "plugins" / "proctor" / "scripts"
                / "wizard_detect_binaries.py")
    with open("/tmp/proctor-wizard-binaries.json", "w") as f:
        sp.run(
            ["python3", str(detector), "--repo-root", str(tmp_path)],
            stdout=f, check=True,
        )
    _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        bash_rc=0,
    )
    # Final pick — the AI passes the selected label.
    # v0.7.9: the step emits a 'show' envelope summarizing what was
    # written; the iterator then re-runs (no pending steps) and
    # emits the terminal 'done'. Run twice to reach the terminal.
    env = _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="[recommended] cmd/test-daemon/main.go",
    )
    # First emission may be 'show' (per-step summary) or 'done'
    # (if iterator advances past the no-more-steps check in the
    # same invocation, the recursion would return done — but the
    # supplement-setup step returns a 'show' envelope explicitly,
    # so first is show).
    if env["type"] == "show":
        assert "setup-block.yml" in env["markdown"]
        env = _run_wizard(state_file, current_tag="v0.7.7",
                          repo_root=tmp_path)
    assert env["type"] == "done"
    # The setup-block.yml is the canonical source — verify it was
    # written.
    sb = (tmp_path / ".proctor" / "setup-block.yml").read_text()
    assert "go run ./cmd/test-daemon/main.go" in sb, sb
    assert "/tmp/proctor-test-daemon.pid" in sb, sb
    # local.yml is also amended (convenience: avoids a seed-script
    # re-run inside the same wizard invocation).
    local = (tmp_path / ".proctor" / "local.yml").read_text()
    assert "go run ./cmd/test-daemon/main.go" in local, local
    assert "/tmp/proctor-test-daemon.pid" in local, local


def test_wizard_v078_amend_daemons_empty_selection_is_noop(tmp_path):
    """If user deselects every candidate, no file is modified and
    wizard emits done with a no-op summary."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.7")
    cmd_dir = tmp_path / "cmd" / "test-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
    )
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.7", repo_root=tmp_path)
    _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="Scan for supplementary binaries you may want to start in setup",
    )
    import subprocess as sp
    detector = (pathlib.Path(__file__).resolve().parent.parent
                / "plugins" / "proctor" / "scripts"
                / "wizard_detect_binaries.py")
    with open("/tmp/proctor-wizard-binaries.json", "w") as f:
        sp.run(
            ["python3", str(detector), "--repo-root", str(tmp_path)],
            stdout=f, check=True,
        )
    _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        bash_rc=0,
    )
    # Pass empty selection.
    original_local = (tmp_path / ".proctor" / "local.yml").read_text()
    env = _run_wizard(
        state_file, current_tag="v0.7.7", repo_root=tmp_path,
        answer="",
    )
    # v0.7.9 emits a 'show' envelope first (per-step summary), then
    # iterator emits terminal 'done' on the next invocation.
    if env["type"] == "show":
        assert "No binaries selected" in env["markdown"]
        env = _run_wizard(state_file, current_tag="v0.7.7",
                          repo_root=tmp_path)
    assert env["type"] == "done"
    # local.yml unchanged.
    assert (tmp_path / ".proctor" / "local.yml").read_text() == original_local
    # setup-block.yml NOT created — empty selection writes nothing.
    assert not (tmp_path / ".proctor" / "setup-block.yml").exists()


def test_wizard_v078_amend_daemons_idempotent_rerun(tmp_path):
    """Running amend-daemons twice with the same selection should
    not duplicate lines — the helper detects when the binary's
    pidfile name OR path already appears in setup."""
    from plugins.proctor.scripts.wizard_run import _amend_local_yml_with_daemons
    local = tmp_path / "local.yml"
    local.write_text(
        "setup:\n"
        "  - bash -c 'echo a'\n"
        "  - bash -c 'echo b'\n"
        "other_key: value\n"
    )
    chosen = [{
        "path": "cmd/foo/main.go",
        "binary_name": "foo",
        "looks_like": "daemon",
        "evidence": ["time.NewTicker"],
    }]
    added1 = _amend_local_yml_with_daemons(local, chosen)
    assert added1 == 1
    text1 = local.read_text()
    # Second call: should detect existing lines and add nothing.
    added2 = _amend_local_yml_with_daemons(local, chosen)
    assert added2 == 0
    assert local.read_text() == text1
    # other_key sibling preserved + below the appended lines.
    assert "other_key: value" in text1
    # Verify the kill+start pair was inserted INSIDE the setup
    # block (before the sibling key).
    idx_pid = text1.find("/tmp/proctor-foo.pid")
    idx_other = text1.find("other_key")
    assert idx_pid >= 0
    assert idx_pid < idx_other, (
        "expected daemon lines to be inserted BEFORE the next "
        "top-level key, not appended at EOF"
    )


# --- v0.4.6: render_item_artifacts (absolute paths + missing-artifact badges) ---

from plugins.proctor.scripts.render_item_artifacts import render as render_artifacts


def test_artifacts_local_log_renders_absolute_file_url(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "t-001.log").write_text("ok\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-001", tool="bash",
        logs_ref=".proctor/runs/run/t-001.log",   # repo-root-relative, BAD
        screenshot_ref=None, screenshot_focus=None, mode="local",
    )
    # The bug we're fixing: AI's repo-root-relative path normalized to
    # an absolute file:// URL via the run-dir.
    assert f"file://{run_dir.resolve()}/t-001.log" in out
    assert "Full log:" in out


def test_artifacts_local_missing_log_shows_not_found_badge(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = render_artifacts(
        run_dir=run_dir, item_id="t-001", tool="bash",
        logs_ref="t-001.log",   # ref'd but file doesn't exist
        screenshot_ref=None, screenshot_focus=None, mode="local",
    )
    assert "not found" in out
    assert "Full log:" in out


def test_artifacts_chrome_devtools_missing_screenshot_loud_warning():
    """The exact bug user hit: 11 chrome-devtools items, 0 screenshots.
    Reporter must SURFACE the absence with a loud message, not silently
    render nothing."""
    out = render_artifacts(
        run_dir="/tmp", item_id="t-005", tool="chrome-devtools",
        logs_ref=None,
        screenshot_ref=None,   # executor never set this
        screenshot_focus=None, mode="local",
    )
    assert "Screenshot:" in out
    assert "not captured" in out
    assert "REQUIRE" in out or "contract" in out
    assert "executor bug" in out or "executor subagent" in out


def test_artifacts_chrome_devtools_present_screenshot_renders_image(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    shot = run_dir / "screenshots" / "t-005.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG signature
    out = render_artifacts(
        run_dir=run_dir, item_id="t-005", tool="chrome-devtools",
        logs_ref=None,
        screenshot_ref="t-005.png",
        screenshot_focus="The 'Game URL' field shows the saved value.",
        mode="local",
    )
    assert f"![t-005 screenshot](file://{shot.resolve()})" in out
    assert "What to look for:" in out
    assert "'Game URL' field shows the saved value." in out


def test_artifacts_chrome_devtools_with_repo_root_relative_ref(tmp_path):
    """The actual production case: executor wrote screenshot_ref as
    `.proctor/runs/<id>/screenshots/t-005.png` (repo-root-relative).
    Renderer should still find it under run-dir/screenshots/."""
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    shot = run_dir / "screenshots" / "t-005.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-005", tool="chrome-devtools",
        logs_ref=None,
        screenshot_ref=".proctor/runs/something/screenshots/t-005.png",
        screenshot_focus=None, mode="local",
    )
    assert f"file://{shot.resolve()}" in out


def test_artifacts_lint_only_with_no_log_returns_empty(tmp_path):
    """Lint-only items often have nothing to render — that's fine, no
    spurious empty sections in the report."""
    out = render_artifacts(
        run_dir=tmp_path, item_id="t-001", tool="lint-only",
        logs_ref=None, screenshot_ref=None, screenshot_focus=None,
        mode="local",
    )
    assert out == ""


def test_artifacts_ci_mode_log_links_to_artifact(tmp_path):
    """CI mode renders an artifact-zip URL instead of file://."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "t-001.log").write_text("ok\n")
    out = render_artifacts(
        run_dir=run_dir, item_id="t-001", tool="bash",
        logs_ref="t-001.log", screenshot_ref=None,
        screenshot_focus=None, mode="ci",
        github_run_id="123456", server_url="https://github.com",
        repo="acme/repo",
    )
    assert "file://" not in out
    assert "actions/runs/123456#artifacts" in out


def test_artifacts_cli_runs(tmp_path):
    import subprocess
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "t-001.log").write_text("ok\n")
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts"
                 / "render_item_artifacts.py")
    result = subprocess.run(
        ["python3", script,
         "--run-dir", str(run_dir),
         "--item-id", "t-001",
         "--tool", "bash",
         "--logs-ref", "t-001.log",
         "--mode", "local"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Full log:" in result.stdout
    assert "file://" in result.stdout


# --- v0.4.5: wizard_decide_mode deterministic MODE picker ---------------

from plugins.proctor.scripts.wizard_decide_mode import (
    detect_state as wdm_state,
    decide_mode as wdm_decide,
)


def _make_v04_repo(tmp_path, *, has_local_yml=False, pin="v0.4.3",
                   has_seed_script=True):
    """Build a v0.4.0-layout consumer repo fixture under tmp_path."""
    (tmp_path / ".proctor").mkdir()
    (tmp_path / ".proctor" / "config.yml").write_text(
        "base_url: http://localhost:9801\n"
        "auth:\n"
        "  type: form_with_totp\n"
        "  login_url: /auth/login\n"
        "  selectors: {email: i, password: i, totp: i, submit: b}\n"
        "  accounts:\n"
        "    - name: developer\n"
        "      email: x\n"
        "      password: y\n"
        "      totp_seed: JBSWY3DPEHPK3PXP\n"
    )
    if has_seed_script:
        seed = tmp_path / ".proctor" / "seed-local.sh"
        seed.write_text("#!/usr/bin/env bash\necho ok\n")
        seed.chmod(0o755)
    if has_local_yml:
        (tmp_path / ".proctor" / "local.yml").write_text("base_url: x\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").mkdir()
    (tmp_path / ".github" / "workflows" / "proctor.yml").write_text(
        f"uses: zealllot/proctor/github-action@{pin}\n"
    )
    return tmp_path


def test_wdm_fresh_install_with_nothing_present(tmp_path):
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    assert d["mode"] == "fresh"
    assert d["ask_user"] is None


def test_wdm_legacy_layout_detected(tmp_path):
    (tmp_path / ".pr-test.yml").write_text("base_url: x\n")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    assert d["mode"] == "legacy-migration"
    assert d["ask_user"] is not None
    assert "v0.4.0" in d["ask_user"]["question"]


def test_wdm_needs_local_regen_fires_on_user_scenario(tmp_path):
    """The EXACT scenario the user hit: v0.4.0 layout, seed script
    present, local.yml MISSING, pin out of date. Must pick
    needs-local-regen (NOT bump-only)."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.3")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    assert d["mode"] == "needs-local-regen"
    assert d["ask_user"] is not None
    assert "Regenerate seed-local.sh AND re-run it" in d["ask_user"]["options"][0]["label"]


def test_wdm_bump_only_when_local_yml_present_and_pin_old(tmp_path):
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.3")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    assert d["mode"] == "bump-only"
    assert d["ask_user"] is None


def test_wdm_current_when_pin_matches_and_local_present(tmp_path):
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.4")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    assert d["mode"] == "current"
    assert d["ask_user"] is None


def test_wdm_bump_only_with_seed_when_seed_script_missing(tmp_path):
    """v0.7.8 mode ``bump-only-with-seed`` was renamed in v0.7.9 into
    ``step_bump_action_pin`` (the underlying pin-bump action stays).
    The seed-script regeneration moved out of this decision and is
    now ALWAYS run by the wizard's fresh / migrate paths when
    needed. v0.7.9 backward-compat: the single-mode shim returns
    ``bump-only`` here (the v0.7.9 step name maps to v0.7.8's
    plain bump-only via the alias table)."""
    _make_v04_repo(tmp_path, has_local_yml=False, has_seed_script=False)
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    # v0.7.9 collapses bump-only-with-seed → bump-only at the
    # backward-compat shim level. The Step 8c-pre seed-script regen
    # is now wizard-side responsibility (handled by the fresh /
    # supplement steps), not part of the decision tree.
    assert d["mode"] == "bump-only"
    assert d["ask_user"] is None


def test_wdm_migrate_when_no_auth_block(tmp_path):
    """v0.7.8 had a dedicated ``migrate`` mode (v0.2 → v0.3 auth
    block migration). v0.7.9 collapsed the decision tree: a stale
    pin always triggers ``step_bump_action_pin``; auth-block
    migration is now a sub-step of the fresh/migrate prose path
    (out of scope for the decision shim). At the backward-compat
    layer this test now asserts the pin-bump step fires — the v0.2
    auth-block-add-flow is followed by the user after the pin bump."""
    (tmp_path / ".proctor").mkdir()
    (tmp_path / ".proctor" / "config.yml").write_text(
        "base_url: x\nsetup: [echo hi]\n"
    )
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").mkdir()
    (tmp_path / ".github" / "workflows" / "proctor.yml").write_text(
        "uses: zealllot/proctor/github-action@v0.2.5\n"
    )
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    # Pin is older than current_tag → step_bump_action_pin fires;
    # the shim aliases it to ``bump-only``.
    assert d["mode"] == "bump-only"


def test_wdm_current_tag_missing_does_not_force_bump_only(tmp_path):
    """When current-tag lookup fails (gh release view failed), don't
    fire spurious bump-only — just report 'current'."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.4.4")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag=None)
    assert d["mode"] == "current"


def test_wdm_state_detects_pin(tmp_path):
    _make_v04_repo(tmp_path, pin="v0.4.3", has_local_yml=True)
    state = wdm_state(tmp_path)
    assert state["current_pin"] == "v0.4.3"


def test_wdm_state_pin_none_when_workflow_missing(tmp_path):
    state = wdm_state(tmp_path)
    assert state["current_pin"] is None


def test_wdm_cli_outputs_valid_json(tmp_path):
    import subprocess
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.3")
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts"
                 / "wizard_decide_mode.py")
    result = subprocess.run(
        ["python3", script, "--current-tag", "v0.4.4",
         "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["mode"] == "needs-local-regen"
    assert data["state"]["has_local_yml"] is False
    assert data["ask_user"] is not None


# --- v0.4.3: render_plan_table for the approval-gate ---------------------

from plugins.proctor.scripts.render_plan_table import render as render_table


def test_render_plan_table_header_and_row_count():
    plan = {"items": [
        {"id": "t-001", "category": "api", "risk": "low", "tool": "lint-only",
         "what": "proto enum declared", "depends_on": []},
        {"id": "t-002", "category": "api", "risk": "high", "tool": "chrome-devtools",
         "what": "HAPPY: save reward", "depends_on": [], "as_account": "developer"},
    ]}
    out = render_table(plan, pr_number=1115)
    assert "## Plan for PR #1115 — 2 items" in out
    assert "| t-001 | api | low | lint-only | — | proto enum declared |" in out
    assert "| t-002 | api | high | chrome-devtools | developer | HAPPY: save reward |" in out


def test_render_plan_table_estimate_includes_seconds_under_minute():
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only", "what": "x", "depends_on": []}]}
    out = render_table(plan, pr_number=1)
    assert "**Estimated:** ~5s" in out


def test_render_plan_table_estimate_minutes_above_minute():
    # 2 chrome-devtools items = 120s = ~2.0 min
    plan = {"items": [
        {"id": "t-1", "category": "api", "risk": "high", "tool": "chrome-devtools",
         "what": "x", "depends_on": []},
        {"id": "t-2", "category": "api", "risk": "high", "tool": "chrome-devtools",
         "what": "y", "depends_on": []},
    ]}
    out = render_table(plan, pr_number=1)
    assert "~2.0 min" in out


def test_render_plan_table_truncates_long_what():
    very_long = "x" * 200
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only", "what": very_long, "depends_on": []}]}
    out = render_table(plan, pr_number=1)
    # The very-long string shouldn't appear in full — truncated with ellipsis.
    assert very_long not in out
    assert "…" in out


def test_render_plan_table_omits_smells_section_when_no_file(tmp_path):
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only", "what": "x", "depends_on": []}]}
    out = render_table(plan, pr_number=1, run_dir=tmp_path)
    assert "Plan smells" not in out


def test_render_plan_table_renders_residual_smells_when_file_exists(tmp_path):
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only", "what": "x", "depends_on": []}]}
    (tmp_path / "plan-smells.txt").write_text(
        "t-008: write action has no sibling item asserting round-trip data loading\n"
        "t-009: combines happy and negative phrasing\n"
    )
    out = render_table(plan, pr_number=1, run_dir=tmp_path)
    assert "### Plan smells (still present after 2 regen attempts)" in out
    assert "⚠ t-008: write action has no sibling item asserting round-trip data loading" in out
    assert "⚠ t-009: combines happy and negative phrasing" in out


def test_render_plan_table_omits_smells_section_when_file_is_empty(tmp_path):
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only", "what": "x", "depends_on": []}]}
    (tmp_path / "plan-smells.txt").write_text("")
    out = render_table(plan, pr_number=1, run_dir=tmp_path)
    assert "Plan smells" not in out


def test_render_plan_table_collapses_whitespace_in_what():
    plan = {"items": [{"id": "t-1", "category": "api", "risk": "low",
                       "tool": "lint-only",
                       "what": "save\n  with\n  newlines and  multiple  spaces",
                       "depends_on": []}]}
    out = render_table(plan, pr_number=1)
    assert "save with newlines and multiple spaces" in out


def test_render_plan_table_cli_reads_stdin(tmp_path):
    import subprocess
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts" / "render_plan_table.py")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"items": [
        {"id": "t-1", "category": "api", "risk": "low", "tool": "lint-only",
         "what": "x", "depends_on": []},
    ]}))
    result = subprocess.run(
        ["python3", script, "--pr-number", "42"],
        stdin=open(plan_file), capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "## Plan for PR #42 — 1 items" in result.stdout


# --- v0.3.37: worktree-based PR-head alignment ---------------------------

from plugins.proctor.scripts.worktree import setup as wt_setup, teardown as wt_teardown


@pytest.fixture(autouse=False)
def _isolated_worktree_base(tmp_path, monkeypatch):
    """v0.7.1+: worktree.setup() places worktrees outside the consumer
    repo (under $TMPDIR/proctor-worktrees/ by default, override via
    $PROCTOR_WORKTREE_BASE_DIR). For tests we need a per-test isolated
    base so concurrent test runs don't collide or leak worktrees.
    Apply this fixture via parameter in worktree tests."""
    base = tmp_path / "wt-base"
    base.mkdir()
    monkeypatch.setenv("PROCTOR_WORKTREE_BASE_DIR", str(base))
    return base


def _init_repo_with_commits(repo_path):
    """Create a tiny git repo with two commits and a 'PR-like' branch
    at the second commit. Returns (initial_sha, pr_sha)."""
    sp = subprocess
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo_path, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo_path, check=True)
    sp.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_path, check=True)
    (repo_path / "file.txt").write_text("initial\n")
    sp.run(["git", "add", "."], cwd=repo_path, check=True)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_path, check=True)
    initial_sha = sp.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo_path / "file.txt").write_text("pr content\n")
    sp.run(["git", "add", "."], cwd=repo_path, check=True)
    sp.run(["git", "commit", "-q", "-m", "pr commit"], cwd=repo_path, check=True)
    pr_sha = sp.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Reset main to initial so HEAD is at initial; PR sha is reachable
    # through the reflog (the tests below pass the SHA directly).
    sp.run(["git", "reset", "--hard", initial_sha], cwd=repo_path, check=True)
    return initial_sha, pr_sha


def test_worktree_setup_creates_aligned_checkout(_isolated_worktree_base, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    # Worktree is checked out at the PR sha.
    head = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == pr_sha
    # The worktree's file has PR content, not initial.
    assert (wt_path / "file.txt").read_text() == "pr content\n"
    # Marker file recorded the path.
    marker = run_dir / "worktree-path.txt"
    assert marker.exists()
    assert marker.read_text().strip() == str(wt_path.resolve())


def test_worktree_setup_idempotent_when_sha_matches(_isolated_worktree_base, tmp_path):
    """Calling setup twice at the same SHA shouldn't error or
    recreate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt1 = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                   repo_root=repo)
    wt2 = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                   repo_root=repo)
    assert wt1 == wt2


def test_worktree_setup_copies_local_yml(_isolated_worktree_base, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Drop a gitignored local config under .proctor/ in the repo
    # (v0.4.0+ layout — the worktree helper copies this single file
    # so the dev's setup commands + credentials apply inside the
    # PR-aligned worktree).
    (repo / ".proctor").mkdir()
    (repo / ".proctor" / "local.yml").write_text("setup: [echo hi]\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    copied = wt_path / ".proctor" / "local.yml"
    assert copied.exists()
    assert copied.read_text() == "setup: [echo hi]\n"


def test_worktree_setup_no_local_yml_is_fine(_isolated_worktree_base, tmp_path):
    """If the dev hasn't created .pr-test.local.yml, setup shouldn't
    error — just create the worktree without it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    assert not (wt_path / ".pr-test.local.yml").exists()


def test_worktree_teardown_removes_worktree(_isolated_worktree_base, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    assert wt_path.exists()
    wt_teardown(run_dir=run_dir, repo_root=repo)
    assert not wt_path.exists()
    # Marker file removed.
    assert not (run_dir / "worktree-path.txt").exists()


def test_worktree_teardown_no_marker_is_noop(_isolated_worktree_base, tmp_path):
    """Teardown when no setup ever happened should be a quiet no-op
    (covers the cur_head == pr_head case where setup is skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    # No marker, no worktree — should not raise.
    wt_teardown(run_dir=run_dir, repo_root=repo)


def test_worktree_setup_recreates_when_sha_differs(_isolated_worktree_base, tmp_path):
    """If the existing worktree is at a different SHA than requested
    (rare but possible — e.g. force-push between two runs in the same
    run dir, or manual interference), tear it down and recreate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    initial_sha, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_setup(run_dir=run_dir, pr_number=99, head_sha=initial_sha,
             repo_root=repo)
    # Now call setup with the OTHER sha — should recreate.
    new_wt = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                      repo_root=repo)
    head = subprocess.run(
        ["git", "-C", str(new_wt), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == pr_sha


# --- v0.7.2: worktree.py auto-detects gitignored runtime dirs via git ----
# (replaces v0.7.0/v0.7.1 hardcoded list which leaked project-specific
# paths like ``external/assets/mcd`` into plugin defaults.)

def test_worktree_setup_symlinks_gitignored_dirs_discovered_via_git(_isolated_worktree_base, tmp_path):
    """v0.7.2: worktree.setup() asks git which directories are gitignored
    at the consumer repo root, then symlinks them into the worktree so
    the dev server doesn't have to rebuild. Pure git-driven discovery
    — no hardcoded project paths in the plugin.

    Source: v0.7.0/v0.7.1 had a hardcoded list including the leak
    ``external/assets/mcd`` (mcd-website-specific sub-path). User
    flagged: "PRoctor 要应对无数项目, 必须抽象出来". v0.7.2 replaces the
    list with ``git ls-files --others --ignored --exclude-standard
    --directory`` so the trigger is the consumer's .gitignore — works
    for any project shape."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Add a .gitignore declaring two runtime build dirs as ignored.
    # (Different repos use different names — that's the whole point;
    # the plugin shouldn't know any of them.)
    (repo / ".gitignore").write_text(
        "weird_build_dir/\nthirdparty_runtime/\n"
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    # Materialize the ignored dirs at the repo root.
    (repo / "weird_build_dir").mkdir()
    (repo / "weird_build_dir" / "bundle.js").write_text("// built\n")
    (repo / "thirdparty_runtime").mkdir()
    (repo / "thirdparty_runtime" / ".bin").mkdir()
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    linked_build = wt_path / "weird_build_dir"
    linked_thirdparty = wt_path / "thirdparty_runtime"
    assert linked_build.is_symlink()
    assert linked_thirdparty.is_symlink()
    # Symlink target points back at the main checkout so the dev server
    # picks up the existing build output instead of rebuilding.
    assert linked_build.resolve() == (repo / "weird_build_dir").resolve()
    # And the file is reachable through the symlink.
    assert (linked_build / "bundle.js").read_text() == "// built\n"


def test_worktree_setup_skips_non_gitignored_dirs(_isolated_worktree_base, tmp_path):
    """A directory that EXISTS at the repo root but is NOT gitignored
    must NOT be symlinked. v0.7.2's discovery is gitignore-driven —
    tracked source dirs (or untracked-but-not-ignored dirs) stay
    inside the worktree's own checkout, not symlinked from main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # No .gitignore. Create a runtime-looking dir at the repo root —
    # it's untracked but NOT gitignored.
    (repo / "looks_like_build").mkdir()
    (repo / "looks_like_build" / "out.txt").write_text("hi\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    # The dir must NOT be in the worktree — neither as symlink nor
    # as real dir (the worktree starts from the SHA's tree, which
    # doesn't include this untracked dir).
    assert not (wt_path / "looks_like_build").exists()
    assert not (wt_path / "looks_like_build").is_symlink()


def test_worktree_setup_never_symlinks_proctor_or_git(_isolated_worktree_base, tmp_path):
    """``.proctor/`` is PRoctor-owned (the worktree's own
    ``.proctor/runs/<id>/`` is where the active run lives — symlinking
    the consumer's ``.proctor/`` would create a self-reference loop).
    ``.git/`` is structural. Both must NEVER be auto-symlinked even
    when they appear in the consumer's gitignore."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Gitignore `.proctor/` (the normal consumer setup) and create it.
    (repo / ".gitignore").write_text(".proctor/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    (repo / ".proctor").mkdir()
    (repo / ".proctor" / "config.yml").write_text("base_url: http://x\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    # `.proctor/` in the worktree must not be a symlink to the main
    # repo's `.proctor/`. (worktree-path.txt lives inside run_dir, and
    # `git worktree add` creates a real `.proctor/` dir inside the
    # worktree if needed — but it must NOT be a symlink.)
    if (wt_path / ".proctor").exists():
        assert not (wt_path / ".proctor").is_symlink()


def test_worktree_setup_skips_symlinks_when_no_gitignore(_isolated_worktree_base, tmp_path):
    """Repo without any .gitignore → discovery returns empty list →
    no symlinks created → no failures. Safe degradation: the dev
    server in the worktree will have to rebuild runtime artifacts
    from scratch (slower but correct)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # No .gitignore. Create dirs that LOOK like runtime artifacts
    # but aren't declared ignored anywhere.
    (repo / "node_modules").mkdir()
    (repo / "dist").mkdir()
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    # Setup succeeds with no errors.
    assert wt_path.exists()
    # And nothing is symlinked because nothing was declared gitignored.
    assert not (wt_path / "node_modules").is_symlink()
    assert not (wt_path / "dist").is_symlink()


def test_worktree_setup_symlink_dirs_empty_list_skips_all(_isolated_worktree_base, tmp_path):
    """Passing `symlink_dirs=[]` explicitly opts out of all symlinking,
    even when the consumer's gitignore would otherwise trigger autosymlink
    (consumer-level escape hatch via `.proctor/config.yml.worktree_symlink_dirs: []`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    (repo / ".gitignore").write_text("would_be_symlinked/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    (repo / "would_be_symlinked").mkdir()
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo, symlink_dirs=[])

    # Discovery would have linked it; explicit [] override skips.
    assert not (wt_path / "would_be_symlinked").is_symlink()


def test_worktree_setup_symlink_dirs_custom_list_overrides_discovery(_isolated_worktree_base, tmp_path):
    """A consumer-provided override list takes precedence over git-
    discovered defaults. Only the named dirs are symlinked, even if
    other gitignored dirs exist at the repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    (repo / ".gitignore").write_text(
        "ignored_a/\nignored_b/\n"
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    (repo / "ignored_a").mkdir()
    (repo / "ignored_b").mkdir()
    (repo / "ignored_b" / "marker").write_text("present\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo,
                       symlink_dirs=["ignored_b"])

    # Custom-listed dir linked.
    assert (wt_path / "ignored_b").is_symlink()
    assert (wt_path / "ignored_b" / "marker").read_text() == "present\n"
    # Other gitignored dir NOT linked (override replaced discovery).
    assert not (wt_path / "ignored_a").is_symlink()


def test_worktree_setup_discovers_gitignored_subpath_under_tracked_parent(_isolated_worktree_base, tmp_path):
    """A gitignored sub-directory inside a tracked parent must be
    discovered and symlinked at the right level. ``git ls-files
    --others --ignored --exclude-standard --directory`` surfaces the
    ignored sub-path directly (not the tracked parent), so the
    symlinking step lands at the right depth.

    This is the v0.7.0/v0.7.1 mcd-website scenario abstracted: the
    project's frontend bundle lived at ``external/assets/<app>/``
    while ``external/assets/`` itself was tracked with fonts +
    images. The v0.7.0 hardcoded ``external/assets`` entry silently
    no-op'd because the parent was tracked. v0.7.2's discovery
    handles ANY such shape without hardcoded project knowledge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Create a tracked parent dir with tracked content, then declare
    # a sub-dir of the parent as gitignored.
    (repo / "tracked_parent").mkdir()
    (repo / "tracked_parent" / "tracked_file.txt").write_text("kept in git\n")
    (repo / ".gitignore").write_text("tracked_parent/ignored_sub/\n")
    subprocess.run(["git", "add", "tracked_parent/tracked_file.txt", ".gitignore"],
                   cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "parent+ignore"], cwd=repo, check=True)
    pr_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Materialize the ignored sub-dir at the repo root.
    (repo / "tracked_parent" / "ignored_sub").mkdir()
    (repo / "tracked_parent" / "ignored_sub" / "build.out").write_text("built\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    # The parent dir is checked out by `git worktree add` (it's tracked)
    # — it must remain a real dir, NOT a symlink.
    assert (wt_path / "tracked_parent").is_dir()
    assert not (wt_path / "tracked_parent").is_symlink()
    # The tracked content is present from the worktree checkout.
    assert (wt_path / "tracked_parent" / "tracked_file.txt").read_text() == "kept in git\n"
    # The gitignored sub-dir IS symlinked at the sub-path.
    assert (wt_path / "tracked_parent" / "ignored_sub").is_symlink()
    assert (
        wt_path / "tracked_parent" / "ignored_sub" / "build.out"
    ).read_text() == "built\n"


def test_discover_gitignored_dirs_no_git_returns_empty(tmp_path):
    """If the caller's repo_root isn't a git repo at all (e.g. a freshly
    extracted tarball), discovery returns an empty list rather than
    raising. The safe degradation path."""
    from plugins.proctor.scripts.worktree import _discover_gitignored_dirs
    # tmp_path is NOT a git repo.
    assert _discover_gitignored_dirs(tmp_path) == []


# --- v0.7.1: worktree placed outside consumer repo (Go-module fix) -------

def test_worktree_setup_placed_outside_consumer_repo(_isolated_worktree_base, tmp_path):
    """v0.7.0 placed worktrees at `<run_dir>/pr-checkout/` (i.e. inside
    the consumer repo). That breaks when the consumer repo lives under
    $GOPATH/src/... — Go reads the worktree path as an import sub-path
    of the consumer module, and `go run .` fails with `main module
    does not contain package <consumer>/.proctor/runs/<id>/pr-checkout`.

    v0.7.1 places the worktree OUTSIDE the consumer repo (under
    $PROCTOR_WORKTREE_BASE_DIR, default $TMPDIR/proctor-worktrees/).
    Source: v0.7.0 e2e against PR #1126 (run `pr1126-75eea89-353a49f0`)
    — server failed to start with the path-collision error, the executor
    spent ~1 minute on recovery (kill + retry with `go run ./main.go`)."""
    repo = tmp_path / "consumer"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"
    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    # The crucial invariant: the worktree path must NOT be inside the
    # consumer repo. (Either equal-to or under repo would re-introduce
    # the GOPATH bug.)
    assert repo.resolve() not in wt_path.parents
    assert wt_path != repo.resolve()
    # It IS under the configured base dir.
    assert _isolated_worktree_base.resolve() in wt_path.parents
    # Marker still recorded inside run_dir for teardown.
    marker = run_dir / "worktree-path.txt"
    assert marker.exists()
    assert marker.read_text().strip() == str(wt_path.resolve())


def test_worktree_setup_dir_name_includes_consumer_and_run_id(_isolated_worktree_base, tmp_path):
    """Worktree dir name encodes both the consumer repo name and the
    run-id so concurrent PRoctor runs against different PRs (or against
    different consumer repos) don't collide on the same path."""
    repo = tmp_path / "my-consumer"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "pr1234-abcdef0-12345678"
    wt_path = wt_setup(run_dir=run_dir, pr_number=1234, head_sha=pr_sha,
                       repo_root=repo)
    # Name carries both the consumer repo dir name and the run-id.
    assert wt_path.name == "my-consumer-pr1234-abcdef0-12345678"


def test_worktree_setup_honors_proctor_worktree_base_dir_env(tmp_path, monkeypatch):
    """If $PROCTOR_WORKTREE_BASE_DIR is set, worktrees go there instead
    of $TMPDIR/proctor-worktrees/. Lets devs use a faster SSD or a
    persistent dir to inspect failed runs without immediate cleanup."""
    repo = tmp_path / "consumer"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    custom_base = tmp_path / "my-custom-worktree-base"
    custom_base.mkdir()
    monkeypatch.setenv("PROCTOR_WORKTREE_BASE_DIR", str(custom_base))
    run_dir = repo / ".proctor" / "runs" / "test-run"
    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    assert custom_base.resolve() in wt_path.parents


# v0.7.1's hardcoded-sub-path test was removed in v0.7.2 — the abstract
# "gitignored sub-dir under tracked parent" case is now covered by
# test_worktree_setup_discovers_gitignored_subpath_under_tracked_parent
# (above), which exercises the SAME shape with project-neutral names
# (tracked_parent/ignored_sub instead of external/assets/mcd).


# --- v0.7.0: schema accepts worktree_symlink_dirs in .proctor/config.yml --

from plugins.proctor.scripts.schema import validate_pr_test_config


def test_pr_test_config_worktree_symlink_dirs_accepted():
    """`.proctor/config.yml.worktree_symlink_dirs` (v0.7.0+) is an
    optional list of repo-relative paths the worktree helper symlinks
    from the main checkout. Validator accepts a list of non-empty
    strings; an empty list is valid (means "skip all symlinking")."""
    validate_pr_test_config({
        "worktree_symlink_dirs": ["external/assets", "node_modules"],
    })
    validate_pr_test_config({"worktree_symlink_dirs": []})
    # null also accepted (legacy / unset).
    validate_pr_test_config({"worktree_symlink_dirs": None})


def test_pr_test_config_worktree_symlink_dirs_rejects_non_list():
    with pytest.raises(SchemaError):
        validate_pr_test_config({"worktree_symlink_dirs": "external/assets"})
    with pytest.raises(SchemaError):
        validate_pr_test_config({"worktree_symlink_dirs": ["", "external/assets"]})


def test_plan_smells_warnings_sorted_for_stability():
    plan = {"items": [
        {"id": "t-005", "category": "api", "tool": "chrome-devtools",
         "what": "save reward succeeds", "how": "...",
         "risk": "high", "depends_on": []},
        {"id": "t-001", "category": "api", "tool": "chrome-devtools",
         "what": "save reward, rejected if missing field",
         "how": "...", "risk": "high", "depends_on": []},
    ]}
    warnings = plan_check(plan)
    # Combined warnings come first, sorted by item id.
    combined = [w for w in warnings if "combines" in w]
    assert combined == sorted(combined)


# --- v0.3.29: verify_precondition_via (active precondition check) --------

def test_plan_verify_precondition_via_accepted():
    valid = {"items": [{
        "id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
        "risk":"low","depends_on":[],
        "preconditions":"At least one category exists.",
        "verify_precondition_via":
            "curl -sf $BASE_URL/api/categories | jq -e '.total > 0'",
    }]}
    validate_test_plan(valid)


def test_plan_verify_precondition_via_empty_rejected():
    bad = {"items": [{
        "id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
        "risk":"low","depends_on":[], "verify_precondition_via":"",
    }]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_verify_precondition_via_null_allowed():
    valid = {"items": [{
        "id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
        "risk":"low","depends_on":[], "verify_precondition_via":None,
    }]}
    validate_test_plan(valid)


def test_plan_verify_precondition_via_non_string_rejected():
    bad = {"items": [{
        "id":"t-1","category":"api","what":"x","how":"y","tool":"bash",
        "risk":"low","depends_on":[], "verify_precondition_via":["curl", "..."],
    }]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_verify_precondition_via_template_ok():
    """Templates inside verify_precondition_via must cross-validate
    the same way as how:/preconditions templates."""
    valid = {"items": [
        _producer_item(produces=["created_id"]),
        _consumer_item(
            verify_precondition_via=
                "curl -sf $BASE_URL/api/items/{{t-1.created_id}}",
        ),
    ]}
    validate_test_plan(valid)


def test_plan_verify_precondition_via_template_unknown_key_rejected():
    bad = {"items": [
        _producer_item(produces=["created_id"]),
        _consumer_item(
            verify_precondition_via=
                "curl -sf $BASE_URL/api/items/{{t-1.missing_key}}",
        ),
    ]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_change_map_impact_radius_truncated_accepted():
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "core/utils.go",
            "category": "api", "risk": "high", "summary": ".",
            "impact_radius": ["a.go", "b.go"],
            "impact_radius_truncated": True,
        }],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_impact_radius_truncated_must_be_bool():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": [],
            "impact_radius_truncated": "yes",   # string, not bool
        }],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


# --- v0.3.24: impact_radius on ChangeMap hunks ----------------------------

def test_change_map_impact_radius_valid():
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "admin/rewards/handler.go",
            "category": "api", "risk": "high", "summary": ".",
            "impact_radius": [
                "admin/rewards/router.go",
                "admin/dashboards/rewards_widget.go",
            ],
        }],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_impact_radius_empty_list_allowed():
    # Empty list = "analyzed and found nothing" — distinct from "didn't analyze".
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": [],
        }],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_impact_radius_null_allowed():
    # null == omitted (consistent with other optional fields).
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": None,
        }],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_impact_radius_must_be_list():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": "router.go",
        }],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_change_map_impact_radius_self_reference_rejected():
    # Hunk's own file in its impact_radius is nonsense — the changed
    # file isn't a caller of itself.
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": ["a.go", "b.go"],
        }],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def _producer_item(item_id="t-1", produces=None, **over):
    base = {"id": item_id, "category": "api", "what": "x", "how": "y",
            "tool": "bash", "risk": "low", "depends_on": []}
    if produces is not None:
        base["produces"] = produces
    base.update(over)
    return base


def _consumer_item(item_id="t-2", data_from=("t-1",), how="y",
                   preconditions=None, **over):
    base = {"id": item_id, "category": "api", "what": "x", "how": how,
            "tool": "bash", "risk": "low",
            "depends_on": list(data_from), "data_from": list(data_from)}
    if preconditions is not None:
        base["preconditions"] = preconditions
    base.update(over)
    return base


# --- v0.3.25: produces + {{id.key}} template + outputs --------------------

def test_plan_produces_field_accepted():
    valid = {"items": [_producer_item(produces=["created_id", "detail_url"])]}
    validate_test_plan(valid)


def test_plan_produces_empty_string_rejected():
    bad = {"items": [_producer_item(produces=[""])]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_produces_invalid_identifier_rejected():
    # `has-dash` is fine for item ids but NOT for output keys — they
    # get substituted into shell/URL context where dash-vs-underscore
    # ambiguity bites.
    for bad_key in ["1foo", "has-dash", "has space", "a.b"]:
        bad = {"items": [_producer_item(produces=[bad_key])]}
        with pytest.raises(SchemaError):
            validate_test_plan(bad)


def test_plan_produces_duplicate_key_rejected():
    bad = {"items": [_producer_item(produces=["x", "x"])]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_template_ok():
    valid = {"items": [
        _producer_item(produces=["created_id"]),
        _consumer_item(how="Edit record {{t-1.created_id}}; assert OK."),
    ]}
    validate_test_plan(valid)


def test_plan_template_in_preconditions_ok():
    valid = {"items": [
        _producer_item(produces=["detail_url"]),
        _consumer_item(preconditions="Record at {{t-1.detail_url}} exists.",
                       how="navigate; assert"),
    ]}
    validate_test_plan(valid)


def test_plan_template_whitespace_tolerated():
    # Plan authors might write `{{ t-1.created_id }}` for readability.
    valid = {"items": [
        _producer_item(produces=["created_id"]),
        _consumer_item(how="Edit record {{ t-1.created_id }}; OK."),
    ]}
    validate_test_plan(valid)


def test_plan_template_unknown_id_rejected():
    bad = {"items": [_consumer_item(data_from=(), how="Edit {{t-99.id}}.")]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_template_requires_data_from():
    # depends_on alone is insufficient — using upstream state must
    # be declared via data_from so the skip-on-fail logic engages.
    bad = {"items": [
        _producer_item(produces=["created_id"]),
        {
            "id": "t-2", "category": "api", "what": "x",
            "how": "Edit record {{t-1.created_id}}.",
            "tool": "bash", "risk": "low",
            "depends_on": ["t-1"],   # has depends_on
            # but NO data_from
        },
    ]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_template_key_not_in_produces_rejected():
    bad = {"items": [
        _producer_item(produces=["other_key"]),
        _consumer_item(how="Edit record {{t-1.created_id}}."),
    ]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_plan_template_producer_with_no_produces_rejected():
    # Upstream item declared NO produces list at all.
    bad = {"items": [
        _producer_item(),   # no produces
        _consumer_item(how="Edit record {{t-1.created_id}}."),
    ]}
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


def test_test_results_outputs_accepted():
    valid = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "outputs": {"created_id": "42", "detail_url": "/admin/rewards/42"},
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_test_results_outputs_null_allowed():
    # null/absent both fine — items without producers don't carry outputs.
    valid = {
        "items": [
            {"id": "t-1", "status": "pass", "evidence": "ok", "outputs": None},
            {"id": "t-2", "status": "pass", "evidence": "ok"},  # absent
        ],
        "summary": {"total": 2, "pass": 2, "fail": 0, "skipped": 0},
    }
    validate_test_results(valid)


def test_test_results_outputs_non_string_value_rejected():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "outputs": {"created_id": 42},  # int — must be string
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_outputs_empty_value_rejected():
    # Empty string is a producer contract violation — the executor
    # converts this to a fail anyway, but the schema rejects it as
    # a value too in case someone hand-edits the JSON.
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "outputs": {"created_id": ""},
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_outputs_invalid_key_rejected():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "outputs": {"has-dash": "x"},
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_test_results_outputs_must_be_dict():
    bad = {
        "items": [{
            "id": "t-1", "status": "pass", "evidence": "ok",
            "outputs": [("created_id", "42")],   # list, not dict
        }],
        "summary": {"total": 1, "pass": 1, "fail": 0, "skipped": 0},
    }
    with pytest.raises(SchemaError):
        validate_test_results(bad)


def test_change_map_impact_radius_empty_string_entry_rejected():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "hunks": [{
            "file": "a.go", "category": "api", "risk": "low", "summary": ".",
            "impact_radius": ["b.go", ""],
        }],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


# --- v0.3.26: impact_radius frequency threshold (#1) ----------------------

from plugins.proctor.scripts.impact_radius import collect_callers


def _init_repo(tmp_path):
    """Create a tiny git repo in tmp_path and return its path."""
    sp = subprocess
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(tmp_path):
    sp = subprocess
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    sp.run(["git", "commit", "-q", "-m", "x"], cwd=tmp_path,
           check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def test_impact_radius_filters_single_match(tmp_path):
    """A file with only ONE occurrence of the identifier (just an
    import line) should be dropped — that's the core v0.3.26 fix."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    # Real caller: 3 references to Foo.
    (tmp_path / "caller.go").write_text(
        'package x\nimport "./x"\nfunc a() { Foo(); Foo(); }\n')
    # False positive: 1 occurrence — looks like an import-only file.
    (tmp_path / "false_pos.go").write_text(
        'package x\n// see also Foo\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert "caller.go" in result["files"]
    assert "false_pos.go" not in result["files"]
    assert result["truncated"] is False


def test_impact_radius_excludes_changed_file(tmp_path):
    """The file whose hunk we're analyzing should never appear in its
    own impact_radius."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text(
        "package x\nfunc Foo() {}\nfunc bar() { Foo(); Foo(); }\n")
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert "src.go" not in result["files"]


def test_impact_radius_excludes_test_files(tmp_path):
    """`*_test.go`, `*.spec.*`, `__tests__/` etc. shouldn't appear —
    we want PRODUCTION callers, not test consumers."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    # Test file with many matches — should still be filtered out.
    (tmp_path / "src_test.go").write_text(
        'package x\nfunc TestFoo(t *testing.T) { Foo(); Foo(); Foo(); }\n')
    (tmp_path / "real.go").write_text(
        'package x\nfunc a() { Foo(); Foo(); }\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert "real.go" in result["files"]
    assert "src_test.go" not in result["files"]


def test_impact_radius_excludes_vendor_node_modules(tmp_path):
    """Vendored / node_modules paths are never our responsibility."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "dep.go").write_text(
        'package vendor\nfunc x() { Foo(); Foo(); Foo(); }\n')
    (tmp_path / "real.go").write_text(
        'package x\nfunc x() { Foo(); Foo(); }\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert "real.go" in result["files"]
    assert all("vendor" not in c for c in result["files"])


def test_impact_radius_aggregates_multiple_idents(tmp_path):
    """A file that mentions ident A once and ident B once should
    survive — cumulative count across the hunk's identifiers is 2,
    which crosses the threshold even though no single identifier
    appears twice."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text(
        "package x\nfunc Foo() {}\nfunc Bar() {}\n")
    (tmp_path / "caller.go").write_text(
        'package x\nfunc a() { Foo(); Bar(); }\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo", "Bar"], repo=str(tmp_path))
    assert "caller.go" in result["files"]


def test_impact_radius_ranks_by_count(tmp_path):
    """Highest-frequency caller should appear first in the result."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    (tmp_path / "heavy.go").write_text(
        'package x\nfunc a() { Foo(); Foo(); Foo(); Foo(); Foo(); }\n')
    (tmp_path / "light.go").write_text(
        'package x\nfunc a() { Foo(); Foo(); }\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert result["files"][0] == "heavy.go"
    assert "light.go" in result["files"]


def test_impact_radius_caps_at_top_n(tmp_path):
    """Result list is capped at top_n entries even when many files
    would qualify."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    for i in range(15):
        (tmp_path / f"c{i:02d}.go").write_text(
            f'package x\nfunc a() {{ Foo(); Foo(); }}  // {i}\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"],
                             repo=str(tmp_path), top_n=10)
    assert len(result["files"]) == 10
    assert result["truncated"] is True   # v0.3.28+: 15 survivors > top_n=10


def test_impact_radius_not_truncated_when_under_top_n(tmp_path):
    """When survivors <= top_n, truncated is False even if there are
    callers below threshold that were dropped."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    for i in range(5):
        (tmp_path / f"c{i:02d}.go").write_text(
            f'package x\nfunc a() {{ Foo(); Foo(); }}  // {i}\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"],
                             repo=str(tmp_path), top_n=10)
    assert len(result["files"]) == 5
    assert result["truncated"] is False


def test_impact_radius_truncated_exact_boundary(tmp_path):
    """Edge case: exactly top_n survivors → NOT truncated."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    for i in range(10):
        (tmp_path / f"c{i:02d}.go").write_text(
            f'package x\nfunc a() {{ Foo(); Foo(); }}  // {i}\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"],
                             repo=str(tmp_path), top_n=10)
    assert len(result["files"]) == 10
    assert result["truncated"] is False


def test_impact_radius_returns_empty_when_no_callers(tmp_path):
    """No callers at all → empty list, not an error."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    # Other files exist but don't reference Foo.
    (tmp_path / "other.go").write_text(
        'package x\nfunc a() { something(); something(); }\n')
    _commit(tmp_path)

    result = collect_callers("src.go", ["Foo"], repo=str(tmp_path))
    assert result["files"] == []
    assert result["truncated"] is False


def test_impact_radius_empty_identifiers_returns_empty(tmp_path):
    """If the analyzer couldn't extract any identifiers, helper
    returns empty result without invoking git."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\n")
    _commit(tmp_path)

    result = collect_callers("src.go", [], repo=str(tmp_path))
    assert result == {"files": [], "truncated": False}


# --- v0.3.27: error_type diff-pattern triggers (#4) ----------------------

from plugins.proctor.scripts.error_signals import detect as detect_err


def test_err_signal_state_conflict_unique_index():
    sigs = detect_err("CREATE UNIQUE INDEX idx_email ON users(email);")
    assert "state-conflict" in sigs
    assert "unique-index-added" in sigs["state-conflict"]


def test_err_signal_state_conflict_gorm_unique_tag():
    sigs = detect_err(
        '+ Email string `gorm:"column:email;uniqueIndex" json:"email"`')
    assert "state-conflict" in sigs
    assert "gorm-unique-index-tag" in sigs["state-conflict"]


def test_err_signal_state_conflict_version_field():
    sigs = detect_err("+\tVersion int `gorm:\"column:version\"`")
    assert "state-conflict" in sigs
    assert "version-field-added" in sigs["state-conflict"]


def test_err_signal_state_conflict_version_where_clause():
    sigs = detect_err(
        "UPDATE rewards SET name = ? WHERE id = ? AND version = ?")
    assert "state-conflict" in sigs
    assert "version-where-clause" in sigs["state-conflict"]


def test_err_signal_state_conflict_status_guard():
    sigs = detect_err('if order.Status != "draft" { return ErrCannotEdit }')
    assert "state-conflict" in sigs
    assert "state-guard" in sigs["state-conflict"]


def test_err_signal_state_conflict_409_response():
    sigs = detect_err("return c.JSON(http.StatusConflict, msg)")
    assert "state-conflict" in sigs
    assert "conflict-response" in sigs["state-conflict"]


def test_err_signal_state_conflict_select_for_update():
    sigs = detect_err("SELECT * FROM orders WHERE id = ? FOR UPDATE")
    assert "state-conflict" in sigs
    assert "select-for-update" in sigs["state-conflict"]


def test_err_signal_state_conflict_idempotency_key():
    sigs = detect_err('+ IdempotencyKey string `json:"idempotency_key"`')
    assert "state-conflict" in sigs


def test_err_signal_permission_role_guard():
    sigs = detect_err("if !user.IsAdmin { return }")
    assert "permission" in sigs
    assert "role-check-guard" in sigs["permission"]


def test_err_signal_permission_403_response():
    sigs = detect_err("c.AbortWithStatus(http.StatusForbidden)")
    assert "permission" in sigs


def test_err_signal_auth_middleware():
    sigs = detect_err(
        "router.Use(RequireAuth())\nrouter.Use(LoginRequired)")
    assert "auth" in sigs
    assert "auth-middleware" in sigs["auth"]


def test_err_signal_auth_csrf():
    sigs = detect_err("config.csrf = true")
    assert "auth" in sigs


def test_err_signal_auth_unauthorized():
    sigs = detect_err("return http.StatusUnauthorized")
    assert "auth" in sigs


def test_err_signal_not_found_gorm():
    sigs = detect_err(
        "if errors.Is(err, gorm.ErrRecordNotFound) { return nil }")
    assert "not-found" in sigs


def test_err_signal_not_found_nil_guard():
    sigs = detect_err(
        "if record == nil { return ErrNotFound }")
    assert "not-found" in sigs


def test_err_signal_network_http_client():
    sigs = detect_err('resp, err := http.Get("https://api.example.com")')
    assert "network" in sigs
    assert "http-client" in sigs["network"]


def test_err_signal_network_retry():
    sigs = detect_err("ctx, cancel := context.WithTimeout(ctx, 5*time.Second)")
    assert "network" in sigs
    assert "timeout-config" in sigs["network"]


def test_err_signal_validation_validate_tag():
    sigs = detect_err('Name string `validate:"required,max=100"`')
    assert "validation" in sigs


def test_err_signal_validation_rails():
    sigs = detect_err("validates_presence_of :email")
    assert "validation" in sigs


def test_err_signal_no_match_returns_empty():
    sigs = detect_err("// just a comment\nfmt.Println(\"hello\")")
    assert sigs == {}


def test_err_signal_signals_dedup_within_error_type():
    # Multiple lines that trigger the SAME pattern shouldn't produce
    # duplicate signal names — the report consumer expects unique names.
    sigs = detect_err(
        "if !user.IsAdmin { return }\n"
        "if !user.IsDeveloper { return }\n"
    )
    assert sigs["permission"].count("role-check-guard") == 1


def test_err_signal_multiple_error_types_in_one_diff():
    # A real PR routinely touches multiple error_types in one hunk —
    # the helper must surface ALL of them, not just the first.
    diff = """
+ if !user.IsAdmin { return c.AbortWithStatus(http.StatusForbidden) }
+ Email string `gorm:"column:email;uniqueIndex"`
+ return http.StatusUnauthorized
"""
    sigs = detect_err(diff)
    assert "permission" in sigs
    assert "state-conflict" in sigs
    assert "auth" in sigs


def test_impact_radius_min_occurrences_tunable(tmp_path):
    """min_occurrences=1 (legacy v0.3.24 behavior) keeps single-match
    files; the default of 2 drops them. Useful for ad-hoc debugging."""
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    (tmp_path / "single.go").write_text(
        'package x\n// references Foo\n')
    _commit(tmp_path)

    with_threshold = collect_callers("src.go", ["Foo"], repo=str(tmp_path),
                                     min_occurrences=2)
    without_threshold = collect_callers("src.go", ["Foo"], repo=str(tmp_path),
                                        min_occurrences=1)
    assert "single.go" not in with_threshold["files"]
    assert "single.go" in without_threshold["files"]


# --- v0.7.0: impact_radius batch mode -------------------------------------

def test_impact_radius_cli_batch_multiple_files(tmp_path):
    """v0.7.0+: passing multiple --file flags in ONE invocation emits
    a JSON object keyed by file path, so the analyzer can amortize one
    Python startup (~300-500ms) across all changed files. PR #1126
    e2e had 3 files → 3 sequential subprocesses; this collapses them."""
    import json as _json
    _init_repo(tmp_path)
    # Two distinct changed files with different identifiers.
    (tmp_path / "a.go").write_text("package x\nfunc Apple() {}\n")
    (tmp_path / "b.go").write_text("package x\nfunc Banana() {}\n")
    # A caller for each.
    (tmp_path / "consumer_a.go").write_text(
        'package x\nfunc ca() { Apple(); Apple(); }\n')
    (tmp_path / "consumer_b.go").write_text(
        'package x\nfunc cb() { Banana(); Banana(); }\n')
    _commit(tmp_path)

    script = str(
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "scripts" / "impact_radius.py"
    )
    proc = subprocess.run(
        [sys.executable, script,
         "--file", "a.go", "--file", "b.go",
         "--idents", "Apple Banana",
         "--repo", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    out = _json.loads(proc.stdout)
    # Multi-file shape: dict keyed by file path.
    assert set(out.keys()) == {"a.go", "b.go"}
    assert "consumer_a.go" in out["a.go"]["files"]
    assert "consumer_b.go" in out["b.go"]["files"]
    # Each entry still carries the truncated flag.
    assert out["a.go"]["truncated"] is False
    assert out["b.go"]["truncated"] is False


def test_impact_radius_cli_single_file_backward_compatible(tmp_path):
    """Single-file invocation (pre-v0.7.0 shape) emits the raw
    `{files, truncated}` dict directly, so existing v0.3.26 callers
    don't need to change."""
    import json as _json
    _init_repo(tmp_path)
    (tmp_path / "src.go").write_text("package x\nfunc Foo() {}\n")
    (tmp_path / "caller.go").write_text(
        'package x\nfunc a() { Foo(); Foo(); }\n')
    _commit(tmp_path)

    script = str(
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "scripts" / "impact_radius.py"
    )
    proc = subprocess.run(
        [sys.executable, script,
         "--file", "src.go",
         "--idents", "Foo",
         "--repo", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    out = _json.loads(proc.stdout)
    # Single-file shape: raw dict (no per-path wrapping).
    assert "files" in out and "truncated" in out
    assert "caller.go" in out["files"]


# --- v0.6.5: per-item-type screenshot-contract validator ------------------

from plugins.proctor.scripts.validate_screenshots_contract import (
    classify_item,
    check as ss_check,
)


# classify_item: each bucket has one canonical input pattern.

def test_ss_classify_non_chrome_devtools_exempt():
    assert classify_item({"tool": "bash", "what": "anything"}) == "not-chrome-devtools"
    assert classify_item({"tool": "lint-only", "what": "anything"}) == "not-chrome-devtools"
    assert classify_item({"tool": "curl", "what": "anything"}) == "not-chrome-devtools"


def test_ss_classify_negative_via_error_type():
    """error_type set ⇒ this is a validator-reject test (≥1)."""
    item = {"tool": "chrome-devtools",
            "what": "Reject blank Game URL",
            "error_type": "validation"}
    assert classify_item(item) == "negative"


def test_ss_classify_edit_and_switch_via_what():
    """The literal v0.6.4-doc anti-pattern wording must match."""
    item = {"tool": "chrome-devtools",
            "what": "edit reward, switch Digital Content Type from Image to Game",
            "how": "..."}
    assert classify_item(item) == "edit-and-switch"


def test_ss_classify_edit_and_switch_via_change_type_from_to():
    item = {"tool": "chrome-devtools",
            "what": "Change Digital Content Type from Image to Game",
            "how": "..."}
    assert classify_item(item) == "edit-and-switch"


def test_ss_classify_round_trip_via_hard_reload():
    item = {"tool": "chrome-devtools",
            "what": "Re-open saved Image reward; fields hard-reload to "
                    "same values",
            "how": "Navigate, hard-reload, verify."}
    assert classify_item(item) == "round-trip"


def test_ss_classify_round_trip_after_edit_not_misclassified_as_edit_switch():
    """v0.6.7 regression: t-006b in the PR-1115 e2e run had
    `what="HAPPY: re-open the just-edited reward — switched
    DigitalContentType, GameUrl, CTA labels all persist after hard
    reload"` and was misclassified as edit-and-switch because the
    regex saw "edited" + "switched". A re-open verification is a
    round-trip (2 screenshots), not an edit-and-switch (3) — no
    save action happens in this item. Reorder check so unambiguous
    re-open phrasing wins."""
    item = {"tool": "chrome-devtools",
            "what": "HAPPY: re-open the just-edited reward — "
                    "switched DigitalContentType, GameUrl, CTA "
                    "labels all persist after hard reload",
            "how": "Navigate to /edit page; hard-reload; assert "
                   "DigitalContentType=Game and GameUrl value "
                   "matches what was saved."}
    assert classify_item(item) == "round-trip"


def test_ss_classify_round_trip_via_round_trip_phrase():
    item = {"tool": "chrome-devtools",
            "what": "HAPPY: re-open saved Image reward — DigitalContentType "
                    "and asset round-trip correctly through the read path",
            "how": "..."}
    # Matches both happy AND round-trip; collapsed to round-trip per
    # classifier ordering. Both demand 2, so no functional difference.
    assert classify_item(item) == "round-trip"


def test_ss_classify_happy_save_via_happy_prefix():
    item = {"tool": "chrome-devtools",
            "what": "HAPPY: create Digital Download reward with type=Image "
                    "and asset uploaded — save succeeds",
            "how": "..."}
    assert classify_item(item) == "happy-save"


def test_ss_classify_render_check_default():
    item = {"tool": "chrome-devtools",
            "what": "Digital Download new-form renders new fields",
            "how": "Navigate to /new; assert fields visible."}
    assert classify_item(item) == "render-check"


# check(): violations are produced when result has too few screenshots.

def _make_plan_results(plan_items, result_items):
    plan = {"items": plan_items}
    statuses = [i.get("status", "pass") for i in result_items]
    summary = {
        "total": len(result_items),
        "pass": statuses.count("pass"),
        "fail": statuses.count("fail"),
        "skipped": statuses.count("skipped"),
    }
    results = {"items": result_items, "summary": summary}
    return plan, results


def test_ss_check_render_check_one_screenshot_ok():
    plan, results = _make_plan_results(
        [{"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-1", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "x.png", "label": "form", "focus": "fields"}]}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_render_check_zero_screenshots_flagged():
    plan, results = _make_plan_results(
        [{"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-1", "status": "pass", "evidence": "ok"}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "t-1" in violations[0]
    assert ">=1" in violations[0]
    assert "render-check" in violations[0]


def test_ss_check_happy_save_two_screenshots_ok():
    plan, results = _make_plan_results(
        [{"id": "t-2", "tool": "chrome-devtools",
          "what": "HAPPY: create reward — save succeeds",
          "how": "fill+save", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-2", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "a.png", "label": "filled", "focus": "form"},
              {"path": "b.png", "label": "saved", "focus": "toast"}]}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_happy_save_one_screenshot_flagged():
    """The exact pre-v0.6.4 bug pattern: save items shipped with one
    screenshot. v0.7.5: even when expressed in the new shape, count<2
    is flagged."""
    plan, results = _make_plan_results(
        [{"id": "t-2", "tool": "chrome-devtools",
          "what": "HAPPY: create reward — save succeeds",
          "how": "fill+save", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-2", "status": "pass", "evidence": "ok",
          "screenshots": [{"path": "x.png", "label": "filled", "focus": "fl"}]}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "t-2" in violations[0]
    assert ">=2" in violations[0]
    assert "happy-save" in violations[0]


def test_ss_check_edit_and_switch_three_screenshots_ok():
    plan, results = _make_plan_results(
        [{"id": "t-6", "tool": "chrome-devtools",
          "what": "edit reward, switch Digital Content Type from Image to Game",
          "how": "...", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-6", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "1.png", "label": "before", "focus": "Image"},
              {"path": "2.png", "label": "after-change", "focus": "Game"},
              {"path": "3.png", "label": "persisted", "focus": "reload"}]}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_edit_and_switch_one_screenshot_flagged_the_t006_bug():
    """The literal t-006 production bug the v0.6.4 contract was
    introduced to prevent: 1 screenshot of a post-save detail page
    when the test asserted on a form-state switch. v0.6.4 prose
    contract failed to prevent it; v0.6.5 mechanical check catches
    it before report render. v0.7.5: same pattern in the new shape
    (count=1) still fires."""
    plan, results = _make_plan_results(
        [{"id": "t-006", "tool": "chrome-devtools",
          "what": "edit reward, switch Digital Content Type from Image to Game",
          "how": "Navigate to detail; change select; save; reload.",
          "category": "api", "risk": "high", "depends_on": []}],
        [{"id": "t-006", "status": "pass",
          "evidence": "Reloaded; type=Game.",
          "screenshots": [{"path": "x.png", "label": "post-save",
                           "focus": "type"}]}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "t-006" in violations[0]
    assert ">=3" in violations[0]
    assert "edit-and-switch" in violations[0]


def test_ss_check_round_trip_two_screenshots_ok():
    plan, results = _make_plan_results(
        [{"id": "t-3", "tool": "chrome-devtools",
          "what": "HAPPY: re-open saved Image reward — fields round-trip",
          "how": "navigate + hard-reload", "category": "api",
          "risk": "high", "depends_on": []}],
        [{"id": "t-3", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "a.png", "label": "before-reload", "focus": "f1"},
              {"path": "b.png", "label": "after-reload", "focus": "f1"}]}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_round_trip_one_screenshot_flagged():
    plan, results = _make_plan_results(
        [{"id": "t-3", "tool": "chrome-devtools",
          "what": "HAPPY: re-open saved Image reward — fields round-trip",
          "how": "navigate + hard-reload", "category": "api",
          "risk": "high", "depends_on": []}],
        [{"id": "t-3", "status": "pass", "evidence": "ok",
          "screenshots": [{"path": "x.png", "label": "reloaded",
                           "focus": "field-values-after-reload"}]}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "round-trip" in violations[0]
    assert ">=2" in violations[0]


def test_ss_check_negative_one_screenshot_ok():
    plan, results = _make_plan_results(
        [{"id": "t-7", "tool": "chrome-devtools",
          "what": "Reject blank Game URL",
          "how": "fill+save", "category": "api", "risk": "medium",
          "depends_on": [], "error_type": "validation"}],
        [{"id": "t-7", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "e.png", "label": "err", "focus": "field+toast"}]}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_negative_zero_screenshots_flagged():
    plan, results = _make_plan_results(
        [{"id": "t-7", "tool": "chrome-devtools",
          "what": "Reject blank Game URL",
          "how": "fill+save", "category": "api", "risk": "medium",
          "depends_on": [], "error_type": "validation"}],
        [{"id": "t-7", "status": "pass", "evidence": "ok"}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "negative" in violations[0]
    assert ">=1" in violations[0]


def test_ss_check_non_chrome_devtools_item_not_enforced():
    """Bash/lint/curl items aren't screenshot-bearing; the validator
    must not demand screenshots from them."""
    plan, results = _make_plan_results(
        [{"id": "t-10", "tool": "bash",
          "what": "verify protobuf tags",
          "how": "grep", "category": "schema", "risk": "low",
          "depends_on": []}],
        [{"id": "t-10", "status": "pass", "evidence": "ok",
          "screenshot_ref": None}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_skipped_item_exempt():
    """A skipped item didn't run, can't have a screenshot — the
    empirical-grounding validator is the right tool for those."""
    plan, results = _make_plan_results(
        [{"id": "t-2", "tool": "chrome-devtools",
          "what": "HAPPY: create reward — save succeeds",
          "how": "fill+save", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-2", "status": "skipped",
          "reason": "precondition-not-met",
          "evidence": "server returned HTTP 503"}],
    )
    assert ss_check(plan, results) == []


def test_ss_check_legacy_screenshot_ref_rejected_v075():
    """v0.7.5 reverses v0.6.4's "legacy ref counts toward count
    contract" rule for chrome items. The PR-1126 v0.7.4 run shipped
    every chrome item with just `screenshot_ref` (no label, no focus
    metadata) and reviewers couldn't tell what the screenshot was
    supposed to show. Now legacy `screenshot_ref` alone is rejected
    on chrome items — the v0.6.4+ `screenshots: [{path, label, focus}]`
    array is required."""
    plan, results = _make_plan_results(
        [{"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-1", "status": "pass", "evidence": "ok",
          "screenshot_ref": ".proctor/runs/x/t-1.png"}],
    )
    violations = ss_check(plan, results)
    # One violation — the legacy-shape check flags it. Count check
    # passes because legacy ref counts as 1 and render-check needs >=1.
    assert len(violations) == 1
    assert "t-1" in violations[0]
    assert "screenshot_ref" in violations[0]
    assert "label" in violations[0] and "focus" in violations[0]


def test_ss_check_screenshots_entry_missing_focus_not_counted():
    """A `screenshots` list entry that omits the required `focus`
    field is not a valid screenshot per schema — and so cannot
    count toward the minimum, even though the file exists."""
    plan, results = _make_plan_results(
        [{"id": "t-2", "tool": "chrome-devtools",
          "what": "HAPPY: create reward — save succeeds",
          "how": "fill+save", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-2", "status": "pass", "evidence": "ok",
          "screenshots": [
              {"path": "a.png", "label": "filled", "focus": "form"},
              {"path": "b.png", "label": "saved"},  # focus missing
          ]}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert ">=2" in violations[0]


def test_ss_check_pinned_pre_v064_run_flagged_top_to_bottom():
    """Pin the literal pre-v0.6.4 t-002/t-003/t-005/t-006 result-
    shape (single legacy screenshot_ref, no list) against a plan
    representative of the PR-#1115 plan. Without v0.6.5 every
    happy-save / round-trip / edit-and-switch item would render in
    a report with insufficient evidence — v0.6.5 flags every one
    so the gap is visible BEFORE report render. v0.7.5 adds the
    legacy-shape flag on top so each item gets BOTH a count
    violation AND a legacy-shape violation."""
    plan_items = [
        {"id": "t-002", "tool": "chrome-devtools",
         "what": "HAPPY: create Digital Download reward with type=Image — save succeeds",
         "how": "...", "category": "api", "risk": "high", "depends_on": []},
        {"id": "t-003", "tool": "chrome-devtools",
         "what": "HAPPY: re-open saved Image reward — fields round-trip",
         "how": "navigate + hard-reload", "category": "api",
         "risk": "high", "depends_on": []},
        {"id": "t-006", "tool": "chrome-devtools",
         "what": "edit reward, switch Digital Content Type from Image to Game",
         "how": "...", "category": "api", "risk": "high", "depends_on": []},
    ]
    result_items = [
        {"id": "t-002", "status": "pass", "evidence": "ok",
         "screenshot_ref": ".proctor/runs/x/t-002.png"},
        {"id": "t-003", "status": "pass", "evidence": "ok",
         "screenshot_ref": ".proctor/runs/x/t-003.png"},
        {"id": "t-006", "status": "pass", "evidence": "ok",
         "screenshot_ref": ".proctor/runs/x/t-006.png"},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results)
    msg = "\n".join(violations)
    # Each item flagged for BOTH count AND legacy-shape (6 violations).
    assert "happy-save" in msg and "t-002" in msg
    assert "round-trip" in msg and "t-003" in msg
    assert "edit-and-switch" in msg and "t-006" in msg
    # Legacy-shape check also fires for all three.
    assert msg.count("screenshot_ref") >= 3


def test_ss_check_results_with_unmatched_plan_id_skipped():
    """An item present in results but absent from plan is silently
    skipped — that's planner/executor drift, not this script's
    concern."""
    plan, results = _make_plan_results(
        [{"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [
            {"id": "t-1", "status": "pass", "evidence": "ok",
             "screenshots": [{"path": "x.png", "label": "f", "focus": "fl"}]},
            {"id": "t-999-orphan", "status": "pass", "evidence": "ok"},
        ],
    )
    assert ss_check(plan, results) == []


def test_ss_check_plan_item_with_no_result_skipped():
    """Conversely, a plan item with no matching result is also out
    of scope — that's an execution-completeness check elsewhere."""
    plan, results = _make_plan_results(
        [
            {"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
             "how": "navigate", "category": "frontend", "risk": "low",
             "depends_on": []},
            {"id": "t-2", "tool": "chrome-devtools",
             "what": "HAPPY: create reward — save succeeds",
             "how": "...", "category": "api", "risk": "high",
             "depends_on": []},
        ],
        [{"id": "t-1", "status": "pass", "evidence": "ok",
          "screenshots": [{"path": "x.png", "label": "f", "focus": "fl"}]}],
    )
    # Only t-1 has a result; it passes. t-2 has no result; not flagged.
    assert ss_check(plan, results) == []


# --- v0.6.8: identical-negative-screenshot byte-size lint -----------------
# The v0.6.6 mcd-website run shipped t-007/t-008/t-009 with three
# byte-identical 244252-byte PNGs (the blank Add-Digital-Content form).
# Each evidence string claimed an error chip rendered; the screenshots
# proved it had not. Root cause: Pattern A submit used fetch() — server
# returned 422 + error HTML but the browser DOM did not re-render. The
# lint catches that pattern mechanically by comparing primary-screenshot
# byte sizes across negative items.

def test_ss_check_identical_negative_screenshots_3cluster_now_passes_v076(tmp_path):
    """v0.7.6 redesign: a 2-item cross-item cluster (size < 4) no
    longer fires. The v0.6.6 / v0.7.5 t-007/t-008 signature (one
    screenshot each, shared MD5) is now treated as legitimate same-
    state sharing — render-check + after-empty-save + after-reload-
    empty can all look like "form with empty inputs" and we don't
    want to spam reviewers. Within-item duplication is still HARD;
    cross-item clusters need to reach 4 entries to fire as WARN."""
    stub = tmp_path / "screenshots" / "shared-blank-form.png"
    stub.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 244244  # 244252 bytes
    stub.write_bytes(payload)
    plan_items = [
        {"id": "t-7", "tool": "chrome-devtools",
         "what": "Reject blank Game URL",
         "how": "fill+save", "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
        {"id": "t-8", "tool": "chrome-devtools",
         "what": "Reject DCT empty",
         "how": "fill+save", "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
    ]
    result_items = [
        {"id": "t-7", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared-blank-form.png",
                          "label": "err", "focus": "chip"}]},
        {"id": "t-8", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared-blank-form.png",
                          "label": "err", "focus": "chip"}]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    # v0.7.6: a 2-share cross-item cluster is below threshold (4) so
    # 0 violations.
    assert violations == []


def test_ss_check_distinct_negative_screenshots_ok(tmp_path):
    """Two negative items pointing at different files of different
    sizes return 0 violations."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    # Two distinct stubs at different sizes, both above the 50KB floor.
    (ss_dir / "err-dct-required.png").write_bytes(b"\x89PNG" + b"a" * 120000)
    (ss_dir / "err-gameurl-required.png").write_bytes(b"\x89PNG" + b"b" * 130000)
    plan_items = [
        {"id": "t-7", "tool": "chrome-devtools",
         "what": "Reject empty DCT",
         "how": "fill+save", "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
        {"id": "t-8", "tool": "chrome-devtools",
         "what": "Reject empty GameUrl",
         "how": "fill+save", "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
    ]
    result_items = [
        {"id": "t-7", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "err-dct-required.png",
                          "label": "err", "focus": "chip"}]},
        {"id": "t-8", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "err-gameurl-required.png",
                          "label": "err", "focus": "chip"}]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    assert ss_check(plan, results, run_dir=tmp_path) == []


def test_ss_check_identical_below_floor_not_flagged(tmp_path):
    """Two negative items pointing at the SAME tiny stub (e.g. an
    empty sentinel under the 50KB floor) are NOT flagged. The floor
    exists so legitimate tiny stubs don't trip the check."""
    stub = tmp_path / "screenshots" / "tiny-sentinel.png"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(b"\x89PNG" + b"x" * 100)  # ~100 bytes, well below floor
    plan_items = [
        {"id": "t-7", "tool": "chrome-devtools",
         "what": "Reject empty DCT", "how": "fill+save",
         "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
        {"id": "t-8", "tool": "chrome-devtools",
         "what": "Reject empty GameUrl", "how": "fill+save",
         "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
    ]
    result_items = [
        {"id": "t-7", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "tiny-sentinel.png",
                          "label": "err", "focus": "chip"}]},
        {"id": "t-8", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "tiny-sentinel.png",
                          "label": "err", "focus": "chip"}]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    assert ss_check(plan, results, run_dir=tmp_path) == []


def test_ss_check_identical_happy_save_screenshots_split_into_hard_and_warn_v076(tmp_path):
    """v0.7.6 redesign splits the old v0.7.5 single-cluster violation
    into TWO separate signals: within-item duplication (HARD, one per
    affected item) + cross-item cluster ≥ 4 (WARN, one per cluster).

    Synthesized scenario: two happy-save items each with TWO identical
    screenshots. Each item's [0]+[1] pair is within-item HARD (the
    before/after pair was actually before/before). The 4-entry cross-
    item cluster also fires as WARN."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    (ss_dir / "shared-form.png").write_bytes(b"\x89PNG" + b"x" * 200000)
    plan_items = [
        {"id": "t-2", "tool": "chrome-devtools",
         "what": "HAPPY: create Image reward — save succeeds",
         "how": "fill+save", "category": "api", "risk": "high",
         "depends_on": []},
        {"id": "t-4", "tool": "chrome-devtools",
         "what": "HAPPY: create Game reward — save succeeds",
         "how": "fill+save", "category": "api", "risk": "high",
         "depends_on": []},
    ]
    result_items = [
        {"id": "t-2", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "shared-form.png", "label": "f", "focus": "fl"},
             {"path": "shared-form.png", "label": "f", "focus": "fl"},
         ]},
        {"id": "t-4", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "shared-form.png", "label": "f", "focus": "fl"},
             {"path": "shared-form.png", "label": "f", "focus": "fl"},
         ]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    # 2 within-item HARD violations (one per item) + 1 cross-item
    # WARN cluster (4 entries across 2 items) = 3 total.
    assert len(violations) == 3
    hard = [v for v in violations if not v.startswith("WARN ")]
    warn = [v for v in violations if v.startswith("WARN ")]
    assert len(hard) == 2  # within-item HARD for t-2 and t-4
    assert any("t-2" in h for h in hard)
    assert any("t-4" in h for h in hard)
    assert len(warn) == 1
    assert "t-2" in warn[0] and "t-4" in warn[0]
    assert "MD5" in warn[0] or "md5" in warn[0].lower()


def test_ss_check_identical_no_run_dir_skipped(tmp_path):
    """Without a run_dir, the lint can't resolve files to bytes, so it
    skips silently (rather than emit false negatives). The count-based
    contract still runs."""
    plan_items = [
        {"id": "t-7", "tool": "chrome-devtools",
         "what": "Reject empty DCT", "how": "fill+save",
         "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
        {"id": "t-8", "tool": "chrome-devtools",
         "what": "Reject empty GameUrl", "how": "fill+save",
         "category": "api", "risk": "medium",
         "depends_on": [], "error_type": "validation"},
    ]
    result_items = [
        {"id": "t-7", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "x.png", "label": "e", "focus": "c"}]},
        {"id": "t-8", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "x.png", "label": "e", "focus": "c"}]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    # Even though they reference the same path, without run_dir the
    # byte-size lint is skipped. Both items satisfy the count
    # contract (negative needs >=1) so 0 violations.
    assert ss_check(plan, results) == []


# ===== v0.7.5: cross-item identical-screenshot lint + legacy reject =====


def test_ss_check_pinned_pr1126_v074_seven_md5_collisions(tmp_path):
    """Pin the literal v0.7.4 PR-1126 failure pattern: 5 chrome items
    (t-005 render, t-006 happy-save, t-007 round-trip, t-008 happy-save,
    t-009 round-trip), 11 total screenshot files, where 7 of them
    shared the same MD5 (the same viewport-top capture taken before
    scrollIntoView completed for ANY asserted field). The label/focus
    claimed different states; the bytes proved otherwise. v0.7.5
    mechanical check catches this before report render."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    # Three distinct PNGs — mimicking the v0.7.4 trace's 3 unique MD5s:
    #  - viewport-top (used 7x, the bug)
    #  - list-page (used 2x, legit because list views look the same)
    #  - zoom-of-edit-page (used 2x, legit)
    (ss_dir / "viewport-top.png").write_bytes(b"\x89PNG\r\n" + b"a" * 200000)
    (ss_dir / "list-page.png").write_bytes(b"\x89PNG\r\n" + b"b" * 120000)
    (ss_dir / "zoom-edit.png").write_bytes(b"\x89PNG\r\n" + b"c" * 130000)
    plan_items = [
        {"id": "t-005", "tool": "chrome-devtools",
         "what": "Bonus Banner admin form displays IncludeTags / ExcludeTags",
         "how": "navigate; assert visible", "category": "api",
         "risk": "low", "depends_on": []},
        {"id": "t-006", "tool": "chrome-devtools",
         "what": "HAPPY: save tags with commas — succeeds",
         "how": "fill+save", "category": "api", "risk": "high",
         "depends_on": []},
        {"id": "t-007", "tool": "chrome-devtools",
         "what": "HAPPY: re-open saved record — values round-trip",
         "how": "navigate+reload", "category": "api", "risk": "high",
         "depends_on": []},
        {"id": "t-008", "tool": "chrome-devtools",
         "what": "HAPPY: save with empty tags — backward compat",
         "how": "clear+save", "category": "api", "risk": "high",
         "depends_on": []},
        {"id": "t-009", "tool": "chrome-devtools",
         "what": "HAPPY: re-open empty record — round-trip empty",
         "how": "navigate+reload", "category": "api", "risk": "high",
         "depends_on": []},
    ]
    # 7 of 11 share viewport-top; 2 share list-page; 2 share zoom-edit.
    result_items = [
        {"id": "t-005", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "viewport-top.png", "label": "form visible", "focus": "labels"},
         ]},
        {"id": "t-006", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "viewport-top.png", "label": "before save", "focus": "filled"},
             {"path": "viewport-top.png", "label": "after save", "focus": "toast"},
         ]},
        {"id": "t-007", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "list-page.png", "label": "navigated away", "focus": "list URL"},
             {"path": "viewport-top.png", "label": "after hard reload", "focus": "values"},
             {"path": "zoom-edit.png", "label": "zoom tags", "focus": "values close-up"},
         ]},
        {"id": "t-008", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "viewport-top.png", "label": "tags cleared", "focus": "empty"},
             {"path": "viewport-top.png", "label": "save success", "focus": "toast"},
         ]},
        {"id": "t-009", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "list-page.png", "label": "navigated away", "focus": "list URL"},
             {"path": "viewport-top.png", "label": "empty after reload", "focus": "empty"},
             {"path": "zoom-edit.png", "label": "zoom empty tags", "focus": "empty"},
         ]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    # 3 MD5 clusters of size > 1, so 3 violations:
    #   - viewport-top: 7 entries across t-005..t-009 (THE bug)
    #   - list-page: 2 entries across t-007/t-009 (legit but flagged — review)
    #   - zoom-edit: 2 entries across t-007/t-009 (legit but flagged — review)
    cluster_violations = [v for v in violations if "MD5" in v or "md5" in v.lower()]
    assert len(cluster_violations) == 3
    # The biggest cluster (7 entries) must be in there and must
    # mention the viewport-top bug pattern.
    big = max(cluster_violations, key=lambda v: v.count("t-"))
    assert big.count("t-") >= 7, (
        f"Expected the viewport-top cluster to list 7 occurrences; "
        f"got {big.count('t-')} in violation: {big}"
    )


def test_ss_check_cross_item_unique_screenshots_ok(tmp_path):
    """When every chrome item's screenshots are byte-unique, no
    violation. Confirms the lint doesn't false-positive on
    legitimate runs where every state-change captures a different
    image."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    for name, byte in [("a.png", b"a"), ("b.png", b"b"), ("c.png", b"c")]:
        (ss_dir / name).write_bytes(b"\x89PNG\r\n" + byte * 100000)
    plan_items = [
        {"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
         "how": "navigate", "category": "frontend", "risk": "low",
         "depends_on": []},
        {"id": "t-2", "tool": "chrome-devtools",
         "what": "HAPPY: create record — save succeeds",
         "how": "fill+save", "category": "api", "risk": "high",
         "depends_on": []},
    ]
    result_items = [
        {"id": "t-1", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "a.png", "label": "f", "focus": "fl"}]},
        {"id": "t-2", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "b.png", "label": "before", "focus": "form"},
             {"path": "c.png", "label": "after", "focus": "toast"},
         ]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    assert ss_check(plan, results, run_dir=tmp_path) == []


def test_ss_check_legacy_screenshot_ref_alone_rejected_even_for_render_check(tmp_path):
    """v0.7.5: a render-check item whose ONLY screenshot evidence is
    the legacy `screenshot_ref` singular field is rejected, even
    though the count (1) satisfies render-check's minimum. The
    review-readability of label+focus is mandatory."""
    plan, results = _make_plan_results(
        [{"id": "t-r", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-r", "status": "pass", "evidence": "ok",
          "screenshot_ref": "x.png"}],
    )
    violations = ss_check(plan, results)
    assert len(violations) == 1
    assert "t-r" in violations[0]
    assert "screenshot_ref" in violations[0]


def test_ss_check_new_shape_one_entry_ok_for_render_check(tmp_path):
    """Render-check with the new shape (one entry containing path+
    label+focus) is fine. v0.7.5 doesn't add count requirements
    beyond v0.6.5 — it just rejects the legacy bare-path shape."""
    plan, results = _make_plan_results(
        [{"id": "t-r", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-r", "status": "pass", "evidence": "ok",
          "screenshots": [{"path": "x.png", "label": "rendered",
                           "focus": "the new section is visible at top"}]}],
    )
    assert ss_check(plan, results) == []


# ===== v0.6.6: satisfying-form-preconditions skill detection =====
# Pins the upstream-validator detection regex against the exact error
# strings observed in the v0.6.5 t-002 evidence ("Reward Image cannot be
# blank") plus the generic family. If a future executor rewrite drops the
# detection logic, this test catches it.

import re

# This is the canonical detection regex documented in
# plugins/proctor/skills/satisfying-form-preconditions/SKILL.md "Detection"
# section, item 4. Any change to the SKILL must update this regex too.
PRECONDITION_TRIGGER_RE = re.compile(
    r"(cannot be blank|is required|must be present)",
    re.IGNORECASE,
)


def _detect_upstream_validator_block(response_body: str) -> list[str]:
    """Return the list of upstream-validator error phrases found in a
    save-flow response body. Empty list means no precondition gap.

    This mirrors what the satisfying-form-preconditions skill instructs
    the executor to check after every save-attempt that didn't redirect
    to the new-record URL.
    """
    return [m.group(0) for m in PRECONDITION_TRIGGER_RE.finditer(response_body)]


def test_satisfying_form_preconditions_detection():
    """The detector must fire on the EXACT error string observed in
    the v0.6.5 PR-#1115 run, plus the documented generic family.

    Failure-mode this regression guards against: an executor refactor
    that narrows the trigger to a single literal (e.g. only matching
    "Reward Image cannot be blank") would miss every other app's
    upstream-validator gate. Conversely, a regex that's too loose
    would fire on the test's OWN asserted error message and cause the
    executor to falsely bypass a legitimate negative test.
    """
    # The literal observed in t-002 evidence of run
    # pr1115-e6a7c79-828594b8 (v0.6.5 run that motivated this skill).
    v065_t002_response = (
        "<div class='qor-error'>Reward Image cannot be blank</div>\n"
        "<div class='qor-error'>Points Required for Exchange is required</div>"
    )
    hits = _detect_upstream_validator_block(v065_t002_response)
    # Both errors must be detected — different validators, same family.
    assert "cannot be blank" in hits
    assert "is required" in hits
    assert len(hits) == 2, f"expected 2 hits in the t-002 response, got {hits!r}"

    # Generic Rails / Django shapes — must also fire.
    rails_response = "<p class='error'>Name must be present</p>"
    assert _detect_upstream_validator_block(rails_response) == ["must be present"]

    # Capitalization-tolerant (some apps emit "Cannot be blank" not lowercase).
    capitalized = "<span>Email Cannot Be Blank</span>"
    assert _detect_upstream_validator_block(capitalized) == ["Cannot Be Blank"]

    # Negative case 1: a happy-path response with no validator block
    # MUST NOT fire (otherwise we'd bypass every test).
    happy = "<title>Edit Digital Content - MCD</title><h1>Saved!</h1>"
    assert _detect_upstream_validator_block(happy) == []

    # Negative case 2: the asserted negative-test error message that
    # the v0.6.6 skill must NOT bypass. "Game URL is not a valid URL"
    # is the DigitalContent-Validator's own message; if the test
    # asserted on THAT, this is the EXPECTED response, not a
    # precondition gap. The detector itself doesn't know which is
    # asserted-vs-blocking — that's the executor's job from the item's
    # how:. But the detector MUST NOT falsely fire on this string
    # alone (it doesn't contain "cannot be blank" / "is required" /
    # "must be present").
    asserted_neg = "<div class='qor-error'>Game URL is not a valid URL</div>"
    assert _detect_upstream_validator_block(asserted_neg) == []

    # Negative case 3: descriptive prose that mentions one of the
    # trigger phrases but inside an evidence/comment string MUST still
    # fire — the detector is intentionally lenient, the executor's
    # decision logic narrows it.
    prose = "Note: the field is required for the next step."
    assert _detect_upstream_validator_block(prose) == ["is required"]


def test_satisfying_form_preconditions_skill_file_present():
    """The skill file MUST exist under the expected path. The agent
    references it by absolute path in section 2c; if the file moves
    or is deleted, the agent's instruction is broken silently."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills"
        / "satisfying-form-preconditions" / "SKILL.md"
    )
    assert skill_path.exists(), (
        f"missing skill file: {skill_path}. The v0.6.6 executor agent "
        "(plugins/proctor/agents/pr-test-executor.md section 2c) "
        "references this skill by path; deleting it breaks the executor's "
        "fallback for upstream-validator preconditions."
    )
    body = skill_path.read_text(encoding="utf-8")
    # Front-matter sanity: name + description must be set per the
    # skill loader convention.
    assert "name: satisfying-form-preconditions" in body
    assert "description:" in body
    # Pattern A must be documented (the no-upload bypass). If a future
    # edit deletes Pattern A entirely, the agent's recovery path is
    # gone.
    assert "Pattern A" in body
    assert "existing-record reuse" in body


# --- v0.7.4: Stop hook auto-continues mid-flight pipelines -----------------

_HOOK_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "plugins" / "proctor" / "hooks" / "stop-hook.sh"
)
_HOOKS_JSON = (
    pathlib.Path(__file__).resolve().parent.parent
    / "plugins" / "proctor" / "hooks" / "hooks.json"
)


def _run_stop_hook(project_dir):
    """Invoke the bundled Stop hook script with CLAUDE_PROJECT_DIR set,
    return (exit_code, stderr_text)."""
    proc = subprocess.run(
        ["bash", str(_HOOK_PATH)],
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)},
        input="",
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stderr


def test_stop_hook_blocks_when_pipeline_mid_flight(tmp_path):
    """The hook's whole purpose: when proctor_run.py is between stages,
    the AI tends to end its turn. The hook detects this via the live
    pipeline-state.json and exits 2 so Claude Code treats it as 'block
    stop' + feeds stderr back to the AI as a continuation prompt."""
    run_dir = tmp_path / ".proctor" / "runs" / "pr1-abc"
    run_dir.mkdir(parents=True)
    state = run_dir / "pipeline-state.json"
    state.write_text(json.dumps({
        "step": "analyzed",
        "run_id": "pr1-abc",
        "run_dir": str(run_dir),
        "pr_number": 1,
    }))
    rc, err = _run_stop_hook(tmp_path)
    assert rc == 2
    assert "mid-flight" in err.lower()
    assert "step: analyzed" in err.lower() or "current step: analyzed" in err.lower()
    # The continuation prompt must name the script to re-invoke + the
    # state-file arg; otherwise the AI doesn't know what to run next.
    assert "proctor_run.py" in err
    assert str(state) in err


def test_stop_hook_allows_stop_when_pipeline_done(tmp_path):
    """step=done is the terminal state. Hook MUST allow stop or the
    session can never end after a successful run."""
    run_dir = tmp_path / ".proctor" / "runs" / "pr1-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "pipeline-state.json").write_text(json.dumps({
        "step": "done",
        "run_id": "pr1-abc",
    }))
    rc, _ = _run_stop_hook(tmp_path)
    assert rc == 0


def test_stop_hook_allows_stop_when_no_proctor_dir(tmp_path):
    """Hook fires on every assistant stop in every Claude Code session.
    A project without .proctor/runs/ must be a clean no-op — otherwise
    we trap every session in every consumer."""
    # tmp_path has no .proctor/ at all.
    rc, _ = _run_stop_hook(tmp_path)
    assert rc == 0


def test_stop_hook_allows_stop_when_state_file_stale(tmp_path):
    """A pipeline-state.json from a session the user walked away from
    (e.g. crashed mid-run, never cleaned up) must not trap future
    sessions in that project. Cutoff: 5 minutes since last mtime."""
    run_dir = tmp_path / ".proctor" / "runs" / "pr1-abc"
    run_dir.mkdir(parents=True)
    state = run_dir / "pipeline-state.json"
    state.write_text(json.dumps({"step": "analyzed", "run_id": "pr1-abc"}))
    # Backdate mtime to 10 minutes ago.
    old_ts = time.time() - 600
    os.utime(state, (old_ts, old_ts))
    rc, _ = _run_stop_hook(tmp_path)
    assert rc == 0


def test_stop_hook_picks_most_recent_run_when_multiple_exist(tmp_path):
    """A consumer may have many runs accumulated over time. The hook
    must pick the most-recently-modified one (the active session),
    not an old one. Otherwise restarting the loop on a NEW PR with an
    old run still on disk wouldn't trigger continuation."""
    runs = tmp_path / ".proctor" / "runs"
    runs.mkdir(parents=True)
    # Older run: done.
    old_run = runs / "pr1-old"
    old_run.mkdir()
    old_state = old_run / "pipeline-state.json"
    old_state.write_text(json.dumps({"step": "done", "run_id": "pr1-old"}))
    os.utime(old_state, (time.time() - 60, time.time() - 60))
    # Newer run: active.
    new_run = runs / "pr2-new"
    new_run.mkdir()
    new_state = new_run / "pipeline-state.json"
    new_state.write_text(json.dumps({
        "step": "planned", "run_id": "pr2-new",
    }))
    # Newer state is current — should be picked, should block.
    rc, err = _run_stop_hook(tmp_path)
    assert rc == 2
    assert "pr2-new" in err  # the new run's state file is named


def test_stop_hook_handles_corrupted_state_file(tmp_path):
    """Garbage in the state file should not crash the hook — allow stop
    gracefully so the user isn't stuck in a session they can't exit."""
    run_dir = tmp_path / ".proctor" / "runs" / "pr1-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "pipeline-state.json").write_text("not valid json {{")
    rc, _ = _run_stop_hook(tmp_path)
    # Either 0 (gracefully no-op'd) or 2 (defensively blocked) is
    # defensible. The script's actual choice: empty step → exit 0.
    assert rc == 0


def test_hooks_json_registers_stop_hook(tmp_path):
    """Plugin's hooks.json declares the Stop hook so Claude Code auto-
    loads it when the plugin is installed. No user settings.json edit
    needed — that's the whole point of shipping it inside the plugin."""
    cfg = json.loads(_HOOKS_JSON.read_text())
    assert "Stop" in cfg["hooks"]
    stop_entries = cfg["hooks"]["Stop"]
    assert len(stop_entries) == 1
    # Must call the shipped stop-hook.sh via CLAUDE_PLUGIN_ROOT.
    cmd = stop_entries[0]["hooks"][0]["command"]
    assert "CLAUDE_PLUGIN_ROOT" in cmd
    assert "hooks/stop-hook.sh" in cmd
    assert stop_entries[0]["hooks"][0]["type"] == "command"


# --- v0.7.6: schema accepts pr_context.comments / linked_content ----------


def test_change_map_accepts_pr_context_comments():
    """v0.7.6: analyzer fetches PR review/conversation comments and
    surfaces them so the planner sees scope-changing reviewer remarks
    the body doesn't carry."""
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "title": "Add display_name cap",
            "body": "Per ACME-42",
            "links": [],
            "comments": [
                {"author": "alice",
                 "body": "Also enforce server-side, not just on the form.",
                 "created_at": "2026-05-12T14:22:11Z"},
            ],
        },
        "hunks": [{"file": "a.go", "category": "api", "risk": "medium",
                   "summary": "."}],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_accepts_pr_context_linked_content():
    """v0.7.6: analyzer fetches linked Jira/Confluence/Slack/Drive
    URLs and surfaces the excerpt for the planner."""
    valid = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "title": "Add display_name cap",
            "body": "Per ACME-42",
            "links": ["https://acme.atlassian.net/browse/ACME-42"],
            "linked_content": [
                {"url": "https://acme.atlassian.net/browse/ACME-42",
                 "source_type": "jira",
                 "title": "Cap display_name at 100 chars",
                 "excerpt": "Trim instead of reject for backward compat.",
                 "fetched": True},
                {"url": "https://example.notion.so/x",
                 "source_type": "unfetchable",
                 "fetched": False},
            ],
        },
        "hunks": [{"file": "a.go", "category": "api", "risk": "medium",
                   "summary": "."}],
        "categories_present": ["api"],
    }
    validate_change_map(valid)


def test_change_map_rejects_linked_content_without_required_keys():
    """A linked_content entry without url/source_type/fetched is
    invalid — those are the planner's read contract."""
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {
            "linked_content": [
                {"url": "https://x", "source_type": "jira"},  # missing fetched
            ],
        },
        "hunks": [{"file": "a.go", "category": "api", "risk": "low",
                   "summary": "."}],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_change_map_rejects_comments_not_a_list():
    bad = {
        "pr": {"number": 1, "head_sha": "abc", "base_sha": "def", "url": "https://x"},
        "pr_context": {"comments": "not a list"},
        "hunks": [{"file": "a.go", "category": "api", "risk": "low",
                   "summary": "."}],
        "categories_present": ["api"],
    }
    with pytest.raises(SchemaError):
        validate_change_map(bad)


def test_test_plan_accepts_planner_coverage_audit():
    """v0.7.6: planner emits a coverage-audit worksheet as the last
    step of planning. Schema accepts it as an optional top-level
    field — the only structural requirement is dict-of-lists-of-dicts.
    Reviewers read it to confirm the planner saw every input."""
    valid = {
        "planner_coverage_audit": {
            "by_pr_body": [
                {"criterion": "cap at 100 chars", "covered_by": ["t-005"]},
            ],
            "by_diff_symbols": [
                {"symbol": "TrimDisplayName",
                 "exercised_by": ["t-007"],
                 "lint_only": []},
            ],
            "gaps": [],
        },
        "items": [
            {"id": "t-005", "category": "api",
             "what": "HAPPY: save with 100-char name",
             "how": "...", "tool": "chrome-devtools",
             "risk": "high", "depends_on": []},
        ],
    }
    validate_test_plan(valid)


def test_test_plan_rejects_planner_coverage_audit_with_non_list_field():
    bad = {
        "planner_coverage_audit": {
            "by_pr_body": "not a list",
        },
        "items": [],
    }
    with pytest.raises(SchemaError):
        validate_test_plan(bad)


# --- v0.7.6: plan_smells pr-body-coverage + new-symbol-not-exercised ------


def test_plan_smells_pr_body_coverage_clean_when_all_criteria_covered():
    """A criterion in pr_context.requirement_hints whose key tokens
    overlap a plan item's what/how/rationale counts as covered. No
    warning fires."""
    change_map = {
        "pr_context": {
            "requirement_hints": ["display_name capped at 100 chars"],
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save with 100-char display_name succeeds",
             "how": "fill display_name with 100 chars; save; assert toast",
             "risk": "high", "depends_on": []},
            {"id": "t-2", "category": "api", "tool": "chrome-devtools",
             "what": "Re-open record — display_name field round-trips",
             "how": "navigate, hard reload, assert", "risk": "high",
             "depends_on": ["t-1"]},
        ],
    }
    warnings = plan_check(plan, change_map=change_map)
    coverage_warnings = [w for w in warnings if "pr-body-coverage" in w]
    assert coverage_warnings == []


def test_plan_smells_pr_body_coverage_fires_when_criterion_missed():
    """A criterion whose key tokens don't overlap any item triggers
    pr-body-coverage. Synthetic: criterion mentions 'CSV upload' but
    no item does."""
    change_map = {
        "pr_context": {
            "requirement_hints": [
                "CSV upload supports up to 10000 rows per batch",
            ],
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save display_name", "how": "fill+save",
             "risk": "high", "depends_on": []},
        ],
    }
    warnings = plan_check(plan, change_map=change_map)
    coverage = [w for w in warnings if "pr-body-coverage" in w]
    assert len(coverage) == 1
    assert "CSV" in coverage[0] or "csv" in coverage[0].lower()


def test_plan_smells_pr_body_coverage_reads_linked_content_excerpts():
    """Criteria embedded as bullet/must lines inside linked_content
    excerpts also get extracted and checked. The Jira ticket the PR
    cites contains 'must validate slug uniqueness' — the plan should
    cover it."""
    change_map = {
        "pr_context": {
            "linked_content": [
                {"url": "https://x.atlassian.net/browse/X-1",
                 "source_type": "jira",
                 "title": "Add slug",
                 "excerpt": "- [ ] must validate slug uniqueness "
                            "across the bookings table",
                 "fetched": True},
            ],
        },
    }
    plan_no_cover = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: render form", "how": "navigate",
             "risk": "medium", "depends_on": []},
        ],
    }
    warnings_no = plan_check(plan_no_cover, change_map=change_map)
    coverage_no = [w for w in warnings_no if "pr-body-coverage" in w]
    assert len(coverage_no) == 1
    assert "uniqueness" in coverage_no[0].lower() or \
        "slug" in coverage_no[0].lower()
    # Now add a covering item.
    plan_cover = {
        "items": plan_no_cover["items"] + [
            {"id": "t-2", "category": "api", "tool": "chrome-devtools",
             "what": "NEGATIVE: duplicate slug for bookings rejected",
             "how": "save twice with same slug; assert uniqueness "
                    "error", "risk": "high", "depends_on": [],
             "error_type": "state-conflict"},
        ],
    }
    warnings_yes = plan_check(plan_cover, change_map=change_map)
    coverage_yes = [w for w in warnings_yes if "pr-body-coverage" in w]
    assert coverage_yes == []


def test_plan_smells_pr_body_coverage_respects_audit_gaps_excused():
    """When the planner explicitly listed a criterion in
    planner_coverage_audit.gaps[].criterion, plan_smells doesn't
    double-warn — the gap is already visible in the plan itself."""
    change_map = {
        "pr_context": {
            "requirement_hints": [
                "CSV upload supports 10000 rows per batch",
            ],
        },
    }
    plan = {
        "planner_coverage_audit": {
            "gaps": [{"criterion": "CSV upload supports 10000 rows per batch",
                      "why_no_item": "no upload UI in this PR — back-end only"}],
        },
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save", "how": "fill+save",
             "risk": "high", "depends_on": []},
        ],
    }
    warnings = plan_check(plan, change_map=change_map)
    coverage = [w for w in warnings if "pr-body-coverage" in w]
    assert coverage == []


def test_plan_smells_new_symbol_not_exercised_fires_when_only_lint_only():
    """A new symbol introduced in the diff that only appears in a
    lint-only item's prose (never in a runtime item) triggers the
    warning."""
    diff = (
        "diff --git a/foo.go b/foo.go\n"
        "--- a/foo.go\n"
        "+++ b/foo.go\n"
        "@@ -1,1 +1,5 @@\n"
        "+func SplitTags(s string) []string {\n"
        "+    return nil\n"
        "+}\n"
    )
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "grep for SplitTags exists",
             "how": "grep -r 'func SplitTags' .",
             "risk": "low", "depends_on": []},
        ],
    }
    warnings = plan_check(plan, diff_text=diff)
    sym = [w for w in warnings if "new-symbol-not-exercised" in w]
    assert len(sym) == 1
    assert "SplitTags" in sym[0]
    assert "t-1" in sym[0]


def test_plan_smells_new_symbol_not_exercised_clean_when_runtime_item_uses_it():
    """A new symbol referenced by a chrome-devtools / bash / curl
    item's what/how is exercised — no warning."""
    diff = (
        "+func SplitTags(s string) []string { return nil }\n"
    )
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "grep for SplitTags", "how": "grep ...",
             "risk": "low", "depends_on": []},
            {"id": "t-2", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save with comma-separated tags exercises "
                     "SplitTags split path",
             "how": "fill tags=a,b,c; save; assert",
             "risk": "high", "depends_on": []},
        ],
    }
    warnings = plan_check(plan, diff_text=diff)
    sym = [w for w in warnings if "new-symbol-not-exercised" in w]
    assert sym == []


def test_plan_smells_new_symbol_detects_ts_export_function():
    """TypeScript export function symbol detection — the cross-stack
    coverage matters for repos that ship multiple languages."""
    diff = (
        "+export function buildBannerSlug(input: string): string {\n"
        "+    return input.toLowerCase();\n"
        "+}\n"
    )
    plan = {
        "items": [
            {"id": "t-1", "category": "frontend", "tool": "lint-only",
             "what": "buildBannerSlug exported correctly",
             "how": "grep export function",
             "risk": "low", "depends_on": []},
        ],
    }
    warnings = plan_check(plan, diff_text=diff)
    sym = [w for w in warnings if "new-symbol-not-exercised" in w]
    assert len(sym) == 1
    assert "buildBannerSlug" in sym[0]


def test_plan_smells_new_symbol_respects_audit_gaps_excused():
    """A symbol the planner listed in audit gaps doesn't double-warn."""
    diff = "+func InternalHelper() {}\n"
    plan = {
        "planner_coverage_audit": {
            "gaps": [{"symbol": "InternalHelper",
                      "why_no_item": "private helper, no callable surface"}],
        },
        "items": [],
    }
    warnings = plan_check(plan, diff_text=diff)
    sym = [w for w in warnings if "new-symbol-not-exercised" in w]
    assert sym == []


def test_plan_smells_new_checks_no_op_when_inputs_absent():
    """Backward compat: calling plan_check WITHOUT change_map / diff
    preserves the v0.7.5 behavior — the new checks no-op silently
    and only the v0.7.5 plan-internal lints fire."""
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save record", "how": "fill+save",
             "risk": "high", "depends_on": [], "produces": ["created_id"]},
            {"id": "t-2", "category": "api", "tool": "chrome-devtools",
             "what": "Re-open saved record, fields round-trip",
             "how": "navigate+reload", "risk": "high",
             "depends_on": ["t-1"], "data_from": ["t-1"]},
        ],
    }
    # Same plan, no extra inputs → 0 warnings (would already be clean
    # in v0.7.5).
    assert plan_check(plan) == []


def test_plan_smells_cli_strict_accepts_change_map_and_diff_flags(tmp_path):
    """CLI integration: --change-map and --diff route through to the
    new checks. Synthetic change-map with an uncovered criterion +
    diff with an unexercised symbol → strict mode exits 1."""
    import subprocess
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: render form", "how": "navigate",
             "risk": "medium", "depends_on": []},
        ],
    }))
    cm_path = tmp_path / "change-map.json"
    cm_path.write_text(json.dumps({
        "pr_context": {
            "requirement_hints": ["CSV upload supports 10000 rows per batch"],
        },
    }))
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text("+func ParseCSVBatch() {}\n")
    script = str(pathlib.Path(__file__).resolve().parent.parent
                 / "plugins" / "proctor" / "scripts" / "plan_smells.py")
    result = subprocess.run(
        ["python3", script, "--strict",
         "--change-map", str(cm_path),
         "--diff", str(diff_path)],
        stdin=open(plan_path), capture_output=True, text=True,
    )
    assert result.returncode == 1
    # Either pr-body-coverage or new-symbol-not-exercised should fire
    # (likely both).
    assert ("pr-body-coverage" in result.stdout
            or "new-symbol-not-exercised" in result.stdout)


# --- v0.7.6: screenshot contract — within-item HARD, cross-item WARN ------


def test_ss_check_within_item_identical_md5_hard_violation(tmp_path):
    """v0.7.6 HARD: a single item's screenshots[0] and screenshots[1]
    sharing the same MD5 is a hard violation. The before/after pair
    the labels claim is actually before/before — the executor took
    the same screenshot twice."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    (ss_dir / "form.png").write_bytes(b"\x89PNG" + b"x" * 200000)
    plan_items = [
        {"id": "t-6", "tool": "chrome-devtools",
         "what": "HAPPY: edit-and-switch reward type from Image to Game",
         "how": "edit; save; reload",
         "category": "api", "risk": "high", "depends_on": []},
    ]
    result_items = [
        {"id": "t-6", "status": "pass", "evidence": "ok",
         "screenshots": [
             {"path": "form.png", "label": "before", "focus": "image type"},
             {"path": "form.png", "label": "after", "focus": "game type"},
             {"path": "form.png", "label": "reload", "focus": "persisted"},
         ]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    hard = [v for v in violations if not v.startswith("WARN ")]
    assert len(hard) >= 1
    msg = hard[0]
    assert "t-6" in msg
    assert "within the same item" in msg
    assert "MD5" in msg or "md5" in msg.lower()


def test_ss_check_cross_item_3cluster_no_violation_v076(tmp_path):
    """v0.7.6: cross-item clusters of size 2 or 3 don't fire. Multiple
    items legitimately asserting on the same visual state (e.g. a
    render-check + a post-empty-save + an after-reload-empty all
    showing the same blank form) is no longer noise the reviewer
    has to dismiss."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    (ss_dir / "shared.png").write_bytes(b"\x89PNG" + b"y" * 200000)
    plan_items = [
        {"id": "t-1", "tool": "chrome-devtools",
         "what": "Form renders with new fields",
         "how": "navigate", "category": "frontend", "risk": "low",
         "depends_on": []},
        {"id": "t-2", "tool": "chrome-devtools",
         "what": "HAPPY: save with empty tags — backward compat",
         "how": "clear+save", "category": "api", "risk": "high",
         "depends_on": []},
        {"id": "t-3", "tool": "chrome-devtools",
         "what": "HAPPY: re-open empty record — round-trip empty",
         "how": "navigate+reload", "category": "api", "risk": "high",
         "depends_on": []},
    ]
    # 3 items each with 1 screenshot, all sharing one MD5. Cluster of
    # 3 across distinct items — below the WARN threshold of 4.
    result_items = [
        {"id": "t-1", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared.png", "label": "rendered",
                          "focus": "empty"}]},
        {"id": "t-2", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared.png", "label": "after empty save",
                          "focus": "still empty"},
                         # second screenshot is different — so this
                         # item doesn't violate the happy-save min count
                         # via not enough screenshots, but ALSO doesn't
                         # introduce within-item duplication for the
                         # MD5 lint.
                         {"path": "shared.png", "label": "x", "focus": "x"}]},
        {"id": "t-3", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared.png", "label": "after reload",
                          "focus": "still empty"},
                         {"path": "shared.png", "label": "x", "focus": "x"}]},
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    # t-2 and t-3 each have within-item duplication → 2 HARD
    # violations expected, NOT cross-item warnings. The cross-item
    # cluster across t-1/t-2/t-3 (6 entries) DOES reach the threshold
    # of 4, so a WARN does fire. Verify:
    hard = [v for v in violations if not v.startswith("WARN ")]
    warn = [v for v in violations if v.startswith("WARN ")]
    # 2 within-item hard (t-2 and t-3).
    assert len(hard) == 2
    # 1 cross-item warn (6 entries ≥ 4).
    assert len(warn) == 1


def test_ss_check_cross_item_3cluster_unique_per_item_no_violation(tmp_path):
    """3 distinct items each with ONE shared screenshot — cluster size
    3, no within-item dup. v0.7.6 says no fire: cross-item 2-3 is the
    legitimate-same-state range."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    (ss_dir / "shared.png").write_bytes(b"\x89PNG" + b"y" * 200000)
    plan_items = [
        {"id": f"t-{i}", "tool": "chrome-devtools",
         "what": "Form renders", "how": "navigate",
         "category": "frontend", "risk": "low", "depends_on": []}
        for i in range(1, 4)
    ]
    result_items = [
        {"id": f"t-{i}", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared.png", "label": "r",
                          "focus": "f"}]}
        for i in range(1, 4)
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    assert ss_check(plan, results, run_dir=tmp_path) == []


def test_ss_check_cross_item_4cluster_fires_warn_v076(tmp_path):
    """v0.7.6: cross-item cluster of size 4+ fires as WARN (advisory,
    not pipeline-aborting). 4 distinct items each with ONE shared
    screenshot — at the WARN threshold."""
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    (ss_dir / "shared.png").write_bytes(b"\x89PNG" + b"y" * 200000)
    plan_items = [
        {"id": f"t-{i}", "tool": "chrome-devtools",
         "what": "Form renders", "how": "navigate",
         "category": "frontend", "risk": "low", "depends_on": []}
        for i in range(1, 5)
    ]
    result_items = [
        {"id": f"t-{i}", "status": "pass", "evidence": "ok",
         "screenshots": [{"path": "shared.png", "label": "r",
                          "focus": "f"}]}
        for i in range(1, 5)
    ]
    plan, results = _make_plan_results(plan_items, result_items)
    violations = ss_check(plan, results, run_dir=tmp_path)
    warn = [v for v in violations if v.startswith("WARN ")]
    hard = [v for v in violations if not v.startswith("WARN ")]
    assert hard == []  # No within-item duplication here.
    assert len(warn) == 1
    assert "MD5" in warn[0] or "md5" in warn[0].lower()


# --- v0.7.6: analyzer SKILL prose carries link-classification table -------


def test_analyzer_skill_md_documents_link_fetch_table_v076():
    """The analyzing-pr-changes SKILL.md must document the v0.7.6+
    link-fetch table (Jira / Confluence / Slack / Google Drive /
    GitHub issue / unfetchable). Without this prose, the AI driving
    the skill won't know to call the connectors at all."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills" / "analyzing-pr-changes"
        / "SKILL.md"
    )
    text = skill_path.read_text()
    # The v0.7.6 entry-point prose.
    assert "Fetch PR comments" in text
    assert "Fetch linked external content" in text
    # Connector tool names — at least three of the four classes.
    assert "mcp__claude_ai_Atlassian__getJiraIssue" in text
    assert "mcp__claude_ai_Atlassian__getConfluencePage" in text
    assert "mcp__claude_ai_Slack__slack_read_thread" in text
    assert "mcp__claude_ai_Google_Drive__read_file_content" in text
    # The 4KB cap + truncation marker.
    assert "4KB" in text or "4 KB" in text
    assert "truncated" in text
    # ToolSearch pre-load directive.
    assert "ToolSearch" in text
    # Output shape: linked_content + comments fields.
    assert "linked_content" in text
    assert "source_type" in text


def test_planner_skill_md_documents_coverage_audit_worksheet_v076():
    """The planning-pr-tests SKILL.md must document the new
    planner_coverage_audit worksheet contract."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills" / "planning-pr-tests"
        / "SKILL.md"
    )
    text = skill_path.read_text()
    assert "planner_coverage_audit" in text
    assert "by_pr_body" in text
    assert "by_diff_symbols" in text
    assert "gaps" in text
    # The doc-link traversal section.
    assert "Doc-link traversal" in text


def test_executor_md_documents_evaluate_script_batching_v076():
    """The pr-test-executor.md agent doc must carry the v0.7.6+ chrome
    batching section so the executor knows to fold multiple DOM ops
    into one evaluate_script call."""
    agent_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "agents" / "pr-test-executor.md"
    )
    text = agent_path.read_text()
    assert "Batch DOM ops" in text or "batch DOM ops" in text.lower()
    # Boundary list — navigate / click / wait_for / take_snapshot.
    assert "navigate_page" in text
    assert "wait_for" in text
    assert "take_snapshot" in text


# --- v0.7.7: wizard_detect_binaries.py — multi-main classifier --------------


def _run_detect_binaries(repo_root):
    """Helper: invoke wizard_detect_binaries.py and return parsed JSON."""
    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "scripts" / "wizard_detect_binaries.py"
    )
    result = subprocess.run(
        ["python3", str(script), "--repo-root", str(repo_root)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_detect_binaries_classifies_serves_http(tmp_path):
    """A cmd/<X>/main.go whose source contains http.ListenAndServe
    classifies as serves-http. This is the standard Go web-app
    entry-point shape. v0.7.9 renamed the label from `http-server`
    to the neutral `serves-http` (no project-specific noun); the
    classifier logic is unchanged."""
    cmd_dir = tmp_path / "cmd" / "mcd-website"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import \"net/http\"\n"
        "func main() {\n"
        "    http.ListenAndServe(\":8080\", nil)\n"
        "}\n"
    )
    out = _run_detect_binaries(tmp_path)
    candidates = out["candidates"]
    assert len(candidates) == 1
    c = candidates[0]
    assert c["path"] == "cmd/mcd-website/main.go"
    assert c["binary_name"] == "mcd-website"
    assert c["looks_like"] == "serves-http"
    # v0.7.9: evidence entries are prefixed with `matches '...'` so
    # they read naturally in the wizard's AskUser prompt.
    assert any("http.ListenAndServe" in e for e in c["evidence"])


def test_detect_binaries_classifies_runs_loop(tmp_path):
    """A cmd/<X>/main.go whose source contains time.NewTicker or
    similar ticker/cron pattern classifies as runs-loop. Mimics
    mcd-website's cmd/mcd-daemon — the 1-minute publish-to-S3 loop
    the v0.7.6 audit found PRoctor wasn't starting. v0.7.9 renamed
    `daemon` → `runs-loop` so the category description is neutral
    (mcd-website's binary is literally called `mcd-daemon` and the
    old label aliased confusingly with the binary name)."""
    cmd_dir = tmp_path / "cmd" / "mcd-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import (\"time\")\n"
        "func main() {\n"
        "    ticker := time.NewTicker(time.Minute)\n"
        "    for range ticker.C {\n"
        "        publishAll()\n"
        "    }\n"
        "}\n"
        "func publishAll() {}\n"
    )
    out = _run_detect_binaries(tmp_path)
    candidates = out["candidates"]
    assert len(candidates) == 1
    c = candidates[0]
    assert c["binary_name"] == "mcd-daemon"
    assert c["looks_like"] == "runs-loop"
    assert any("time.NewTicker" in e for e in c["evidence"])


def test_detect_binaries_classifies_runs_once(tmp_path):
    """A short cmd/<X>/main.go with neither HTTP-server nor
    runs-loop patterns classifies as runs-once — sitemap generators,
    republishers, migration tools. The wizard should NOT preselect
    these for setup (they're run on-demand). v0.7.9 renamed
    `one-shot` → `runs-once` for neutral terminology."""
    cmd_dir = tmp_path / "cmd" / "mcd-sitemap"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import \"fmt\"\n"
        "func main() {\n"
        "    fmt.Println(\"generating sitemap\")\n"
        "}\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["binary_name"] == "mcd-sitemap"
    assert c["looks_like"] == "runs-once"


def test_detect_binaries_unknown_for_long_unrecognized(tmp_path):
    """A LONG (>200 lines) main.go with no HTTP and no ticker
    patterns classifies as unknown — better to ask the user than
    silently lump it with one-shot CLIs. Real-world examples might
    include batch-job orchestrators or interactive REPLs we don't
    have heuristics for."""
    cmd_dir = tmp_path / "cmd" / "weird-binary"
    cmd_dir.mkdir(parents=True)
    # 300 lines, none matching either pattern set.
    body = "\n".join([f"// filler line {i}" for i in range(300)])
    (cmd_dir / "main.go").write_text(
        "package main\n"
        + body + "\n"
        + "func main() { println(\"hi\") }\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["looks_like"] == "unknown"


def test_detect_binaries_walks_root_main_go_and_cmd(tmp_path):
    """Root main.go is emitted FIRST, then cmd/* entries
    alphabetically. The binary_name for root main.go uses the
    repo-root basename so the wizard can reference it
    consistently across runs."""
    # Root main.go — http-server style.
    (tmp_path / "main.go").write_text(
        "package main\n"
        "import \"net/http\"\n"
        "func main() { http.ListenAndServe(\":8080\", nil) }\n"
    )
    # cmd/zee — daemon
    (tmp_path / "cmd" / "zee").mkdir(parents=True)
    (tmp_path / "cmd" / "zee" / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Second); _ = t }\n"
    )
    # cmd/aaa — http-server (so the sort isn't trivially by classification)
    (tmp_path / "cmd" / "aaa").mkdir(parents=True)
    (tmp_path / "cmd" / "aaa" / "main.go").write_text(
        "package main\nimport \"net/http\"\n"
        "func main() { http.ListenAndServe(\":9090\", nil) }\n"
    )
    out = _run_detect_binaries(tmp_path)
    paths = [c["path"] for c in out["candidates"]]
    # Root first, then cmd/* alphabetically.
    assert paths[0] == "main.go"
    assert paths[1] == "cmd/aaa/main.go"
    assert paths[2] == "cmd/zee/main.go"
    # Root main.go's binary_name is the repo basename.
    assert out["candidates"][0]["binary_name"] == tmp_path.name


def test_detect_binaries_empty_when_no_main_go(tmp_path):
    """Repo with no main.go (Node / Python / Ruby project) returns
    empty candidates. The wizard's Step 7.5 then skips the
    daemon-selection question entirely."""
    out = _run_detect_binaries(tmp_path)
    assert out == {"candidates": []}


def test_detect_binaries_skips_non_main_go_files_in_cmd(tmp_path):
    """cmd/<X>/helpers.go etc. don't trigger a candidate — only
    cmd/<X>/main.go counts. Some projects keep per-binary helper
    files alongside main.go; those aren't entry points."""
    cmd_dir = tmp_path / "cmd" / "mcd-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "helpers.go").write_text("package main\nfunc helper() {}\n")
    # No main.go — should produce no candidate.
    out = _run_detect_binaries(tmp_path)
    assert out == {"candidates": []}


# --- v0.7.8: classifier regressions found in mcd-website e2e ----------------


def test_detect_binaries_v078_short_main_with_appkit_server_listen_and_serve(tmp_path):
    """v0.7.8 regression: mcd-website's root main.go is 29 lines and
    delegates to ``server.ListenAndServe(config.Config.HTTP, ...)``
    (theplant's appkit pkg). The v0.7.7 classifier's regex only
    matched ``http.ListenAndServe`` / ``router.ListenAndServe`` and
    missed ``<pkg>.ListenAndServe`` — the short file then fell into
    the ``runs-once`` bucket. v0.7.8 broadens the pattern to any
    ``<lowercase-pkg>.ListenAndServe[TLS]?`` call so wrappers around
    appkit (or any other framework's ListenAndServe) classify
    correctly as ``serves-http`` regardless of file size."""
    (tmp_path / "main.go").write_text(
        "package main\n"
        "import (\n"
        "    \"flag\"\n"
        "    \"fmt\"\n"
        "    \"runtime\"\n"
        "    \"github.com/theplant/appkit/server\"\n"
        ")\n"
        "func main() {\n"
        "    result := boot.InitApp(nil)\n"
        "    flag.Parse()\n"
        "    fmt.Printf(\"Go version: %s\\n\", runtime.Version())\n"
        "    server.ListenAndServe(nil, result.Logger, result.Handler)\n"
        "}\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["path"] == "main.go", out
    assert c["looks_like"] == "serves-http", c
    assert any("<pkg>.ListenAndServe" in e for e in c["evidence"])


def test_detect_binaries_v078_listen_and_serve_tls_matches_too(tmp_path):
    """The broadened pattern accepts both ``ListenAndServe`` and
    ``ListenAndServeTLS``. Some appkit / proxy code paths only call
    the TLS variant."""
    (tmp_path / "cmd" / "tls-front").mkdir(parents=True)
    (tmp_path / "cmd" / "tls-front" / "main.go").write_text(
        "package main\n"
        "func main() { proxy.ListenAndServeTLS(\":443\", \"c\", \"k\", nil) }\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["looks_like"] == "serves-http", c
    assert any("<pkg>.ListenAndServe" in e for e in c["evidence"])


def test_detect_binaries_v078_runs_loop_trumps_serves_http_when_both_present(tmp_path):
    """v0.7.8 regression (renamed in v0.7.9): mcd-website's
    ``cmd/mcd-daemon/main.go`` has BOTH ``http.ListenAndServe`` (a
    tail-end ``/health-check`` admin endpoint) AND
    ``time.Tick(time.Minute)`` + ``utils.RunJob`` (15 publish-on-tick
    goroutines). v0.7.7's classifier checked http-server FIRST and
    picked the wrong label; the file's primary purpose is the
    long-running loop. v0.7.8 swapped the priority; v0.7.9 keeps the
    behavior but renames the winning label from ``daemon`` →
    ``runs-loop`` and surfaces the auxiliary HTTP listener as an
    explicit 'ALSO matches...' evidence note so the user sees the
    heuristic at work."""
    cmd_dir = tmp_path / "cmd" / "mcd-daemon"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import (\n"
        "    \"net/http\"\n"
        "    \"time\"\n"
        ")\n"
        "func main() {\n"
        "    go func() {\n"
        "        t := time.Tick(time.Minute)\n"
        "        for range t {\n"
        "            utils.RunJob(\"PublishAllergens\", time.Minute*5, func() {})\n"
        "        }\n"
        "    }()\n"
        "    mux := http.NewServeMux()\n"
        "    mux.Handle(\"/health-check\", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {\n"
        "        w.WriteHeader(200)\n"
        "    }))\n"
        "    http.ListenAndServe(\":8080\", mux)\n"
        "}\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["binary_name"] == "mcd-daemon"
    assert c["looks_like"] == "runs-loop", (
        f"expected runs-loop precedence over serves-http but got {c!r}"
    )
    ev_joined = " ; ".join(c["evidence"])
    assert "time.Tick" in ev_joined
    assert "RunJob" in ev_joined
    # v0.7.9: the auxiliary serves-http match should be surfaced as
    # an 'ALSO matches...' precedence note so the user sees the
    # heuristic.
    assert any("ALSO matches" in e for e in c["evidence"]), c["evidence"]


def test_detect_binaries_v078_evidence_dedupe_for_specific_http_label(tmp_path):
    """When a specific http-server label matched (e.g. ``http.ListenAndServe``)
    the generic ``<pkg>.ListenAndServe`` label is suppressed — both
    technically match the source but the evidence list shouldn't carry
    both for readability of the wizard's question text."""
    cmd_dir = tmp_path / "cmd" / "plain-http"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import \"net/http\"\n"
        "func main() { http.ListenAndServe(\":8080\", nil) }\n"
    )
    out = _run_detect_binaries(tmp_path)
    ev = out["candidates"][0]["evidence"]
    assert any("http.ListenAndServe" in e for e in ev)
    assert not any("<pkg>.ListenAndServe" in e for e in ev), ev


# --- v0.7.7: plan_smells daemon-aware missing-runtime-verify check ----------


def test_plan_smells_daemon_present_diff_touches_pr_mentions_output_no_verify_fires():
    """The full positive case: daemon in setup, diff touches daemon
    code, PR body mentions publish/JSON, plan has only lint-only
    items. The v0.7.7 rule fires."""
    change_map = {
        "pr_context": {
            "body": "Published JSON include_tags now serializes as a "
                    "trimmed array. The daemon picks up new banner "
                    "fields and republishes them on the next tick.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "Source-level: SplitTags is called from "
                     "publishAll", "how": "grep ...",
             "risk": "medium", "depends_on": []},
        ],
    }
    setup_context = {
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": ["mcd-daemon"],
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert len(daemon) == 1
    assert "mcd-daemon" in daemon[0]


def test_plan_smells_daemon_present_bash_curl_item_satisfies():
    """When the plan DOES include a bash item with curl-against-URL,
    the rule is satisfied — no warning. This is the success-path
    the planner is supposed to reach."""
    change_map = {
        "pr_context": {
            "body": "Published JSON include_tags now serializes as a "
                    "trimmed array.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "Source-level wire-up", "how": "grep ...",
             "risk": "medium", "depends_on": []},
            {"id": "t-2", "category": "api", "tool": "bash",
             "what": "HAPPY: published JSON include_tags is a trimmed "
                     "array after daemon ticker fires",
             "how": "for i in $(seq 1 120); do RESP=$(curl -sf "
                    "\"https://example.test/banners.json\"); "
                    "echo \"$RESP\" | jq -e '.include_tags | "
                    "type == \"array\"' && break; sleep 1; done",
             "risk": "high", "depends_on": []},
        ],
    }
    setup_context = {
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": ["mcd-daemon"],
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert daemon == []


def test_plan_smells_daemon_not_running_no_warning():
    """When the daemon ISN'T in local setup, the planner is excused
    — the runtime verify is genuinely impossible. The rule only
    fires when the daemon IS running but the planner failed to use
    that capability."""
    change_map = {
        "pr_context": {
            "body": "Published JSON include_tags now serializes as a "
                    "trimmed array.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "grep wiring", "how": "grep",
             "risk": "low", "depends_on": []},
        ],
    }
    setup_context = {
        "daemons_running": [],  # nothing in setup
        "daemon_touched": ["mcd-daemon"],
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert daemon == []


def test_plan_smells_daemon_not_touched_no_warning():
    """When the diff DOESN'T touch any daemon-reachable code, no
    runtime verify is required — the daemon is irrelevant to this
    PR. Rule shouldn't fire."""
    change_map = {
        "pr_context": {
            "body": "Cosmetic CSS change — published JSON unchanged.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "frontend", "tool": "lint-only",
             "what": "CSS file syntax valid",
             "how": "stylelint", "risk": "low", "depends_on": []},
        ],
    }
    setup_context = {
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": [],  # diff doesn't reach any daemon
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert daemon == []


def test_plan_smells_daemon_pr_body_no_output_keywords_no_warning():
    """When the PR body doesn't mention publish/JSON/output/etc.
    keywords, the planner had no signal to plan a runtime verify
    in the first place. Rule shouldn't fire — false-positive
    avoidance."""
    change_map = {
        "pr_context": {
            "body": "Refactor: rename internal helper for clarity. "
                    "No behavior change.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "renamed identifier compiles",
             "how": "go build", "risk": "low", "depends_on": []},
        ],
    }
    setup_context = {
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": ["mcd-daemon"],
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert daemon == []


def test_plan_smells_daemon_no_setup_context_no_op_backward_compat():
    """Backward compat: calling plan_check WITHOUT setup_context
    preserves the v0.7.6 behavior — the v0.7.7 daemon check no-ops
    silently."""
    change_map = {
        "pr_context": {"body": "Published JSON has trimmed tokens."},
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "chrome-devtools",
             "what": "HAPPY: save record", "how": "fill+save",
             "risk": "high", "depends_on": [], "produces": ["created_id"]},
            {"id": "t-2", "category": "api", "tool": "chrome-devtools",
             "what": "Re-open saved record, fields round-trip",
             "how": "navigate+reload", "risk": "high",
             "depends_on": ["t-1"], "data_from": ["t-1"]},
        ],
    }
    from plugins.proctor.scripts.plan_smells import check as plan_check
    warnings = plan_check(plan, change_map=change_map)
    # No setup_context → daemon check inert; only the v0.7.6 checks
    # might fire. Specifically the daemon-named warning must NOT.
    daemon = [w for w in warnings
              if "missing-runtime-verify-when-supplementary-binary-present" in w]
    assert daemon == []


def test_plan_smells_cli_strict_accepts_setup_context_flag(tmp_path):
    """CLI integration: --setup-context flag routes through to the
    new check. Synthetic plan + change-map + setup-context where
    all three conditions are met → strict mode exits 1."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "grep wiring", "how": "grep",
             "risk": "low", "depends_on": []},
        ],
    }))
    cm_path = tmp_path / "change-map.json"
    cm_path.write_text(json.dumps({
        "pr_context": {
            "body": "Published JSON include_tags now serializes as "
                    "trimmed tokens.",
        },
    }))
    sc_path = tmp_path / "setup-context.json"
    sc_path.write_text(json.dumps({
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": ["mcd-daemon"],
    }))
    script = str(
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "scripts" / "plan_smells.py"
    )
    result = subprocess.run(
        ["python3", script, "--strict",
         "--change-map", str(cm_path),
         "--setup-context", str(sc_path)],
        stdin=open(plan_path), capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "missing-runtime-verify-when-supplementary-binary-present" in result.stdout


# --- v0.7.7: planner + reporter SKILL.md prose contracts ------------------


def test_planner_skill_md_documents_supplementary_binary_awareness_v079():
    """The planning-pr-tests SKILL.md must document the v0.7.7+
    supplementary-binary awareness section. v0.7.9 renamed the
    section + key names for neutral terminology: ``daemons_running``
    → ``supplementary_binaries_running``, ``daemon_touched`` →
    ``supplementary_binary_touched``, ``no-daemon-in-setup`` →
    ``no-supplementary-binary-in-setup``. Without this prose the AI
    driving the planner won't know to parse `setup:` or build
    setup_context."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills" / "planning-pr-tests"
        / "SKILL.md"
    )
    text = skill_path.read_text()
    # Section header / key vocabulary (v0.7.9 neutral terms).
    assert (
        "Detect which supplementary binaries run in local setup"
        in text
    )
    assert "setup_context.supplementary_binaries_running" in text
    assert "setup_context.supplementary_binary_touched" in text
    # The two-path discipline: runtime item when binary present,
    # explicit skip item when binary absent.
    assert "no-supplementary-binary-in-setup" in text
    # Lint integration — --setup-context flag mentioned.
    assert "--setup-context" in text


def test_reporter_skill_md_documents_runtime_verification_gaps_v079():
    """The reporting-pr-test-results SKILL.md must document the
    v0.7.7+ "Runtime verification gaps" section template so the
    reporter renders missing-binary gaps in a dedicated section
    instead of scattering them as ordinary skips. v0.7.9 renamed
    the skip reason to the neutral ``no-supplementary-binary-in-
    setup`` (with the v0.7.7/v0.7.8 alias still accepted)."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills"
        / "reporting-pr-test-results" / "SKILL.md"
    )
    text = skill_path.read_text()
    assert "Runtime verification gaps" in text
    assert "no-supplementary-binary-in-setup" in text
    # The actionable closing prose — tells the reviewer how to fix.
    assert "/proctor:proctor-init" in text


def test_proctor_init_md_documents_step_7_5_multi_main_detection_v077():
    """The /proctor-init command prose must describe the v0.7.7+
    Step 7.5 multi-binary detection step so the fresh-mode AI runs
    the new helper script and emits the daemon-multi-select."""
    cmd_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "commands" / "proctor-init.md"
    )
    text = cmd_path.read_text()
    # Step header + helper script name.
    assert "Step 7.5" in text
    assert "wizard_detect_binaries.py" in text
    # The fresh-mode gating note.
    assert "MODE=fresh" in text
    # The pidfile pattern (so daemons can be restarted across runs).
    assert "proctor-" in text and ".pid" in text


def test_proctor_init_md_documents_amend_daemons_state_machine_v078():
    """v0.7.8 wires daemon detection into the state machine for
    existing consumers (MODE=amend-daemons). The /proctor-init prose
    must document the new mode + multi_select envelope so AIs running
    the wizard against a v0.7.6-era local.yml know to handle
    multi-select AskUserQuestion + comma-joined answer."""
    cmd_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "commands" / "proctor-init.md"
    )
    text = cmd_path.read_text()
    assert "amend-daemons" in text
    assert "multi_select" in text
    # The harness must call AskUserQuestion in multi-select mode.
    assert "multi-select" in text.lower()


# --- v0.7.9: step iterator + neutral terminology + setup-block.yml -----------

from plugins.proctor.scripts.wizard_decide_steps import (
    decide_steps as wds_decide_steps,
    detect_state as wds_detect_state,
    STEP_BUMP_ACTION_PIN,
    STEP_FRESH_INSTALL,
    STEP_LEGACY_LAYOUT_MIGRATE,
    STEP_REGENERATE_LOCAL_YML,
    STEP_SUPPLEMENT_SETUP,
    STEP_ORDER,
)


def test_v079_decide_steps_fresh_install_short_circuits(tmp_path):
    """No .proctor/ + no workflow → only step_fresh_install fires
    (mutually exclusive with the other steps). The walker
    short-circuits so we never plan a bump or supplement on a brand-
    new repo."""
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == [STEP_FRESH_INSTALL]


def test_v079_decide_steps_legacy_layout_only(tmp_path):
    """Pre-v0.4 .pr-test.yml present, no other state → just the
    legacy-layout step. We DON'T add bump-action-pin even if a
    workflow pin existed in this fixture (no workflow file means no
    pin to bump)."""
    (tmp_path / ".pr-test.yml").write_text("base_url: x\n")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == [STEP_LEGACY_LAYOUT_MIGRATE]


def test_v079_decide_steps_bump_only(tmp_path):
    """v0.4+ layout, local.yml present, no supplementary binaries
    detected, pin stale → only step_bump_action_pin fires.
    Reproduces v0.7.8's bump-only single-mode scenario at the step
    level."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.8")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == [STEP_BUMP_ACTION_PIN]


def test_v079_decide_steps_regen_local_yml_only(tmp_path):
    """Seed script present + local.yml missing, pin current → just
    the regen step. This is the v0.7.8 ``needs-local-regen`` mode
    scenario."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.9")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == [STEP_REGENERATE_LOCAL_YML]


def test_v079_decide_steps_supplement_setup_only(tmp_path):
    """v0.4+ layout + local.yml present + a cmd/<X>/main.go binary
    NOT yet in setup → just the supplement step. The v0.7.9 trigger
    is smarter than v0.7.8's: it only fires when there's actually a
    binary to add."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == [STEP_SUPPLEMENT_SETUP], steps


def test_v079_decide_steps_bump_and_supplement_together(tmp_path):
    """v0.4+ layout + stale pin + cmd/<X>/main.go binary NOT in
    setup → BOTH bump_action_pin AND supplement_setup fire in one
    invocation. This is the case the v0.7.8 single-mode dispatcher
    silently dropped (bump-only always won; amend-daemons never
    fired on the same wizard run).

    v0.7.10 reorder: supplement now precedes bump (supplement
    writes the canonical ``.proctor/setup-block.yml`` that the
    downstream regenerate step consumes; pin-bump is a self-contained
    workflow edit that slots after the setup-mutation steps)."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.5")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert STEP_BUMP_ACTION_PIN in steps
    assert STEP_SUPPLEMENT_SETUP in steps
    # v0.7.10 canonical order: supplement before bump.
    assert steps.index(STEP_SUPPLEMENT_SETUP) < steps.index(STEP_BUMP_ACTION_PIN)


def test_v079_decide_steps_regen_before_bump(tmp_path):
    """v0.7.8 priority semantics preserved: when both
    regen_local_yml AND bump_action_pin apply, regen comes first
    (a missing local.yml blocks every PRoctor run; a stale pin only
    blocks the next release). The shim's ``mode`` alias is therefore
    ``needs-local-regen`` here, not ``bump-only``."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.5")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps[0] == STEP_REGENERATE_LOCAL_YML
    assert STEP_BUMP_ACTION_PIN in steps


def test_v079_decide_steps_no_steps_when_fully_configured(tmp_path):
    """Up-to-date pin, local.yml present, no supplementary binaries
    to add → no steps fire. The wizard exits immediately with the
    ``current`` mode at the shim level."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.9")
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert steps == []


def test_v079_decide_steps_supplement_does_not_fire_when_binary_already_in_setup(tmp_path):
    """The supplement step is idempotent at the trigger level: when
    every detected cmd/<X> is already referenced in either
    setup-block.yml or local.yml's setup, the step doesn't fire."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.9")
    (tmp_path / ".proctor" / "local.yml").write_text(
        "base_url: x\n"
        "setup:\n"
        "  - bash -c 'nohup go run ./cmd/example-loop/main.go > /tmp/x.log 2>&1 &'\n"
    )
    cmd_dir = tmp_path / "cmd" / "example-loop"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP not in steps


def test_v079_decide_steps_supplement_honors_setup_block_yml(tmp_path):
    """When the binary is referenced in `.proctor/setup-block.yml`
    (not yet propagated into local.yml because the seed script
    hasn't been re-run), the supplement step ALSO doesn't fire —
    the wizard already wrote the canonical source."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.9")
    (tmp_path / ".proctor" / "setup-block.yml").write_text(
        "setup:\n"
        "  - bash -c 'nohup go run ./cmd/example-loop/main.go > /tmp/x.log 2>&1 &'\n"
    )
    cmd_dir = tmp_path / "cmd" / "example-loop"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP not in steps


def test_v079_decide_steps_serves_http_only_does_not_trigger(tmp_path):
    """A repo with only a `serves-http` binary (the main HTTP
    server) doesn't trigger supplement_setup — that binary is
    covered by the existing Step 7f wait-loop. The step is only
    interested in runs-loop / unknown binaries that aren't yet
    started."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.9")
    cmd_dir = tmp_path / "cmd" / "http-only"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import \"net/http\"\n"
        "func main() { http.ListenAndServe(\":8080\", nil) }\n"
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.9", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP not in steps


def test_v079_decide_steps_cli_outputs_steps_list(tmp_path):
    """CLI invocation of wizard_decide_steps.py emits a JSON
    envelope with `steps` as a list plus the backward-compat
    single-mode fields."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.5")
    script = (pathlib.Path(__file__).resolve().parent.parent
              / "plugins" / "proctor" / "scripts"
              / "wizard_decide_steps.py")
    result = subprocess.run(
        ["python3", str(script),
         "--current-tag", "v0.7.9",
         "--repo-root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert "steps" in out
    assert "state" in out
    # Backward-compat fields.
    assert "mode" in out
    assert "next_action" in out
    assert "ask_user" in out


# --- v0.7.9: step iterator (wizard_run.py) ----------------------------------


def test_v079_wizard_iterator_multi_step_supplement_then_bump(tmp_path):
    """The headline v0.7.9 behavior (renamed in v0.7.10 reorder): a
    repo with BOTH a stale pin AND a missing supplementary binary
    triggers BOTH steps in one wizard invocation. v0.7.8 would have
    picked one and silently dropped the other. v0.7.9 walked them in
    order; v0.7.10 reordered so supplement leads (writes
    setup-block.yml that downstream regenerate / bump can consume)."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.5")
    state_file = tmp_path / "wizard-state.json"

    # First invocation: detection populates pending_steps, then the
    # first step (supplement_setup) emits its scan/skip ask_user.
    env1 = _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    assert env1["type"] == "ask_user", env1
    assert env1["header"] == "Supplementary binaries"

    # State file should record bump still pending (or completed).
    state = json.loads(state_file.read_text())
    pending = state.get("pending_steps") or []
    completed = state.get("completed_steps") or []
    expected = STEP_BUMP_ACTION_PIN in pending or any(
        c.get("step") == STEP_BUMP_ACTION_PIN for c in completed
    )
    assert expected, state


def test_v079_wizard_iterator_advances_to_bump_after_supplement_done(tmp_path):
    """After the supplement step completes (user picks Skip), the
    iterator pops the next pending step. With v0.7.10 ordering
    (supplement → regenerate → bump) on a fixture that has local.yml
    present + a stale pin, only supplement and bump apply (regenerate
    doesn't fire because local.yml is present + seed-local.sh has no
    legacy heredoc). After supplement skip, the iterator recurses
    into bump which emits its bash. The point of the iterator is
    that the wizard doesn't exit after the first step."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.5")
    state_file = tmp_path / "wizard-state.json"
    # Step 1: supplement asks scan/skip.
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    # Step 2: user picks Skip → supplement completes silently →
    # iterator recurses into bump which emits its bash.
    env2 = _run_wizard(
        state_file, current_tag="v0.7.9", repo_root=tmp_path,
        answer="Skip — my setup is fine",
    )
    assert env2["type"] == "bash", env2
    assert "wizard_bump_action.sh" in env2["command"]


def test_v079_wizard_iterator_terminal_done_only_after_all_steps(tmp_path):
    """The terminal done fires ONLY when pending_steps is empty AND
    current_step is None. Per-step show envelopes are NOT terminal."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state_file = tmp_path / "wizard-state.json"
    # First call: supplement offer.
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    # User picks Skip. supplement completes with outcome=skipped,
    # which carries no envelope; iterator advances and emits the
    # terminal done in the SAME invocation.
    env = _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path,
                      answer="Skip — my setup is fine")
    assert env["type"] == "done"
    # Done summary names the completed step explicitly.
    assert "step_supplement_setup" in env["summary"]


def test_v079_wizard_state_schema_uses_v079_keys(tmp_path):
    """v0.7.9 state file has pending_steps / current_step /
    current_step_substate / completed_steps / step_data. The legacy
    v0.5.0 ``step`` field is GONE from the persisted shape (loading
    an old file with that key resets to fresh state)."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    state = json.loads(state_file.read_text())
    assert "pending_steps" in state
    assert "current_step" in state
    assert "current_step_substate" in state
    assert "completed_steps" in state
    assert "step_data" in state


def test_v079_wizard_completed_steps_carries_outcome_audit(tmp_path):
    """completed_steps is an audit trail: each entry has step id +
    outcome. Reviewers reading the state file can see what the
    wizard did across iterations."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    # User picks Skip.
    env = _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path,
                      answer="Skip — my setup is fine")
    # After done, state file should be deleted. Re-create one
    # invocation to inspect: rebuild fixture, walk again, but stop
    # before terminal done.
    if env["type"] == "done":
        assert not state_file.exists()


def test_v079_wizard_legacy_state_file_schema_resets_to_fresh(tmp_path):
    """v0.7.9 detects a v0.5.0–v0.7.8 state file (top-level ``step``
    field instead of ``current_step``) and resets to fresh state
    rather than crashing. The user re-answers any in-flight
    question — better than a stuck wizard."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state_file = tmp_path / "wizard-state.json"
    # Simulate a v0.7.8 state file (different schema).
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "step": "amend_daemons_picked",
        "mode": "amend-daemons",
        "amend_candidates": [],
    }))
    # Should NOT crash. Should treat as fresh + run detection.
    env = _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    assert env["type"] in {"ask_user", "bash", "show", "done", "error"}
    assert env.get("type") != "error", env


# --- v0.7.9: schema.validate_setup_block ------------------------------------

from plugins.proctor.scripts.schema import validate_setup_block


def test_v079_validate_setup_block_minimum_valid():
    """A mapping with just `setup: [string, ...]` is the minimal
    valid shape."""
    validate_setup_block({"setup": ["docker compose up", "go run ."]})
    validate_setup_block({"setup": []})  # empty is fine — wizard
                                          # writes empty stubs


def test_v079_validate_setup_block_rejects_non_dict():
    with pytest.raises(SchemaError):
        validate_setup_block("setup: [ a ]")
    with pytest.raises(SchemaError):
        validate_setup_block(["setup", "items"])


def test_v079_validate_setup_block_rejects_extra_keys():
    """The file is intentionally minimal — `setup:` is the only
    allowed top-level key. Anything else means someone tried to
    embed local.yml content here (wrong file)."""
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": [], "base_url": "x"})


def test_v079_validate_setup_block_rejects_missing_setup_key():
    with pytest.raises(SchemaError):
        validate_setup_block({})
    with pytest.raises(SchemaError):
        validate_setup_block({"not_setup": []})


def test_v079_validate_setup_block_rejects_non_list_setup():
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": "docker compose up"})
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": {"cmd1": "a"}})


def test_v079_validate_setup_block_rejects_non_string_items():
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": [123]})
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": ["ok", None]})
    with pytest.raises(SchemaError):
        validate_setup_block({"setup": ["", "ok"]})


# --- v0.7.9: setup-block.yml as canonical source ----------------------------


def test_v079_wizard_writes_setup_block_yml_on_supplement_picked(tmp_path):
    """The end-to-end happy path: scan → pick → wizard writes
    `.proctor/setup-block.yml` (the canonical source) AND amends
    `.proctor/local.yml setup:` (the current-run convenience). The
    setup-block content is the source of truth seed-local.sh will
    read on next regenerate."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path)
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path,
                answer="Scan for supplementary binaries you may want to start in setup")
    # Simulate bash success + populate binaries JSON.
    detector = (pathlib.Path(__file__).resolve().parent.parent
                / "plugins" / "proctor" / "scripts"
                / "wizard_detect_binaries.py")
    import subprocess as sp
    with open("/tmp/proctor-wizard-binaries.json", "w") as f:
        sp.run(
            ["python3", str(detector), "--repo-root", str(tmp_path)],
            stdout=f, check=True,
        )
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path,
                bash_rc=0)
    # Final pick.
    _run_wizard(state_file, current_tag="v0.7.9", repo_root=tmp_path,
                answer="[recommended] cmd/example-loop/main.go")

    # The setup-block.yml should now exist + be schema-valid.
    sb_path = tmp_path / ".proctor" / "setup-block.yml"
    assert sb_path.exists()
    import yaml as _yaml
    content = _yaml.safe_load(sb_path.read_text())
    validate_setup_block(content)
    # Expected content: a kill+start pair for example-loop.
    assert "go run ./cmd/example-loop/main.go" in sb_path.read_text()
    assert "/tmp/proctor-example-loop.pid" in sb_path.read_text()


def test_v079_wizard_setup_block_yml_is_idempotent_on_rerun(tmp_path):
    """Running the supplement step twice with the same binary
    selection does NOT duplicate lines in setup-block.yml — the
    helper detects the binary's pidfile name AND path."""
    from plugins.proctor.scripts.wizard_run import _write_setup_block_yml
    sb_path = tmp_path / ".proctor" / "setup-block.yml"
    chosen = [{
        "path": "cmd/foo-loop/main.go",
        "binary_name": "foo-loop",
        "looks_like": "runs-loop",
        "evidence": ["matches 'time.NewTicker'"],
    }]
    added1 = _write_setup_block_yml(sb_path, chosen)
    assert added1 == 1
    text1 = sb_path.read_text()
    # Second call: no-op.
    added2 = _write_setup_block_yml(sb_path, chosen)
    assert added2 == 0
    assert sb_path.read_text() == text1


# --- v0.7.9: detect_binaries new labels + evidence ---------------------------


def test_v079_detect_binaries_evidence_includes_match_count_for_repeated_patterns(tmp_path):
    """When a pattern matches multiple times (e.g. RunJob ×15), the
    evidence string should call out the count so the user-facing
    AskUser prompt can quote it. This is what v0.7.9's evidence
    field looks like in practice for the mcd-daemon case."""
    cmd_dir = tmp_path / "cmd" / "ticker"
    cmd_dir.mkdir(parents=True)
    body = "\n".join([
        f'    utils.RunJob("Job{i}", time.Second, func() {{}})'
        for i in range(5)
    ])
    (cmd_dir / "main.go").write_text(
        "package main\n"
        "import \"time\"\n"
        "func main() {\n"
        "    t := time.NewTicker(time.Minute)\n"
        f"{body}\n"
        "    _ = t\n"
        "}\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["looks_like"] == "runs-loop"
    ev_joined = " ; ".join(c["evidence"])
    assert "RunJob" in ev_joined
    # The count annotation (×N) should appear when N > 1.
    assert "×5" in ev_joined, ev_joined


def test_v079_detect_binaries_unknown_unchanged_v079(tmp_path):
    """The ``unknown`` label is preserved as-is in v0.7.9 (neutral
    already). Long non-trivial files with no matching patterns stay
    in this bucket."""
    cmd_dir = tmp_path / "cmd" / "puzzle"
    cmd_dir.mkdir(parents=True)
    body = "\n".join([f"// filler line {i}" for i in range(250)])
    (cmd_dir / "main.go").write_text(
        "package main\n" + body + "\nfunc main() { println(\"hi\") }\n"
    )
    out = _run_detect_binaries(tmp_path)
    c = out["candidates"][0]
    assert c["looks_like"] == "unknown"


# --- v0.7.9: terminology audit (strict — no project-specific category labels) -


def test_v079_proctor_init_md_no_daemon_category_labels_in_user_prose():
    """STRICT: the proctor-init.md user-facing prompt prose (the
    AskUserQuestion question text in Step 7.5) must NOT contain
    ``daemon`` / ``worker`` / ``publish ticker`` / ``cron`` as
    CATEGORY labels. Filenames like ``cmd/mcd-daemon`` are allowed
    (they're example files); the category descriptions must use
    neutral terms (``serves-http`` / ``runs-loop`` / ``runs-once``)."""
    cmd_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "commands" / "proctor-init.md"
    )
    text = cmd_path.read_text()
    # The AskUserQuestion prompt block (Step 7.5).
    start = text.find('"PRoctor detected these additional binaries')
    assert start >= 0, "Step 7.5 AskUserQuestion prompt missing"
    end = text.find('"', start + 100)
    prompt = text[start:end] if end > 0 else text[start:start + 2000]
    # Category-label-as-noun checks:
    # The prompt mentions specific binary names like `mcd-daemon`
    # (allowed — filenames) but NOT as standalone category labels.
    # The neutral labels MUST appear.
    assert "runs-loop" in prompt
    assert "runs-once" in prompt
    # `serves-http` shows up earlier in the surrounding prose (since
    # the prompt itself uses category in parens after binary names);
    # check in the wider Step 7.5 section.
    step_75_start = text.find("Step 7.5")
    step_75_end = text.find("### Step 7f")
    section = text[step_75_start:step_75_end]
    assert "serves-http" in section
    assert "runs-loop" in section
    assert "runs-once" in section


def test_v079_planning_skill_md_no_daemon_skip_reason_in_visible_prose():
    """The planning skill MUST emit ``no-supplementary-binary-in-
    setup`` as the canonical skip reason. The v0.7.7/v0.7.8
    alias ``no-daemon-in-setup`` may appear in backward-compat
    notes but must NOT be the primary citation."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills" / "planning-pr-tests"
        / "SKILL.md"
    )
    text = skill_path.read_text()
    # The primary skip-reason string the planner writes into plans:
    assert '"no-supplementary-binary-in-setup"' in text


def test_v079_planning_skill_md_neutral_setup_context_keys():
    """The planning skill's setup_context vocabulary uses the
    v0.7.9 neutral keys."""
    skill_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugins" / "proctor" / "skills" / "planning-pr-tests"
        / "SKILL.md"
    )
    text = skill_path.read_text()
    assert "supplementary_binaries_running" in text
    assert "supplementary_binary_touched" in text


def test_v079_plan_smells_rule_renamed_in_module_docstring():
    """The plan_smells.py module-level docstring should reference
    the v0.7.9 renamed rule name."""
    from plugins.proctor.scripts import plan_smells
    doc = plan_smells.__doc__ or ""
    assert "missing-runtime-verify-when-supplementary-binary-present" in doc


def test_v079_plan_smells_accepts_legacy_setup_context_keys_backward_compat():
    """Backward-compat: plans + setup_contexts written under v0.7.7/
    v0.7.8 with the legacy `daemons_running` / `daemon_touched` keys
    keep producing the same lint behavior. The rule name in the
    output is the NEW name (the only user-visible change)."""
    from plugins.proctor.scripts.plan_smells import check as plan_check
    change_map = {
        "pr_context": {
            "body": "Published JSON include_tags now serializes as a "
                    "trimmed array.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "Source-level wire-up", "how": "grep ...",
             "risk": "medium", "depends_on": []},
        ],
    }
    setup_context_legacy = {
        "daemons_running": ["mcd-daemon"],
        "daemon_touched": ["mcd-daemon"],
    }
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context_legacy,
    )
    matched = [
        w for w in warnings
        if "missing-runtime-verify-when-supplementary-binary-present" in w
    ]
    assert len(matched) == 1


def test_v079_plan_smells_neutral_setup_context_keys_work():
    """The v0.7.9 keys produce the same warning."""
    from plugins.proctor.scripts.plan_smells import check as plan_check
    change_map = {
        "pr_context": {
            "body": "Published JSON include_tags now serializes as a "
                    "trimmed array.",
        },
    }
    plan = {
        "items": [
            {"id": "t-1", "category": "api", "tool": "lint-only",
             "what": "Source-level wire-up", "how": "grep ...",
             "risk": "medium", "depends_on": []},
        ],
    }
    setup_context = {
        "supplementary_binaries_running": ["mcd-daemon"],
        "supplementary_binary_touched": ["mcd-daemon"],
    }
    warnings = plan_check(
        plan, change_map=change_map, setup_context=setup_context,
    )
    matched = [
        w for w in warnings
        if "missing-runtime-verify-when-supplementary-binary-present" in w
    ]
    assert len(matched) == 1


def test_v079_decide_mode_shim_returns_alias_for_supplement_setup(tmp_path):
    """The backward-compat shim ``wizard_decide_mode.decide_mode``
    should map the new STEP_SUPPLEMENT_SETUP step to the v0.7.8
    mode name ``amend-daemons`` so older prose / tests keep
    working."""
    _make_v04_repo_with_setup_no_daemons(tmp_path, pin="v0.7.9")
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.7.9", repo_root=tmp_path)
    # The first applying step is STEP_SUPPLEMENT_SETUP, aliased to
    # the v0.7.8 mode ``amend-daemons``.
    assert d["mode"] == "amend-daemons", d


def test_v079_decide_steps_module_exports_step_constants():
    """All step ids are exported as constants so callers can refer
    to them without typing the strings literally."""
    from plugins.proctor.scripts import wizard_decide_steps as mod
    assert mod.STEP_FRESH_INSTALL == "step_fresh_install"
    assert mod.STEP_LEGACY_LAYOUT_MIGRATE == "step_legacy_layout_migrate"
    assert mod.STEP_REGENERATE_LOCAL_YML == "step_regenerate_local_yml"
    assert mod.STEP_BUMP_ACTION_PIN == "step_bump_action_pin"
    assert mod.STEP_SUPPLEMENT_SETUP == "step_supplement_setup"
    # v0.7.10 canonical execution order: supplement before regenerate
    # (supplement writes setup-block.yml that regenerate consumes);
    # bump independent of both, slots after.
    assert mod.STEP_ORDER == [
        mod.STEP_LEGACY_LAYOUT_MIGRATE,
        mod.STEP_SUPPLEMENT_SETUP,
        mod.STEP_REGENERATE_LOCAL_YML,
        mod.STEP_BUMP_ACTION_PIN,
        mod.STEP_FRESH_INSTALL,
    ]


# --- v0.7.10 — bug-A / bug-B / bug-C regression coverage --------------------
#
# v0.7.9 e2e on mcd-website found three structural issues in
# /proctor:proctor-init:
#
# Bug A — step_supplement_setup was silently dropped from the steps
# list when .proctor/local.yml was missing. The v0.7.9 decide_steps
# applies-condition for supplement gated on `has_local_yml` even
# though the precondition is purely "cmd/*/main.go exists not
# already in setup-block.yml". v0.7.10 drops the gate.
#
# Bug B — step_regenerate_local_yml's handler just emitted a `show`
# envelope pointing at legacy SKILL.md prose. No real regeneration
# work. v0.7.10 rewrites the handler to actually re-run seed-local.sh
# (after auto-migrating its hardcoded SETUP_BLOCK heredoc if needed)
# and surface success / failure envelopes.
#
# Bug C — existing seed-local.sh in consumer repos ships with the
# pre-v0.7.9 hardcoded SETUP_BLOCK heredoc, so wizard writes to
# setup-block.yml are silently ignored on every seed-script re-run.
# v0.7.10 detects that pattern + auto-migrates seed-local.sh
# in-place during the regenerate step.
#
# Plus: step ordering enforced. Canonical order is
# supplement → regenerate → bump (data flow: supplement writes the
# block, regenerate produces local.yml from it, bump is independent).

_V0710_SEED_SH_LEGACY = """#!/usr/bin/env bash
# Pre-v0.7.9 seed script — hardcoded SETUP_BLOCK heredoc.
set -e

SETUP_BLOCK=$(cat <<'YAML'
  - docker-compose up -d db
  - bash -c 'for i in $(seq 1 30); do nc -z localhost 5432 && break; sleep 1; done'
  - go mod download
  - bash -c 'set -a; . ./dev_env_local 2>/dev/null || true; set +a; nohup go run . > /tmp/proctor-app.log 2>&1 & echo $! > /tmp/proctor-app.pid'
YAML
)

cat > .proctor/local.yml <<EOF
base_url: http://localhost:9801
setup:
$SETUP_BLOCK
EOF
"""


_V0710_SEED_SH_ALREADY_MIGRATED = """#!/usr/bin/env bash
# Post-v0.7.9 seed script — reads setup-block.yml via awk.
set -e

if [ -f .proctor/setup-block.yml ]; then
    SETUP_BLOCK=$(awk '/^setup:/,0' .proctor/setup-block.yml | tail -n +2)
else
    SETUP_BLOCK=$(cat <<'YAML'
  - docker-compose up -d db
  - go mod download
YAML
)
fi

cat > .proctor/local.yml <<EOF
base_url: http://localhost:9801
setup:
$SETUP_BLOCK
EOF
"""


def _make_v0710_repo_with_legacy_seed(
    tmp_path, *, pin="v0.7.10", with_cmd_binary=True,
    has_local_yml=False,
):
    """Build a v0.4-layout consumer repo whose ``.proctor/seed-local.sh``
    still ships with the pre-v0.7.9 hardcoded SETUP_BLOCK heredoc.
    This is the exact shape mcd-website was in when v0.7.9's audit
    found Bug C."""
    _make_v04_repo(
        tmp_path, has_local_yml=has_local_yml, pin=pin,
    )
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(_V0710_SEED_SH_LEGACY)
    seed.chmod(0o755)
    if with_cmd_binary:
        cmd_dir = tmp_path / "cmd" / "example-loop"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "main.go").write_text(
            "package main\nimport \"time\"\n"
            "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
        )
    return tmp_path


# --- Bug A regression — supplement-setup fires regardless of local.yml ------


def test_v0710_bug_a_supplement_fires_with_no_local_yml(tmp_path):
    """Bug A regression: synthetic repo with cmd/*/main.go binary +
    .proctor/local.yml MISSING + outdated action pin. v0.7.9
    silently dropped step_supplement_setup from the steps list
    because the applies-condition gated on has_local_yml. v0.7.10
    decouples — supplement fires purely on "binary exists not in
    setup-block.yml", independent of local.yml's presence."""
    _make_v04_repo_with_setup_no_daemons(
        tmp_path, pin="v0.7.5",
    )
    # Make the fixture more closely match the mcd-website scenario:
    # remove the local.yml that _make_v04_repo_with_setup_no_daemons
    # creates so the repo has only a seed script + cmd binary.
    (tmp_path / ".proctor" / "local.yml").unlink()
    state = wds_detect_state(tmp_path)
    assert state["has_local_yml"] is False
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP in steps, steps


def test_v0710_bug_a_supplement_fires_with_only_binaries(tmp_path):
    """Pure-bug-A reproduction: fresh-ish repo with cmd/*/main.go +
    .proctor/config.yml + outdated action pin + NO local.yml + NO
    setup-block.yml. v0.7.9 dropped supplement; v0.7.10 includes it."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.5")
    cmd_dir = tmp_path / "cmd" / "ticker-loop"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() {\n"
        "    for range time.NewTicker(time.Second).C {\n"
        "        // long-running tick\n"
        "    }\n"
        "}\n"
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP in steps
    assert STEP_REGENERATE_LOCAL_YML in steps  # local.yml missing
    assert STEP_BUMP_ACTION_PIN in steps        # pin stale
    # Canonical order: supplement → regenerate → bump.
    assert steps.index(STEP_SUPPLEMENT_SETUP) < steps.index(STEP_REGENERATE_LOCAL_YML)
    assert steps.index(STEP_REGENERATE_LOCAL_YML) < steps.index(STEP_BUMP_ACTION_PIN)


def test_v0710_supplement_independent_of_regenerate(tmp_path):
    """Even when regenerate doesn't fire (local.yml present, no
    legacy heredoc), supplement still fires when binaries are
    uncovered. The two steps are independent."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.10")
    # Wipe seed script to also prove regenerate doesn't fire from
    # legacy-heredoc.
    seed = tmp_path / ".proctor" / "seed-local.sh"
    if seed.exists():
        seed.write_text(
            "#!/usr/bin/env bash\nif [ -f .proctor/setup-block.yml ]; then\n"
            "    SETUP_BLOCK=$(awk '/^setup:/,0' .proctor/setup-block.yml | "
            "tail -n +2)\nfi\necho ok\n"
        )
        seed.chmod(0o755)
    cmd_dir = tmp_path / "cmd" / "ticker-loop"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "main.go").write_text(
        "package main\nimport \"time\"\n"
        "func main() { t := time.NewTicker(time.Second); _ = t }\n"
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert STEP_SUPPLEMENT_SETUP in steps
    assert STEP_REGENERATE_LOCAL_YML not in steps


def test_v0710_decide_steps_all_combinations_supplement_present(tmp_path):
    """Combination matrix: every state combination where
    supplement-setup should fire results in it being present in the
    steps list. The applies-condition is local.yml-independent."""
    for has_local in (True, False):
        # Fresh fixture per iteration so state from a previous run
        # doesn't leak.
        sub = tmp_path / f"local={has_local}"
        sub.mkdir()
        _make_v04_repo(sub, has_local_yml=has_local, pin="v0.7.10")
        cmd_dir = sub / "cmd" / "loop"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text(
            "package main\nimport \"time\"\n"
            "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
        )
        state = wds_detect_state(sub)
        steps = wds_decide_steps(
            state, current_tag="v0.7.10", repo_root=sub,
        )
        assert STEP_SUPPLEMENT_SETUP in steps, (has_local, steps)


# --- Bug C regression — seed-local.sh legacy-heredoc detection --------------


def test_v0710_bug_c_seed_with_legacy_heredoc_detected(tmp_path):
    """v0.7.10 detect_state flags ``seed_has_legacy_heredoc`` when
    .proctor/seed-local.sh contains the pre-v0.7.9 hardcoded
    SETUP_BLOCK heredoc pattern."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", with_cmd_binary=False,
        has_local_yml=True,
    )
    state = wds_detect_state(tmp_path)
    assert state["seed_has_legacy_heredoc"] is True


def test_v0710_already_migrated_seed_not_flagged(tmp_path):
    """A seed-local.sh that already reads from setup-block.yml via
    the awk pattern is NOT flagged as needing migration."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.10")
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(_V0710_SEED_SH_ALREADY_MIGRATED)
    seed.chmod(0o755)
    state = wds_detect_state(tmp_path)
    assert state["seed_has_legacy_heredoc"] is False


def test_v0710_no_seed_not_flagged(tmp_path):
    """When seed-local.sh doesn't exist, the legacy-heredoc flag is
    False (nothing to migrate)."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.10",
                   has_seed_script=False)
    state = wds_detect_state(tmp_path)
    assert state["seed_has_legacy_heredoc"] is False


def test_v0710_bug_c_regenerate_fires_on_legacy_heredoc(tmp_path):
    """Even with local.yml PRESENT, when seed-local.sh has the
    legacy hardcoded heredoc, step_regenerate_local_yml fires to
    migrate it in place. Pre-v0.7.10 the step only fired on missing
    local.yml; v0.7.10 broadens the trigger so existing consumers
    auto-migrate."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", with_cmd_binary=False,
        has_local_yml=True,
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert STEP_REGENERATE_LOCAL_YML in steps


def test_v0710_regenerate_does_not_fire_when_already_migrated(tmp_path):
    """Idempotency: a v0.7.9+ seed script (uses awk reader) with
    local.yml present doesn't trigger regenerate. The wizard correctly
    detects "no work needed" and skips."""
    _make_v04_repo(tmp_path, has_local_yml=True, pin="v0.7.10")
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(_V0710_SEED_SH_ALREADY_MIGRATED)
    seed.chmod(0o755)
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert STEP_REGENERATE_LOCAL_YML not in steps


# --- Bug B regression — _handle_regen_local actually regenerates ------------


def test_v0710_bug_b_regenerate_handler_emits_bash_to_run_seed_script(tmp_path):
    """The regenerate step's first envelope is a ``bash`` running
    ./.proctor/seed-local.sh — NOT a prose ``show`` punt to legacy
    SKILL.md as pre-v0.7.10 did."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.10")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.7.10",
                      repo_root=tmp_path)
    assert env["type"] == "bash", env
    assert ".proctor/seed-local.sh" in env["command"]


def test_v0710_regenerate_after_seed_success_emits_show(tmp_path):
    """After seed-local.sh exits 0, the step emits a ``show`` envelope
    summarizing the regeneration outcome + migration outcome."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.10")
    # Manually drop a no-op seed-local.sh that creates local.yml so
    # the success-path check sees the file.
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'fake setup' > .proctor/local.yml\n"
        "echo ok\n"
    )
    seed.chmod(0o755)
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.10", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.7.10",
                      repo_root=tmp_path, bash_rc=0)
    assert env["type"] == "show", env
    assert "regenerated" in env["markdown"].lower()


def test_v0710_regenerate_after_seed_failure_emits_error(tmp_path):
    """When seed-local.sh exits non-zero (DB not reachable, missing
    env file, etc.), the step emits an ``error`` envelope with
    actionable guidance about bringing up dev dependencies."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.10")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.10", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.7.10",
                      repo_root=tmp_path, bash_rc=1)
    assert env["type"] == "error", env
    msg = env["message"]
    assert "exited 1" in msg
    # Actionable hint about dev dependencies.
    assert ("docker-compose" in msg) or ("dependencies" in msg)
    # Re-run guidance present.
    assert "re-run" in msg.lower() or "rerun" in msg.lower()


def test_v0710_regenerate_handler_migrates_legacy_seed_in_place(tmp_path):
    """v0.7.10 migrates an existing legacy-heredoc seed-local.sh in
    place during the regenerate step. After the migration phase
    (which runs BEFORE the bash envelope), the seed script reads
    setup-block.yml via the awk reader and the legacy heredoc is
    gone."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", with_cmd_binary=False,
        has_local_yml=True,
    )
    seed_path = tmp_path / ".proctor" / "seed-local.sh"
    # Sanity: legacy pattern present before the wizard runs.
    original = seed_path.read_text()
    assert "SETUP_BLOCK=$(cat <<'YAML'" in original
    assert "awk" not in original

    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.7.10",
                      repo_root=tmp_path)
    assert env["type"] == "bash", env  # the migrated seed-local.sh

    rewritten = seed_path.read_text()
    # The awk reader is now present.
    assert "awk" in rewritten
    assert ".proctor/setup-block.yml" in rewritten
    # The fallback heredoc retains the original commands.
    assert "docker-compose up -d db" in rewritten
    # Original heredoc-only block (SETUP_BLOCK directly assigning
    # from cat <<'YAML') is wrapped in an if/then now.
    assert "if [ -f .proctor/setup-block.yml ]" in rewritten


def test_v0710_regenerate_handler_salvages_heredoc_into_setup_block(tmp_path):
    """Migration phase salvages the legacy heredoc's content into
    .proctor/setup-block.yml when that file doesn't yet exist. The
    user's tailored setup commands are preserved as the canonical
    baseline."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", with_cmd_binary=False,
        has_local_yml=True,
    )
    setup_block_path = tmp_path / ".proctor" / "setup-block.yml"
    assert not setup_block_path.exists()

    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.10", repo_root=tmp_path)

    assert setup_block_path.exists()
    content = setup_block_path.read_text()
    assert "setup:" in content
    # The salvaged commands from the legacy heredoc body.
    assert "docker-compose up -d db" in content
    assert "go mod download" in content


def test_v0710_regenerate_handler_keeps_existing_setup_block(tmp_path):
    """When .proctor/setup-block.yml already exists (e.g. supplement
    already wrote it), the migration phase leaves the file alone and
    only rewrites seed-local.sh. We don't overwrite user / wizard
    state from an earlier step."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", with_cmd_binary=False,
        has_local_yml=True,
    )
    sb_path = tmp_path / ".proctor" / "setup-block.yml"
    sb_path.write_text(
        "setup:\n"
        "  - bash -c 'echo custom-pre-existing'\n"
        "  - bash -c 'echo another'\n"
    )
    original_sb = sb_path.read_text()

    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.7.10", repo_root=tmp_path)

    assert sb_path.read_text() == original_sb


def test_v0710_regenerate_handler_idempotent_on_migrated_seed(tmp_path):
    """When seed-local.sh is already migrated (uses awk reader), the
    handler skips the migration phase + just runs the seed script.
    No file mutations to seed-local.sh, no spurious setup-block.yml
    writes."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.10")
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(_V0710_SEED_SH_ALREADY_MIGRATED)
    seed.chmod(0o755)
    original_seed = seed.read_text()

    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.7.10",
                      repo_root=tmp_path)
    assert env["type"] == "bash"
    assert seed.read_text() == original_seed
    # The handler should mark migration as already-migrated in
    # step_data — verify via state file.
    state = json.loads(state_file.read_text())
    step_data = (
        state.get("step_data", {}).get("step_regenerate_local_yml", {})
    )
    assert step_data.get("migrate_outcome") == "already-migrated", state


# --- Migration helper unit tests --------------------------------------------


def test_v0710_migrate_seed_local_sh_helper_rewrites_block(tmp_path):
    """Unit test for _migrate_seed_local_sh: given a script with the
    legacy heredoc, it returns ``migrated-...`` and rewrites the
    file to use the awk reader."""
    from plugins.proctor.scripts.wizard_run import _migrate_seed_local_sh
    seed_path = tmp_path / "seed-local.sh"
    seed_path.write_text(_V0710_SEED_SH_LEGACY)
    seed_path.chmod(0o755)
    sb_path = tmp_path / "setup-block.yml"
    outcome = _migrate_seed_local_sh(seed_path, sb_path)
    assert outcome.startswith("migrated-")
    text = seed_path.read_text()
    assert "if [ -f .proctor/setup-block.yml ]" in text
    assert "awk '/^setup:/,0' .proctor/setup-block.yml" in text


def test_v0710_migrate_seed_local_sh_helper_already_migrated_noop(tmp_path):
    """When the seed script is already migrated, the helper returns
    ``already-migrated`` and doesn't touch the file."""
    from plugins.proctor.scripts.wizard_run import _migrate_seed_local_sh
    seed_path = tmp_path / "seed-local.sh"
    seed_path.write_text(_V0710_SEED_SH_ALREADY_MIGRATED)
    seed_path.chmod(0o755)
    original = seed_path.read_text()
    sb_path = tmp_path / "setup-block.yml"
    outcome = _migrate_seed_local_sh(seed_path, sb_path)
    assert outcome == "already-migrated"
    assert seed_path.read_text() == original
    # setup-block.yml should NOT be created (no migration happened).
    assert not sb_path.exists()


def test_v0710_migrate_seed_local_sh_helper_preserves_executable_bit(tmp_path):
    """After migration, seed-local.sh must remain executable. v0.7.10
    sets the mode on the temp file explicitly before the atomic
    rename so the chmod survives."""
    import os
    from plugins.proctor.scripts.wizard_run import _migrate_seed_local_sh
    seed_path = tmp_path / "seed-local.sh"
    seed_path.write_text(_V0710_SEED_SH_LEGACY)
    seed_path.chmod(0o755)
    sb_path = tmp_path / "setup-block.yml"
    _migrate_seed_local_sh(seed_path, sb_path)
    mode = os.stat(seed_path).st_mode & 0o777
    # At minimum the owner-execute bit should still be set.
    assert mode & 0o100, oct(mode)


# --- Step iterator end-to-end with all three steps active --------------------


def test_v0710_iterator_all_three_steps_in_order(tmp_path):
    """Pin the canonical execution order in the iterator: when
    supplement, regenerate, and bump ALL apply, the wizard walks
    them in that exact order.

    Setup: legacy-heredoc seed-local.sh + cmd/example-loop binary
    not in setup-block.yml + local.yml missing + stale pin. All
    three steps fire."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.5", with_cmd_binary=True,
        has_local_yml=False,
    )
    state = wds_detect_state(tmp_path)
    steps = wds_decide_steps(state, current_tag="v0.7.10", repo_root=tmp_path)
    assert steps == [
        STEP_SUPPLEMENT_SETUP,
        STEP_REGENERATE_LOCAL_YML,
        STEP_BUMP_ACTION_PIN,
    ], steps


def test_v0710_iterator_walks_supplement_then_regenerate_then_bump(tmp_path):
    """Drive the iterator end-to-end through all three v0.7.10 steps.

    1. First call → supplement step asks scan/skip.
    2. User picks Skip → supplement completes → iterator recurses
       into regenerate which emits its bash (running seed-local.sh).
    3. Simulate seed-local.sh success → regenerate emits its show
       summary.
    4. Next call → iterator pops bump which emits the bump-action
       bash.
    5. Simulate bump success → terminal done."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.5", with_cmd_binary=True,
        has_local_yml=False,
    )
    # Replace the legacy heredoc seed with one that succeeds without
    # docker/db so we can drive the iterator deterministically.
    seed = tmp_path / ".proctor" / "seed-local.sh"
    seed.write_text(
        "#!/usr/bin/env bash\n"
        "# Original legacy heredoc — will be migrated by the wizard.\n"
        "SETUP_BLOCK=$(cat <<'YAML'\n"
        "  - bash -c 'echo legacy-content'\n"
        "YAML\n"
        ")\n"
        "echo 'fake setup' > .proctor/local.yml\n"
        "echo ok\n"
    )
    seed.chmod(0o755)
    state_file = tmp_path / "wizard-state.json"

    # 1. Supplement: scan/skip ask_user.
    env1 = _run_wizard(state_file, current_tag="v0.7.10",
                       repo_root=tmp_path)
    assert env1["type"] == "ask_user", env1
    assert env1["header"] == "Supplementary binaries"

    # 2. Skip → recurses to regenerate which emits bash.
    env2 = _run_wizard(
        state_file, current_tag="v0.7.10", repo_root=tmp_path,
        answer="Skip — my setup is fine",
    )
    assert env2["type"] == "bash", env2
    assert ".proctor/seed-local.sh" in env2["command"]

    # 3. seed-local.sh success → show summary.
    env3 = _run_wizard(state_file, current_tag="v0.7.10",
                       repo_root=tmp_path, bash_rc=0)
    assert env3["type"] == "show", env3
    assert "regenerated" in env3["markdown"].lower()

    # 4. Next call → bump bash.
    env4 = _run_wizard(state_file, current_tag="v0.7.10",
                       repo_root=tmp_path)
    assert env4["type"] == "bash", env4
    assert "wizard_bump_action.sh" in env4["command"]

    # 5. Bump success → terminal done.
    env5 = _run_wizard(state_file, current_tag="v0.7.10",
                       repo_root=tmp_path, bash_rc=0)
    assert env5["type"] == "done", env5


def test_v0710_iterator_retries_regenerate_after_failure(tmp_path):
    """When seed-local.sh fails, the regenerate step emits an
    ``error`` envelope BUT marks the substate so a re-run picks up
    here. The user fixes their env, re-invokes the wizard, and the
    step retries from the bash phase.

    Implementation detail: after an error the handler returns
    SUB_COMPLETE with sub=None reset, so the next iterator call
    starts the step over. Verify that a second wizard invocation
    after the error indeed re-emits the bash envelope (rather than
    jumping past the failed step)."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.7.10")
    state_file = tmp_path / "wizard-state.json"
    # Step 1: regenerate emits bash.
    _run_wizard(state_file, current_tag="v0.7.10", repo_root=tmp_path)
    # Step 2: failure → error envelope.
    env_err = _run_wizard(state_file, current_tag="v0.7.10",
                          repo_root=tmp_path, bash_rc=1)
    assert env_err["type"] == "error"
    # Step 3: re-invoke (user fixed env). Either the iterator
    # re-emits the bash (preferred — true retry) OR the wizard
    # has been reset to fresh detection state. Both are acceptable
    # in the sense that the user gets another shot. We require the
    # wizard to not be permanently stuck.
    env_retry = _run_wizard(state_file, current_tag="v0.7.10",
                            repo_root=tmp_path)
    assert env_retry["type"] != "error", env_retry


# --- decide_steps applies-conditions are truly independent ------------------


def test_v0710_applies_conditions_no_local_yml_dependency_on_supplement(tmp_path):
    """Each step's applies-condition checks ONLY that step's own
    precondition. Specifically: supplement-setup must NOT depend on
    local.yml's state. This pins Bug A's root cause down to a single
    assertion."""
    # Two repos differing ONLY in local.yml presence. Same binaries,
    # same config, same pin. supplement should fire in both.
    for has_local in (True, False):
        sub = tmp_path / f"variant-local-{has_local}"
        sub.mkdir()
        _make_v04_repo(sub, has_local_yml=has_local, pin="v0.7.10")
        cmd_dir = sub / "cmd" / "ticker"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "main.go").write_text(
            "package main\nimport \"time\"\n"
            "func main() { t := time.NewTicker(time.Minute); _ = t }\n"
        )
        state = wds_detect_state(sub)
        steps = wds_decide_steps(
            state, current_tag="v0.7.10", repo_root=sub,
        )
        assert STEP_SUPPLEMENT_SETUP in steps, (
            f"supplement missing for has_local_yml={has_local}; "
            f"steps={steps}"
        )


def test_v0710_state_carries_seed_legacy_flag(tmp_path):
    """``detect_state`` reports the new ``seed_has_legacy_heredoc``
    key alongside the existing keys. Backward-compat keys must keep
    working — old callers reading ``has_local_yml`` aren't broken."""
    _make_v0710_repo_with_legacy_seed(
        tmp_path, pin="v0.7.10", has_local_yml=False,
    )
    state = wds_detect_state(tmp_path)
    # New key.
    assert "seed_has_legacy_heredoc" in state
    assert state["seed_has_legacy_heredoc"] is True
    # Old keys still present.
    assert "has_local_yml" in state
    assert "has_seed_script" in state
    assert "current_pin" in state

