"""Deterministic MODE decision for the /proctor:proctor-init wizard.

Replaces the v0.3-and-earlier "AI walks a bulleted list of conditions
and picks the first match" decision flow. Real runs showed the AI
silently skipping bullets whose conditions were stated in terms of
detection-block-computed variables (`NEEDS_LOCAL_REGEN=yes`) and
falling through to later bullets whose conditions were observable
file-facts (bump-only-by-pin-age). The wizard then took the wrong
branch, ran bump-only, and left the user with a missing
`.proctor/local.yml`.

This script (v0.4.5+) reads the actual file state and prints a
single unambiguous JSON object describing what the wizard should do
next. The AI's only job is to run this script, surface the
indicated AskUserQuestion (if any), execute the chosen action, then
re-run the script if the wizard continues.

Stdin: nothing.
Stdout: a single JSON object — see the dataclass below for the
shape. Exit code is always 0; decisions never fail (no-action is
itself a valid decision).

Usage:

    python3 wizard_decide_mode.py \\
        --current-tag v0.4.4 \\
        --repo-root .

The `--current-tag` value comes from the wizard's earlier
`gh release view zealllot/proctor` call. Pass null / omit if the
fetch failed — we fall back to "treat as already current" rather
than firing a spurious bump-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# v0.4.0 layout paths (canonical) + v0.3.x legacy paths (deprecated).
# We check both because the wizard's first job is to detect which
# layout the consumer is on.
_NEW_CONFIG = Path(".proctor") / "config.yml"
_NEW_LOCAL = Path(".proctor") / "local.yml"
_NEW_SEED = Path(".proctor") / "seed-local.sh"
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


def detect_state(repo_root: Path) -> dict:
    """Read the consumer repo's actual file state. Returns a flat
    dict of booleans + the workflow pin string (or None)."""
    has_new_config = (repo_root / _NEW_CONFIG).exists()
    has_old_config = (repo_root / _OLD_CONFIG).exists()
    has_workflow = (repo_root / _WORKFLOW).exists()

    has_new_seed = _is_executable(repo_root / _NEW_SEED)
    has_old_seed = any(
        _is_executable(repo_root / c) for c in _OLD_SEED_CANDIDATES
    )

    # Layout flags. `legacy_layout` fires when ANY v0.3.x marker is
    # present without the corresponding v0.4.0 path — the migration
    # branch needs to fire.
    legacy_layout = (has_old_config and not has_new_config) or (
        has_old_seed and not has_new_seed
    )
    new_layout = has_new_config or has_new_seed

    # Auth block — true if EITHER layout's config file has `auth:`.
    # We check both so a partially-migrated repo doesn't fail-open.
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
        "legacy_layout": legacy_layout,
        "new_layout": new_layout,
        "current_pin": _extract_pin(repo_root / _WORKFLOW),
    }


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _grep_qE(pattern: str, path: Path) -> bool:
    """Multiline-anchored regex search on the file's content. Returns
    False when the file doesn't exist (mirrors `grep -qE … 2>/dev/null`
    behaviour the wizard's shell snippets use)."""
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


def decide_mode(state: dict, current_tag: str | None) -> dict:
    """Walk the priority-ordered decision rules. Return the FIRST
    matching one. Each rule is fully-described — no fall-through to
    later rules once one fires. The order encodes which scenarios
    "win" when multiple are technically true (e.g. legacy_layout +
    needs-local-regen → migrate first; local-regen happens on the
    re-run after migration)."""
    s = state

    # Rule 1: brand-new repo (neither config nor workflow). Most
    # common first-time install.
    if not s["has_new_config"] and not s["has_old_config"] and not s["has_workflow"]:
        return _decision(
            mode="fresh",
            next_action=(
                "Run the full wizard from Section 1 onward to set up "
                "PRoctor from scratch."
            ),
            ask_user=None,
        )

    # Rule 2: v0.3.x layout still in place — migrate to v0.4.0 first.
    if s["legacy_layout"] and not s["has_new_config"]:
        return _decision(
            mode="legacy-migration",
            next_action=(
                "Walk the layout-migration block to git-mv the v0.3.x "
                "files into .proctor/ and update .gitignore."
            ),
            ask_user={
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
                            ".gitignore. Plugin reads either layout at "
                            "runtime, but new layout is cleaner."
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
        )

    # Rule 3: NEEDS_LOCAL_REGEN — seed script exists, but
    # .proctor/local.yml is missing. The developer either never ran
    # the seed script, or deleted it because it was broken. This
    # rule's priority is HIGH so the wizard doesn't silently
    # fall-through to bump-only and leave the user with a missing
    # local.yml.
    if s["has_seed_script"] and not s["has_local_yml"]:
        return _decision(
            mode="needs-local-regen",
            next_action=(
                "Ask the user how to regenerate .proctor/local.yml. "
                "After their answer the wizard runs the chosen action."
            ),
            ask_user={
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
                            "script, then prompts you to run it. "
                            "Catches the case where existing seed "
                            "script was built with wrong env-source."
                        ),
                    },
                    {
                        "label": "Just run the existing seed-local.sh",
                        "description": (
                            "Faster. Uses whatever setup commands "
                            "are baked into the seed script — won't "
                            "pick up v0.4.x setup-confirmation "
                            "improvements."
                        ),
                    },
                    {
                        "label": "Skip — I'll handle .proctor/local.yml myself",
                        "description": (
                            "Wizard does nothing extra; falls through "
                            "to the pin-bump check."
                        ),
                    },
                ],
            },
        )

    # Rule 4: seed script missing but auth block present. Generate
    # the seed script via Step 8c-pre. No user input needed.
    if not s["has_seed_script"] and s["has_auth_block"]:
        return _decision(
            mode="bump-only-with-seed",
            next_action=(
                "Bump pin if needed AND run Step 8c-pre to generate "
                "the missing seed script."
            ),
            ask_user=None,
        )

    # Rule 5: pre-v0.3 config — no auth block. Offer v0.2 → v0.3
    # migration via AskUserQuestion (matches the existing 'migrate'
    # MODE prose).
    if not s["has_auth_block"] and (s["has_new_config"] or s["has_old_config"]):
        return _decision(
            mode="migrate",
            next_action=(
                "Existing v0.2.x consumer. Offer migration to v0.3 "
                "existing-env mode (add auth block, drop setup:)."
            ),
            ask_user={
                "header": "PRoctor migration",
                "question": (
                    "Detected existing PRoctor integration without an "
                    "`auth:` block (v0.2.x). v0.3.0 added auth + "
                    "multi-account testing. How would you like to "
                    "proceed?"
                ),
                "options": [
                    {
                        "label": "Migrate to v0.3 existing-env mode (Recommended)",
                        "description": (
                            "Add auth: block, drop setup:, bump pin. "
                            "Run sections 7 to capture login + accounts."
                        ),
                    },
                    {
                        "label": "Keep v0.2 setup-based config, just bump the pin",
                        "description": "Minimal change.",
                    },
                    {
                        "label": "Start fresh",
                        "description": (
                            "Discard existing config and re-run the "
                            "wizard from scratch."
                        ),
                    },
                ],
            },
        )

    # Rule 6: pin out of date — bump-only.
    if (
        current_tag
        and s["current_pin"]
        and s["current_pin"] != current_tag
    ):
        return _decision(
            mode="bump-only",
            next_action=(
                f"Bump action pin {s['current_pin']} → {current_tag}. "
                "No other changes."
            ),
            ask_user=None,
        )

    # Rule 7: fully configured + up to date. Nothing to do.
    return _decision(
        mode="current",
        next_action=(
            "PRoctor is already integrated and up to date. No action "
            "needed; the wizard exits with a summary."
        ),
        ask_user=None,
    )


def _decision(mode: str, next_action: str, ask_user: dict | None) -> dict:
    return {
        "mode": mode,
        "next_action": next_action,
        "ask_user": ask_user,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--current-tag",
        default=None,
        help="The latest PRoctor release tag (e.g. v0.4.4); used to "
             "decide whether bump-only fires. Pass null/omit when the "
             "wizard couldn't fetch it.",
    )
    p.add_argument(
        "--repo-root",
        default=".",
        help="Consumer repo root (default: cwd).",
    )
    args = p.parse_args()

    root = Path(args.repo_root).resolve()
    state = detect_state(root)
    decision = decide_mode(state, args.current_tag)

    sys.stdout.write(json.dumps({"state": state, **decision}, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
