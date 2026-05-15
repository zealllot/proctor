"""Step-iterator driver for the /proctor:proctor-init wizard.

v0.5.0 introduced the state-machine driver. v0.7.9 generalized it
into a step iterator over an ordered list. v0.7.11 simplifies the
step set: the v0.7.7–v0.7.10 ``step_supplement_setup`` (detect
``cmd/*/main.go`` binaries, classify them, write ``setup-block.yml``)
is REMOVED. PRoctor doesn't try to be smart about project startup
anymore — the project owns its launch. The new
``step_dev_launcher`` just asks the user for their launch command
(``./dev.sh all`` / ``make dev`` / ``pnpm dev`` / etc.) and writes
it into ``.proctor/config.yml.dev_launcher``.

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
import re
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


# v0.7.7–v0.7.10 dropped binaries JSON to /tmp/proctor-wizard-binaries.json
# between the wizard's scan-bash envelope and the multi-select. Gone in
# v0.7.11 — step_dev_launcher asks the user directly, no scanning.


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


# v0.7.11: the v0.7.7–v0.7.10 helpers `_amend_local_yml_with_daemons`
# (inserted go-run kill+start pairs into local.yml) and
# `_write_setup_block_yml` (wrote `.proctor/setup-block.yml`) are
# gone — they belonged to the deleted supplement-setup step. PRoctor
# doesn't insert project-specific launch lines anymore; the project
# owns its launch via its own `./dev.sh` / `make dev` / `pnpm dev`
# script and the wizard's dev_launcher step just records the
# command.


# --- helper: write a YAML block into .proctor/config.yml ------------

def _append_dev_launcher_to_config(
    config_path: Path,
    start: str,
    stop: str | None,
    wait_for: str | None,
    wait_timeout_seconds: int | None,
) -> None:
    """Append a ``dev_launcher:`` block to ``config_path``. Idempotent:
    when the block already exists the file is left untouched.

    Implementation note: string-level edit so existing comments and
    formatting are preserved. The YAML written matches what
    ``schema.validate_pr_test_config`` accepts.
    """
    text = config_path.read_text() if config_path.exists() else ""
    if re.search(r"^dev_launcher:", text, re.MULTILINE):
        return  # already present — caller checked but be defensive
    lines = ["dev_launcher:", f"  start: {_yaml_quote(start)}"]
    if stop:
        lines.append(f"  stop: {_yaml_quote(stop)}")
    if wait_for:
        lines.append(f"  wait_for: {_yaml_quote(wait_for)}")
    if wait_timeout_seconds:
        lines.append(f"  wait_timeout_seconds: {wait_timeout_seconds}")
    block = "\n".join(lines) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    new_text = text + block
    tmp_path = config_path.with_suffix(config_path.suffix + ".wizard-tmp")
    tmp_path.write_text(new_text)
    tmp_path.replace(config_path)


def _yaml_quote(value: str) -> str:
    """Render ``value`` as a YAML scalar that round-trips through any
    safe loader. Single-quote-wrap when the string contains characters
    a bare scalar can't carry; otherwise leave as a plain scalar."""
    if value == "":
        return "''"
    # Strings with these characters or a leading reserved character
    # MUST be quoted in YAML.
    needs_quote = (
        any(c in value for c in ":#&*!|>'\"%@`{}[]\n")
        or value[0] in "-?,"
        or value != value.strip()
    )
    if not needs_quote:
        return value
    # Single-quote form: escape internal single quotes by doubling them.
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


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


@_register("step_dev_launcher")
def _handle_dev_launcher(
    state, step_data, answer, bash_rc, repo_root, plugin_root,
    current_tag,
):
    """Ask how the project starts its local dev environment and
    record the answer.

    Three paths the user can pick from the initial ask_user:

    A) "I have a one-click script" — wizard follows up with
       open-ended prompts for ``start`` / ``stop`` / ``wait_for`` /
       ``wait_timeout_seconds`` then appends a ``dev_launcher:``
       block to ``.proctor/config.yml``.
    B) "Show me a generic template I can adapt" — wizard copies
       ``plugins/proctor/templates/dev-launcher.sh.template`` to
       ``.proctor/dev-launcher-template.sh`` (chmod +x). No
       project-specific code is written; the file has TODO markers
       the user fills in with another Claude Code session.
    C) "Skip — keep using the legacy ``setup:`` array" — wizard
       emits a ``show`` envelope explaining the legacy path remains
       supported. No file changes.

    Sub-states:
      None → 'offered' (initial 3-option ask)
      'offered' + answer A → 'ask_start'   (open-ended)
      'ask_start' + answer → 'ask_stop'
      'ask_stop' + answer → 'ask_wait_for'
      'ask_wait_for' + answer → 'ask_timeout'
      'ask_timeout' + answer → write config, SUB_COMPLETE
      'offered' + answer B → write template, SUB_COMPLETE
      'offered' + answer C → SUB_COMPLETE (skipped)
    """
    sub = step_data.get("sub")
    if sub is None:
        info = _step_info("step_dev_launcher")
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
                    "wizard expected an --answer after the "
                    "dev-launcher offer."
                ),
                step_data,
                SUB_COMPLETE,
            )
        if "one-click script" in answer:
            return (
                _ask_user(
                    header="Dev launcher — start command",
                    question=(
                        "What's the bash command to bring up your "
                        "full local dev environment? Examples: "
                        "`./dev.sh all`, `make dev`, `pnpm dev`, "
                        "`docker-compose up -d`. PRoctor will run "
                        "this once at the start of each test run."
                    ),
                    options=[],
                ),
                {**step_data, "sub": "ask_start"},
                "ask_start",
            )
        if "generic template" in answer:
            return _emit_template_path(
                repo_root, plugin_root, step_data,
            )
        # Default = skip / "keep using the legacy" branch.
        return (
            _show(
                "## Skipped dev_launcher\n\n"
                "PRoctor will keep reading the legacy `setup:` "
                "array from `.proctor/local.yml` (or "
                "`.proctor/config.yml`) on each test run. This is "
                "fine for projects with simple needs. When your "
                "setup grows, re-run `/proctor:proctor-init` and "
                "pick a one-click script — the dev_launcher block "
                "supersedes `setup:` cleanly without breaking the "
                "legacy path for older consumers."
            ),
            {**step_data, "outcome": "skipped"},
            SUB_COMPLETE,
        )
    if sub == "ask_start":
        start_cmd = (answer or "").strip()
        if not start_cmd:
            return (
                _error(
                    "wizard expected a non-empty start command. "
                    "Re-run the wizard and provide one (e.g. "
                    "`./dev.sh all` / `make dev`)."
                ),
                step_data,
                SUB_COMPLETE,
            )
        return (
            _ask_user(
                header="Dev launcher — stop command (optional)",
                question=(
                    "Bash command to tear down the environment "
                    "after tests. Leave blank for no teardown. "
                    "Examples: `./dev.sh stop`, `make stop`, "
                    "`docker-compose down`, "
                    "`pkill -f 'pnpm dev'`."
                ),
                options=[],
            ),
            {**step_data, "sub": "ask_stop", "start": start_cmd},
            "ask_stop",
        )
    if sub == "ask_stop":
        stop_cmd = (answer or "").strip() or None
        return (
            _ask_user(
                header="Dev launcher — readiness check (optional)",
                question=(
                    "Bash command that exits 0 when the dev env "
                    "is ready (PRoctor polls this until success). "
                    "Leave blank to skip polling (PRoctor just "
                    "sleeps 2 seconds after `start`). Example: "
                    "`curl -fsS http://localhost:8080/healthz "
                    ">/dev/null 2>&1`."
                ),
                options=[],
            ),
            {**step_data, "sub": "ask_wait_for", "stop": stop_cmd},
            "ask_wait_for",
        )
    if sub == "ask_wait_for":
        wait_for_cmd = (answer or "").strip() or None
        return (
            _ask_user(
                header="Dev launcher — readiness timeout",
                question=(
                    "How long to poll `wait_for` before giving up "
                    "(seconds). Leave blank for default 60. Type "
                    "an integer (e.g. 90)."
                ),
                options=[],
            ),
            {
                **step_data,
                "sub": "ask_timeout",
                "wait_for": wait_for_cmd,
            },
            "ask_timeout",
        )
    if sub == "ask_timeout":
        raw = (answer or "").strip()
        wait_timeout = None
        if raw:
            try:
                wait_timeout = int(raw)
                if wait_timeout <= 0:
                    raise ValueError
            except ValueError:
                return (
                    _error(
                        f"wait_timeout_seconds must be a positive "
                        f"integer (got {raw!r}). Re-run the wizard "
                        f"and try again."
                    ),
                    step_data,
                    SUB_COMPLETE,
                )
        config_path = repo_root / ".proctor" / "config.yml"
        try:
            _append_dev_launcher_to_config(
                config_path,
                start=step_data["start"],
                stop=step_data.get("stop"),
                wait_for=step_data.get("wait_for"),
                wait_timeout_seconds=wait_timeout,
            )
        except Exception as e:  # noqa: BLE001
            return (
                _show(
                    f"## Failed to write dev_launcher block: {e}\n\n"
                    f"`.proctor/config.yml` left in its prior "
                    f"state."
                ),
                {**step_data, "outcome": f"write-failed: {e}"},
                SUB_COMPLETE,
            )
        stop_text = step_data.get("stop") or "(no-op)"
        wait_for_text = (
            step_data.get("wait_for")
            or "(none; PRoctor will sleep 2 s after start)"
        )
        timeout_text = (
            f"{wait_timeout}" if wait_timeout else "60 (default)"
        )
        return (
            _show(
                "## Wrote `dev_launcher` block to "
                "`.proctor/config.yml`\n\n"
                f"- start: `{step_data['start']}`\n"
                f"- stop: `{stop_text}`\n"
                f"- wait_for: `{wait_for_text}`\n"
                f"- wait_timeout_seconds: {timeout_text}\n\n"
                "PRoctor's executor will run `start` at the "
                "beginning of each test run, optionally poll "
                "`wait_for`, then execute the plan; `stop` (when "
                "set) runs after all items complete regardless of "
                "pass/fail."
            ),
            {
                **step_data,
                "outcome": "wrote-dev-launcher",
            },
            SUB_COMPLETE,
        )
    return _error(
        f"unknown dev-launcher sub-state {sub!r}"
    ), step_data, SUB_COMPLETE


def _emit_template_path(repo_root, plugin_root, step_data):
    """Copy the generic dev-launcher template into the consumer
    repo's ``.proctor/`` dir. Inert shell skeleton with TODO
    markers — the wizard explicitly does NOT auto-generate
    project-specific bash logic."""
    src = plugin_root / "templates" / "dev-launcher.sh.template"
    dst = repo_root / ".proctor" / "dev-launcher-template.sh"
    if not src.is_file():
        return (
            _show(
                f"## Template not found\n\n"
                f"Expected `{src}` (shipped with the plugin); the "
                f"file is missing. Open an issue at "
                f"https://github.com/zealllot/proctor with the "
                f"PRoctor version you have installed."
            ),
            {**step_data, "outcome": "template-missing"},
            SUB_COMPLETE,
        )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
        import os as _os
        _os.chmod(dst, 0o755)
    except OSError as e:
        return (
            _show(
                f"## Failed to write template: {e}\n\n"
                f"PRoctor left `.proctor/` untouched."
            ),
            {**step_data, "outcome": f"template-write-failed: {e}"},
            SUB_COMPLETE,
        )
    return (
        _show(
            "## Wrote `.proctor/dev-launcher-template.sh`\n\n"
            "A generic skeleton with TODO markers for the "
            "project-specific parts (DB up, main server start, "
            "workers, env file handling). The wizard intentionally "
            "did NOT generate any project-specific bash logic.\n\n"
            "**Next:** open a NEW Claude Code session in this "
            "repo and ask:\n\n"
            "> help me fill in `.proctor/dev-launcher-template.sh` "
            "based on this project's structure\n\n"
            "Claude will detect your binaries / Makefile / "
            "package.json / docker-compose.yml and complete the "
            "TODOs. When done, rename to `./dev.sh` (or whatever "
            "fits your team's convention), then re-run "
            "`/proctor:proctor-init` to record the launch "
            "command."
        ),
        {**step_data, "outcome": "wrote-template"},
        SUB_COMPLETE,
    )


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
    # Hint the user when dev_launcher was just written so they
    # know what comes next.
    if any(
        c["step"] == "step_dev_launcher"
        and c.get("outcome") == "wrote-dev-launcher"
        for c in completed
    ):
        lines.append("")
        lines.append(
            "Next: PRoctor will use the new dev_launcher block on "
            "its next test run. Verify with `claude /proctor:proctor "
            "<PR#>` or a dry run of the start command."
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
