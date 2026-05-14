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

# Directories we NEVER symlink into the worktree even when they're
# gitignored. ``.git/`` is structural to git itself; ``.proctor/`` is
# PRoctor-owned (the worktree's `.proctor/runs/<id>/` is where this very
# run lives — symlinking the consumer's `.proctor/` would create a
# self-reference loop).
_NEVER_SYMLINK = frozenset({".git", ".proctor"})


def _discover_gitignored_dirs(repo_root: Path) -> list[str]:
    """Ask git which directories are gitignored at this repo root.

    Returns a list of repo-root-relative paths (no trailing slash). Each
    path is a directory that:
      - Is matched by ``.gitignore`` / ``.git/info/exclude`` / global
        excludes (i.e. ``git check-ignore`` would mark it ignored), AND
      - Exists on disk right now, AND
      - Is not in ``_NEVER_SYMLINK``.

    Implementation: ``git ls-files --others --ignored
    --exclude-standard --directory`` enumerates every gitignored path,
    and ``--directory`` collapses each fully-ignored directory to its
    top-most path (so ``node_modules/`` appears as one entry rather
    than every file inside). When a directory is tracked but a sub-dir
    inside is ignored (e.g. ``external/assets/`` tracked + ``external/
    assets/mcd/`` ignored), git surfaces the ignored sub-path directly.

    The result list is what worktree.py's symlinking step iterates over
    when ``symlink_dirs`` is not explicitly overridden. Pure git-driven
    — no hardcoded project knowledge.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files",
             "--others", "--ignored", "--exclude-standard", "--directory"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        # Not a git repo, or git command unavailable. Fall back to
        # an empty list — nothing gets symlinked, dev server has to
        # rebuild from scratch (the safe degradation).
        return []
    dirs: list[str] = []
    for line in out.splitlines():
        # ``--directory`` marks directory entries with a trailing slash.
        # Everything else is a single ignored file; we don't symlink
        # individual files (use ``_GITIGNORED_FILES_TO_COPY`` for those).
        if not line.endswith("/"):
            continue
        rel = line.rstrip("/")
        # Skip protected paths and anything nested under them.
        top = rel.split("/", 1)[0]
        if top in _NEVER_SYMLINK:
            continue
        dirs.append(rel)
    return dirs


def _default_worktree_path(repo_root: Path, run_dir: Path) -> Path:
    """Choose where to place the worktree.

    v0.7.0 placed it at ``<run_dir>/pr-checkout/`` (inside the consumer
    repo). That broke when the consumer repo lives under
    ``$GOPATH/src/...``: ``go run .`` from the worktree path resolves
    the dir to ``<module-name>/.proctor/runs/<id>/pr-checkout``, which
    Go reads as a sub-package import that doesn't exist in the parent
    module, and ``go run`` fails with::

        main module (github.com/.../<consumer-module>) does not contain
        package github.com/.../.proctor/runs/<id>/pr-checkout

    v0.7.1 places worktrees OUTSIDE the consumer repo by default, at
    ``$TMPDIR/proctor-worktrees/<consumer-name>-<run-id>/``. The path
    stays correlated to the run via the ``worktree-path.txt`` marker
    inside ``run_dir``, so teardown still finds it.

    Override via env ``PROCTOR_WORKTREE_BASE_DIR`` if the dev wants a
    different parent (e.g. a faster local SSD, or a persistent dir to
    inspect failed runs without immediate cleanup).
    """
    import os
    import tempfile
    base = os.environ.get("PROCTOR_WORKTREE_BASE_DIR")
    if base:
        base_path = Path(base).resolve()
    else:
        base_path = Path(tempfile.gettempdir()) / "proctor-worktrees"
    base_path.mkdir(parents=True, exist_ok=True)
    # Encode consumer repo name + run-id in the dir so concurrent runs
    # against different PRs don't collide.
    consumer_name = repo_root.name
    run_id = run_dir.name
    return (base_path / f"{consumer_name}-{run_id}").resolve()


def setup(
    run_dir: Path,
    pr_number: int,
    head_sha: str,
    repo_root: Path | None = None,
    symlink_dirs: list[str] | None = None,
) -> Path:
    """Ensure a worktree at ``head_sha`` exists.

    Returns the absolute path to the worktree.

    v0.7.1 — worktree is placed outside the consumer repo (default:
    under ``$TMPDIR/proctor-worktrees/``). See ``_default_worktree_path``
    for the rationale (Go module conflicts when consumer repo is in
    ``$GOPATH/src/``). The location is recorded in
    ``<run_dir>/worktree-path.txt`` for teardown.

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
    worktree_path = _default_worktree_path(repo_root, run_dir)
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
    # (v0.7.0+). v0.7.2: when caller doesn't override, ask git directly
    # via ``--others --ignored --exclude-standard --directory`` instead
    # of using a hardcoded list — works for any project without leaking
    # project-specific paths into the plugin defaults. Explicit override
    # via ``symlink_dirs`` parameter (CLI: ``--symlink-dirs``) still
    # honored for consumers that want exact control.
    dirs_to_link = (
        symlink_dirs if symlink_dirs is not None
        else _discover_gitignored_dirs(repo_root)
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
