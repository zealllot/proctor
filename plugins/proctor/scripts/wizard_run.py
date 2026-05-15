"""State-machine driver for the /proctor:proctor-init wizard.

v0.4.x had the wizard as 1300 lines of prose in
`commands/proctor-init.md` describing a multi-step branching
procedure the AI was supposed to follow. Real runs showed the AI
stalling between steps (Crunched / Brewed / Cooked for 1-5 minutes
each) — the prose-driven control flow gives the AI too many
"what next" decision points. User had to type "继续" repeatedly.
v0.5.0 moves the control flow into this Python state machine: the
AI's job becomes I/O relay (run script, surface AskUserQuestion
envelopes, write answers back), not multi-step procedure
interpretation.

## IPC protocol

Each invocation reads `--state-file`, advances by ONE state
transition, writes state back, and emits exactly one JSON envelope
to stdout describing what the AI should do next:

```jsonc
// Type 1: AskUserQuestion — script needs a user choice to proceed.
{"type": "ask_user", "header": "...", "question": "...",
 "options": [{"label": "...", "description": "..."}, ...]}
//
// AI must: call AskUserQuestion(header=..., options=[...]), then
// re-invoke wizard_run.py with `--answer "<selected label>"`.

// Type 2: Show — emit the markdown to chat as-is.
{"type": "show", "markdown": "..."}
// AI must: print the markdown to chat verbatim, then re-invoke
// wizard_run.py (with no --answer).

// Type 3: Done — wizard complete.
{"type": "done", "summary": "..."}
// AI must: print the summary, exit the wizard loop.

// Type 4: Error — abort with a message.
{"type": "error", "message": "..."}
// AI must: print the message, exit the wizard loop with the error.

// Type 5: Bash — run a single Bash command (used for actions the
// AI's Bash tool can do but the script doesn't try to subprocess
// itself — e.g. interactive git push, gh CLI calls).
{"type": "bash", "command": "...", "description": "what this does"}
// AI must: run the command via the Bash tool, then re-invoke
// wizard_run.py with `--bash-rc <exit_code>` so the script can
// decide what to do next based on the result.
```

## Loop discipline

The AI's harness in `commands/proctor-init.md` is a tight `while`-style
prose: invoke this script → handle one envelope → invoke again until
DONE. Stall only happens if the AI breaks the loop — the prose
explicitly forbids stopping between iterations.

## State persistence

`--state-file` defaults to `.proctor/wizard-state.json`. Persisted
between invocations so the wizard can resume mid-flow if the AI
crashes / user kills the session. Schema is internal; consumers
shouldn't edit by hand.

## Currently implemented modes (v0.5.0)

- `current` — fully configured, just print summary.
- `bump-only` — bump action pin, commit, push.
- `needs-local-regen` — ask user 3 options, branch accordingly.
- `legacy-migration` — git-mv v0.3.x layout to v0.4.0.

Not yet implemented (fallback to legacy SKILL.md prose):
- `fresh` — full new install (Section 1-8 of the legacy wizard).
- `migrate` — v0.2 → v0.3 migration (auth block addition).
- `bump-only-with-seed` — pin bump + Step 8c-pre seed script regen.

When the decided mode falls into the fallback set, this script
emits a `show` envelope saying "this mode isn't migrated to v0.5
yet — fall back to the legacy SKILL.md prose section for the
detailed steps" and a `done` envelope. The AI then runs the legacy
prose.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# State steps. Each invocation moves to the next step. Empty string
# means "haven't started yet" → run detection.
_STEP_INIT = ""
_STEP_DECIDED = "decided"
_STEP_NEEDS_LOCAL_REGEN_ASKED = "needs_local_regen_asked"
_STEP_LEGACY_MIGRATION_ASKED = "legacy_migration_asked"
_STEP_BUMP_DONE = "bump_done"
# v0.7.8: amend-daemons mode — multi-step flow that scans
# cmd/*/main.go via wizard_detect_binaries.py and amends an
# existing `.proctor/local.yml setup:` with go-run lines for
# user-selected daemons. See wizard_decide_mode.py rule 7 for the
# triggering condition.
_STEP_AMEND_DAEMONS_OFFERED = "amend_daemons_offered"
_STEP_AMEND_DAEMONS_SCANNED = "amend_daemons_scanned"
_STEP_AMEND_DAEMONS_PICKED = "amend_daemons_picked"
_STEP_DONE = "done"

# Path where wizard_run.py asks the AI to dump the binaries JSON
# between the bash envelope and the next state transition. Kept in
# /tmp so it doesn't litter the repo, fixed path so re-entry after
# AI crash can find it without re-scanning.
_BINARIES_JSON_PATH = "/tmp/proctor-wizard-binaries.json"


def _load_state(state_file: Path) -> dict:
    """Read state from disk; return empty fresh state if file missing
    or empty. Defensive against corrupted state — if JSON parse fails,
    reset to fresh (better than locking user out)."""
    if not state_file.exists():
        return {"step": _STEP_INIT}
    try:
        data = json.loads(state_file.read_text())
        if not isinstance(data, dict):
            return {"step": _STEP_INIT}
        return data
    except (json.JSONDecodeError, OSError):
        return {"step": _STEP_INIT}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n")


def _emit(envelope: dict) -> None:
    """Write the envelope to stdout as a single JSON line + newline."""
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.write("\n")


def _ask_user(
    header: str,
    question: str,
    options: list[dict],
    multi_select: bool = False,
) -> dict:
    """Build an ``ask_user`` envelope.

    ``multi_select=True`` signals to the AI harness that
    AskUserQuestion should be called in multi-select mode; the AI
    must then pass back a comma-separated label list as
    ``--answer "label1, label2, label3"``. v0.7.8 introduced this
    flag for the amend-daemons binary-picker step. Single-select
    callers keep the simpler "exact label" answer contract."""
    envelope: dict = {
        "type": "ask_user",
        "header": header,
        "question": question,
        "options": options,
    }
    if multi_select:
        envelope["multi_select"] = True
    return envelope


def _show(markdown: str) -> dict:
    return {"type": "show", "markdown": markdown}


def _done(summary: str) -> dict:
    return {"type": "done", "summary": summary}


def _error(message: str) -> dict:
    return {"type": "error", "message": message}


def _bash(command: str, description: str = "") -> dict:
    return {"type": "bash", "command": command, "description": description}


def _amend_local_yml_with_daemons(
    local_path: Path,
    chosen: list[dict],
) -> int:
    """Insert daemon kill+start lines into ``setup:`` of a local.yml.

    For each candidate in ``chosen``, append two lines to the
    ``setup:`` list (matching the prose in ``commands/proctor-init.md``
    Step 7.5):

    .. code-block:: yaml

        - bash -c '[ -f /tmp/proctor-<NAME>.pid ] && kill ...'
        - bash -c 'set -a; . ./dev_env_local ...; nohup go run ./<PATH> ...'

    Returns the count of candidates actually added (skips those
    whose path/binary-name already appears anywhere in the setup
    block — idempotent re-runs don't duplicate). Preserves comments
    and indentation by working as a string-level edit (no YAML
    round-trip).

    Insertion point: end of the setup list, BEFORE any non-list
    sibling line (next top-level key, or EOF). Tolerant of varied
    indentation: it samples the existing ``- `` items' indentation
    and matches it for the new lines.
    """
    text = local_path.read_text()
    lines = text.splitlines(keepends=True)

    # Find the `setup:` line.
    setup_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("setup:") and not line.lstrip().startswith("setup: ["):
            setup_idx = i
            break
    if setup_idx is None:
        raise ValueError("local.yml has no expanded `setup:` block")

    # Walk forward to find the end of the list. The list ends at
    # the first line that:
    # - is not blank/whitespace-only
    # - is not a comment whose indent matches the items
    # - and whose indent is <= the `setup:` line's indent
    setup_indent = len(lines[setup_idx]) - len(lines[setup_idx].lstrip())
    # Sample item indent — look at first `  - ` after setup_idx.
    item_indent = setup_indent + 2  # YAML default
    for j in range(setup_idx + 1, min(setup_idx + 30, len(lines))):
        stripped = lines[j].lstrip()
        if stripped.startswith("- "):
            item_indent = len(lines[j]) - len(stripped)
            break

    # Insertion point: scan from setup_idx+1 forward to find last
    # list-item or sibling line.
    insert_at = len(lines)
    for j in range(setup_idx + 1, len(lines)):
        stripped_full = lines[j].rstrip("\n")
        bare = stripped_full.lstrip()
        cur_indent = len(stripped_full) - len(bare)
        if not bare or bare.startswith("#"):
            continue  # blank or comment — keep going
        if cur_indent <= setup_indent:
            insert_at = j
            break

    # Build new lines + idempotency filter.
    existing_setup_block = "".join(lines[setup_idx:insert_at])
    added = 0
    new_chunk: list[str] = []
    for c in chosen:
        name = c["binary_name"]
        path = c["path"]
        # Idempotency: skip if name OR path already mentioned in
        # setup. Re-running amend-daemons after a successful first
        # pass should be a no-op.
        if f"proctor-{name}.pid" in existing_setup_block:
            continue
        if f"./{path}" in existing_setup_block:
            continue
        pad = " " * item_indent
        kill_line = (
            f"{pad}- bash -c '[ -f /tmp/proctor-{name}.pid ] && "
            f"kill \"$(cat /tmp/proctor-{name}.pid)\" 2>/dev/null; "
            f"true'\n"
        )
        start_line = (
            f"{pad}- bash -c 'set -a; . ./dev_env_local 2>/dev/null "
            f"|| . ./dev_env 2>/dev/null || true; set +a; "
            f"nohup go run ./{path} > /tmp/proctor-{name}.log "
            f"2>&1 & echo $! > /tmp/proctor-{name}.pid'\n"
        )
        new_chunk.append(kill_line)
        new_chunk.append(start_line)
        added += 1

    if added == 0:
        return 0

    # If insertion point is mid-file and the line before it is not
    # already a newline-terminated list item, drop in cleanly.
    # Atomic write: write to tmp, fsync, rename.
    new_lines = lines[:insert_at] + new_chunk + lines[insert_at:]
    new_text = "".join(new_lines)
    tmp_path = local_path.with_suffix(local_path.suffix + ".wizard-tmp")
    tmp_path.write_text(new_text)
    tmp_path.replace(local_path)
    return added


def _detect_and_decide(repo_root: Path, current_tag: str | None) -> dict:
    """Delegate to wizard_decide_mode.py for the actual decision —
    keeps the decision logic in one place across versions."""
    from wizard_decide_mode import detect_state, decide_mode  # local import — avoid cycle when imported as a module
    state = detect_state(repo_root)
    return {"state": state, **decide_mode(state, current_tag, repo_root=repo_root)}


def _run_step(
    state: dict,
    answer: str | None,
    bash_rc: int | None,
    repo_root: Path,
    current_tag: str | None,
    plugin_root: Path,
) -> tuple[dict, dict]:
    """Advance the state machine by ONE transition.

    Returns (envelope_to_emit, new_state)."""
    step = state.get("step", _STEP_INIT)

    # ---------- STEP_INIT: run detection + mode decision ----------
    if step == _STEP_INIT:
        decision = _detect_and_decide(repo_root, current_tag)
        state["detected_state"] = decision["state"]
        state["mode"] = decision["mode"]
        state["next_action"] = decision["next_action"]
        state["step"] = _STEP_DECIDED

        mode = decision["mode"]

        # `current` — nothing to do.
        if mode == "current":
            state["step"] = _STEP_DONE
            return _done(
                "PRoctor is already integrated and up to date. "
                "No wizard actions taken."
            ), state

        # `bump-only` — single bash invocation does it all.
        if mode == "bump-only":
            current_pin = decision["state"]["current_pin"]
            script = plugin_root / "scripts" / "wizard_bump_action.sh"
            cmd = f'bash "{script}" "{current_tag}"'
            return _bash(
                cmd,
                description=(
                    f"Bump PRoctor action pin {current_pin} → "
                    f"{current_tag} (edit + diff + commit + push, "
                    f"atomic)."
                ),
            ), state

        # `needs-local-regen` — ask user the 3-option question.
        if mode == "needs-local-regen":
            return _ask_user(
                header="Local config",
                question=decision["ask_user"]["question"],
                options=decision["ask_user"]["options"],
            ), {**state, "step": _STEP_NEEDS_LOCAL_REGEN_ASKED}

        # `legacy-migration` — ask user the migration question.
        if mode == "legacy-migration":
            return _ask_user(
                header="Layout migration",
                question=decision["ask_user"]["question"],
                options=decision["ask_user"]["options"],
            ), {**state, "step": _STEP_LEGACY_MIGRATION_ASKED}

        # v0.7.8: `amend-daemons` — local.yml exists, setup is
        # non-empty, but no `go run ./cmd/` daemon lines. Offer
        # to scan + add.
        if mode == "amend-daemons":
            return _ask_user(
                header="Daemon scan",
                question=decision["ask_user"]["question"],
                options=decision["ask_user"]["options"],
            ), {**state, "step": _STEP_AMEND_DAEMONS_OFFERED}

        # Modes that fall back to legacy SKILL.md prose for now.
        if mode in ("fresh", "migrate", "bump-only-with-seed"):
            state["step"] = _STEP_DONE
            return _show(
                f"## Wizard mode = `{mode}`\n\n"
                f"This mode is not yet covered by the v0.5 state "
                f"machine. The wizard will fall back to the legacy "
                f"SKILL.md prose flow for the detailed steps.\n\n"
                f"Continue manually by reading the relevant section "
                f"in `commands/proctor-init.md`:\n"
                f"- `fresh` → Sections 1–8\n"
                f"- `migrate` → Sections 7–8 (after the migrate-mode "
                f"AskUserQuestion)\n"
                f"- `bump-only-with-seed` → Section 8 pin bump + "
                f"Step 8c-pre seed script regeneration.\n\n"
                f"(v0.5.x will migrate these modes into this state "
                f"machine; for now the legacy prose still works.)"
            ), state

        return _error(
            f"Unknown mode '{mode}' from wizard_decide_mode. "
            f"This is a wizard_run.py bug — please report."
        ), state

    # ---------- STEP_DECIDED → bash bump done ----------
    # The AI just ran wizard_bump_action.sh. bash_rc is the exit code.
    if step == _STEP_DECIDED and state.get("mode") == "bump-only":
        if bash_rc is None:
            return _error(
                "wizard expected a --bash-rc value after the bump "
                "action ran. The harness should pass --bash-rc with "
                "the exit code from the previous Bash invocation."
            ), state
        if bash_rc != 0:
            state["step"] = _STEP_DONE
            return _done(
                f"Pin bump script exited {bash_rc}. Review the bash "
                f"output above; the most common cause is a push that "
                f"needs manual intervention (auth, force-push policy). "
                f"The commit IS in place locally."
            ), state
        state["step"] = _STEP_DONE
        return _done(
            f"PRoctor action pinned to {current_tag}. Committed + "
            f"pushed. Wizard complete."
        ), state

    # ---------- STEP_NEEDS_LOCAL_REGEN_ASKED → branch on answer ----------
    if step == _STEP_NEEDS_LOCAL_REGEN_ASKED:
        if not answer:
            return _error(
                "wizard expected an --answer after the "
                "needs-local-regen question. Re-invoke with the "
                "user's selected option label."
            ), state

        if "Regenerate seed-local.sh AND re-run" in answer:
            state["step"] = _STEP_DONE
            return _show(
                "## Next: regenerate seed-local.sh + re-run it\n\n"
                "The chosen path walks Section 7's setup-confirmation "
                "(Step 7f) + Section 8c-pre's seed-script "
                "regeneration. v0.5.0 hasn't migrated this path into "
                "the state machine yet — fall back to the legacy "
                "SKILL.md prose: start at Section 7a (login mechanism "
                "confirmation) and walk through 7e + 7f.\n\n"
                "After regeneration, run `./.proctor/seed-local.sh` "
                "to populate `.proctor/local.yml`."
            ), state

        if "Just run the existing seed-local.sh" in answer:
            state["step"] = _STEP_DONE
            return _done(
                "Wizard exit. Run `./.proctor/seed-local.sh` to "
                "populate `.proctor/local.yml` using the existing "
                "(possibly stale) baked-in setup commands."
            ), state

        # Skip — fall through to bump-only if applicable.
        state["step"] = _STEP_DONE
        current_pin = state["detected_state"].get("current_pin")
        if current_tag and current_pin and current_pin != current_tag:
            return _done(
                "Wizard exit. `.proctor/local.yml` skip honored. "
                f"Note: action pin is {current_pin}, latest is "
                f"{current_tag} — run the wizard again to bump if "
                f"you want."
            ), state
        return _done(
            "Wizard exit. `.proctor/local.yml` skip honored. "
            "Action pin already current."
        ), state

    # ---------- STEP_LEGACY_MIGRATION_ASKED → branch on answer ----------
    if step == _STEP_LEGACY_MIGRATION_ASKED:
        if not answer:
            return _error(
                "wizard expected an --answer after the legacy-"
                "migration question."
            ), state

        if "Migrate to v0.4.0 layout" in answer:
            state["step"] = _STEP_DONE
            return _show(
                "## Migrating to v0.4.0 layout\n\n"
                "The migration is multi-step (preview → execute → "
                "summary) and v0.5.0 keeps it in the legacy SKILL.md "
                "prose (lines 71-145). Fall back to that section for "
                "the actual `git mv` + `.gitignore` patching steps."
            ), state

        # Keep current layout.
        state["step"] = _STEP_DONE
        return _done(
            "Wizard exit. Layout migration declined. The v0.3.x "
            "compatibility shim in schema.load_config will keep "
            "reading the legacy paths with a deprecation warning "
            "each run."
        ), state

    # ---------- v0.7.8: amend-daemons flow ----------
    # Three-state chain: offered → scanned (post-bash) → picked
    # (post-multi-select). Final write happens in the picked
    # transition; emits `done`.
    if step == _STEP_AMEND_DAEMONS_OFFERED:
        if not answer:
            return _error(
                "wizard expected an --answer after the amend-daemons "
                "offer question."
            ), state
        if "Scan for daemon binaries" not in answer:
            # User picked "Skip — my setup is fine".
            state["step"] = _STEP_DONE
            return _done(
                "Wizard exit. Daemon scan declined. Re-run "
                "/proctor:proctor-init later to revisit."
            ), state
        # User picked "Scan". Emit a bash envelope to run the
        # detector and dump its JSON to /tmp. We can't read the
        # bash stdout through the state machine (envelope contract
        # only returns rc), so the detector writes to a fixed file
        # and the next state reads it back.
        detector = plugin_root / "scripts" / "wizard_detect_binaries.py"
        cmd = (
            f'python3 "{detector}" --repo-root "{repo_root}" '
            f'> "{_BINARIES_JSON_PATH}"'
        )
        return _bash(
            cmd,
            description=(
                "Scan cmd/*/main.go + root main.go for daemons "
                "(writes JSON to /tmp/proctor-wizard-binaries.json)."
            ),
        ), {**state, "step": _STEP_AMEND_DAEMONS_SCANNED}

    if step == _STEP_AMEND_DAEMONS_SCANNED:
        if bash_rc is None:
            return _error(
                "wizard expected a --bash-rc after the daemon scan "
                "bash command. The harness should pass --bash-rc with "
                "the detector's exit code."
            ), state
        if bash_rc != 0:
            state["step"] = _STEP_DONE
            return _done(
                f"wizard_detect_binaries.py exited {bash_rc}. The "
                "scan failed; review the bash output above. Local "
                ".proctor/local.yml left untouched."
            ), state
        try:
            data = json.loads(Path(_BINARIES_JSON_PATH).read_text())
            candidates = data.get("candidates", [])
        except (OSError, json.JSONDecodeError) as e:
            state["step"] = _STEP_DONE
            return _done(
                f"Could not read binary scan output from "
                f"{_BINARIES_JSON_PATH}: {e}. Re-run the wizard."
            ), state
        if not candidates:
            state["step"] = _STEP_DONE
            return _done(
                "Daemon scan found no Go binaries under cmd/ or root "
                "main.go. Nothing to amend. (This is normal for "
                "Node / Python / Ruby projects.)"
            ), state

        # Build multi-select options. http-server / daemon /
        # one-shot / unknown — all surfaced; descriptions cite the
        # evidence so user can sanity-check the heuristic. Daemons
        # are listed first (most likely to want), then unknowns,
        # then one-shots (least likely).
        priority = {"daemon": 0, "unknown": 1, "http-server": 2, "one-shot": 3}
        ordered = sorted(
            candidates,
            key=lambda c: (priority.get(c.get("looks_like", "unknown"), 4),
                           c.get("path", "")),
        )
        options = []
        for c in ordered:
            label_prefix = {
                "daemon": "[recommended]",
                "http-server": "[skip — already covered by Step 7f]",
                "one-shot": "[skip — run on-demand]",
                "unknown": "[unsure]",
            }.get(c["looks_like"], "[?]")
            ev = ", ".join(c.get("evidence", [])) or "no evidence"
            options.append({
                "label": f"{label_prefix} {c['path']}",
                "description": (
                    f"binary_name={c['binary_name']} · "
                    f"looks_like={c['looks_like']} · evidence: {ev}"
                ),
            })
        # Stash the candidates list so the next transition can map
        # selected labels back to paths/binary-names without
        # re-reading the JSON.
        new_state = {
            **state,
            "step": _STEP_AMEND_DAEMONS_PICKED,
            "amend_candidates": ordered,
        }
        return _ask_user(
            header="Daemons to start in setup",
            question=(
                "Pick the binaries PRoctor should start during "
                "setup. Daemons (publish loops / cron / workers) "
                "are the typical pick. http-server entries are "
                "already covered by the existing setup wait-loop "
                "(Step 7f) — only re-add here if you want to start "
                "an additional server. one-shot / unknown entries "
                "are off by default; pick only if you know they "
                "should run on every test."
            ),
            options=options,
            multi_select=True,
        ), new_state

    if step == _STEP_AMEND_DAEMONS_PICKED:
        if answer is None:
            return _error(
                "wizard expected an --answer after the daemon "
                "multi-select. Pass the selected labels as a "
                "comma-separated string (use '' if user picked "
                "nothing)."
            ), state
        candidates = state.get("amend_candidates", [])
        # Empty string == "user deselected everything" — valid;
        # treat as a no-op.
        selected_labels = [s.strip() for s in answer.split(",") if s.strip()]
        # Map labels back to candidate dicts by suffix match (label
        # ends with the path field). Tolerant: if a label doesn't
        # match any candidate, skip it.
        chosen = []
        for label in selected_labels:
            for c in candidates:
                if c.get("path") and label.endswith(c["path"]):
                    chosen.append(c)
                    break
        if not chosen:
            state["step"] = _STEP_DONE
            return _done(
                "No daemons selected. `.proctor/local.yml` left "
                "untouched."
            ), state
        # Amend local.yml. Path resolution: prefer .proctor/local.yml,
        # fall back to .pr-test.local.yml (v0.3.x layout).
        local_path = repo_root / ".proctor" / "local.yml"
        if not local_path.exists():
            local_path = repo_root / ".pr-test.local.yml"
        if not local_path.exists():
            state["step"] = _STEP_DONE
            return _done(
                "Could not find `.proctor/local.yml` (or legacy "
                "`.pr-test.local.yml`) — nothing to amend. The "
                "amend-daemons mode shouldn't have fired without "
                "this file present; this is a wizard bug."
            ), state
        try:
            added = _amend_local_yml_with_daemons(local_path, chosen)
        except Exception as e:  # noqa: BLE001 — surface any failure
            state["step"] = _STEP_DONE
            return _done(
                f"Failed to amend {local_path}: {e}. The file was "
                "not modified (write is atomic — original is "
                "preserved)."
            ), state
        state["step"] = _STEP_DONE
        names = ", ".join(c["binary_name"] for c in chosen)
        return _done(
            f"Amended {local_path}: added {added} daemon line "
            f"group(s) to `setup:` ({names}). Re-run PRoctor and "
            "the planner will see the daemon(s) running."
        ), state

    # ---------- already done — re-invocation should never happen ----------
    if step == _STEP_DONE:
        return _done(
            "Wizard already complete (no more state transitions)."
        ), state

    return _error(
        f"Unknown wizard step '{step}'. State file may be corrupted; "
        f"delete `.proctor/wizard-state.json` and re-invoke the "
        f"wizard."
    ), state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state-file",
        default=".proctor/wizard-state.json",
        help="Path to the wizard's persistent state JSON. Defaults "
             "to .proctor/wizard-state.json so the wizard's state "
             "lives alongside the rest of PRoctor's consumer files.",
    )
    p.add_argument(
        "--current-tag",
        default=None,
        help="Latest PRoctor release tag from the harness's gh "
             "lookup. Passed through to wizard_decide_mode.",
    )
    p.add_argument(
        "--repo-root",
        default=".",
        help="Consumer repo root (default: cwd).",
    )
    p.add_argument(
        "--answer",
        default=None,
        help="The user's selection label from the previous ask_user "
             "envelope. Required when the previous emit was ask_user.",
    )
    p.add_argument(
        "--bash-rc",
        type=int,
        default=None,
        help="The exit code of the previous bash envelope's command. "
             "Required when the previous emit was bash.",
    )
    p.add_argument(
        "--plugin-root",
        default=None,
        help="The plugin's root directory (where scripts/ lives). "
             "Used to construct paths to sibling helpers. Defaults "
             "to the parent of this script's directory.",
    )
    args = p.parse_args(argv)

    state_file = Path(args.state_file)
    repo_root = Path(args.repo_root).resolve()
    plugin_root = (
        Path(args.plugin_root).resolve()
        if args.plugin_root
        else Path(__file__).resolve().parent.parent
    )

    state = _load_state(state_file)
    envelope, new_state = _run_step(
        state=state,
        answer=args.answer,
        bash_rc=args.bash_rc,
        repo_root=repo_root,
        current_tag=args.current_tag,
        plugin_root=plugin_root,
    )
    # v0.7.3: when the wizard reaches `step=done`, delete the state file
    # instead of persisting it. The file's only purpose is "resume after
    # interrupt" — once we've emitted `done` there's nothing to resume,
    # and leaving the stale file confuses subsequent re-runs (a fresh
    # wizard invocation should start from scratch, not resume "done").
    # Also: keeps `.proctor/` clean of bookkeeping artifacts the consumer
    # doesn't need to see.
    if envelope.get("type") == "done":
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass
    else:
        _save_state(state_file, new_state)
    _emit(envelope)
    return 0


# Allow `from wizard_decide_mode import ...` at the local-import line
# above to resolve to the sibling script.
sys.path.insert(0, str(Path(__file__).resolve().parent))


if __name__ == "__main__":
    raise SystemExit(main())
