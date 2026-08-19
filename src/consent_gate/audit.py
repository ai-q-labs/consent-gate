"""Pre-signature audit.

Two kinds of check, deliberately kept apart:

*   **Deterministic red flags** - regexes over the drafted text.  No model in
    the loop, so the result is reproducible and can be unit-tested.  These
    catch the failures that cost money: a liability clause with no cap, an
    auto-renewal nobody asked for, a placeholder that survived into a binding
    document.

*   **Traceability** - the interesting one.  Every clause carries a
    ``traces_to`` pointer back to a phrase in the operator's prompt or to a
    declared assumption.  A clause that traces to nothing is a clause the
    model invented.  Inventing recitals is harmless; inventing obligations is
    not, so the severity depends on whether the clause binds anybody.

The report is bound to the SHA-256 of the rendered PDF, not to the draft
object, because bytes are what get signed.
"""

from __future__ import annotations

import re
from typing import Iterable

from .ledger import utcnow
from .models import (
    AuditReport,
    DocumentRequest,
    Draft,
    Finding,
    VerificationResult,
    sha256_hex,
)

# --------------------------------------------------------------------------
# deterministic rules

PLACEHOLDER_RE = re.compile(
    r"(\[\s*(?:TBD|TODO|XXX|FILL[ _]?IN|INSERT[^\]]*)\s*\]|\{\{[^}]*\}\}|<insert[^>]*>|\bLorem ipsum\b)",
    re.IGNORECASE,
)
# A run of underscores is an unfilled blank in the body of an agreement and a
# perfectly ordinary signature line at the bottom of one. Scanned separately so
# the signature block does not produce a blocking finding on every draft.
RULE_LINE_RE = re.compile(r"_{4,}")
BLANK_AMOUNT_RE = re.compile(
    r"(?:[$€£¥]|USD|EUR|JPY)\s*(?:_+|\.{3,}|\bTBD\b)", re.IGNORECASE
)

OBLIGATION_RE = re.compile(
    r"\b(shall|must|agrees to|is obligated to|undertakes to|will be liable|indemnif)\w*\b",
    re.IGNORECASE,
)

RED_FLAGS: list[tuple[str, str, str, re.Pattern[str]]] = [
    (
        "block",
        "UNCAPPED_LIABILITY",
        "Liability appears to be unlimited. A cap is the single clause most often regretted.",
        re.compile(
            r"\b(unlimited liability|without limitation of liability|liability shall not be limited)\b",
            re.I,
        ),
    ),
    (
        "warn",
        "INDEMNITY",
        "An indemnity / hold-harmless obligation is present.",
        re.compile(r"\b(indemnif\w+|hold harmless)\b", re.I),
    ),
    (
        "warn",
        "AUTO_RENEWAL",
        "The agreement renews itself unless cancelled.",
        re.compile(r"\b(automatically renew\w*|auto-renew\w*|evergreen term)\b", re.I),
    ),
    (
        "warn",
        "PERPETUAL_GRANT",
        "A perpetual or irrevocable grant of rights is present.",
        re.compile(r"\b(perpetual|irrevocabl\w+)\b", re.I),
    ),
    (
        "warn",
        "ASSIGNMENT",
        "Rights or obligations can be transferred to a third party.",
        re.compile(r"\b(may assign|assignable|assignment of this agreement)\b", re.I),
    ),
    (
        "warn",
        "ARBITRATION",
        "Disputes are pushed to arbitration, waiving court remedies.",
        re.compile(r"\b(binding arbitration|waives? [^.]{0,30}jury)\b", re.I),
    ),
]

# (code, message, body pattern, heading pattern). A section counts as present if
# either the body says it or a clause is headed with it - a clause titled
# "Effective Date and Term" is a term clause even when its text never uses the
# phrase "term of this agreement".
REQUIRED_SECTIONS: list[tuple[str, str, re.Pattern[str], re.Pattern[str]]] = [
    (
        "GOVERNING_LAW",
        "No governing law / jurisdiction clause.",
        re.compile(r"\b(governing law|jurisdiction|governed by the laws)\b", re.I),
        re.compile(r"\b(governing law|jurisdiction|venue|applicable law)\b", re.I),
    ),
    (
        "TERM",
        "No term or duration clause.",
        re.compile(
            r"\b(term of this agreement|shall remain in effect|expires? on|duration)\b", re.I
        ),
        re.compile(r"\b(term|duration|effective date)\b", re.I),
    ),
    (
        "TERMINATION",
        "No termination clause.",
        re.compile(r"\bterminat\w+\b", re.I),
        re.compile(r"\bterminat\w+\b", re.I),
    ),
]


def _body_text(draft: Draft) -> str:
    """Everything that binds. Excludes the signature block."""
    parts = [draft.title, draft.preamble]
    for clause in draft.clauses:
        parts.append(clause.heading)
        parts.append(clause.text)
    return "\n".join(p for p in parts if p)


def _draft_text(draft: Draft) -> str:
    body = _body_text(draft)
    return f"{body}\n{draft.signature_block}" if draft.signature_block else body


def _locate(draft: Draft, pattern: re.Pattern[str]) -> str:
    for clause in draft.clauses:
        if pattern.search(clause.text) or pattern.search(clause.heading):
            return clause.heading
    if pattern.search(draft.preamble):
        return "Preamble"
    return "document"


# --------------------------------------------------------------------------
# the audit


def audit(
    draft: Draft,
    request: DocumentRequest,
    pdf_bytes: bytes,
    verification: VerificationResult | None = None,
) -> AuditReport:
    findings: list[Finding] = []
    text = _draft_text(draft)
    body = _body_text(draft)

    # 1. placeholders that survived into a binding document
    for match in dict.fromkeys(m.group(0) for m in PLACEHOLDER_RE.finditer(text)):
        findings.append(
            Finding(
                severity="block",
                code="PLACEHOLDER",
                message=f"Unfilled placeholder {match!r} is still in the document.",
                where=_locate(draft, re.compile(re.escape(match), re.I)),
            )
        )
    if RULE_LINE_RE.search(body):
        findings.append(
            Finding(
                severity="block",
                code="BLANK_TO_FILL_IN",
                message="A blank line to be filled in by hand is still in the binding text.",
                where=_locate(draft, RULE_LINE_RE),
            )
        )
    if BLANK_AMOUNT_RE.search(text):
        findings.append(
            Finding(
                severity="block",
                code="BLANK_AMOUNT",
                message="A monetary amount is blank or unresolved.",
                where=_locate(draft, BLANK_AMOUNT_RE),
            )
        )

    # 2. red flags
    for severity, code, message, pattern in RED_FLAGS:
        if pattern.search(text):
            findings.append(
                Finding(
                    severity=severity, code=code, message=message, where=_locate(draft, pattern)
                )
            )

    # 3. clauses a human would expect, and that are missing
    headings = "\n".join(c.heading for c in draft.clauses)
    for code, message, body_pattern, heading_pattern in REQUIRED_SECTIONS:
        if body_pattern.search(text) or heading_pattern.search(headings):
            continue
        findings.append(
            Finding(severity="warn", code=f"MISSING_{code}", message=message, where="document")
        )

    # 4. traceability - the clauses nobody asked for
    known = _known_sources(request)
    for clause in draft.clauses:
        trace = (clause.traces_to or "").strip()
        if trace and trace.lower() in known:
            continue
        binds = bool(OBLIGATION_RE.search(clause.text))
        findings.append(
            Finding(
                severity="warn" if binds else "info",
                code="UNREQUESTED_OBLIGATION" if binds else "UNREQUESTED_CLAUSE",
                message=(
                    "This clause creates an obligation that does not trace back to "
                    "anything you asked for."
                    if binds
                    else "This clause was added by the model and traces to nothing you said."
                ),
                where=clause.heading,
            )
        )

    # 5. assumptions the model made on the operator's behalf
    for assumption in request.assumptions:
        findings.append(
            Finding(
                severity="warn",
                code="ASSUMPTION",
                message=(
                    f"{assumption.field} was assumed to be {assumption.value!r} "
                    f"({assumption.why})."
                ),
                where=assumption.field,
            )
        )

    # 6. is the other side even real
    findings.extend(_verification_findings(request, verification))

    return AuditReport(
        document_sha256=sha256_hex(pdf_bytes),
        findings=findings,
        checked_at=utcnow(),
    )


def _known_sources(request: DocumentRequest) -> set[str]:
    known = {a.field.lower() for a in request.assumptions}
    known.update(k.lower() for k in request.terms)
    for word in re.findall(r"[A-Za-z][A-Za-z0-9'\-]{3,}", request.source_prompt):
        known.add(word.lower())
    return known


def _verification_findings(
    request: DocumentRequest, verification: VerificationResult | None
) -> Iterable[Finding]:
    counterparties = [p for p in request.parties if p.role != "sender"]
    if verification is None:
        yield Finding(
            severity="warn",
            code="UNVERIFIED_COUNTERPARTY",
            message=(
                "The other party was not checked against any external source. "
                "You are about to be bound to an entity nobody confirmed exists."
            ),
            where=counterparties[0].organisation if counterparties else "counterparty",
        )
        return
    if not verification.verified:
        # Lead with what was actually observed. "No evidence found" is true but
        # useless; "the domain does not resolve" is what a reader needs.
        observed = next(
            (e.value for e in verification.evidence if e.claim == "dns"),
            "",
        ) or next((e.value for e in verification.evidence), "")
        detail = f"The signer's domain {observed}." if observed else verification.note
        yield Finding(
            severity="block",
            code="COUNTERPARTY_NOT_FOUND",
            message=(
                f"{verification.counterparty or 'The counterparty'} could not be "
                f"confirmed to exist. {detail}".strip()
            ),
            where=verification.counterparty,
        )
        return
    for party in counterparties:
        domain = party.email.split("@")[-1].lower() if "@" in party.email else ""
        if not domain:
            continue
        if not any(
            domain in e.source_url.lower() or domain in e.value.lower()
            for e in verification.evidence
        ):
            yield Finding(
                severity="warn",
                code="EMAIL_DOMAIN_UNCONFIRMED",
                message=(
                    f"The signer's email domain {domain!r} does not appear in any source "
                    f"that confirms {verification.counterparty!r}."
                ),
                where=party.email,
            )
