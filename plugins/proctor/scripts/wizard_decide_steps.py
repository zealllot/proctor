"""Deterministic STEP decision for the /proctor:proctor-init wizard.

v0.7.8 had ``wizard_decide_mode.py`` return a single ``mode`` string
(``bump-only`` / ``needs-local-regen`` / ``amend-daemons`` / etc.).
Real runs against mcd-website hit a structural ceiling: modes are
mutually exclusive, but real upgrade scenarios are not. Two
back-to-back ``/proctor:proctor-init`` runs:

- Run 1: action pin out of date → ``bump-only`` won (first-match) →
  wizard exited. The ``amend-daemons`` mode (v0.7.8's new
  supplementary-binaries flow) never fired because ``bump-only``
  matched first.
- Run 2: pin now current, but ``.proctor/local.yml`` was missing →
  ``needs-local-regen`` won → wizard exited. Still didn't fire
  ``amend-daemons``.

The fix (v0.7.9): instead of "pick one mode and exit", return an
ordered LIST of steps that all apply to the consumer's current state.
The wizard runs each step in turn, advancing through one big state
machine that may emit ``ask_user`` / ``bash`` / ``show`` envelopes
across many invocations before reaching ``done``.

## Step inventory + applies-conditions

Each step is independent and may or may not apply to the consumer's
current state. The ``decide_steps()`` walker returns every step
whose applies-condition is True, in the canonical execution order:

| Step id                       | Applies when                                                                                  |
|-------------------------------|-----------------------------------------------------------------------------------------------|
| ``step_legacy_layout_migrate``| ``.pr-test.yml`` exists (pre-v0.4 layout) and ``.proctor/config.yml`` does NOT exist            |
| ``step_regenerate_local_yml`` | ``.proctor/seed-local.sh`` exists AND ``.proctor/local.yml`` is missing                       |
| ``step_bump_action_pin``      | ``.github/workflows/proctor.yml`` action pin is older than ``--current-tag``                  |
| ``step_supplement_setup``     | Consumer has ``cmd/*/main.go`` binaries NOT referenced in the current setup-block             |
| ``step_fresh_install``        | None of the above and no ``.proctor/`` directory exists at all                                |

Execution order is fixed (the table order above) because step
prerequisites flow downward: layout migration must run before pin
bump (the pin lives in a workflow that might not exist yet);
local.yml regeneration must run before supplement-setup (the latter
writes a setup-block that the seed script consumes); pin bump
slots between regen and supplement because it's a self-contained
file edit that doesn't depend on either neighbour.

The order also preserves v0.7.8 mode-priority semantics for the
backward-compat single-``mode`` field: ``needs-local-regen`` won
over ``bump-only`` in v0.7.8's dispatcher, so we keep
``step_regenerate_local_yml`` ahead of ``step_bump_action_pin``
here. Tests that pin the v0.7.8 priority order continue to pass.

## Output shape

```jsonc
{
  "state": { /* same flat dict as wizard_decide_mode v0.7.8 */ },
  "steps": ["step_bump_action_pin", "step_supplement_setup"],
  "current_tag": "v0.7.8",

  // Backward-compat fields (v0.7.9 deprecated, kept for any prose
  // that still expects a single-mode answer):
  "mode": "step_bump_action_pin",  // first step in `steps`, or "current" if empty
  "next_action": "...",            // human-readable description
  "ask_user": { ... } | null       // pulled from first step's first-question
}
```

When no step applies (``steps: []``), the wizard is fully
configured. ``mode`` is then ``"current"`` and the wizard exits
immediately.

## CLI

```
python3 wizard_decide_steps.py --current-tag v0.7.9 --repo-root .
```

Stdout: one JSON object as above. Exit code is always 0; no-step is
a valid decision.

## Backward compat

``wizard_decide_mode.py`` (the v0.7.8 entry point) still exists as a
thin shim that re-exports ``detect_state`` and ``decide_mode``
(returns just the first step + the legacy fields). Tests and any
prose that calls the older script keep working without edits.
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
STEP_SUPPLEMENT_SETUP = "step_supplement_setup"
STEP_FRESH_INSTALL = "step_fresh_install"

# Execution order. The list serves as both the iteration order and
# the canonical list of legal step ids.
#
# v0.7.10 reorder: ``step_supplement_setup`` now precedes
# ``step_regenerate_local_yml``. The data flow is supplement (writes
# ``.proctor/setup-block.yml``) → regenerate (rewrites seed-local.sh
# to read setup-block.yml, then re-runs it to produce local.yml). If
# regenerate ran first, the supplement step would later write into
# setup-block.yml but the local.yml the user just regenerated would
# already be stale (no supplementary binaries in its setup block).
#
# ``step_bump_action_pin`` is independent — it's a self-contained edit
# of the workflow file. We slot it AFTER the two setup-mutation steps
# so the wizard's first user-visible action is the substantive setup
# work rather than a one-line pin bump.
#
# v0.7.9 ordering was: legacy → regenerate → bump → supplement → fresh.
# The new order fixes Bug A from the v0.7.9 audit: supplement was
# dropped when local.yml was missing (the v0.7.9 ``decide_steps`` gated
# supplement on ``has_local_yml``), but the only true precondition is
# "there's a cmd/* binary not yet covered by setup-block.yml". Bug C
# (seed-local.sh ships with a hardcoded SETUP_BLOCK heredoc) is also
# folded into ``step_regenerate_local_yml`` via the broader "needs
# rewrite" check (see ``_seed_needs_rewrite``).
STEP_ORDER = [
    STEP_LEGACY_LAYOUT_MIGRATE,
    STEP_SUPPLEMENT_SETUP,
    STEP_REGENERATE_LOCAL_YML,
    STEP_BUMP_ACTION_PIN,
    STEP_FRESH_INSTALL,
]


def detect_state(repo_root: Path) -> dict:
    """Read the consumer repo's actual file state. Returns a flat
    dict of booleans + the workflow pin string (or None).

    Same shape as v0.7.8's ``wizard_decide_mode.detect_state`` so any
    test/caller that imports it keeps working."""
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
        # v0.7.10: when seed-local.sh exists and still ships with the
        # pre-v0.7.9 hardcoded SETUP_BLOCK heredoc, the wizard must
        # rewrite it to read setup-block.yml — otherwise wizard
        # amendments to setup-block.yml are silently ignored on every
        # seed-script re-run. ``seed_has_legacy_heredoc`` is True when
        # the heredoc pattern is present AND the awk reader pattern is
        # absent. Other shapes (already migrated, or no seed script
        # at all) leave it False.
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
# inside an existing seed-local.sh. Pre-v0.7.9 seed scripts shipped with:
#
#     SETUP_BLOCK=$(cat <<'YAML'
#       - <hardcoded command 1>
#       - <hardcoded command 2>
#       ...
#     YAML
#     )
#
# v0.7.9+ replaced this with an awk reader that pulls the block from
# ``.proctor/setup-block.yml`` (falling back to a generic heredoc when
# the file is missing). Existing consumers' seed scripts weren't
# auto-migrated in v0.7.9, so wizard amendments to setup-block.yml
# were ignored. v0.7.10's ``step_regenerate_local_yml`` does the
# in-place rewrite.
_LEGACY_HEREDOC_RE = re.compile(
    r"SETUP_BLOCK=\$\(cat\s+<<\s*'YAML'\s*$", re.MULTILINE,
)
_AWK_READER_RE = re.compile(
    r"awk\s+'[^']*setup:[^']*'\s+\.proctor/setup-block\.yml",
)


def _seed_has_legacy_heredoc(seed_path: Path) -> bool:
    """True when ``seed_path`` is a file that contains the pre-v0.7.9
    hardcoded SETUP_BLOCK heredoc AND does NOT contain the v0.7.9 awk
    reader pattern. Either of:

    - The seed script doesn't exist → False (no migration needed).
    - The script already has the awk reader → False (already
      migrated).
    - The script has only the heredoc → True (needs rewrite).
    """
    if not seed_path.is_file():
        return False
    try:
        text = seed_path.read_text(errors="replace")
    except OSError:
        return False
    if _AWK_READER_RE.search(text):
        return False
    return bool(_LEGACY_HEREDOC_RE.search(text))


# --- applies-condition helpers --------------------------------------

def _setup_lacks_cmd_binary_lines(local_yml_path: Path) -> bool:
    """Read ``.proctor/local.yml`` and return True when its
    ``setup:`` has content (a real setup, not an empty stub) but NO
    line mentions ``go run ./cmd/`` — the marker line that v0.7.7+
    multi-binary detection emits per selected supplementary binary.

    Intentionally a substring check, not a full YAML parse. The
    setup array's lines are quoted strings; ``go run ./cmd/`` (with
    the trailing slash) is distinctive enough that no other Go
    invocation collides with it."""
    try:
        text = local_yml_path.read_text(errors="replace")
    except OSError:
        return False
    if "setup:" not in text:
        return False
    if "go run ./cmd/" in text:
        return False
    return _has_setup_content(text)


def _has_setup_content(local_yml_text: str) -> bool:
    """True when `setup:` appears to have at least one non-empty
    list item. Heuristic: look for ``setup:\\n`` followed (within
    ~50 lines) by a ``  - `` list-item marker."""
    idx = local_yml_text.find("setup:")
    if idx < 0:
        return False
    tail = local_yml_text[idx:]
    first_line_end = tail.find("\n")
    first_line = tail[:first_line_end] if first_line_end >= 0 else tail
    if first_line.strip() in ("setup: []", "setup: ~", "setup: null"):
        return False
    lines = tail.split("\n")[1:50]
    return any(line.lstrip().startswith("- ") for line in lines)


def _setup_block_lists_all_cmd_binaries(
    repo_root: Path,
) -> bool:
    """Return True when every ``cmd/*/main.go`` the consumer ships
    is already referenced in either ``.proctor/setup-block.yml`` or
    ``.proctor/local.yml setup:``. When False, ``step_supplement_setup``
    fires.

    Uses ``wizard_detect_binaries`` as the source of truth for "what
    binaries exist". Matches by binary_name (``proctor-<name>.pid``)
    OR path (``./cmd/<name>/main.go``) so the wizard's amend output
    from v0.7.8 (which writes both) is recognized as covering the
    binary."""
    # Lazy import to avoid cycle when this module is imported by
    # wizard_decide_mode (the shim).
    try:
        from wizard_detect_binaries import detect_binaries
    except ImportError:
        # Sibling import path setup — same as wizard_run.py.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from wizard_detect_binaries import detect_binaries  # type: ignore

    # Only count binaries that are RUNS-LOOP or UNKNOWN — those are
    # the ones the wizard cares about adding to setup. ``serves-http``
    # is the project's main server (covered by the existing wait-loop
    # pattern); ``runs-once`` is a CLI tool the user runs by hand.
    candidates = [
        c for c in detect_binaries(repo_root)
        if c.get("looks_like") in ("runs-loop", "unknown", "daemon")
        # legacy v0.7.8 label
    ]
    if not candidates:
        return True  # nothing to supplement; step doesn't apply

    # Combined setup source text: setup-block.yml plus local.yml's
    # setup: block (best-effort substring scan).
    sources_text = ""
    sb = repo_root / _NEW_SETUP_BLOCK
    if sb.exists():
        try:
            sources_text += sb.read_text(errors="replace")
        except OSError:
            pass
    for candidate_local in (
        repo_root / _NEW_LOCAL,
        repo_root / _OLD_LOCAL,
    ):
        if candidate_local.exists():
            try:
                sources_text += "\n" + candidate_local.read_text(errors="replace")
            except OSError:
                pass

    if not sources_text:
        # No setup source exists at all — step_supplement_setup should
        # bootstrap setup-block.yml from scratch with the detected
        # binaries. v0.7.10: previously this returned True (skipped
        # the step), deferring to regenerate / fresh; but Bug A's fix
        # decouples these — supplement writes setup-block.yml
        # regardless, then regenerate / fresh produce local.yml from
        # it.
        return False

    for c in candidates:
        path = c.get("path", "")
        name = c.get("binary_name", "")
        if path and f"./{path}" in sources_text:
            continue
        if name and f"proctor-{name}.pid" in sources_text:
            continue
        return False  # at least one candidate is not yet covered
    return True


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
      else (it fires only when there's no ``.proctor/`` directory
      at all). When it fires, the list is just ``[step_fresh_install]``.
    - ``step_supplement_setup`` requires reading the repo's
      ``cmd/*/main.go`` files (delegated to wizard_detect_binaries),
      so it needs ``repo_root``. When ``repo_root`` is None the step
      is conservatively skipped.
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

    # Walk each applies-condition independently. Each step's check
    # depends ONLY on that step's own precondition — no hidden coupling
    # between checks. Bug A from the v0.7.9 audit was caused by
    # gating supplement on ``has_local_yml``; v0.7.10 removes that
    # cross-dependency. The final ``steps`` list is sorted per
    # ``STEP_ORDER`` before return, so check order here doesn't affect
    # execution order.

    # Legacy layout — pre-v0.4 .pr-test.yml present and no new config
    # yet. Must run before everything else so subsequent steps see
    # the canonical .proctor/ paths.
    if s["legacy_layout"] and not s["has_new_config"]:
        steps.append(STEP_LEGACY_LAYOUT_MIGRATE)

    # Supplement setup — fires whenever the repo has at least one
    # cmd/*/main.go binary that isn't yet referenced in
    # .proctor/setup-block.yml or .proctor/local.yml's setup block.
    # v0.7.10: NO LONGER gated on has_local_yml. The supplement step
    # writes to setup-block.yml regardless; the subsequent
    # step_regenerate_local_yml reads that file when producing the
    # new local.yml. This is Bug A's fix.
    if (
        s["has_new_config"]
        and not _setup_block_lists_all_cmd_binaries(repo_root)
    ):
        steps.append(STEP_SUPPLEMENT_SETUP)

    # Regenerate .proctor/local.yml — seed script present AND EITHER
    # local.yml is missing OR seed-local.sh still has the pre-v0.7.9
    # hardcoded SETUP_BLOCK heredoc (needs in-place rewrite so wizard
    # amendments to setup-block.yml are honored). Bug C's fix folds
    # the seed-script-migration trigger into this step.
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
            "Ask the user how to regenerate .proctor/local.yml. "
            "After their answer the wizard runs the chosen action."
        ),
        "ask_user": {
            "header": "Local config",
            "question": (
                "Detected `.proctor/seed-local.sh` exists but "
                "`.proctor/local.yml` is missing. The local "
                "config is what PRoctor reads at runtime "
                "(setup commands + credentials). How would "
                "you like to proceed?"
            ),
            "options": [
                {
                    "label": "Regenerate seed-local.sh AND re-run it (Recommended)",
                    "description": (
                        "Walks Step 7f to confirm env-source + "
                        "setup commands, regenerates the seed "
                        "script, then prompts you to run it."
                    ),
                },
                {
                    "label": "Just run the existing seed-local.sh",
                    "description": (
                        "Faster. Uses whatever setup commands "
                        "are baked into the seed script."
                    ),
                },
                {
                    "label": "Skip — I'll handle .proctor/local.yml myself",
                    "description": (
                        "Wizard does nothing extra; continues "
                        "to the next step."
                    ),
                },
            ],
        },
    },
    STEP_SUPPLEMENT_SETUP: {
        "next_action": (
            "Scan cmd/*/main.go binaries and offer to add the "
            "long-running ones to .proctor/setup-block.yml."
        ),
        "ask_user": {
            "header": "Supplementary binaries",
            "question": (
                "Detected `cmd/*/main.go` binaries in this repo "
                "that aren't currently started in your local "
                "setup. PRoctor can start them alongside your "
                "main server during test runs so the planner can "
                "verify their output at runtime. Scan now?"
            ),
            "options": [
                {
                    "label": "Scan for supplementary binaries you may want to start in setup",
                    "description": (
                        "Runs the multi-main classifier against "
                        "cmd/*/main.go + root main.go, then asks "
                        "which ones to add to setup-block.yml."
                    ),
                },
                {
                    "label": "Skip — my setup is fine",
                    "description": (
                        "Wizard moves on to the next step (or "
                        "exits if nothing else is pending)."
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

    ``mode`` is set to the FIRST step in ``steps`` (deprecated alias);
    if the list is empty, ``mode`` is ``"current"`` (mirroring v0.7.8's
    "fully configured, no action" decision).
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
        # Deprecated v0.7.8-compat fields. Older prose / tests that
        # read `mode` see the first step name; for steps that map
        # cleanly to v0.7.8 modes the names are aliased below.
        "mode": _mode_alias(first),
        "next_action": info.get("next_action", ""),
        "ask_user": info.get("ask_user"),
    }


# v0.7.8 → v0.7.9 mode-name aliases for backward compat. Older prose
# and tests that look for `mode == "amend-daemons"` keep working when
# the equivalent step fires.
_MODE_ALIASES = {
    STEP_LEGACY_LAYOUT_MIGRATE: "legacy-migration",
    STEP_BUMP_ACTION_PIN: "bump-only",
    STEP_REGENERATE_LOCAL_YML: "needs-local-regen",
    STEP_SUPPLEMENT_SETUP: "amend-daemons",
    STEP_FRESH_INSTALL: "fresh",
}


def _mode_alias(step_id: str) -> str:
    return _MODE_ALIASES.get(step_id, step_id)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--current-tag",
        default=None,
        help="The latest PRoctor release tag (e.g. v0.7.9).",
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


# Allow `from wizard_detect_binaries import ...` at the local-import
# inside `_setup_block_lists_all_cmd_binaries` to resolve to the
# sibling script.
sys.path.insert(0, str(Path(__file__).resolve().parent))


if __name__ == "__main__":
    raise SystemExit(main())
