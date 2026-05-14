import json
import pathlib
import subprocess
import sys
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


def test_wizard_first_invocation_on_user_scenario_emits_ask_user(tmp_path):
    """The exact user bug scenario: v0.4.0 layout, seed script present,
    local.yml missing, pin out of date → state machine's first
    invocation should emit type=ask_user."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.3")
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "ask_user"
    assert env["header"] == "Local config"
    assert any("Regenerate seed-local.sh AND re-run" in o["label"]
               for o in env["options"])


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


def test_wizard_needs_local_regen_recommended_path(tmp_path):
    """Two-iteration loop: ask_user → user picks 'Regenerate' →
    show envelope pointing at legacy prose for the regen flow."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    env1 = _run_wizard(state_file, current_tag="v0.4.6",
                       repo_root=tmp_path)
    assert env1["type"] == "ask_user"
    env2 = _run_wizard(
        state_file, current_tag="v0.4.6", repo_root=tmp_path,
        answer="Regenerate seed-local.sh AND re-run it (Recommended)",
    )
    assert env2["type"] == "show"
    assert "regenerate seed-local.sh" in env2["markdown"].lower()


def test_wizard_needs_local_regen_just_run_existing(tmp_path):
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path,
                      answer="Just run the existing seed-local.sh")
    assert env["type"] == "done"
    assert "seed-local.sh" in env["summary"]


def test_wizard_needs_local_regen_skip(tmp_path):
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path,
                      answer="Skip — I'll handle .proctor/local.yml myself")
    assert env["type"] == "done"


def test_wizard_fresh_falls_back_to_legacy_prose(tmp_path):
    """Fresh install isn't migrated to the state machine yet — emit
    a show envelope pointing at the legacy prose, then done."""
    state_file = tmp_path / "wizard-state.json"
    env = _run_wizard(state_file, current_tag="v0.4.6",
                      repo_root=tmp_path)
    assert env["type"] == "show"
    assert "fresh" in env["markdown"]
    assert "legacy SKILL.md" in env["markdown"]
    # State should be marked done so a second invocation doesn't loop.
    env2 = _run_wizard(state_file, current_tag="v0.4.6",
                       repo_root=tmp_path)
    # The script is already at step=done; subsequent invocations should
    # return a `done` envelope, not loop forever.
    assert env2["type"] == "done"


def test_wizard_state_file_persists_between_invocations(tmp_path):
    """The state file is the only thing carrying context between
    invocations — losing it would break the loop."""
    _make_v04_repo(tmp_path, has_local_yml=False, pin="v0.4.6")
    state_file = tmp_path / "wizard-state.json"
    _run_wizard(state_file, current_tag="v0.4.6", repo_root=tmp_path)
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["mode"] == "needs-local-regen"
    assert state["step"]  # non-empty: we've advanced past _STEP_INIT


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
    _make_v04_repo(tmp_path, has_local_yml=False, has_seed_script=False)
    state = wdm_state(tmp_path)
    d = wdm_decide(state, current_tag="v0.4.4")
    # Seed script missing but auth block present → regenerate seed
    # script via Step 8c-pre (no user input needed).
    assert d["mode"] == "bump-only-with-seed"
    assert d["ask_user"] is None


def test_wdm_migrate_when_no_auth_block(tmp_path):
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
    assert d["mode"] == "migrate"
    assert d["ask_user"] is not None


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


def test_worktree_setup_creates_aligned_checkout(tmp_path):
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


def test_worktree_setup_idempotent_when_sha_matches(tmp_path):
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


def test_worktree_setup_copies_local_yml(tmp_path):
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


def test_worktree_setup_no_local_yml_is_fine(tmp_path):
    """If the dev hasn't created .pr-test.local.yml, setup shouldn't
    error — just create the worktree without it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)
    assert not (wt_path / ".pr-test.local.yml").exists()


def test_worktree_teardown_removes_worktree(tmp_path):
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


def test_worktree_teardown_no_marker_is_noop(tmp_path):
    """Teardown when no setup ever happened should be a quiet no-op
    (covers the cur_head == pr_head case where setup is skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    # No marker, no worktree — should not raise.
    wt_teardown(run_dir=run_dir, repo_root=repo)


def test_worktree_setup_recreates_when_sha_differs(tmp_path):
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


# --- v0.7.0: worktree.py auto-symlinks gitignored runtime build dirs -----

def test_worktree_setup_symlinks_default_runtime_dirs(tmp_path):
    """When the main repo has gitignored runtime build dirs (e.g.
    `external/assets`, `node_modules`), worktree.setup() symlinks them
    into the worktree so the dev server doesn't have to rebuild.
    Source: v0.6.9 e2e against PR #1126 (run `pr1126-75eea89-b7a2689b`)
    — server started from worktree but `external/assets/mcd/` was
    missing, blank pages, manual symlink + restart cost ~3min."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Drop two of the default-symlinked dirs at the main repo root.
    (repo / "external" / "assets").mkdir(parents=True)
    (repo / "external" / "assets" / "mcd.bundle.js").write_text("// built\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / ".bin").mkdir()
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    linked_assets = wt_path / "external" / "assets"
    linked_nm = wt_path / "node_modules"
    assert linked_assets.is_symlink()
    assert linked_nm.is_symlink()
    # Symlink target points back at the main checkout so the dev server
    # picks up the existing build output instead of rebuilding.
    assert linked_assets.resolve() == (repo / "external" / "assets").resolve()
    # And the file is reachable through the symlink.
    assert (linked_assets / "mcd.bundle.js").read_text() == "// built\n"


def test_worktree_setup_skips_symlink_when_source_absent(tmp_path):
    """Default symlink list contains common gitignored runtime dirs
    (`dist`, `.next`, `vendor`, ...) that not every repo has. Missing
    sources are silently skipped — no broken symlinks created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo)

    # None of the defaults exist in the main repo, so none of them
    # should appear in the worktree.
    for d in ("external/assets", "node_modules", "dist", "build",
              ".next", "vendor"):
        assert not (wt_path / d).exists(), f"unexpected {d} in worktree"
        assert not (wt_path / d).is_symlink(), f"broken symlink at {d}"


def test_worktree_setup_symlink_dirs_empty_list_skips_all(tmp_path):
    """Passing `symlink_dirs=[]` explicitly opts out of all symlinking,
    even when default sources are present (consumer-level escape hatch
    via `.proctor/config.yml.worktree_symlink_dirs: []`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    # Default-symlinked source exists, but we override with [] to skip.
    (repo / "external" / "assets").mkdir(parents=True)
    (repo / "node_modules").mkdir()
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo, symlink_dirs=[])

    assert not (wt_path / "external" / "assets").exists()
    assert not (wt_path / "external" / "assets").is_symlink()
    assert not (wt_path / "node_modules").exists()
    assert not (wt_path / "node_modules").is_symlink()


def test_worktree_setup_symlink_dirs_custom_list(tmp_path):
    """A consumer-provided override list — only the named dirs are
    symlinked, even if other defaults exist at the main repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _, pr_sha = _init_repo_with_commits(repo)
    (repo / "external" / "assets").mkdir(parents=True)
    (repo / "custom_cache").mkdir()
    (repo / "custom_cache" / "marker").write_text("present\n")
    run_dir = repo / ".proctor" / "runs" / "test-run"

    wt_path = wt_setup(run_dir=run_dir, pr_number=99, head_sha=pr_sha,
                       repo_root=repo,
                       symlink_dirs=["custom_cache"])

    # Custom dir linked.
    assert (wt_path / "custom_cache").is_symlink()
    assert (wt_path / "custom_cache" / "marker").read_text() == "present\n"
    # Default `external/assets` NOT linked (override replaced defaults).
    assert not (wt_path / "external" / "assets").is_symlink()


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
    screenshot."""
    plan, results = _make_plan_results(
        [{"id": "t-2", "tool": "chrome-devtools",
          "what": "HAPPY: create reward — save succeeds",
          "how": "fill+save", "category": "api", "risk": "high",
          "depends_on": []}],
        [{"id": "t-2", "status": "pass", "evidence": "ok",
          "screenshot_ref": ".proctor/runs/x/t-2.png"}],
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
    it before report render."""
    plan, results = _make_plan_results(
        [{"id": "t-006", "tool": "chrome-devtools",
          "what": "edit reward, switch Digital Content Type from Image to Game",
          "how": "Navigate to detail; change select; save; reload.",
          "category": "api", "risk": "high", "depends_on": []}],
        [{"id": "t-006", "status": "pass",
          "evidence": "Reloaded; type=Game.",
          "screenshot_ref": ".proctor/runs/x/t-006.png"}],
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
          "screenshot_ref": "x.png"}],
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


def test_ss_check_legacy_screenshot_ref_counted_for_render_check():
    """v0.6.3-and-earlier results may legitimately set only the
    legacy `screenshot_ref` field. For render-check (min 1) that
    is sufficient; for happy-save (min 2) it is not. Backward
    compatibility preserved at the floor, not above it."""
    plan, results = _make_plan_results(
        [{"id": "t-1", "tool": "chrome-devtools", "what": "form renders",
          "how": "navigate", "category": "frontend", "risk": "low",
          "depends_on": []}],
        [{"id": "t-1", "status": "pass", "evidence": "ok",
          "screenshot_ref": ".proctor/runs/x/t-1.png"}],
    )
    assert ss_check(plan, results) == []


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
    so the gap is visible BEFORE report render."""
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
    # All three flagged.
    assert len(violations) == 3
    ids = "\n".join(violations)
    assert "t-002" in ids and "happy-save" in ids
    assert "t-003" in ids and "round-trip" in ids
    assert "t-006" in ids and "edit-and-switch" in ids


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

def test_ss_check_identical_negative_screenshots_warns(tmp_path):
    """Synthesize two negative items pointing at the SAME 100KB+ stub
    file; check returns a violation containing both item IDs and the
    byte size. This is the literal v0.6.6 t-007/t-008 signature
    (244252-byte PNG used as both screenshots)."""
    # Write a single 100KB+ stub file the two items will reference.
    stub = tmp_path / "screenshots" / "shared-blank-form.png"
    stub.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 244244  # 244252 bytes, the t-006 signature
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
    # Exactly one violation — the (t-7, t-8) pair.
    assert len(violations) == 1
    msg = violations[0]
    assert "t-7" in msg and "t-8" in msg
    assert str(len(payload)) in msg  # the byte size is reported
    assert "identical" in msg.lower()


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


def test_ss_check_identical_happy_save_screenshots_ok(tmp_path):
    """The lint targets NEGATIVE items only. Two happy-save items
    legitimately sharing a 'before' screenshot (e.g. they both
    start from the same blank form) must NOT be flagged — only
    negatives are checked, because for negatives the asserted
    artifact IS the rendered error."""
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
    # No violations — the lint only flags identical NEGATIVE
    # screenshots, and these are happy-save items.
    assert ss_check(plan, results, run_dir=tmp_path) == []


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
