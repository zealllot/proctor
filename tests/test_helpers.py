import json
import subprocess
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
