"""Detect error-handling patterns in diff text to suggest `error_type` items.

The v0.3.22 `error_type` enum (validation / permission / network /
state-conflict / not-found / auth) is only useful if the planner
actually picks the right type per diff. v0.3.22 left this to the AI's
inference, which means in practice the planner over-emits `validation`
(easy to spot — there's always validator code) and under-emits the
harder cases — especially `state-conflict`, which manifests as small
schema constraints, version fields, or status-guard branches that
don't visually look like "error handling".

This helper scans hunk added-lines for unambiguous patterns and emits
a structured `{error_type: [signal_names]}` map. The planner's
SKILL.md tells it: "for every error_type the helper flagged, plan at
least one matching negative item". Stops the planner from missing the
non-obvious cases.

Conservative by design: false negatives (helper misses a signal) are
acceptable — the planner falls back to inference. False positives
(helper emits for unrelated code) are NOT acceptable, since they'd
make the planner waste items on hypothetical-only failure modes.
Patterns are tuned to be specific.

Language coverage today: Go primary (handlers, GORM, gorm tags),
Python, Ruby/Rails idioms, SQL DDL, generic HTTP status / error
patterns. Add patterns by editing `SIGNALS` below — keep them tight.
"""

from __future__ import annotations

import re

# Map error_type → list of (compiled_pattern, signal_name) tuples.
# signal_name is the human-readable label the report shows
# ("planned because the diff added a unique index").
#
# Patterns are case-sensitive unless explicitly marked (?i). Symbol
# names (`StatusConflict`, `ErrNotFound`) are language conventions
# common across Go ecosystems; SQL keywords are uppercased because
# real migrations use that style; tag patterns (`gorm:"uniqueIndex"`,
# `validate:"required"`) are literal because that's how they appear.
SIGNALS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "state-conflict": [
        # SQL unique constraints — duplicate-submit territory.
        (re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b", re.IGNORECASE), "unique-index-added"),
        (re.compile(r"\bADD\s+CONSTRAINT\b.*\bUNIQUE\b", re.IGNORECASE), "unique-constraint-added"),
        (re.compile(r'gorm:["\'][^"\']*\buniqueIndex\b'), "gorm-unique-index-tag"),
        # Optimistic locking / version columns.
        (re.compile(r"\b(?:Version|LockVersion|Revision|ETag)\b\s+(?:int|uint|string)"), "version-field-added"),
        (re.compile(r"\bUPDATE\b[^;]*\bWHERE\b[^;]*\bversion\s*="), "version-where-clause"),
        # 409 / Conflict idiom.
        (re.compile(r"\b(?:StatusConflict|ErrConflict|HTTP_CONFLICT)\b"), "conflict-response"),
        (re.compile(r"\breturn\s+(?:nil,\s*)?(?:fmt\.Errorf|errors\.New)\([^)]*\b(?:already exists|duplicate|conflict)\b", re.IGNORECASE), "duplicate-error-returned"),
        # Status / state-machine guards.
        (re.compile(r'if\s+\w+\.(?:Status|State|Phase)\s*[!=]=\s*[\'"][^\'"]+[\'"]'), "state-guard"),
        # Locking primitives.
        (re.compile(r"\bSELECT\b[^;]+\bFOR\s+UPDATE\b", re.IGNORECASE), "select-for-update"),
        (re.compile(r"\bsync\.(?:Mutex|RWMutex)\b"), "sync-mutex"),
        # Idempotency keys.
        (re.compile(r"\b(?:idempotency_key|IdempotencyKey|idempotency-key)\b"), "idempotency-key"),
    ],
    "permission": [
        # Role-based guards.
        (re.compile(r"\bif\s+!?\w*\.?(?:Is(?:Admin|Developer|Editor|Owner|Staff))\b"), "role-check-guard"),
        (re.compile(r"\b(?:RequireRole|RequirePermission|RequireAdmin|CanAccess|authorize_resource)\b"), "authz-helper"),
        # 403 idiom.
        (re.compile(r"\b(?:StatusForbidden|ErrForbidden|HTTP_FORBIDDEN)\b"), "forbidden-response"),
        # Policy / Pundit / CanCanCan / Casbin-style patterns.
        (re.compile(r"\b(?:policy\.(?:Allow|Deny)|authorize!|cancan|enforce)\b"), "policy-call"),
    ],
    "auth": [
        # Auth middleware / login-required.
        (re.compile(r"\b(?:RequireAuth|RequireLogin|MustAuth|LoginRequired|authenticate_user!?)\b"), "auth-middleware"),
        (re.compile(r"\b(?:csrf|CSRF|CsrfToken)\b"), "csrf-protection"),
        # 401 idiom.
        (re.compile(r"\b(?:StatusUnauthorized|ErrUnauthorized|HTTP_UNAUTHORIZED)\b"), "unauthorized-response"),
        # Session / cookie work.
        (re.compile(r"\b(?:session\.(?:Get|Set|Save)|SessionExpired|TokenExpired)\b"), "session-handling"),
    ],
    "not-found": [
        # Nil-check returning not-found.
        (re.compile(r"==\s*nil[\s)]+(?:\{)?\s*return[^;{]*\b(?:NotFound|ErrNotFound|ErrRecordNotFound)\b"), "nil-not-found-guard"),
        # 404 idiom.
        (re.compile(r"\b(?:StatusNotFound|ErrNotFound|ErrRecordNotFound|HTTP_NOT_FOUND)\b"), "not-found-response"),
        # GORM record-not-found.
        (re.compile(r"\bgorm\.ErrRecordNotFound\b"), "gorm-record-not-found"),
        # Route param lookup → render 404.
        (re.compile(r"\brender\s+(?:status:\s*)?:?(?:not_found|404)\b"), "render-not-found"),
    ],
    "network": [
        # Outbound HTTP calls.
        (re.compile(r"\bhttp\.(?:Get|Post|NewRequest|Client\b)"), "http-client"),
        (re.compile(r"\b(?:Faraday|HTTParty|Net::HTTP|requests\.(?:get|post)|axios\.)"), "http-library"),
        # Retry / backoff.
        (re.compile(r"\b(?:retry|Retry|backoff|Backoff|with_retries)\b"), "retry-logic"),
        # Timeouts.
        (re.compile(r"\bcontext\.WithTimeout\b|\bsetTimeout\b|\btimeout\s*[:=]"), "timeout-config"),
        # Circuit breaker.
        (re.compile(r"\b(?:CircuitBreaker|circuit_breaker|hystrix)\b"), "circuit-breaker"),
    ],
    "validation": [
        # Direct validator calls.
        (re.compile(r"\b(?:validate|Validate)\([^)]"), "validate-call"),
        # Validation error returns.
        (re.compile(r"\b(?:ErrInvalid|ValidationError|StatusBadRequest|HTTP_BAD_REQUEST|400\b)"), "validation-error"),
        # Struct / model validate tags.
        (re.compile(r'validate:["\'][^"\']'), "struct-validate-tag"),
        (re.compile(r"\bvalidates(?:_\w+)*\s+:"), "rails-validates"),
        # Frontend form validation.
        (re.compile(r"\b(?:yup|zod|joi|ajv)\.\w"), "frontend-validation-lib"),
    ],
}


def detect(text: str) -> dict[str, list[str]]:
    """Scan `text` (typically a hunk's added/modified lines) and return
    a map of `error_type` → list of signal names that matched.

    Empty dict means no unambiguous signals — the planner should fall
    back to inference / coverage rules. Multiple matches per
    error_type are returned (de-duplicated) so the report can cite
    which specific pattern triggered the plan."""
    out: dict[str, list[str]] = {}
    for et, patterns in SIGNALS.items():
        hits: list[str] = []
        for rgx, name in patterns:
            if rgx.search(text) and name not in hits:
                hits.append(name)
        if hits:
            out[et] = hits
    return out


def _main() -> int:
    """CLI form for ad-hoc inspection:

        echo "$DIFF" | python3 error_signals.py
    """
    import json
    import sys
    text = sys.stdin.read()
    json.dump(detect(text), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
