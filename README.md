# PRoctor

> AI-driven PR test runner as a Claude Code plugin.

Given a GitHub PR, PRoctor:

1. **Analyzes** the diff and categorizes changes (frontend / api / schema / mobile / cli / e2e-flow / infra / docs).
2. **Plans** concrete tests per category.
3. **Confirms** the plan (interactively in local mode, by PR comment in CI mode).
4. **Executes** tests via `chrome-devtools` MCP, Bash, curl, etc.
5. If anything fails and `auto_fix: true`, **opens a fix PR** with minimal patches.
6. **Reports** back as a structured PR comment.

## Install (local)

```bash
claude plugin add /path/to/proctor/plugins/proctor
```

## Usage

```bash
claude /proctor 123
claude /proctor https://github.com/org/repo/pull/123
```

## Configure

Place `.pr-test.yml` at the repo being tested. See [`examples/.pr-test.yml`](examples/.pr-test.yml).

## CI

See [`github-action/README.md`](github-action/README.md) for the GitHub Action wrapper.

## Pipeline

```
[1] analyze → ChangeMap
[2] plan    → TestPlan
[3] approve → ApprovedPlan
[4] execute → TestResults     (subagent: pr-test-executor)
[5] fix     → FixPRRef        (subagent: pr-test-fixer)
[6] report  → PR comment
```

Each stage is a Claude Code skill with a JSON contract:

- [`analyzing-pr-changes`](plugins/proctor/skills/analyzing-pr-changes/SKILL.md)
- [`planning-pr-tests`](plugins/proctor/skills/planning-pr-tests/SKILL.md)
- [`executing-pr-tests`](plugins/proctor/skills/executing-pr-tests/SKILL.md)
- [`fixing-test-failures`](plugins/proctor/skills/fixing-test-failures/SKILL.md)
- [`reporting-pr-test-results`](plugins/proctor/skills/reporting-pr-test-results/SKILL.md)

Subagents:

- [`pr-test-executor`](plugins/proctor/agents/pr-test-executor.md) — runs one item.
- [`pr-test-fixer`](plugins/proctor/agents/pr-test-fixer.md) — produces one patch.

## Design + plan docs

- Spec: [`docs/superpowers/specs/2026-05-09-proctor-design.md`](docs/superpowers/specs/2026-05-09-proctor-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-05-09-proctor.md`](docs/superpowers/plans/2026-05-09-proctor.md)

## Test PRoctor itself

```bash
# Unit (Python helpers + skill harness)
python3 -m pytest tests/test_helpers.py -q
./tests/run-skill.sh analyzing-pr-changes frontend-only
./tests/run-skill.sh planning-pr-tests   mixed-stack

# End-to-end (requires the proctor-fixtures repo)
./tests/run-e2e.sh
```

## License

MIT (placeholder — confirm before publishing).
