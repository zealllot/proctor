"""DEPRECATED in v0.7.9 — backward-compatibility shim for the v0.7.8
single-mode wizard decision API.

The wizard's decision logic moved to ``wizard_decide_steps.py`` in
v0.7.9, which returns an ORDERED LIST of steps rather than a single
mode. The shape change was forced by real upgrade scenarios that
trigger multiple legitimate steps at once: a stale action pin AND a
new ``cmd/<X>-daemon`` to supplement the setup block, for instance —
the v0.7.8 single-mode dispatcher always picked one and silently
dropped the other.

This module re-exports the v0.7.8 names (``detect_state``,
``decide_mode``) backed by the new step walker, so existing tests
and any prose that imports from here keeps working. New code should
import from ``wizard_decide_steps`` directly.

Equivalence rules:
- ``decide_mode(state, current_tag, repo_root=...)`` returns the
  v0.7.8 ``{mode, next_action, ask_user}`` envelope corresponding to
  the FIRST step in the step walker's output.
- When the step walker returns an empty list, ``mode`` is
  ``"current"`` (same as v0.7.8).
- ``mode`` values use the v0.7.8 names via the alias table in
  ``wizard_decide_steps._MODE_ALIASES``.

The CLI entry point (``python3 wizard_decide_mode.py``) is preserved
and forwards to the step walker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the sibling `wizard_decide_steps` script is importable both
# when this module is run as a CLI (`python3 wizard_decide_mode.py`)
# and when it's imported under its package name from the test suite
# (`from plugins.proctor.scripts.wizard_decide_mode import ...`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Re-export the v0.7.9 implementations.
from wizard_decide_steps import (  # noqa: E402,F401
    detect_state,
    decide_steps,
    STEP_BUMP_ACTION_PIN,
    STEP_FRESH_INSTALL,
    STEP_LEGACY_LAYOUT_MIGRATE,
    STEP_REGENERATE_LOCAL_YML,
    STEP_SUPPLEMENT_SETUP,
    STEP_ORDER,
)
from wizard_decide_steps import (  # noqa: E402
    _STEP_INFO,
    _build_envelope,
    _mode_alias,
)


def decide_mode(
    state: dict,
    current_tag: str | None,
    repo_root: Path | None = None,
) -> dict:
    """Return the v0.7.8 ``{mode, next_action, ask_user}`` envelope.

    Implementation: delegate to ``decide_steps``; envelope follows
    the first step (or ``"current"`` when no steps fire). The
    behavior matches v0.7.8's first-match-wins decision tree
    (step_order in ``wizard_decide_steps`` mirrors the v0.7.8 rule
    priority).

    Note: the v0.7.8 ``bump-only-with-seed`` and ``migrate`` modes
    are NOT directly reproduced — they were special-cased in v0.7.8
    but the step walker handles their underlying conditions via
    different steps (``step_bump_action_pin`` fires whenever the
    pin is stale; ``step_regenerate_local_yml`` covers the
    seed-script-missing or auth-block-missing case). Callers that
    care about the legacy mode names can read ``state`` directly
    and decide.
    """
    steps = decide_steps(state, current_tag, repo_root=repo_root)
    if not steps:
        return {
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
        "mode": _mode_alias(first),
        "next_action": info.get("next_action", ""),
        "ask_user": info.get("ask_user"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--current-tag", default=None)
    p.add_argument("--repo-root", default=".")
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
