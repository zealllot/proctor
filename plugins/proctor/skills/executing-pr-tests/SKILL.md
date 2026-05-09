---
name: executing-pr-tests
description: Use after the approval gate to dispatch each item in an ApprovedPlan to the pr-test-executor subagent and assemble TestResults. Third stage of the PRoctor pipeline. Output is a single TestResults JSON object — no prose.
---

# Executing PR Tests

Input: `approved-plan.json` (a TestPlan filtered by the approval gate),
plus environment from the command (run-id, logs dir, base_url, timeout
seconds, head_sha, repo).

Output: a single `TestResults` JSON object.

## Procedure

1. **Re-fetch PR head SHA** with `gh pr view <PR#> --json headRefOid`.
   If it differs from the cached `head_sha` → abort the run, print
   `[proctor:execute] aborted reason=force-push` to stdout, return:

   ```jsonc
   {"items": [], "summary": {"total": 0, "pass": 0, "fail": 0, "skipped": 0},
    "aborted": "force-push"}
   ```

   The command-level orchestrator will detect `aborted` and skip fix +
   replace report with a force-push notice.

2. **Run setup** from `.pr-test.yml`. Each command via Bash. If any
   exits non-zero → abort with `aborted: "setup-failed"`.

3. **Topologically sort items** by `depends_on`. Items with no
   unresolved deps run in parallel via the Task tool dispatching to
   `pr-test-executor`. Items that depend on others wait until their
   deps land.

4. For each item:
   - Dispatch a fresh subagent with the item JSON, env, and the path
     to `<logs_dir>/<id>.log`.
   - Receive its single-result JSON.
   - Append to results array.

5. After all items finish, **kill setup processes**: send SIGTERM to
   the process group started by setup commands (use `setsid` to start
   them; track the PIDs in `<logs_dir>/setup.pids`).

6. Compute `summary` counters by walking `items`. Validate with
   `schema.py:validate_test_results`. Emit one JSON object.

## Constraints

- Emit exactly one JSON object.
- No item runs twice. No item runs in two subagents simultaneously.
- Subagent output that doesn't parse → record as `{status: "fail", reason: "error", evidence: "executor returned non-JSON"}`.

## Detail references

- Subagent contract: `../../agents/pr-test-executor.md`
- Result schema: `../../scripts/schema.py:validate_test_results`
