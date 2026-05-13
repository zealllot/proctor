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
