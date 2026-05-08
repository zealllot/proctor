# PRoctor

AI-driven PR test runner as a Claude Code plugin. Given a GitHub PR, PRoctor:

1. Analyzes the diff and categorizes changes (frontend / api / schema / mobile / cli / e2e-flow / infra / docs)
2. Plans concrete tests per category
3. Asks for confirmation (local) or posts plan as comment (CI)
4. Runs tests with chrome-devtools, Bash, curl, etc.
5. If anything fails and `auto_fix: true`, opens a fix PR
6. Posts a structured report as a PR comment

## Install (local)

```bash
claude plugin add /path/to/proctor/plugins/proctor
```

## Usage

```bash
claude /proctor 123              # PR number in current repo
claude /proctor https://github.com/org/repo/pull/123
```

## Configuration

Place `.pr-test.yml` at the repo root being tested. See `examples/.pr-test.yml`.

## GitHub Action

See `github-action/README.md`.
