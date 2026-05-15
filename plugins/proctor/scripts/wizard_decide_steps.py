"""Deterministic STEP decision for the /proctor:proctor-init wizard.

v0.7.8 had ``wizard_decide_mode.py`` return a single ``mode`` string
(``bump-only`` / ``needs-local-regen`` / ``amend-daemons`` / etc.).
Real upgrade scenarios are not mutually exclusive — a stale pin AND
a missing local.yml can both apply at once. v0.7.9 turned the
decision into an ORDERED LIST of applicable steps; the wizard's
state machine walks them in turn.

v0.7.11 simplification: dropped ``step_supplement_setup`` (the
"detect cmd/*/main.go binaries and write setup-block.yml" experiment
from v0.7.7–v0.7.10). Auditing the v0.7.10 result on mcd-website
made the right model clear — **the project owns its launch**. The
user demonstrated by hand-writing a 200-line ``./dev.sh`` with proper
subcommands (setup/db/main/cmd/all/stop/status/logs), env-file
handling, PID tracking, recursive child kill. PRoctor doesn't need
to duplicate any of that; it just needs the project's launch command
+ a readiness check, captured in the new ``dev_launcher:`` block in
``.proctor/config.yml``. ``step_dev_launcher`` writes that block.

## Step inventory + applies-conditions

| Step id                       | Applies when                                                                                  |
|-------------------------------|-----------------------------------------------------------------------------------------------|
| ``step_legacy_layout_migrate``| ``.pr-test.yml`` exists (pre-v0.4 layout) and ``.proctor/config.yml`` does NOT exist           |
| ``step_dev_launcher``         | ``.proctor/config.yml`` lacks a ``dev_launcher:`` block AND consumer is on the new layout     |
| ``step_regenerate_local_yml`` | ``.proctor/seed-local.sh`` exists AND (``.proctor/local.yml`` missing OR seed has legacy heredoc) |
| ``step_bump_action_pin``      | ``.github/workflows/proctor.yml`` action pin is older than ``--current-tag``                  |
| ``step_fresh_install``        | None of the above and no ``.proctor/`` directory exists at all                                |

Execution order is fixed (see ``STEP_ORDER`` below). Each step's
applies-condition is independent — no hidden cross-step coupling.

## Output shape

```jsonc
{
  "state": { /* same flat dict as v0.7.8/v0.7.9 */ },
  "steps": ["step_bump_action_pin", "step_dev_launcher"],
  "current_tag": "v0.7.11",

  // Backward-compat single-mode fields:
  "mode": "step_bump_action_pin",  // first step in `steps`, or "current" if empty
  "next_action": "...",
  "ask_user": { ... } | null
}
```

## CLI

```
python3 wizard_decide_steps.py --current-tag v0.7.11 --repo-root .
```
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# v0.4.0 layout paths (canonical) + v0.3.x legacy paths (deprecated).
_NEW_CONFIG = Path(".proctor") / "config.yml"
_NEW_LOCAL = Path(".proctor") / "local.yml"
_NEW_SEED = Path(".proctor") / "seed-local.sh"
# v0.7.9–v0.7.10 wrote a `.proctor/setup-block.yml`. v0.7.11 dropped
# that whole idea (the project owns its launch). We still read the
# path so detect_state can report its presence for any prose / test
# that still references the field, but no step writes to it.
_NEW_SETUP_BLOCK = Path(".proctor") / "setup-block.yml"
_NEW_LOCAL_EXAMPLE = Path(".proctor") / "local.yml.example"

_OLD_CONFIG = Path(".pr-test.yml")
_OLD_LOCAL = Path(".pr-test.local.yml")
_OLD_SEED_CANDIDATES = [
    Path("hack") / "proctor-seed-local.sh",
    Path("scripts") / "proctor-seed-local.sh",
    Path("proctor-seed-local.sh"),
]

_WORKFLOW = Path(".github") / "workflows" / "proctor.yml"

# Regex for the action-pin line in the workflow. Matches `vX.Y.Z`.
_PIN_RE = re.compile(
    r"zealllot/proctor/github-action@(v\d+\.\d+\.\d+(?:[-\w.]+)?)"
)

# Canonical step ids — exported for tests + state machine handlers.
STEP_LEGACY_LAYOUT_MIGRATE = "step_legacy_layout_migrate"
STEP_BUMP_ACTION_PIN = "step_bump_action_pin"
STEP_REGENERATE_LOCAL_YML = "step_regenerate_local_yml"
STEP_DEV_LAUNCHER = "step_dev_launcher"
STEP_FRESH_INSTALL = "step_fresh_install"

# v0.7.11 back-compat: STEP_SUPPLEMENT_SETUP is preserved as an
# ALIAS for STEP_DEV_LAUNCHER. The wizard's old "scan cmd/*/main.go"
# step is gone, but any external prose / test that imports the name
# still resolves to a real step id (now the dev-launcher question).
STEP_SUPPLEMENT_SETUP = STEP_DEV_LAUNCHER

# Execution order. dev_launcher runs early so the rest of the wizard
# can record the project's launch contract before touching seed
# scripts or pin bumps.
STEP_ORDER = [
    STEP_LEGACY_LAYOUT_MIGRATE,
    STEP_DEV_LAUNCHER,
    STEP_REGENERATE_LOCAL_YML,
    STEP_BUMP_ACTION_PIN,
    STEP_FRESH_INSTALL,
]


def detect_state(repo_root: Path) -> dict:
    """Read the consumer repo's actual file state. Returns a flat
    dict of booleans + the workflow pin string (or None).

    Same shape as v0.7.8–v0.7.10's ``detect_state`` (plus the
    ``has_dev_launcher`` key new in v0.7.11) so existing callers
    keep working."""
    has_new_config = (repo_root / _NEW_CONFIG).exists()
    has_old_config = (repo_root / _OLD_CONFIG).exists()
    has_workflow = (repo_root / _WORKFLOW).exists()

    has_new_seed = _is_executable(repo_root / _NEW_SEED)
    has_old_seed = any(
        _is_executable(repo_root / c) for c in _OLD_SEED_CANDIDATES
    )

    legacy_layout = (has_old_config and not has_new_config) or (
        has_old_seed and not has_new_seed
    )
    new_layout = has_new_config or has_new_seed

    has_auth_block = (
        _grep_qE(r"^auth:", repo_root / _NEW_CONFIG)
        or _grep_qE(r"^auth:", repo_root / _OLD_CONFIG)
    )

    return {
        "has_new_config": has_new_config,
        "has_old_config": has_old_config,
        "has_workflow": has_workflow,
        "has_auth_block": has_auth_block,
        "has_seed_script": has_new_seed or has_old_seed,
        "has_new_seed": has_new_seed,
        "has_old_seed": has_old_seed,
        "has_local_yml": (repo_root / _NEW_LOCAL).exists()
            or (repo_root / _OLD_LOCAL).exists(),
        "has_setup_block_yml": (repo_root / _NEW_SETUP_BLOCK).exists(),
        # v0.7.11: True when .proctor/config.yml already contains a
        # `dev_launcher:` top-level key. step_dev_launcher skips when
        # this is True (idempotent re-runs don't re-ask).
        "has_dev_launcher": _grep_qE(
            r"^dev_launcher:", repo_root / _NEW_CONFIG,
        ),
        # v0.7.10 — seed-local.sh legacy-heredoc detection. Kept so
        # step_regenerate_local_yml can still auto-migrate existing
        # consumers' seed scripts.
        "seed_has_legacy_heredoc": _seed_has_legacy_heredoc(
            repo_root / _NEW_SEED,
        ),
        "legacy_layout": legacy_layout,
        "new_layout": new_layout,
        "current_pin": _extract_pin(repo_root / _WORKFLOW),
    }


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _grep_qE(pattern: str, path: Path) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return bool(re.search(pattern, text, re.MULTILINE))


def _extract_pin(workflow_path: Path) -> str | None:
    if not workflow_path.exists():
        return None
    m = _PIN_RE.search(workflow_path.read_text(errors="replace"))
    return m.group(1) if m else None


# v0.7.10 — detect the legacy hardcoded SETUP_BLOCK heredoc pattern
# inside an existing seed-local.sh. Kept in v0.7.11 because the
# regenerate step still uses it to auto-migrate older consumer seed
# scripts (no schema change there).
_LEGACY_HEREDOC_RE = re.compile(
    r"SETUP_BLOCK=\$\(cat\s+<<\s*'YAML'\s*$", re.MULTILINE,
)
_AWK_READER_RE = re.compile(
    r"awk\s+'[^']*setup:[^']*'\s+\.proctor/setup-block\.yml",
)


def _seed_has_legacy_heredoc(seed_path: Path) -> bool:
    """True when ``seed_path`` is a file that contains the pre-v0.7.9
    hardcoded SETUP_BLOCK heredoc AND does NOT contain the v0.7.9
    awk reader pattern."""
    if not seed_path.is_file():
        return False
    try:
        text = seed_path.read_text(errors="replace")
    except OSError:
        return False
    if _AWK_READER_RE.search(text):
        return False
    return bool(_LEGACY_HEREDOC_RE.search(text))


# --- step decision walker -------------------------------------------

def decide_steps(
    state: dict,
    current_tag: str | None,
    repo_root: Path | None = None,
) -> list[str]:
    """Walk the canonical step order and return every step whose
    applies-condition is True against ``state``.

    Notes:
    - ``step_fresh_install`` is mutually exclusive with everything
      else (it fires only when there's no ``.proctor/`` directory).
    - ``step_dev_launcher`` fires whenever ``.proctor/config.yml``
      exists but lacks the ``dev_launcher:`` block. This means
      EXISTING consumers will see the question on their next wizard
      run after upgrading to v0.7.11 — exactly the intent (give the
      user a chance to register their launch command).
    """
    s = state
    if repo_root is None:
        repo_root = Path(".")

    # Fresh install — short-circuit. Nothing else applies.
    if (
        not s["has_new_config"]
        and not s["has_old_config"]
        and not s["has_workflow"]
    ):
        return [STEP_FRESH_INSTALL]

    steps: list[str] = []

    # Legacy layout — pre-v0.4 .pr-test.yml present and no new
    # config yet. Must run before everything else so subsequent
    # steps see the canonical .proctor/ paths.
    if s["legacy_layout"] and not s["has_new_config"]:
        steps.append(STEP_LEGACY_LAYOUT_MIGRATE)

    # Dev-launcher — fires when the consumer is on the new layout
    # AND hasn't yet declared a dev_launcher block. Idempotent: once
    # the block is in config.yml, the step skips forever (unless
    # the user manually deletes it to re-trigger).
    if s["has_new_config"] and not s.get("has_dev_launcher"):
        steps.append(STEP_DEV_LAUNCHER)

    # Regenerate .proctor/local.yml — seed script present AND
    # EITHER local.yml is missing OR seed-local.sh has the
    # pre-v0.7.9 hardcoded SETUP_BLOCK heredoc.
    if s["has_seed_script"] and (
        not s["has_local_yml"] or s.get("seed_has_legacy_heredoc")
    ):
        steps.append(STEP_REGENERATE_LOCAL_YML)

    # Bump action pin — runs whenever the workflow is older than
    # --current-tag. Independent of every other step.
    if (
        current_tag
        and s["current_pin"]
        and s["current_pin"] != current_tag
    ):
        steps.append(STEP_BUMP_ACTION_PIN)

    # Sort by canonical execution order before returning.
    order_index = {sid: i for i, sid in enumerate(STEP_ORDER)}
    return sorted(steps, key=lambda sid: order_index.get(sid, 999))


# --- decision envelope (for compatibility + UX) ---------------------

# Per-step human-readable summaries and ask_user hints. Used by the
# orchestrator's `next_action` field and by the wizard's first
# ask_user envelope when a step has an explicit user prompt.
_STEP_INFO: dict[str, dict] = {
    STEP_LEGACY_LAYOUT_MIGRATE: {
        "next_action": (
            "Walk the layout-migration block to git-mv the v0.3.x "
            "files into .proctor/ and update .gitignore."
        ),
        "ask_user": {
            "header": "Layout migration",
            "question": (
                "Detected v0.3.x config layout "
                "(`.pr-test.yml`, `hack/proctor-seed-local.sh`). "
                "v0.4.0 consolidated everything under `.proctor/`. "
                "Migrate?"
            ),
            "options": [
                {
                    "label": "Migrate to v0.4.0 layout (Recommended)",
                    "description": (
                        "git mv the files into .proctor/, update "
                        ".gitignore."
                    ),
                },
                {
                    "label": "Keep current layout",
                    "description": (
                        "Compatibility shim keeps reading old paths "
                        "with a deprecation warning each run."
                    ),
                },
            ],
        },
    },
    STEP_BUMP_ACTION_PIN: {
        "next_action": "Bump action pin to --current-tag. No other changes.",
        "ask_user": None,
    },
    STEP_REGENERATE_LOCAL_YML: {
        "next_action": (
            "Run .proctor/seed-local.sh to regenerate "
            ".proctor/local.yml (auto-migrating any pre-v0.7.9 "
            "hardcoded SETUP_BLOCK heredoc inside the seed script)."
        ),
        "ask_user": None,
    },
    STEP_DEV_LAUNCHER: {
        "next_action": (
            "Ask the user how their project starts its full local "
            "dev env for PRoctor's test runs (one-click script / "
            "template / skip)."
        ),
        "ask_user": {
            "header": "Dev launcher",
            "question": (
                "How does this project start its full local dev "
                "environment for PRoctor's test runs?\n\n"
                "PRoctor needs to bring up your DB, main server, "
                "and any supplementary processes (workers, queues, "
                "schedulers, etc.) before running tests. There are "
                "three paths."
            ),
            "options": [
                {
                    "label": "I have a one-click script (Recommended)",
                    "description": (
                        "Provide start + stop commands. Examples: "
                        "`./dev.sh all` / `./dev.sh stop`, "
                        "`make dev` / `make stop`, "
                        "`pnpm dev` / `pkill -f 'pnpm dev'`, "
                        "`docker-compose up -d` / `docker-compose down`."
                    ),
                },
                {
                    "label": "Show me a generic template I can adapt",
                    "description": (
                        "PRoctor writes `dev-launcher.sh.template` "
                        "to `.proctor/dev-launcher-template.sh` "
                        "with TODO markers; another Claude Code "
                        "session can fill in the project-specific "
                        "bits."
                    ),
                },
                {
                    "label": "Skip — keep using the legacy `setup:` array",
                    "description": (
                        "Fine for projects with simple needs "
                        "(`docker-compose up` + `go run .`). "
                        "dev_launcher is the recommended path for "
                        "anything more complex."
                    ),
                },
            ],
        },
    },
    STEP_FRESH_INSTALL: {
        "next_action": (
            "Run the full wizard from Section 1 onward to set up "
            "PRoctor from scratch."
        ),
        "ask_user": None,
    },
}


def _build_envelope(
    state: dict, steps: list[str], current_tag: str | None,
) -> dict:
    """Build the JSON envelope ``decide_steps`` emits — includes both
    the v0.7.9 step list and the v0.7.8 backward-compat single-mode
    fields for any caller that hasn't migrated yet.
    """
    if not steps:
        return {
            "state": state,
            "steps": [],
            "current_tag": current_tag,
            "mode": "current",
            "next_action": (
                "PRoctor is already integrated and up to date. "
                "No action needed."
            ),
            "ask_user": None,
        }

    first = steps[0]
    info = _STEP_INFO.get(first, {})
    return {
        "state": state,
        "steps": steps,
        "current_tag": current_tag,
        "mode": _mode_alias(first),
        "next_action": info.get("next_action", ""),
        "ask_user": info.get("ask_user"),
    }


# v0.7.8 → v0.7.9 mode-name aliases for backward compat. v0.7.11
# preserves the ``amend-daemons`` alias on the new dev_launcher
# step so old prose/tests still see a recognizable name.
_MODE_ALIASES = {
    STEP_LEGACY_LAYOUT_MIGRATE: "legacy-migration",
    STEP_BUMP_ACTION_PIN: "bump-only",
    STEP_REGENERATE_LOCAL_YML: "needs-local-regen",
    STEP_DEV_LAUNCHER: "amend-daemons",
    STEP_FRESH_INSTALL: "fresh",
}


def _mode_alias(step_id: str) -> str:
    return _MODE_ALIASES.get(step_id, step_id)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--current-tag",
        default=None,
        help="The latest PRoctor release tag (e.g. v0.7.11).",
    )
    p.add_argument(
        "--repo-root",
        default=".",
        help="Consumer repo root (default: cwd).",
    )
    args = p.parse_args()

    root = Path(args.repo_root).resolve()
    state = detect_state(root)
    steps = decide_steps(state, args.current_tag, repo_root=root)
    envelope = _build_envelope(state, steps, args.current_tag)

    sys.stdout.write(json.dumps(envelope, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
