"""Second opinion: a different, larger model reads the draft against the request.

The deterministic audit in ``audit.py`` stays the authority. This stage can only
*add* findings, and only at ``warn`` or ``info`` - it cannot raise a block, it
cannot clear one, and nothing it says is ever read by the gate.  A reviewer that
could lower the bar would just be a second way for a model to talk its way past
the human.

Why bother, then?  Because the rule-based audit only knows the patterns it was
written for.  A model that did not write the draft is a cheap way to surface the
clause that is legal-looking but wrong for *this* request - and the operator is
the one who decides whether it matters.
"""

from __future__ import annotations

import json
from typing import Any

from .llm import Backend, LLMError
from .models import Draft, DocumentRequest, Finding

MAX_FINDINGS = 8
MAX_MESSAGE = 300
ALLOWED = {"warn", "info"}

INSTRUCTION = """Review this contract draft against the request it was written for.
You did not write the draft. Look for problems a careful reviewer would raise
before a person signs: clauses that contradict the request, terms that are
one-sided against the sender, obligations nobody asked for, missing protections
the request implies, and internal inconsistencies (dates, names, durations).

Return {"findings": [{"severity": "warn" | "info", "clause": <clause heading or
"document">, "concern": <one or two sentences>}]}. At most 8 findings, most
important first. Return {"findings": []} if you have nothing material to add.
Do not restate that a person must approve the document; that is already enforced."""


def second_opinion(backend: Backend, request: DocumentRequest, draft: Draft) -> list[Finding]:
    """Return reviewer findings, capped at ``warn``. Never raises on a bad answer."""
    payload = json.dumps(
        {"request": request.to_json(), "draft": draft.to_json()},
        ensure_ascii=False,
        indent=1,
    )
    try:
        raw = backend.complete_json(INSTRUCTION, payload)
    except LLMError as exc:
        return [
            Finding(
                severity="info",
                code="REVIEWER_UNAVAILABLE",
                message=f"The second-opinion model did not answer ({str(exc)[:120]}).",
                where="document",
            )
        ]
    return _to_findings(raw)


def _to_findings(raw: Any) -> list[Finding]:
    items = raw.get("findings", []) if isinstance(raw, dict) else []
    findings: list[Finding] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        concern = str(item.get("concern", "")).strip()
        if not concern:
            continue
        severity = str(item.get("severity", "info")).lower()
        # A "block" from the reviewer is downgraded, not honoured: blocks come
        # only from rules a person can read.
        if severity not in ALLOWED:
            severity = "warn"
        findings.append(
            Finding(
                severity=severity,
                code="REVIEWER",
                message=concern[:MAX_MESSAGE],
                where=str(item.get("clause", "document"))[:80] or "document",
            )
        )
        if len(findings) >= MAX_FINDINGS:
            break
    return findings
