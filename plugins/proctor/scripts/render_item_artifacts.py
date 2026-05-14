"""Render the logs + screenshot subsection for a single test-result item.

Replaces the v0.3-and-earlier "AI hand-computes absolute paths and
conditionally renders links" prose. Real runs showed the AI produced
repo-root-relative `.proctor/runs/<run-id>/<id>.log` hrefs which the
browser resolves relative to the REPORT'S OWN directory — yielding
`.proctor/runs/<id>/.proctor/runs/<id>/<id>.log` and a 404
(ERR_FILE_NOT_FOUND when opening report.html via file://). The AI
also forgot to render anything at all for missing artifacts, leaving
the reader unable to tell "the test passed but screenshot wasn't
captured" apart from "the test passed and there's nothing to show".

This script normalizes paths to absolute, checks file existence,
and emits unambiguous markdown:

- Logs: when the executor wrote one → `**Full log:** [<name>](file:///abs/path)`.
  When missing or pointed at a non-existent file → italic "(no log
  captured)" with the would-be path so a reviewer can debug.

- Screenshots (chrome-devtools items only): when present → embedded
  image + `_What to look for:_` line from screenshot_focus. When
  absent → loud `**Screenshot:** *(not captured — chrome-devtools
  contract requires take_screenshot; flag as an executor bug)*` so
  the missing-artifact problem is visible in the report instead of
  silently absent.

Used by the reporting-pr-test-results SKILL — called once per item.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _normalize(run_dir: Path, ref: str, sub_dir: str = "") -> Path:
    """Resolve a (possibly repo-root-relative or just-basename) artifact
    reference to an absolute path. Try in order:
      1. Already-absolute path.
      2. <run_dir>/<sub_dir>/<basename of ref> (the canonical executor location).
      3. <cwd>/<ref> (repo-root-relative — what most AIs default to).
    Returns the first matching candidate that exists, or the canonical
    candidate (run_dir/sub_dir/basename) if nothing exists (so the
    "not found" message points at where it SHOULD have been)."""
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return ref_path

    base = ref_path.name
    candidate_1 = run_dir / sub_dir / base if sub_dir else run_dir / base
    if candidate_1.exists():
        return candidate_1.resolve()

    candidate_2 = Path.cwd().resolve() / ref_path
    if candidate_2.exists():
        return candidate_2

    return candidate_1.resolve()


def render(
    run_dir: str | Path,
    item_id: str,
    tool: str,
    logs_ref: str | None,
    screenshot_ref: str | None,
    screenshot_focus: str | None,
    mode: str = "local",
    screenshot_url_base: str | None = None,
    github_run_id: str | None = None,
    server_url: str | None = None,
    repo: str | None = None,
    screenshots: list[dict] | None = None,
) -> str:
    """Return the markdown subsection for one item's artifacts. May
    be an empty string when no artifacts apply (e.g. lint-only item
    with no log)."""
    run_dir = Path(run_dir).resolve()
    parts: list[str] = []

    # --- Logs ---
    if logs_ref:
        log_path = _normalize(run_dir, logs_ref)
        if log_path.exists():
            if mode == "local":
                parts.append(
                    f"**Full log:** [`{log_path.name}`](file://{log_path})"
                )
            else:
                # CI mode — log is inside the workflow's artifact zip.
                artifact_url = (
                    f"{server_url}/{repo}/actions/runs/{github_run_id}#artifacts"
                    if server_url and repo and github_run_id else
                    "(in artifact)"
                )
                parts.append(
                    f"**Full log:** `{log_path.name}` ({artifact_url})"
                )
        else:
            parts.append(
                f"**Full log:** *(not found — executor referenced "
                f"`{logs_ref}` but the file is absent at `{log_path}`)*"
            )

    # --- Screenshots (chrome-devtools items only) ---
    # v0.6.4+: prefer the new multi-screenshot `screenshots` list. Falls
    # back to legacy single `screenshot_ref` + `screenshot_focus` for
    # results emitted by older executors.
    if tool == "chrome-devtools" and screenshots:
        parts.append(f"**Screenshots** ({len(screenshots)} captured):")
        for i, shot in enumerate(screenshots, start=1):
            path = shot.get("path") or ""
            label = shot.get("label") or f"Screenshot {i}"
            focus = shot.get("focus") or ""
            if not path:
                parts.append(
                    f"{i}. **{label}** — *(missing path field)*"
                )
                continue
            shot_path = _normalize(run_dir, path, sub_dir="screenshots")
            if shot_path.exists():
                if mode == "local":
                    parts.append(
                        f"{i}. **{label}**\n\n"
                        f"![{item_id} {label}](file://{shot_path})"
                    )
                elif screenshot_url_base:
                    parts.append(
                        f"{i}. **{label}**\n\n"
                        f"![{item_id} {label}]"
                        f"({screenshot_url_base}{shot_path.name})"
                    )
                else:
                    artifact_url = (
                        f"{server_url}/{repo}/actions/runs/"
                        f"{github_run_id}#artifacts"
                        if server_url and repo and github_run_id else
                        "(in artifact)"
                    )
                    parts.append(
                        f"{i}. **{label}**\n\n"
                        f"`{shot_path.name}` ({artifact_url})"
                    )
                if focus:
                    parts.append(f"   _Focus:_ {focus}")
            else:
                parts.append(
                    f"{i}. **{label}** — *(file not found at "
                    f"`{shot_path}`; executor referenced `{path}`)*"
                )
        # Don't fall through to legacy single-screenshot rendering
        # when the multi-screenshot field is present.
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n"

    if tool == "chrome-devtools":
        if screenshot_ref:
            shot_path = _normalize(run_dir, screenshot_ref, sub_dir="screenshots")
            if shot_path.exists():
                if mode == "local":
                    parts.append("**Screenshot:**\n\n"
                                 f"![{item_id} screenshot](file://{shot_path})")
                elif screenshot_url_base:
                    parts.append("**Screenshot:**\n\n"
                                 f"![{item_id} screenshot]({screenshot_url_base}{shot_path.name})")
                else:
                    artifact_url = (
                        f"{server_url}/{repo}/actions/runs/{github_run_id}#artifacts"
                        if server_url and repo and github_run_id else
                        "(in artifact)"
                    )
                    parts.append(
                        f"**Screenshot:** `{shot_path.name}` ({artifact_url})"
                    )
                if screenshot_focus:
                    parts.append(f"_What to look for:_ {screenshot_focus}")
            else:
                parts.append(
                    f"**Screenshot:** *(file not found — executor "
                    f"referenced `{screenshot_ref}` but the file is "
                    f"absent at `{shot_path}`)*"
                )
        else:
            # chrome-devtools result missing screenshot_ref entirely
            # — the executor subagent skipped take_screenshot. The
            # pr-test-executor agent contract says this is REQUIRED;
            # surface the gap loudly instead of silently rendering
            # nothing, so reviewers can file an executor bug.
            parts.append(
                "**Screenshot:** *(not captured — chrome-devtools "
                "items REQUIRE a screenshot per the pr-test-executor "
                "agent contract, but `screenshot_ref` is absent on "
                "this result. Treat as an executor bug; the test may "
                "have passed without visual verification.)*"
            )

    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--item-id", required=True)
    p.add_argument("--tool", required=True,
                   help="Item's tool: chrome-devtools / bash / curl / "
                        "lint-only / skip")
    p.add_argument("--logs-ref", default="",
                   help="The result's logs_ref field, or empty.")
    p.add_argument("--screenshot-ref", default="",
                   help="The result's screenshot_ref field, or empty. "
                        "Legacy single-screenshot field — prefer "
                        "--screenshots-json for v0.6.4+ results.")
    p.add_argument("--screenshot-focus", default="")
    p.add_argument("--screenshots-json", default=None,
                   help="JSON string for the v0.6.4+ `screenshots` "
                        "field (list of {path, label, focus}). When "
                        "present, takes precedence over the single-"
                        "screenshot legacy fields.")
    p.add_argument("--mode", default="local", choices=["local", "ci"])
    p.add_argument("--screenshot-url-base", default=None)
    p.add_argument("--github-run-id", default=None)
    p.add_argument("--server-url", default=None)
    p.add_argument("--repo", default=None)
    args = p.parse_args()

    screenshots = None
    if args.screenshots_json:
        try:
            screenshots = json.loads(args.screenshots_json)
        except json.JSONDecodeError as e:
            sys.stderr.write(
                f"render_item_artifacts: --screenshots-json failed "
                f"to parse: {e}\n"
            )
            return 2
    sys.stdout.write(render(
        run_dir=args.run_dir,
        item_id=args.item_id,
        tool=args.tool,
        logs_ref=args.logs_ref or None,
        screenshot_ref=args.screenshot_ref or None,
        screenshot_focus=args.screenshot_focus or None,
        mode=args.mode,
        screenshot_url_base=args.screenshot_url_base,
        github_run_id=args.github_run_id,
        server_url=args.server_url,
        repo=args.repo,
        screenshots=screenshots,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
