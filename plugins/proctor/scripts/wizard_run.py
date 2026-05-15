"""Step-iterator driver for the /proctor:proctor-init wizard.

v0.5.0 introduced a state-machine driver to replace 1300+ lines of
prose in ``commands/proctor-init.md``. v0.7.8 added an
``amend-daemons`` mode behind a single-mode dispatcher
(``wizard_decide_mode.py``). Real runs against mcd-website found
the single-mode dispatcher couldn't cover the "multiple things
need doing in one wizard invocation" case — a stale action pin
AND a missing supplementary binary in setup AND a missing
local.yml all at once.

v0.7.9 (this file): the wizard becomes a STEP ITERATOR over the
ordered list returned by ``wizard_decide_steps.decide_steps()``.
Each step is a self-contained state machine with its own sub-states
(offered / scanned / picked / written / etc.). When a step
completes, the iterator pops the next step and starts it. The
terminal ``done`` envelope only fires when ALL pending steps have
completed.

## IPC protocol (unchanged from v0.5.0)

Each invocation reads ``--state-file``, advances by ONE transition,
writes state back, and emits exactly one JSON envelope to stdout:

- ``{"type": "ask_user", "header": "...", "question": "...",
   "options": [...]}`` — AI calls AskUserQuestion, re-invokes with
   ``--answer "<label>"``.
- ``{"type": "show", "markdown": "..."}`` — AI prints the markdown,
   re-invokes (no flags).
- ``{"type": "bash", "command": "...", "description": "..."}`` —
   AI runs the command, re-invokes with ``--bash-rc <exit>``.
- ``{"type": "done", "summary": "..."}`` — wizard complete.
- ``{"type": "error", "message": "..."}`` — abort.

## State persistence

``--state-file`` defaults to ``.proctor/wizard-state.json``.
Schema (v0.7.9):

```jsonc
{
  "pending_steps": ["step_bump_action_pin", "step_supplement_setup"],
  "current_step": "step_bump_action_pin",
  "current_step_substate": "running_bash",
  "completed_steps": [
    {"step": "step_legacy_layout_migrate", "outcome": "shown"}
  ],
  "step_data": { "<step_id>": { /* per-step working data */ } },
  "current_tag": "v0.7.9",
  "detected_state": { /* from wizard_decide_steps.detect_state */ }
}
```

Backward compat with v0.5.0–v0.7.8 schemas: when loading an old
state file with a top-level ``step`` field (no ``pending_steps``),
the loader migrates it transparently — we map the old ``step`` to
the equivalent v0.7.9 ``current_step`` + ``current_step_substate``
when possible, otherwise reset to fresh state. Defensive: a
corrupted state file always resets to fresh rather than locking
the user out.

The state file is auto-deleted on terminal ``done`` (v0.7.3
behavior, preserved). Only the FINAL done — intermediate per-step
completions don't delete the file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# Sub-state markers used inside each step handler. Scoped to the
# step that owns them — different steps reuse the same labels
# without conflict because ``current_step`` plus
# ``current_step_substate`` together identify the position.
SUB_OFFERED = "offered"
SUB_SCANNED = "scanned"
SUB_PICKED = "picked"
SUB_RUNNING_BASH = "running_bash"
SUB_ASKED = "asked"

# Sentinel substate signaling "this step is done — pop next".
SUB_COMPLETE = "complete"


# Path where wizard_run.py asks the AI to dump the binaries JSON
# between the bash envelope and the next state transition. Kept in
# /tmp so it doesn't litter the repo, fixed path so re-entry after
# AI crash can find it without re-scanning.
_BINARIES_JSON_PATH = "/tmp/proctor-wizard-binaries.json"


def _load_state(state_file: Path) -> dict:
    """Read state from disk; return fresh empty state if file missing
    or empty. Defensive against corrupted state and against legacy
    state files (v0.5.0–v0.7.8 single-mode schemas) — both reset to
    fresh state rather than crash."""
    if not state_file.exists():
        return _fresh_state()
    try:
        data = json.loads(state_file.read_text())
        if not isinstance(data, dict):
            return _fresh_state()
        # Legacy schema detection: v0.5.0–v0.7.8 state files had a
        # top-level ``step`` field with values like ``"decided"`` /
        # ``"amend_daemons_offered"``. v0.7.9 replaced those with
        # ``current_step`` + ``current_step_substate``. We reset
        # rather than try to migrate — the legacy state was always
        # short-lived (one wizard invocation typically completes the
        # flow), so the loss is the user re-answering one question.
        if "step" in data and "current_step" not in data:
            return _fresh_state()
        return data
    except (json.JSONDecodeError, OSError):
        return _fresh_state()


def _fresh_state() -> dict:
    return {
        "pending_steps": None,  # None = haven't run detection yet
        "current_step": None,
        "current_step_substate": None,
        "completed_steps": [],
        "step_data": {},
    }


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


# --- amend-local-yml helper (used by step_supplement_setup) --------

def _amend_local_yml_with_daemons(
    local_path: Path,
    chosen: list[dict],
) -> int:
    """Insert kill+start command pairs into ``setup:`` of a local.yml.

    For each candidate in ``chosen``, append two lines to the
    ``setup:`` list:

    .. code-block:: yaml

        - bash -c '[ -f /tmp/proctor-<NAME>.pid ] && kill ...'
        - bash -c 'set -a; . ./dev_env_local ...; nohup go run ./<PATH> ...'

    Returns the count of candidates actually added (skips those
    whose path/binary-name already appears anywhere in the setup
    block — idempotent re-runs don't duplicate). Preserves comments
    and indentation by working as a string-level edit (no YAML
    round-trip).

    Insertion point: end of the setup list, BEFORE any non-list
    sibling line (next top-level key, or EOF).

    Function name preserved from v0.7.8 for backward-compat with
    tests that import it directly. The lines it writes are
    intentionally identical to v0.7.8 — only the surrounding wizard
    UX uses the new ``runs-loop`` / supplementary-binary terminology.
    """
    text = local_path.read_text()
    lines = text.splitlines(keepends=True)

    setup_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("setup:") and not line.lstrip().startswith("setup: ["):
            setup_idx = i
            break
    if setup_idx is None:
        raise ValueError("local.yml has no expanded `setup:` block")

    setup_indent = len(lines[setup_idx]) - len(lines[setup_idx].lstrip())
    item_indent = setup_indent + 2
    for j in range(setup_idx + 1, min(setup_idx + 30, len(lines))):
        stripped = lines[j].lstrip()
        if stripped.startswith("- "):
            item_indent = len(lines[j]) - len(stripped)
            break

    insert_at = len(lines)
    for j in range(setup_idx + 1, len(lines)):
        stripped_full = lines[j].rstrip("\n")
        bare = stripped_full.lstrip()
        cur_indent = len(stripped_full) - len(bare)
        if not bare or bare.startswith("#"):
            continue
        if cur_indent <= setup_indent:
            insert_at = j
            break

    existing_setup_block = "".join(lines[setup_idx:insert_at])
    added = 0
    new_chunk: list[str] = []
    for c in chosen:
        name = c["binary_name"]
        path = c["path"]
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

    new_lines = lines[:insert_at] + new_chunk + lines[insert_at:]
    new_text = "".join(new_lines)
    tmp_path = local_path.with_suffix(local_path.suffix + ".wizard-tmp")
    tmp_path.write_text(new_text)
    tmp_path.replace(local_path)
    return added


def _write_setup_block_yml(
    setup_block_path: Path,
    chosen: list[dict],
) -> int:
    """Write or amend ``.proctor/setup-block.yml`` with kill+start
    command pairs for each chosen binary.

    The file is the canonical source for the ``setup:`` block —
    ``seed-local.sh`` reads from here when regenerating
    ``.proctor/local.yml`` (v0.7.9+; pre-v0.7.9 seed scripts had the
    block hard-coded in a heredoc, so wizard amendments were lost on
    every seed-script re-run). When the file already exists, this
    function reads it, adds only the new entries (idempotent by
    pidfile name / path), and rewrites.

    When the file doesn't exist, it's created with a minimal template
    header + the ``setup:`` array containing one kill+start pair per
    chosen binary.

    Returns the count of binaries actually added (0 if every chosen
    one was already in the existing block).
    """
    template_header = (
        "# AUTO-MANAGED by /proctor:proctor-init wizard. Hand edits\n"
        "# will be honored until the next wizard run that touches\n"
        "# the supplementary-binaries step.\n"
        "#\n"
        "# Re-run `claude /proctor:proctor-init` to update.\n"
        "# Then re-run `./.proctor/seed-local.sh` so the generated\n"
        "# `.proctor/local.yml` picks up the new setup block.\n"
    )
    if setup_block_path.exists():
        try:
            existing = setup_block_path.read_text(errors="replace")
        except OSError:
            existing = ""
    else:
        existing = ""

    if "setup:" not in existing:
        existing = template_header + "setup:\n"

    # Detect existing entries by pidfile name and path (same
    # idempotency as _amend_local_yml_with_daemons).
    added = 0
    new_lines: list[str] = []
    for c in chosen:
        name = c["binary_name"]
        path = c["path"]
        if f"proctor-{name}.pid" in existing:
            continue
        if f"./{path}" in existing:
            continue
        new_lines.append(
            f"  - bash -c '[ -f /tmp/proctor-{name}.pid ] && "
            f"kill \"$(cat /tmp/proctor-{name}.pid)\" 2>/dev/null; "
            f"true'\n"
        )
        new_lines.append(
            f"  - bash -c 'set -a; . ./dev_env_local 2>/dev/null "
            f"|| . ./dev_env 2>/dev/null || true; set +a; "
            f"nohup go run ./{path} > /tmp/proctor-{name}.log "
            f"2>&1 & echo $! > /tmp/proctor-{name}.pid'\n"
        )
        added += 1

    if added == 0:
        # Nothing new to add. But still ensure the template exists
        # — if the file was absent and we just constructed an empty
        # ``setup:`` block above, write it so the next wizard run
        # sees the file.
        if not setup_block_path.exists():
            setup_block_path.parent.mkdir(parents=True, exist_ok=True)
            setup_block_path.write_text(existing)
        return 0

    new_text = existing
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "".join(new_lines)
    setup_block_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = setup_block_path.with_suffix(
        setup_block_path.suffix + ".wizard-tmp"
    )
    tmp_path.write_text(new_text)
    tmp_path.replace(setup_block_path)
    return added


# --- detection + decision (delegates to wizard_decide_steps) -------

def _detect_and_decide(repo_root: Path, current_tag: str | None) -> dict:
    """Call wizard_decide_steps.decide_steps + detect_state. Returns
    the envelope shape (state + steps + backward-compat mode)."""
    from wizard_decide_steps import (
        detect_state,
        decide_steps,
        _build_envelope,
    )
    state = detect_state(repo_root)
    steps = decide_steps(state, current_tag, repo_root=repo_root)
    return _build_envelope(state, steps, current_tag)


# --- step handler interface -----------------------------------------

# Per-step handlers are stored in this registry, keyed by step id.
# Each handler is a function:
#
#   handler(state, step_data, answer, bash_rc, repo_root, plugin_root,
#           current_tag) -> (envelope, new_step_data, sub_state)
#
# `state`: top-level state dict (read-only metadata like detected_state).
# `step_data`: this step's persisted data (read/write).
# `answer` / `bash_rc`: input from the previous AI tool call.
# Returns:
#   envelope     — what to emit this turn.
#   new_step_data — updated step_data to persist.
#   sub_state    — the new substate ('offered', 'scanned', 'picked',
#                  'complete', etc.) controlling the next dispatch.
#                  When sub_state == SUB_COMPLETE, the iterator pops
#                  the next pending step on the NEXT invocation.

_HANDLERS: dict = {}


def _register(step_id: str):
    def deco(fn):
        _HANDLERS[step_id] = fn
        return fn
    return deco


@_register("step_legacy_layout_migrate")
def _handle_legacy_layout(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Legacy migration is multi-step + tightly coupled to the
    consumer's `git mv` invocations. v0.5.0–v0.7.8 punted to legacy
    SKILL.md prose; v0.7.9 keeps that delegation but presents it as
    a regular step that completes after one `show` envelope.

    Sub-states:
      None → 'offered' (ask the user)
      'offered' + answer="Migrate..."  → 'shown', emit show envelope,
                                          then SUB_COMPLETE.
      'offered' + answer="Keep current" → SUB_COMPLETE.
    """
    sub = step_data.get("sub")
    if sub is None:
        info = _step_info("step_legacy_layout_migrate")
        return (
            _ask_user(
                header=info["ask_user"]["header"],
                question=info["ask_user"]["question"],
                options=info["ask_user"]["options"],
            ),
            {**step_data, "sub": SUB_ASKED},
            SUB_ASKED,
        )
    if sub == SUB_ASKED:
        if not answer:
            return (
                _error(
                    "wizard expected an --answer after the legacy-"
                    "layout-migration question."
                ),
                step_data,
                SUB_COMPLETE,
            )
        if "Migrate to v0.4.0 layout" in answer:
            return (
                _show(
                    "## Migrating to v0.4.0 layout\n\n"
                    "The migration is multi-step (preview → execute → "
                    "summary). v0.5.0+ keeps the actual `git mv` /\n"
                    "`.gitignore` patching steps in the legacy "
                    "`commands/proctor-init.md` prose (section 0.5). "
                    "Follow that section, then re-run the wizard — "
                    "subsequent steps in this run will pick up the "
                    "migrated layout."
                ),
                {**step_data, "outcome": "delegated-to-prose"},
                SUB_COMPLETE,
            )
        return (
            _show(
                "## Layout migration declined\n\n"
                "The v0.3.x compatibility shim in `schema.load_config` "
                "will keep reading the legacy paths with a deprecation "
                "warning each run."
            ),
            {**step_data, "outcome": "declined"},
            SUB_COMPLETE,
        )
    return _error(f"unknown legacy_layout sub-state {sub!r}"), step_data, SUB_COMPLETE


@_register("step_bump_action_pin")
def _handle_bump_pin(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Pin bump. Sub-states:
      None → emit bash envelope running wizard_bump_action.sh.
      'running_bash' + bash_rc → SUB_COMPLETE with outcome.
    """
    sub = step_data.get("sub")
    if sub is None:
        current_pin = (
            state.get("detected_state", {}).get("current_pin")
            or "<unknown>"
        )
        script = plugin_root / "scripts" / "wizard_bump_action.sh"
        cmd = f'bash "{script}" "{current_tag}"'
        return (
            _bash(
                cmd,
                description=(
                    f"Bump PRoctor action pin {current_pin} → "
                    f"{current_tag} (edit + diff + commit + push, "
                    f"atomic)."
                ),
            ),
            {**step_data, "sub": SUB_RUNNING_BASH},
            SUB_RUNNING_BASH,
        )
    if sub == SUB_RUNNING_BASH:
        if bash_rc is None:
            return (
                _error(
                    "wizard expected a --bash-rc value after the bump "
                    "action ran."
                ),
                step_data,
                SUB_COMPLETE,
            )
        outcome = "success" if bash_rc == 0 else f"exited {bash_rc}"
        return (
            None,  # no envelope of its own — let the iterator emit
                  # the cross-step done summary
            {**step_data, "outcome": outcome},
            SUB_COMPLETE,
        )
    return _error(f"unknown bump-pin sub-state {sub!r}"), step_data, SUB_COMPLETE


@_register("step_regenerate_local_yml")
def _handle_regen_local(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Regenerate ``.proctor/local.yml`` from the seed script.

    v0.7.10 implementation. Pre-v0.7.10 this handler emitted a
    ``show`` envelope pointing at legacy SKILL.md prose and called it
    done — no actual regeneration happened. The mcd-website audit
    showed that left existing consumers' seed scripts with the
    pre-v0.7.9 hardcoded SETUP_BLOCK heredoc, so wizard writes to
    ``setup-block.yml`` were silently ignored on every re-run.

    The handler now does real work in three phases:

    1. **Migrate seed-local.sh in place** when it still ships with
       the legacy hardcoded heredoc. Salvages the existing heredoc
       content into ``.proctor/setup-block.yml`` (preserving the
       user's tailored setup) when that file doesn't exist, then
       rewrites the heredoc block in seed-local.sh to read from
       ``.proctor/setup-block.yml`` (with a generic fallback heredoc
       for the absent-file case).

       Already-migrated seed scripts (containing the v0.7.9 awk
       reader pattern) skip this phase.

    2. **Emit a ``bash`` envelope** that runs
       ``./.proctor/seed-local.sh`` against the consumer's env. The
       AI invokes the command, captures rc, re-invokes the wizard
       with ``--bash-rc``.

    3. **Handle exit code**. rc=0 → SUB_COMPLETE with
       ``regenerated-and-ran``. rc≠0 → emit an ``error`` envelope
       with actionable guidance (bring DB up via docker-compose,
       re-run the wizard — it picks up here).

    Sub-states: ``None`` → ``running_bash``; ``running_bash`` +
    ``bash_rc`` → SUB_COMPLETE.

    The pre-v0.7.10 ask_user that offered three regeneration
    options is gone — every option except "regenerate + re-run" was
    either a no-op or a punt to legacy prose, so v0.7.10 just does
    the right thing without asking. The user-facing AskUser prose in
    ``wizard_decide_steps._STEP_INFO`` is preserved for the
    backward-compat shim only.
    """
    sub = step_data.get("sub")
    if sub is None:
        seed_path = repo_root / ".proctor" / "seed-local.sh"
        local_path = repo_root / ".proctor" / "local.yml"

        if not seed_path.is_file():
            return (
                _show(
                    "## `.proctor/seed-local.sh` not found\n\n"
                    "Cannot regenerate `.proctor/local.yml` without a "
                    "seed script. Re-run `/proctor:proctor-init` "
                    "after the seed script is in place, or generate "
                    "one via the fresh-install path."
                ),
                {**step_data, "outcome": "seed-script-missing"},
                SUB_COMPLETE,
            )

        # Phase 1: migrate seed-local.sh in place when needed.
        migrate_outcome = "skipped"
        try:
            migrate_outcome = _migrate_seed_local_sh(
                seed_path=seed_path,
                setup_block_path=repo_root / ".proctor" / "setup-block.yml",
            )
        except Exception as e:  # noqa: BLE001
            return (
                _show(
                    f"## Seed-local.sh migration failed\n\n"
                    f"{e}\n\n"
                    f"`.proctor/seed-local.sh` left untouched. Run "
                    "the wizard again after resolving the file-system "
                    "error."
                ),
                {
                    **step_data,
                    "outcome": f"migrate-failed: {e}",
                },
                SUB_COMPLETE,
            )

        # Phase 2: emit the bash envelope that runs seed-local.sh.
        # We deliberately do NOT prefix `docker-compose up -d db` or
        # any consumer-specific env bootstrap — those vary per repo
        # and the user knows which ones to run beforehand. If the DB
        # isn't reachable seed-local.sh's own setup commands will
        # exit non-zero, the wizard surfaces the failure, the user
        # brings up the dependencies, and re-runs the wizard (this
        # step picks up where it left off because rc≠0 doesn't mark
        # the step complete).
        cmd = f'bash "{seed_path}"'
        if local_path.exists():
            description = (
                "Re-run seed-local.sh to refresh "
                ".proctor/local.yml from the current "
                ".proctor/setup-block.yml + config.yml."
            )
        else:
            description = (
                "Run seed-local.sh to generate "
                ".proctor/local.yml from the current "
                ".proctor/setup-block.yml + config.yml."
            )
        return (
            _bash(cmd, description=description),
            {
                **step_data,
                "sub": SUB_RUNNING_BASH,
                "migrate_outcome": migrate_outcome,
            },
            SUB_RUNNING_BASH,
        )

    if sub == SUB_RUNNING_BASH:
        if bash_rc is None:
            return (
                _error(
                    "wizard expected --bash-rc after seed-local.sh "
                    "ran."
                ),
                step_data,
                SUB_COMPLETE,
            )
        migrate_outcome = step_data.get("migrate_outcome", "skipped")
        if bash_rc == 0:
            local_path = repo_root / ".proctor" / "local.yml"
            local_ok = local_path.exists()
            return (
                _show(
                    f"## Regenerated `.proctor/local.yml`\n\n"
                    f"seed-local.sh exited 0 "
                    f"(migration: {migrate_outcome}).\n"
                    + (
                        "`.proctor/local.yml` is present and ready "
                        "to be read by PRoctor.\n"
                        if local_ok else
                        "WARNING: seed-local.sh succeeded but "
                        "`.proctor/local.yml` is not present. The "
                        "seed script may be writing to a different "
                        "path — check its output.\n"
                    )
                ),
                {
                    **step_data,
                    "outcome": (
                        f"regenerated-and-ran "
                        f"(migration: {migrate_outcome})"
                    ),
                },
                SUB_COMPLETE,
            )
        # rc ≠ 0 — emit an error envelope and DON'T mark the step
        # complete, so re-running the wizard picks up here. We
        # surface actionable guidance for the most common failure
        # mode (DB / Redis not running) but stay generic — the
        # bash envelope's output is already in the AI's transcript.
        return (
            _error(
                "seed-local.sh exited "
                f"{bash_rc}. The most common cause is that the local "
                "dev dependencies aren't running yet (Postgres, "
                "Redis, MySQL, etc. depending on your stack). Bring "
                "them up — typically via `docker-compose up -d` "
                "or a project-specific `make dev` — then re-run "
                "`/proctor:proctor-init`. The wizard will resume "
                "at this step.\n\n"
                f"Migration phase outcome: {migrate_outcome}. The "
                "seed-script rewrite (if any) is already on disk; "
                "the failure is downstream."
            ),
            {
                **step_data,
                "outcome": (
                    f"seed-rerun-failed-rc-{bash_rc} "
                    f"(migration: {migrate_outcome})"
                ),
                # Reset sub so re-running re-emits the bash envelope.
                "sub": None,
            },
            SUB_COMPLETE,
        )
    return _error(
        f"unknown regenerate-local-yml sub-state {sub!r}"
    ), step_data, SUB_COMPLETE


# v0.7.10 — seed-local.sh in-place migration. Pre-v0.7.9 seed scripts
# shipped with the SETUP_BLOCK content hardcoded in a heredoc. The
# rewrite replaces the heredoc block with a conditional that reads
# from .proctor/setup-block.yml when present (and falls back to a
# generic heredoc for the file-absent case).
#
# Pattern shape we look for:
#
#     SETUP_BLOCK=$(cat <<'YAML'
#       - <commands>
#       ...
#     YAML
#     )
#
# Rewritten to:
#
#     if [ -f .proctor/setup-block.yml ]; then
#         SETUP_BLOCK=$(awk '/^setup:/,0' .proctor/setup-block.yml | tail -n +2)
#     else
#         SETUP_BLOCK=$(cat <<'YAML'
#       - <generic fallback>
#     YAML
#     )
#     fi

import re as _re  # local alias to avoid colliding with module-top re

_HEREDOC_BLOCK_RE = _re.compile(
    r"^([ \t]*)SETUP_BLOCK=\$\(cat\s+<<\s*'YAML'\s*\n"
    r"(.*?)"
    r"^([ \t]*)YAML\s*\n"
    r"([ \t]*)\)\s*$",
    _re.MULTILINE | _re.DOTALL,
)


def _seed_already_migrated(text: str) -> bool:
    return bool(
        _re.search(
            r"awk\s+'[^']*setup:[^']*'\s+\.proctor/setup-block\.yml",
            text,
        )
    )


def _migrate_seed_local_sh(
    seed_path: Path, setup_block_path: Path,
) -> str:
    """Migrate an existing ``.proctor/seed-local.sh`` to read its
    SETUP_BLOCK content from ``.proctor/setup-block.yml`` instead of
    a hardcoded heredoc.

    Steps:

    1. Read seed-local.sh. If it already contains the v0.7.9 awk
       reader, return ``"already-migrated"`` without touching the
       file.
    2. Locate the ``SETUP_BLOCK=$(cat <<'YAML' ... YAML)`` block.
       Salvage the heredoc body. When ``setup_block_path`` doesn't
       exist, write the salvaged body into it (preserves the user's
       tailored hardcoded commands as the baseline). When the file
       already exists, leave it alone — the wizard's prior writes
       are the source of truth.
    3. Replace the heredoc block with the conditional that reads
       setup-block.yml (with the salvaged heredoc body kept as the
       fallback for when setup-block.yml is absent).
    4. Write seed-local.sh atomically (tmp-file rename).

    Returns one of:
    - ``"already-migrated"`` — no rewrite was needed.
    - ``"migrated-salvaged-setup-block"`` — rewrote seed-local.sh
       and salvaged content into setup-block.yml.
    - ``"migrated-kept-existing-setup-block"`` — rewrote
       seed-local.sh; setup-block.yml already existed so wasn't
       touched.
    - ``"no-heredoc-found"`` — seed-local.sh doesn't contain the
       legacy heredoc pattern and doesn't contain the awk reader
       either. Unusual shape; we leave it alone.

    Idempotent across re-runs: a migrated seed script is detected
    by the awk-reader presence and skipped.
    """
    text = seed_path.read_text(errors="replace")
    if _seed_already_migrated(text):
        return "already-migrated"

    m = _HEREDOC_BLOCK_RE.search(text)
    if m is None:
        return "no-heredoc-found"

    leading_indent = m.group(1)
    heredoc_body = m.group(2)
    yaml_terminator_indent = m.group(3)
    paren_indent = m.group(4)

    # Salvage to setup-block.yml when the file doesn't exist.
    salvage_outcome = "kept-existing-setup-block"
    if not setup_block_path.exists():
        setup_block_path.parent.mkdir(parents=True, exist_ok=True)
        salvaged_yaml = (
            "# AUTO-MANAGED by /proctor:proctor-init wizard. The\n"
            "# contents below were salvaged from the pre-v0.7.9\n"
            "# hardcoded SETUP_BLOCK heredoc inside seed-local.sh\n"
            "# during the v0.7.10 migration. Hand-edit freely — the\n"
            "# wizard's supplement-setup step appends new entries\n"
            "# without overwriting existing ones.\n"
            "setup:\n"
            + heredoc_body
        )
        if not salvaged_yaml.endswith("\n"):
            salvaged_yaml += "\n"
        tmp_sb = setup_block_path.with_suffix(
            setup_block_path.suffix + ".wizard-tmp"
        )
        tmp_sb.write_text(salvaged_yaml)
        tmp_sb.replace(setup_block_path)
        salvage_outcome = "salvaged-setup-block"

    # Build the replacement conditional. Preserve the original block's
    # leading indentation so the rewritten script stays consistent
    # with the surrounding code.
    fallback_body = heredoc_body.rstrip("\n") + "\n"
    replacement = (
        f"{leading_indent}if [ -f .proctor/setup-block.yml ]; then\n"
        f"{leading_indent}    SETUP_BLOCK=$(awk '/^setup:/,0' "
        f".proctor/setup-block.yml | tail -n +2)\n"
        f"{leading_indent}else\n"
        f"{leading_indent}    SETUP_BLOCK=$(cat <<'YAML'\n"
        f"{fallback_body}"
        f"{yaml_terminator_indent}YAML\n"
        f"{paren_indent})\n"
        f"{leading_indent}fi"
    )

    new_text = text[:m.start()] + replacement + text[m.end():]
    tmp_seed = seed_path.with_suffix(seed_path.suffix + ".wizard-tmp")
    tmp_seed.write_text(new_text)
    # Preserve the executable bit.
    import os as _os
    _os.chmod(tmp_seed, _os.stat(seed_path).st_mode)
    tmp_seed.replace(seed_path)

    return f"migrated-{salvage_outcome}"


@_register("step_supplement_setup")
def _handle_supplement(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Scan cmd/* binaries and ask which to add to setup-block.yml.

    Sub-states:
      None → 'offered' (ask scan / skip)
      'offered' + answer="Scan..." → 'scanning' (emit bash to run
                                     wizard_detect_binaries)
      'offered' + answer="Skip..." → SUB_COMPLETE (skipped)
      'scanning' + bash_rc=0 → 'picked' (emit multi-select ask)
      'picked' + answer → write setup-block.yml, SUB_COMPLETE.
    """
    sub = step_data.get("sub")
    if sub is None:
        info = _step_info("step_supplement_setup")
        return (
            _ask_user(
                header=info["ask_user"]["header"],
                question=info["ask_user"]["question"],
                options=info["ask_user"]["options"],
            ),
            {**step_data, "sub": SUB_OFFERED},
            SUB_OFFERED,
        )
    if sub == SUB_OFFERED:
        if not answer:
            return (
                _error(
                    "wizard expected an --answer after the supplement-"
                    "setup offer."
                ),
                step_data,
                SUB_COMPLETE,
            )
        if "Scan for supplementary binaries" not in answer:
            return (
                None,
                {**step_data, "outcome": "skipped"},
                SUB_COMPLETE,
            )
        detector = plugin_root / "scripts" / "wizard_detect_binaries.py"
        cmd = (
            f'python3 "{detector}" --repo-root "{repo_root}" '
            f'> "{_BINARIES_JSON_PATH}"'
        )
        return (
            _bash(
                cmd,
                description=(
                    "Scan cmd/*/main.go + root main.go for "
                    "supplementary binaries (writes JSON to "
                    f"{_BINARIES_JSON_PATH})."
                ),
            ),
            {**step_data, "sub": SUB_SCANNED},
            SUB_SCANNED,
        )
    if sub == SUB_SCANNED:
        if bash_rc is None:
            return (
                _error(
                    "wizard expected --bash-rc after the binary-scan "
                    "command."
                ),
                step_data,
                SUB_COMPLETE,
            )
        if bash_rc != 0:
            return (
                _show(
                    f"## Binary scan failed (exit {bash_rc})\n\n"
                    "Review the bash output above. `.proctor/setup-"
                    "block.yml` left untouched."
                ),
                {**step_data, "outcome": f"scan-failed-rc-{bash_rc}"},
                SUB_COMPLETE,
            )
        try:
            data = json.loads(Path(_BINARIES_JSON_PATH).read_text())
            candidates = data.get("candidates", [])
        except (OSError, json.JSONDecodeError) as e:
            return (
                _show(
                    f"## Could not read binary scan output\n\n"
                    f"Error reading {_BINARIES_JSON_PATH}: {e}. Re-"
                    "run the wizard."
                ),
                {**step_data, "outcome": "scan-output-unreadable"},
                SUB_COMPLETE,
            )
        if not candidates:
            return (
                _show(
                    "## No Go binaries detected\n\n"
                    "Nothing to add to `.proctor/setup-block.yml`. "
                    "(This is normal for Node / Python / Ruby "
                    "projects.)"
                ),
                {**step_data, "outcome": "no-candidates"},
                SUB_COMPLETE,
            )

        # Build multi-select options. Prioritize runs-loop > unknown
        # > serves-http > runs-once (matches v0.7.8 ordering with
        # neutral terminology). Accept legacy v0.7.8 labels too for
        # backward-compat with cached wizard JSON.
        priority = {
            "runs-loop": 0, "daemon": 0,
            "unknown": 1,
            "serves-http": 2, "http-server": 2,
            "runs-once": 3, "one-shot": 3,
        }
        ordered = sorted(
            candidates,
            key=lambda c: (
                priority.get(c.get("looks_like", "unknown"), 4),
                c.get("path", ""),
            ),
        )
        prefix_for = {
            "runs-loop": "[recommended]",
            "daemon": "[recommended]",
            "serves-http": "[skip — main server already in setup]",
            "http-server": "[skip — main server already in setup]",
            "runs-once": "[skip — run on-demand]",
            "one-shot": "[skip — run on-demand]",
            "unknown": "[unsure]",
        }
        options = []
        for c in ordered:
            label_prefix = prefix_for.get(c["looks_like"], "[?]")
            ev = "; ".join(c.get("evidence", [])) or "no evidence"
            options.append({
                "label": f"{label_prefix} {c['path']}",
                "description": (
                    f"binary_name={c['binary_name']} · "
                    f"looks_like={c['looks_like']} · evidence: {ev}"
                ),
            })
        return (
            _ask_user(
                header="Supplementary binaries to start in setup",
                question=(
                    "Select which binaries PRoctor should start "
                    "alongside your main server during setup. "
                    "The classification is heuristic — read each "
                    "binary's evidence and decide based on YOUR "
                    "project's intent.\n\n"
                    "Suggestion: 'runs-loop' binaries are typical "
                    "candidates (they run continuously and emit "
                    "side effects PRoctor may need to observe). "
                    "'runs-once' binaries are typically NOT for "
                    "setup. Your project's intent is authoritative."
                ),
                options=options,
                multi_select=True,
            ),
            {**step_data, "sub": SUB_PICKED, "candidates": ordered},
            SUB_PICKED,
        )
    if sub == SUB_PICKED:
        if answer is None:
            return (
                _error(
                    "wizard expected --answer after the binary "
                    "multi-select."
                ),
                step_data,
                SUB_COMPLETE,
            )
        candidates = step_data.get("candidates", [])
        selected_labels = [
            s.strip() for s in answer.split(",") if s.strip()
        ]
        chosen = []
        for label in selected_labels:
            for c in candidates:
                if c.get("path") and label.endswith(c["path"]):
                    chosen.append(c)
                    break
        if not chosen:
            return (
                _show(
                    "## No binaries selected\n\n"
                    "`.proctor/setup-block.yml` left untouched."
                ),
                {**step_data, "outcome": "no-selection"},
                SUB_COMPLETE,
            )

        # v0.7.9: write to .proctor/setup-block.yml (the canonical
        # source) AND, when an expanded local.yml exists, amend it
        # too so the current run picks up the change without
        # requiring a seed-script re-run.
        setup_block_path = repo_root / ".proctor" / "setup-block.yml"
        local_path = repo_root / ".proctor" / "local.yml"
        if not local_path.exists():
            local_path = repo_root / ".pr-test.local.yml"

        try:
            block_added = _write_setup_block_yml(setup_block_path, chosen)
        except Exception as e:  # noqa: BLE001
            return (
                _show(
                    f"## Failed to write setup-block.yml: {e}\n\n"
                    f"`.proctor/setup-block.yml` left in last known "
                    "good state."
                ),
                {**step_data, "outcome": "write-failed"},
                SUB_COMPLETE,
            )

        local_added = 0
        if local_path.exists():
            try:
                local_added = _amend_local_yml_with_daemons(
                    local_path, chosen,
                )
            except Exception as e:  # noqa: BLE001
                # Setup-block.yml is the source of truth — the
                # local.yml amendment is a convenience. Surface but
                # don't fail the step.
                return (
                    _show(
                        f"## Wrote setup-block.yml; local.yml amend "
                        f"failed: {e}\n\n"
                        f"Run `./.proctor/seed-local.sh` to "
                        "regenerate local.yml from the new setup-"
                        "block."
                    ),
                    {**step_data, "outcome": "wrote-block-not-local"},
                    SUB_COMPLETE,
                )

        names = ", ".join(c["binary_name"] for c in chosen)
        return (
            _show(
                f"## Wrote `.proctor/setup-block.yml` "
                f"({block_added} added, {len(chosen)} requested)\n\n"
                f"Selected supplementary binaries: {names}.\n"
                + (
                    f"Also amended `.proctor/local.yml` "
                    f"({local_added} kill+start pair(s) added).\n"
                    if local_added > 0 else ""
                )
                + "\nRun `./.proctor/seed-local.sh` to regenerate "
                "`.proctor/local.yml` and pick up the new setup."
            ),
            {
                **step_data,
                "outcome": (
                    f"wrote-block-added-{block_added}-"
                    f"local-added-{local_added}"
                ),
            },
            SUB_COMPLETE,
        )
    return _error(
        f"unknown supplement-setup sub-state {sub!r}"
    ), step_data, SUB_COMPLETE


@_register("step_fresh_install")
def _handle_fresh(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Fresh install is the full Section 1-8 walk — stays in legacy
    SKILL.md prose. The step emits one ``show`` pointer envelope,
    then SUB_COMPLETE."""
    return (
        _show(
            "## Wizard mode = fresh install\n\n"
            "First-time PRoctor setup. The full walk (stack "
            "detection → workflow generation → seed script → auth "
            "block → secrets) lives in the legacy "
            "`commands/proctor-init.md` Sections 1–8. v0.5.x will "
            "migrate these into the step iterator; for now the "
            "legacy prose still works."
        ),
        {**step_data, "outcome": "delegated-to-prose"},
        SUB_COMPLETE,
    )


def _step_info(step_id: str) -> dict:
    """Look up a step's metadata (next_action, ask_user) from the
    wizard_decide_steps registry."""
    from wizard_decide_steps import _STEP_INFO
    return _STEP_INFO.get(step_id, {})


# --- step iterator --------------------------------------------------

def _run_iteration(
    state: dict,
    answer: str | None,
    bash_rc: int | None,
    repo_root: Path,
    current_tag: str | None,
    plugin_root: Path,
) -> tuple[dict, dict]:
    """Advance the step iterator by ONE transition.

    Returns ``(envelope_to_emit, new_state)``.

    Algorithm:
    1. If pending_steps is None, run detection + populate it.
    2. If current_step is None and pending_steps is empty → emit
       terminal `done` summary.
    3. If current_step is None → pop the next step from
       pending_steps, set as current_step with empty step_data.
    4. Dispatch to the current step's handler. If the handler
       returns SUB_COMPLETE with no envelope, RECURSE — pop the
       next step and dispatch again in the same invocation (avoids
       the AI having to re-invoke just to advance past a no-op
       completion).
    """
    # 1. Detect on first invocation.
    if state.get("pending_steps") is None:
        decision = _detect_and_decide(repo_root, current_tag)
        state["detected_state"] = decision["state"]
        state["pending_steps"] = list(decision["steps"])
        # Backward-compat: surface the first step's legacy mode name
        # so prose / tests reading `mode` from state work.
        state["mode"] = decision.get("mode")
        state["current_tag"] = current_tag

    # 2. No more work?
    if not state.get("current_step") and not state["pending_steps"]:
        summary = _build_done_summary(state)
        return _done(summary), state

    # 3. Need to pop the next step?
    if not state.get("current_step"):
        next_step = state["pending_steps"].pop(0)
        state["current_step"] = next_step
        state.setdefault("step_data", {})[next_step] = {}

    # 4. Dispatch.
    cur = state["current_step"]
    handler = _HANDLERS.get(cur)
    if handler is None:
        return (
            _error(
                f"unknown step id {cur!r} — no handler registered. "
                "This is a wizard_run.py bug; report it."
            ),
            state,
        )
    step_data = state["step_data"].get(cur, {})
    envelope, new_step_data, sub_state = handler(
        state, step_data, answer, bash_rc, repo_root, plugin_root,
        current_tag,
    )
    state["step_data"][cur] = new_step_data
    state["current_step_substate"] = sub_state

    if sub_state == SUB_COMPLETE:
        # Append to completed_steps, clear current_step, and either
        # emit the handler's farewell envelope or recurse to the
        # next pending step.
        state.setdefault("completed_steps", []).append({
            "step": cur,
            "outcome": new_step_data.get("outcome"),
        })
        state["current_step"] = None
        state["current_step_substate"] = None
        if envelope is not None:
            return envelope, state
        # No envelope — silently pop the next step in the same
        # invocation. The AI's harness has no work to do for an
        # invisible transition; advancing here saves a round-trip.
        return _run_iteration(
            state, None, None, repo_root, current_tag, plugin_root,
        )

    # Mid-step transition — surface the handler's envelope.
    if envelope is None:
        return (
            _error(
                f"step {cur!r} handler returned no envelope but "
                f"sub_state={sub_state!r} (not SUB_COMPLETE). This "
                "is a handler bug."
            ),
            state,
        )
    return envelope, state


def _build_done_summary(state: dict) -> str:
    """Build the terminal ``done`` envelope's summary text by walking
    the completed_steps list."""
    completed = state.get("completed_steps") or []
    if not completed:
        return (
            "PRoctor is already integrated and up to date. No wizard "
            "actions taken."
        )
    lines = ["Wizard complete. Steps executed:"]
    for c in completed:
        outcome = c.get("outcome") or "done"
        lines.append(f"  - {c['step']} → {outcome}")
    # Add a hint when the supplement-setup step ran with a non-skip
    # outcome — the user needs to run seed-local.sh to pick up the
    # changes.
    if any(
        c["step"] == "step_supplement_setup"
        and c.get("outcome", "").startswith("wrote-block-added-")
        for c in completed
    ):
        lines.append("")
        lines.append(
            "Next: run `./.proctor/seed-local.sh` to regenerate "
            "`.proctor/local.yml` from the updated setup-block."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-file", default=".proctor/wizard-state.json")
    p.add_argument("--current-tag", default=None)
    p.add_argument("--repo-root", default=".")
    p.add_argument("--answer", default=None)
    p.add_argument("--bash-rc", type=int, default=None)
    p.add_argument("--plugin-root", default=None)
    args = p.parse_args(argv)

    state_file = Path(args.state_file)
    repo_root = Path(args.repo_root).resolve()
    plugin_root = (
        Path(args.plugin_root).resolve()
        if args.plugin_root
        else Path(__file__).resolve().parent.parent
    )

    state = _load_state(state_file)
    envelope, new_state = _run_iteration(
        state=state,
        answer=args.answer,
        bash_rc=args.bash_rc,
        repo_root=repo_root,
        current_tag=args.current_tag,
        plugin_root=plugin_root,
    )
    # v0.7.3: when the wizard reaches terminal `done`, delete the
    # state file instead of persisting it.
    # v0.7.9: terminal done is the FINAL done — intermediate
    # per-step completions emit other envelopes (or recurse silently)
    # and don't delete.
    if envelope.get("type") == "done":
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass
    else:
        _save_state(state_file, new_state)
    _emit(envelope)
    return 0


# Allow `from wizard_decide_steps import ...` to resolve to the
# sibling script regardless of how this module is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


if __name__ == "__main__":
    raise SystemExit(main())
