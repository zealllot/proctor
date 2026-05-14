"""State-machine driver for the /proctor:proctor pipeline.

v0.5.0 applied the state-machine pattern to the wizard. v0.6.0
extends it to the main `/proctor:proctor` pipeline. Same root
problem: prose-driven inter-stage control flow makes the AI stall
between stages (Stage 1→2 / Stage 2→approval gate / approval→Stage 3
/ etc.). Each transition was a "what's next" decision point and a
1-5 minute Churn opportunity.

The pipeline has 5 stages plus an approval gate:

    pre-flight + fetch  →  analyze  →  plan  →  approval gate  →
    execute  →  fix (conditional)  →  report

Each stage's actual work is done by an existing Skill (analyzing-pr-
changes, planning-pr-tests, executing-pr-tests, fixing-test-failures,
reporting-pr-test-results). Skills are AI primitives — this script
can't invoke them itself. Instead it emits a `dispatch_skill`
envelope telling the AI which Skill to call next. The AI calls it,
the Skill writes the artifact, then the AI re-invokes this script
which validates the artifact and advances to the next stage.

## IPC envelope shape

Same JSON-line stdout protocol as wizard_run.py with one new type:

```jsonc
// dispatch_skill — AI must call the named Skill tool, then re-invoke.
{"type": "dispatch_skill",
 "skill": "proctor:planning-pr-tests",
 "purpose": "Stage 2 — plan",
 "expects_artifact": ".proctor/runs/<id>/test-plan.json"}
```

Other types same as wizard_run.py:
- `bash` — run a command; re-invoke with --bash-rc.
- `show` — emit markdown to chat; re-invoke.
- `ask_user` — call AskUserQuestion; re-invoke with --answer.
- `done` — emit summary, exit loop.
- `error` — emit error, exit.

## State persistence

`--state-file` defaults to `.proctor/runs/<run-id>/pipeline-state.json`.
The state file carries the run-id so the next invocation knows which
run directory's artifacts to validate.

## v0.6.0 scope

Implements the happy path: pre-flight → analyze → plan → approval
gate (Run all only — drop-items and cancel emit fallback prose
pointers) → execute → fix-or-skip → report.

CI-mode-specific branches (require_approval+exit / mutex acquire)
defer to legacy prose; v0.6.x will fold them in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


_STEP_INIT = ""
_STEP_FETCHED = "fetched"
_STEP_ANALYZED = "analyzed"
_STEP_PLAN_DISPATCHED = "plan_dispatched"
_STEP_PLANNED = "planned"
_STEP_TABLE_SHOWN = "table_shown"
_STEP_APPROVED = "approved"
_STEP_EXECUTED = "executed"
_STEP_FIX_DECIDED = "fix_decided"
_STEP_FIXED = "fixed"
_STEP_REPORTED = "reported"
_STEP_DONE = "done"


def _load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"step": _STEP_INIT}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"step": _STEP_INIT}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n")


def _emit(envelope: dict) -> None:
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.write("\n")


def _bash(command: str, description: str = "") -> dict:
    return {"type": "bash", "command": command, "description": description}


def _dispatch_skill(skill: str, purpose: str, expects: str) -> dict:
    return {
        "type": "dispatch_skill",
        "skill": skill,
        "purpose": purpose,
        "expects_artifact": expects,
    }


def _show(markdown: str) -> dict:
    return {"type": "show", "markdown": markdown}


def _ask_user(header: str, question: str, options: list[dict]) -> dict:
    return {"type": "ask_user", "header": header,
            "question": question, "options": options}


def _done(summary: str) -> dict:
    return {"type": "done", "summary": summary}


def _error(message: str) -> dict:
    return {"type": "error", "message": message}


def _run_step(
    state: dict,
    pr_arg: str | None,
    answer: str | None,
    bash_rc: int | None,
    plugin_root: Path,
    mode: str,
) -> tuple[dict, dict]:
    """Advance the state machine by one transition. Returns (envelope, new_state)."""
    step = state.get("step", _STEP_INIT)
    run_id = state.get("run_id")
    run_dir = Path(state["run_dir"]) if state.get("run_dir") else None
    pr_number = state.get("pr_number")

    # ---------- INIT: emit pre-flight bash ----------
    if step == _STEP_INIT:
        if not pr_arg:
            return _error(
                "First invocation requires --pr-arg <PR-number-or-URL>. "
                "Pass it from the orchestrator harness."
            ), state
        state["pr_arg"] = pr_arg
        # Bash one-liner that does parse + fetch + write artifacts +
        # echos the run-id so we can capture it.
        fetch_cmd = (
            f'python3 -c "'
            f"import sys, json, os; "
            f"sys.path.insert(0, \\\"{plugin_root}/scripts\\\"); "
            f"from pr_fetch import parse_pr_arg, fetch_pr, fetch_diff; "
            f"from runlog import make_run_id; "
            f"from datetime import datetime, timezone; "
            f"arg = parse_pr_arg(\\\"{pr_arg}\\\"); "
            f"pr = fetch_pr(arg); "
            f"diff = fetch_diff(arg); "
            f"run_id = make_run_id(pr_number=pr['number'], head_sha=pr['headRefOid'], started_at_iso=datetime.now(timezone.utc).isoformat()); "
            f"run_dir = f'.proctor/runs/{{run_id}}'; "
            f"os.makedirs(run_dir, exist_ok=True); "
            f"open(f'{{run_dir}}/pr.json', 'w').write(json.dumps(pr, indent=2)); "
            f"open(f'{{run_dir}}/diff.patch', 'w').write(diff); "
            f"print(f'RUN_ID={{run_id}}'); "
            f"print(f'RUN_DIR={{run_dir}}'); "
            f"print(f'PR_NUMBER={{pr[\\\"number\\\"]}}'); "
            f'"'
        )
        state["step"] = _STEP_FETCHED
        return _bash(
            fetch_cmd,
            description=(
                "Pre-flight: parse PR arg, fetch metadata + diff "
                "from GitHub, write pr.json + diff.patch under "
                "`.proctor/runs/<run-id>/`. Echos RUN_ID / RUN_DIR / "
                "PR_NUMBER to stdout so the harness can capture them."
            ),
        ), state

    # ---------- After fetch: harness reads RUN_ID from bash output ----------
    if step == _STEP_FETCHED:
        # The harness should pass back run_id/run_dir/pr_number via
        # follow-up --run-id / --run-dir / --pr-number. But to keep
        # the IPC simple, we just trust the state file was updated
        # by the harness with those values.
        if not state.get("run_id") or not state.get("run_dir"):
            return _error(
                "After the pre-flight bash, the harness must update "
                "the state file with run_id / run_dir / pr_number "
                "captured from the bash output, then re-invoke. See "
                "the orchestrator harness in commands/proctor.md."
            ), state
        state["step"] = _STEP_ANALYZED  # next iteration dispatches analyze
        return _dispatch_skill(
            skill="proctor:analyzing-pr-changes",
            purpose=(
                "Stage 1 — walk the diff, classify hunks, write "
                f".proctor/runs/{state['run_id']}/change-map.json."
            ),
            expects=f".proctor/runs/{state['run_id']}/change-map.json",
        ), state

    # ---------- After analyze: validate + dispatch plan ----------
    if step == _STEP_ANALYZED:
        cm_path = run_dir / "change-map.json"
        if not cm_path.exists():
            return _error(
                f"Stage 1 didn't produce {cm_path}. The "
                "analyzing-pr-changes skill must write its output to "
                "this exact path. Aborting."
            ), state
        # Validate via schema.py — surface schema errors as errors.
        sys.path.insert(0, str(plugin_root / "scripts"))
        from schema import validate_change_map, SchemaError  # type: ignore[import-not-found]
        try:
            cm = json.loads(cm_path.read_text())
            validate_change_map(cm)
        except (json.JSONDecodeError, SchemaError) as e:
            return _error(f"change-map.json failed validation: {e}"), state
        state["step"] = _STEP_PLAN_DISPATCHED
        return _dispatch_skill(
            skill="proctor:planning-pr-tests",
            purpose=(
                f"Stage 2 — derive TestPlan from the ChangeMap, write "
                f".proctor/runs/{state['run_id']}/test-plan.json. The "
                f"skill runs its self-audit (plan_smells --strict) "
                f"internally before returning."
            ),
            expects=f".proctor/runs/{state['run_id']}/test-plan.json",
        ), state

    # ---------- After plan: validate + render approval table ----------
    if step == _STEP_PLAN_DISPATCHED:
        tp_path = run_dir / "test-plan.json"
        if not tp_path.exists():
            return _error(
                f"Stage 2 didn't produce {tp_path}. Aborting."
            ), state
        sys.path.insert(0, str(plugin_root / "scripts"))
        from schema import validate_test_plan, SchemaError  # type: ignore[import-not-found]
        try:
            tp = json.loads(tp_path.read_text())
            validate_test_plan(tp)
        except (json.JSONDecodeError, SchemaError) as e:
            return _error(f"test-plan.json failed validation: {e}"), state
        state["step"] = _STEP_PLANNED
        # Emit a bash envelope to render the plan table.
        render_cmd = (
            f'python3 "{plugin_root}/scripts/render_plan_table.py" '
            f'--pr-number {pr_number} '
            f'--run-dir "{run_dir}" '
            f'< "{run_dir}/test-plan.json"'
        )
        return _bash(
            render_cmd,
            description=(
                "Render the plan table for the approval gate. The "
                "stdout becomes the chat content immediately preceding "
                "the AskUserQuestion."
            ),
        ), state

    # ---------- After table rendered: ask the approval question ----------
    if step == _STEP_PLANNED:
        state["step"] = _STEP_TABLE_SHOWN
        return _ask_user(
            header="Approve plan",
            question=(
                f"Run the plan above against the local server? It'll "
                f"execute lint-only / bash / chrome-devtools items, "
                f"capture artifacts, and report back."
            ),
            options=[
                {
                    "label": "Run all items",
                    "description": (
                        "Execute everything in the plan. Local mode "
                        "uses your dev server; CI uses the deployed "
                        "test env."
                    ),
                },
                {
                    "label": "Cancel — let me edit the plan first",
                    "description": (
                        "Abort the run. Hand-edit .proctor/runs/"
                        f"{state['run_id']}/test-plan.json and "
                        "re-invoke /proctor:proctor."
                    ),
                },
            ],
        ), state

    # ---------- After approval answer ----------
    if step == _STEP_TABLE_SHOWN:
        if not answer:
            return _error(
                "Approval gate expected --answer from the AI. "
                "Re-invoke after the user picks an option."
            ), state
        if "Cancel" in answer:
            state["step"] = _STEP_DONE
            return _done(
                "Run aborted at approval gate. Hand-edit "
                f"`.proctor/runs/{state['run_id']}/test-plan.json` "
                "and re-invoke `/proctor:proctor` when ready."
            ), state
        # "Run all" → copy plan → approved
        tp_path = run_dir / "test-plan.json"
        ap_path = run_dir / "approved-plan.json"
        ap_path.write_text(tp_path.read_text())
        state["step"] = _STEP_APPROVED
        return _dispatch_skill(
            skill="proctor:executing-pr-tests",
            purpose=(
                f"Stage 3 — execute the approved plan, write "
                f".proctor/runs/{state['run_id']}/test-results.json. "
                f"The skill handles worktree alignment + auth + per-"
                f"item dispatch."
            ),
            expects=f".proctor/runs/{state['run_id']}/test-results.json",
        ), state

    # ---------- After execute: validate + decide fix ----------
    if step == _STEP_APPROVED:
        tr_path = run_dir / "test-results.json"
        if not tr_path.exists():
            return _error(
                f"Stage 3 didn't produce {tr_path}. Aborting."
            ), state
        sys.path.insert(0, str(plugin_root / "scripts"))
        from schema import validate_test_results, SchemaError  # type: ignore[import-not-found]
        try:
            tr = json.loads(tr_path.read_text())
            validate_test_results(tr)
        except (json.JSONDecodeError, SchemaError) as e:
            return _error(f"test-results.json failed validation: {e}"), state
        # Decide if fix is needed.
        fail_count = tr["summary"]["fail"]
        aborted = tr.get("aborted")
        if aborted:
            state["step"] = _STEP_REPORTED  # skip fix, go to report
            return _show(
                f"### Run aborted\n\nReason: `{aborted}`. Skipping "
                f"fix + jumping to report so the human sees what "
                f"happened."
            ), state
        if fail_count > 0:
            state["step"] = _STEP_EXECUTED
            return _dispatch_skill(
                skill="proctor:fixing-test-failures",
                purpose=(
                    f"Stage 4 — {fail_count} failures detected; "
                    f"generate fix patches and write "
                    f".proctor/runs/{state['run_id']}/fix-pr-ref.json."
                ),
                expects=f".proctor/runs/{state['run_id']}/fix-pr-ref.json",
            ), state
        # No failures → skip fix, write null fix-pr-ref, proceed to report.
        fix_path = run_dir / "fix-pr-ref.json"
        fix_path.write_text("null\n")
        state["step"] = _STEP_FIXED
        return _show(
            "### No failures\n\nAll items passed. Skipping Stage 4 "
            "(fix); writing `fix-pr-ref.json = null` and proceeding "
            "to Stage 5 (report)."
        ), state

    # ---------- After fix: advance to report ----------
    if step == _STEP_EXECUTED:
        fix_path = run_dir / "fix-pr-ref.json"
        if not fix_path.exists():
            return _error(
                f"Stage 4 didn't produce {fix_path}. Aborting."
            ), state
        state["step"] = _STEP_FIXED
        return _show(
            "### Stage 4 done\n\nFix step complete. Proceeding to "
            "Stage 5 (report)."
        ), state

    # ---------- Dispatch report ----------
    if step == _STEP_FIXED:
        state["step"] = _STEP_REPORTED
        return _dispatch_skill(
            skill="proctor:reporting-pr-test-results",
            purpose=(
                f"Stage 5 — render the final report. Writes "
                f".proctor/runs/{state['run_id']}/report.html + "
                f"report.md."
            ),
            expects=f".proctor/runs/{state['run_id']}/report.html",
        ), state

    # ---------- After report: done ----------
    if step == _STEP_REPORTED:
        report_path = run_dir / "report.html"
        state["step"] = _STEP_DONE
        if report_path.exists():
            return _done(
                f"PRoctor pipeline complete. Open the report:\n\n"
                f"  file://{report_path.resolve()}\n\n"
                f"In local mode the report is your own to review. "
                f"In CI mode the reporter posted the markdown version "
                f"as a PR comment."
            ), state
        return _done(
            f"PRoctor pipeline complete (report file not found at "
            f"`{report_path}` — Stage 5 may not have written it; "
            f"check the reporting skill output)."
        ), state

    if step == _STEP_DONE:
        return _done("Pipeline already complete."), state

    return _error(
        f"Unknown pipeline step '{step}'. State file may be "
        f"corrupted; delete the run's pipeline-state.json and "
        f"re-invoke."
    ), state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-file", required=True,
                   help="Path to the pipeline's persistent state JSON, "
                        "typically `.proctor/runs/<run-id>/pipeline-"
                        "state.json` (the harness creates the run-id "
                        "during pre-flight and re-invokes with the "
                        "proper path).")
    p.add_argument("--pr-arg", default=None,
                   help="The PR argument (number or URL) — required on "
                        "the FIRST invocation, ignored thereafter.")
    p.add_argument("--answer", default=None,
                   help="The user's selection label from the previous "
                        "ask_user envelope.")
    p.add_argument("--bash-rc", type=int, default=None,
                   help="Exit code of the previous bash envelope's "
                        "command.")
    p.add_argument("--plugin-root", default=None,
                   help="The plugin's root directory.")
    p.add_argument("--mode", default="local", choices=["local", "ci"],
                   help="Pipeline mode (passed through to skills via "
                        "env in the harness).")
    args = p.parse_args(argv)

    state_file = Path(args.state_file)
    plugin_root = (
        Path(args.plugin_root).resolve()
        if args.plugin_root
        else Path(__file__).resolve().parent.parent
    )

    state = _load_state(state_file)
    envelope, new_state = _run_step(
        state=state,
        pr_arg=args.pr_arg,
        answer=args.answer,
        bash_rc=args.bash_rc,
        plugin_root=plugin_root,
        mode=args.mode,
    )
    _save_state(state_file, new_state)
    _emit(envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
