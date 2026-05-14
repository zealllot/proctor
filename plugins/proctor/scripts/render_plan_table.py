"""Render a TestPlan as a markdown approval-gate block.

Replaces the v0.4.0-and-earlier "AI hand-renders the approval gate"
design. Hand-rendering proved unreliable across v0.3.x → v0.4.2: even
with explicit prose rules forbidding JSON dumps to chat, the AI kept
dumping the plan object before/instead of rendering the table —
consuming its own context budget and stalling for 3-5 minutes at the
approval gate. Five releases of prose-tightening did not fix it.

v0.4.3 takes the rendering out of the AI's hands. This script reads
test-plan.json from stdin, prints a deterministic markdown block
(header + table + estimate), exits. The orchestrator's approval gate
becomes TWO tool calls: bash this script, then AskUserQuestion. No
free attention between them; nothing for the AI to "think about".

Stdin: a TestPlan JSON object (the schema-validated shape).
Stdout: markdown — header line, blank line, table, blank line,
estimate, blank line, optional plan-smells residual block. Suitable
for going straight into the chat transcript.
Exit: 0 on success, non-zero on malformed input.

Usage:

    python3 render_plan_table.py \\
        --pr-number 1115 \\
        --run-dir .proctor/runs/<run-id> \\
        < .proctor/runs/<run-id>/test-plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Best-effort time / cost estimates per tool category. These match
# the numbers the orchestrator prose used; centralising them here so
# tuning is one edit. Time in seconds, cost in dollars.
_TOOL_ESTIMATES: dict[str, tuple[float, float]] = {
    "lint-only":       (5.0,   0.001),
    "bash":            (30.0,  0.005),
    "curl":            (15.0,  0.003),
    "chrome-devtools": (60.0,  0.050),
    "skip":            (0.0,   0.000),
}

# Width caps for the `What` column so the table stays readable in
# chat. Wraps via markdown's natural line behavior; the executor
# never reads the rendered table, so we can be generous.
_WHAT_WIDTH = 100


def _truncate_what(text: str, width: int = _WHAT_WIDTH) -> str:
    """Shorten an item's `what:` for table display. Markdown tables
    don't render embedded newlines well, so collapse whitespace + cap
    length with an ellipsis for the rare 200-char description."""
    one_line = " ".join(text.split())
    if len(one_line) <= width:
        return one_line
    return one_line[: width - 1].rstrip() + "…"


def _estimate(items: list[dict]) -> tuple[float, float]:
    """Sum (seconds, dollars) across items based on tool category.
    Unknown tools default to lint-only's cheap end."""
    total_s = 0.0
    total_d = 0.0
    for it in items:
        s, d = _TOOL_ESTIMATES.get(it.get("tool", "lint-only"),
                                   _TOOL_ESTIMATES["lint-only"])
        total_s += s
        total_d += d
    return total_s, total_d


def _format_estimate(seconds: float, dollars: float) -> str:
    """Render the estimate line. Times under a minute show as seconds,
    over a minute as `~N min`; dollars always to 2 decimals."""
    if seconds < 60:
        time_part = f"~{seconds:.0f}s"
    else:
        time_part = f"~{seconds / 60:.1f} min"
    return f"**Estimated:** {time_part}, ~${dollars:.2f}"


def render(plan: dict, pr_number: int, run_dir: Path | None = None) -> str:
    """Return the markdown approval-gate block as a single string."""
    items = plan.get("items") or []
    total = len(items)

    lines: list[str] = []
    lines.append(f"## Plan for PR #{pr_number} — {total} items")
    lines.append("")
    lines.append("| # | Cat | Risk | Tool | As | What |")
    lines.append("|---|---|---|---|---|---|")
    for it in items:
        as_account = it.get("as_account") or "—"
        what = _truncate_what(it.get("what") or "")
        lines.append(
            f"| {it['id']} | {it['category']} | {it['risk']} | "
            f"{it['tool']} | {as_account} | {what} |"
        )
    lines.append("")

    seconds, dollars = _estimate(items)
    lines.append(_format_estimate(seconds, dollars))
    lines.append("")

    # Residual plan-smells block (only when planning skill exhausted
    # its 2 regen attempts and surfaced the residual warnings, per
    # v0.3.38). Empty / missing file → no section rendered.
    if run_dir is not None:
        smells_path = run_dir / "plan-smells.txt"
        if smells_path.exists():
            content = smells_path.read_text().strip()
            if content:
                lines.append(
                    "### Plan smells (still present after 2 regen attempts)"
                )
                lines.append("")
                for w in content.splitlines():
                    if w.strip():
                        lines.append(f"⚠ {w}")
                lines.append("")

    return "\n".join(lines)


def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pr-number", type=int, required=True,
                   help="The PR number for the header line.")
    p.add_argument("--run-dir", default=None,
                   help="The .proctor/runs/<run-id>/ directory; used "
                        "to surface plan-smells.txt residual warnings "
                        "if the planning skill exhausted its regen "
                        "attempts. Optional.")
    args = p.parse_args()

    try:
        plan = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"render_plan_table: malformed test-plan JSON: {e}\n")
        return 2

    run_dir = Path(args.run_dir) if args.run_dir else None
    sys.stdout.write(render(plan, args.pr_number, run_dir))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
