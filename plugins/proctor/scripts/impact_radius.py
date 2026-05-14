"""Compute filtered impact_radius for a single hunk.

Runs `git grep -c -e '\\bIDENT\\b'` per identifier across the repo,
aggregates per-file match counts across all identifiers, drops files
with total occurrences below ``MIN_OCCURRENCES`` (defaults to 2 — a
single match is almost always just an `import { Foo } from ...` line
or a re-export, NOT a real caller), sorts by descending count, returns
the top ``TOP_N`` paths.

Why this lives outside the SKILL.md procedure (v0.3.26+): the skill
used to ask the AI to run multiple `git grep` invocations and
aggregate counts by hand. That was both error-prone (per-run drift in
parsing) and silent (no way to test the aggregation logic). Moving it
to a script means:
  - threshold and exclude rules can be tuned by editing one file
  - tests pin the filtering behavior against a controlled fixture
  - false positives drop without the analyzer needing language-aware
    import resolution

Invoked by analyzing-pr-changes; safe to call standalone for debugging:

    python3 plugins/proctor/scripts/impact_radius.py \\
        --file <path/to/changed_file.go> \\
        --idents "<ChangedFunc1> <ChangedFunc2>" \\
        --repo .
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from collections import Counter

# Exclude patterns: test files, vendor/build outputs, PRoctor's own
# run artifacts. Same shape as `git grep -- ':!pattern'`. Order doesn't
# matter — git applies them all.
EXCLUDE_GLOBS: list[str] = [
    "*_test.go",
    "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx",
    "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx",
    "tests/", "test/", "__tests__/", "__test__/", "spec/",
    "vendor/", "node_modules/", "dist/", "build/", "target/",
    ".proctor/",
]

# A caller with only one match of a changed identifier is overwhelmingly
# an import statement or a re-export — not a real consumer of the
# changed behavior. Two matches is the minimum that suggests
# `import + use` or `declare-alias + use`. Tune here, document in
# CHANGELOG.
MIN_OCCURRENCES = 2

# Cap the result list to keep planner cost bounded. Larger projects
# routinely produce 50+ "matches" per identifier across re-exports;
# top 10 captures the practical fan-out.
TOP_N = 10


def collect_callers(
    changed_file: str,
    identifiers: list[str],
    repo: str = ".",
    min_occurrences: int = MIN_OCCURRENCES,
    top_n: int = TOP_N,
) -> dict[str, object]:
    """Return ``{"files": [...], "truncated": bool}`` — caller paths
    whose cumulative match count across ``identifiers`` meets
    ``min_occurrences``, excluding the changed file, test/vendor
    paths, and PRoctor artifacts.

    The ``truncated`` flag (v0.3.28+) is True when MORE survivors
    crossed the threshold than ``top_n`` — i.e. the returned 10
    callers do NOT represent the full blast radius. The analyzer
    propagates this to ``ChangeMap.hunks[i].impact_radius_truncated``
    so the planner can auto-upgrade risk to ``high`` and plan extra
    regression items.

    ``{"files": [], "truncated": False}`` when identifiers is empty,
    git is unavailable, or no caller crosses the threshold. The
    caller decides whether empty means "did not analyze" (omit
    field) or "looked, nothing meaningful" (emit empty list)."""
    if not identifiers:
        return {"files": [], "truncated": False}

    counts: Counter[str] = Counter()
    # Use the long-form pathspec magic `:(exclude)PATTERN` instead of
    # the `:!PATTERN` shorthand: the shorthand fails with
    # `fatal: Unimplemented pathspec magic '_'` when the next char is
    # `_` (which we hit on `__tests__/` and `__test__/`).
    excludes = [f":(exclude){changed_file}"] + [
        f":(exclude){g}" for g in EXCLUDE_GLOBS
    ]

    for ident in identifiers:
        # `git grep -o` emits one line per MATCH (not per matching
        # line) — `Foo(); Foo();` on a single source line yields two
        # output lines `path:Foo`. We want true occurrence counts so
        # the threshold differentiates "imported once" from "used
        # repeatedly", regardless of whether the caller code happens
        # to put multiple calls on one line.
        cmd = [
            "git", "grep", "-o", "--untracked", "-e",
            rf"\b{re.escape(ident)}\b", "--",
        ] + excludes
        try:
            proc = subprocess.run(
                cmd, cwd=repo, capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return []
        # Each output line is "path:<matched-text>". Strip the trailing
        # match and count one occurrence per line. Exit code 1 means
        # "no matches" — not an error here.
        for line in proc.stdout.splitlines():
            path, sep, _ = line.partition(":")
            if not sep or not path:
                continue
            counts[path] += 1

    # Defensive — the `:!<changed_file>` exclude SHOULD have done this
    # already, but git pathspecs match relative to repo root and the
    # caller might pass a path that doesn't match the file's actual
    # location. Drop manually so we never list a file as its own caller.
    counts.pop(changed_file, None)

    survivors = [(p, c) for p, c in counts.items() if c >= min_occurrences]
    survivors.sort(key=lambda pc: (-pc[1], pc[0]))
    truncated = len(survivors) > top_n
    return {
        "files": [p for p, _ in survivors[:top_n]],
        "truncated": truncated,
    }


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    # --file is repeatable (v0.7.0+). --files is the preferred plural
    # spelling. Either flag accumulates into the same list so the
    # analyzer can amortize one Python startup over all changed files.
    p.add_argument("--file", "--files", dest="files", action="append",
                   default=None,
                   help="The changed file (excluded from results). May "
                        "be passed multiple times (v0.7.0+); when more "
                        "than one file is supplied the output shape "
                        "becomes a JSON object keyed by file path.")
    p.add_argument("--idents", required=True,
                   help="Space-separated identifier list.")
    p.add_argument("--min-occurrences", type=int, default=MIN_OCCURRENCES,
                   help=f"Drop callers with cumulative count below this "
                        f"(default {MIN_OCCURRENCES}).")
    p.add_argument("--top", type=int, default=TOP_N,
                   help=f"Cap the result list (default {TOP_N}).")
    p.add_argument("--repo", default=".",
                   help="Repo root for git grep (default cwd).")
    args = p.parse_args()

    if not args.files:
        p.error("at least one --file / --files argument is required")

    idents = [i for i in args.idents.split() if i]

    # Single-file: emit the result dict directly (backward-compatible
    # with v0.3.26 — pre-v0.7.0 consumers parse this shape).
    if len(args.files) == 1:
        result = collect_callers(
            args.files[0], idents, repo=args.repo,
            min_occurrences=args.min_occurrences, top_n=args.top,
        )
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    # Multi-file (v0.7.0+): run collect_callers in parallel threads
    # since each invocation shells out to `git grep` (IO-bound) and the
    # work is fully independent per file. Emit a JSON object keyed by
    # file path so the analyzer can substitute results in one round-trip.
    results: dict[str, dict[str, object]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                collect_callers, f, idents, repo=args.repo,
                min_occurrences=args.min_occurrences, top_n=args.top,
            ): f
            for f in args.files
        }
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
    json.dump(results, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
