"""Manage a PR-aligned git worktree for the executing-pr-tests skill.

When the developer's checkout is on a different SHA than the PR's
head (typical case: dev is on their feature branch, runs PRoctor
against someone ELSE's PR), running chrome-devtools tests against
the local dev server tests the wrong code. The user-visible symptom
is "branch-mismatch" skips: validators / form fields the PR adds
aren't present in the running server, so tests can't find them.

This helper (v0.3.37+) creates an ephemeral worktree under the run
directory at the PR's head SHA, copies the gitignored
``.proctor/local.yml`` (which carries the dev's setup + credentials)
into it, and surfaces the worktree path. The executing-pr-tests
skill then runs setup/teardown/test commands from that worktree
instead of the user's checkout, so the dev server compiles + runs
PR's code without disturbing the user's working tree.

Idempotent — calling setup when the worktree is already in place is
a no-op. Teardown best-effort — if `git worktree remove` fails
(busy file, missing dir), the marker file is left for the user to
clean up manually.

CLI:

    python3 worktree.py setup --run-dir <dir> --pr-number <n> \\
        --head-sha <sha>
    python3 worktree.py teardown --run-dir <dir>

setup prints the worktree path to stdout (single line, no quotes)
so a shell caller can do::

    WORKTREE=$(python3 worktree.py setup --run-dir ... )
    cd "$WORKTREE"
    <setup commands>
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Gitignored files we copy from the repo root into the worktree
# because the worktree starts as a clean checkout (no untracked
# files). `.proctor/local.yml` carries the dev's setup commands +
# auth credentials and must be present for the run to work.
# Add other paths here if a wider class of repos needs them.
_GITIGNORED_FILES_TO_COPY = [".proctor/local.yml"]

# Common gitignored runtime-built directories worth symlinking from the
# main repo into the worktree so the dev server doesn't have to rebuild
# them. Each entry must exist at the main repo root to be linked.
# Override via .proctor/config.yml's `worktree_symlink_dirs` field.
_DEFAULT_GITIGNORED_DIRS_TO_SYMLINK = [
    "external/assets",       # frontend bundle output (mcd-website pattern)
    "node_modules",          # JS deps
    "dist",                  # generic build output
    "build",                 # generic build output
    ".next",                 # Next.js
    "vendor",                # Go vendoring (rare with go.mod, but...)
]


def setup(
    run_dir: Path,
    pr_number: int,
    head_sha: str,
    repo_root: Path | None = None,
    symlink_dirs: list[str] | None = None,
) -> Path:
    """Ensure a worktree at ``head_sha`` exists under ``run_dir``.

    Returns the absolute path to the worktree.

    - Idempotent: if the worktree already exists at the right SHA,
      returns the path without re-creating.
    - If ``head_sha`` isn't in the local object database, fetches the
      PR head ref (``pull/<n>/head``) from origin.
    - Copies ``_GITIGNORED_FILES_TO_COPY`` from the repo root into
      the worktree.
    - Writes a marker file at ``<run_dir>/worktree-path.txt`` so
      teardown can locate the worktree later.
    """
    repo_root = (repo_root or Path.cwd()).resolve()
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = (run_dir / "pr-checkout").resolve()
    marker = run_dir / "worktree-path.txt"

    # If worktree already exists, verify it's at the right SHA and
    # short-circuit.
    if worktree_path.exists() and (worktree_path / ".git").exists():
        existing = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if existing == head_sha:
            return worktree_path
        # Wrong SHA in existing worktree — tear it down and recreate.
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove",
             "--force", str(worktree_path)],
            check=False,
        )

    # Ensure head_sha is in the local object DB. `git cat-file -e`
    # exits 0 if the object exists, non-zero otherwise.
    have_sha = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", head_sha],
        capture_output=True, check=False,
    ).returncode == 0
    if not have_sha:
        # Fetch the PR head ref from origin. This works for GitHub-
        # backed remotes; if it fails on an exotic remote, surface
        # the error.
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "origin",
             f"pull/{pr_number}/head"],
            check=True,
        )
        # Sanity-check: did the fetch land us at the expected SHA?
        fetched = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "FETCH_HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if fetched != head_sha:
            raise RuntimeError(
                f"fetched PR #{pr_number} head ({fetched}) does not match "
                f"expected SHA ({head_sha}). Was the PR force-pushed since "
                f"PRoctor captured pr.head_sha? Aborting the worktree "
                f"setup so we don't test against a stale revision."
            )

    # Create the worktree at the exact SHA (detached HEAD — we don't
    # want to claim the PR's branch name in the user's repo).
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--detach",
         str(worktree_path), head_sha],
        check=True,
    )

    # Copy gitignored config files so the dev's local setup +
    # credentials apply inside the worktree. v0.4.0+ paths live under
    # `.proctor/` so we mkdir the parent before copying.
    for fname in _GITIGNORED_FILES_TO_COPY:
        src = repo_root / fname
        if src.exists():
            dst = worktree_path / fname
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Symlink gitignored runtime-built directories from the main repo
    # into the worktree so the dev server doesn't have to rebuild them
    # (v0.7.0+). Default list covers `external/assets`, `node_modules`,
    # `dist`, `build`, `.next`, `vendor`; override via the
    # `symlink_dirs` parameter (CLI: `--symlink-dirs`).
    dirs_to_link = (
        symlink_dirs if symlink_dirs is not None
        else _DEFAULT_GITIGNORED_DIRS_TO_SYMLINK
    )
    for d in dirs_to_link:
        src = repo_root / d
        if not src.is_dir():
            continue
        dst = worktree_path / d
        # Don't overwrite tracked dirs (the worktree already created
        # them via checkout) or an existing symlink from a prior run.
        if dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src.resolve())

    marker.write_text(str(worktree_path) + "\n")
    return worktree_path


def teardown(run_dir: Path, repo_root: Path | None = None) -> None:
    """Remove the worktree recorded by ``setup``.

    Best-effort: failures are swallowed. ``git worktree remove`` can
    fail if the worktree has untracked changes the dev wants to keep,
    or if a process inside the worktree is still holding a file. In
    both cases the dev can `git worktree remove --force` manually.
    """
    repo_root = (repo_root or Path.cwd()).resolve()
    marker = run_dir / "worktree-path.txt"
    if not marker.exists():
        return
    worktree_path = Path(marker.read_text().strip())
    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove",
             "--force", str(worktree_path)],
            check=False,
        )
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup", help="Create the PR-aligned worktree.")
    sp.add_argument("--run-dir", required=True,
                    help="The .proctor/runs/<run-id>/ directory.")
    sp.add_argument("--pr-number", type=int, required=True)
    sp.add_argument("--head-sha", required=True)
    sp.add_argument("--repo-root", default=None,
                    help="Defaults to current working directory.")
    sp.add_argument("--symlink-dirs", default=None,
                    help="Comma-separated dirs (relative to repo root) to "
                         "symlink from main checkout into the worktree. "
                         "Defaults to a built-in list of common gitignored "
                         "runtime-built dirs. Pass empty string to skip.")

    sp = sub.add_parser("teardown", help="Remove the PR-aligned worktree.")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--repo-root", default=None)

    args = p.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else None
    if args.cmd == "setup":
        # Empty string -> []  (explicit "skip all symlinks").
        # None (flag not passed) -> None (use built-in defaults).
        # Any other value -> split on comma, strip whitespace, drop empties.
        sd_arg = args.symlink_dirs
        if sd_arg is None:
            symlink_dirs: list[str] | None = None
        elif sd_arg == "":
            symlink_dirs = []
        else:
            symlink_dirs = [s.strip() for s in sd_arg.split(",") if s.strip()]
        path = setup(
            Path(args.run_dir), args.pr_number, args.head_sha, repo_root,
            symlink_dirs=symlink_dirs,
        )
        print(str(path))
    else:
        teardown(Path(args.run_dir), repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
