"""Stage 2 - plain prompt to structured intent.

The model is asked for two things, and the second is the important one:

1.  The request it thinks you made.
2.  Every value it had to invent to make that request complete.

Most prompt-to-document pipelines only ask for (1), which is how a throwaway
sentence turns into a two-year auto-renewing term nobody chose.  Here the
invented values are first-class objects that travel all the way to the review
screen, and the audit raises one warning per assumption.
"""

from __future__ import annotations

from typing import Any

from .llm import Backend
from .models import Assumption, DocumentRequest, Party

INSTRUCTION = """extract-intent
You convert a plain-language request for a document into structured intent.

Return JSON with exactly these keys:
  doc_type      short slug, e.g. "mutual-nda", "consulting-agreement", "sow"
  title         the document's title as it should appear on page 1
  parties       array of {role, first_name, last_name, email, organisation}
                role is "sender" for the person making the request and
                "counterparty" for everyone who must sign
  terms         object of term name -> value, ONLY for things the request
                actually states (dates, amounts, durations, scope)
  assumptions   array of {field, value, why} for EVERY value you supplied that
                the request did not state. Be exhaustive. If you chose a
                governing law, a notice period, a term length, a confidentiality
                duration or a liability cap that the request is silent about,
                it belongs here. An empty array means the request specified
                everything, which is almost never true.

Never put an invented value in `terms`. `terms` is what the human said;
`assumptions` is what you added."""


def extract_intent(backend: Backend, prompt: str) -> DocumentRequest:
    raw = backend.complete_json(INSTRUCTION, prompt)
    return _to_request(raw, prompt)


def _to_request(raw: dict[str, Any], prompt: str) -> DocumentRequest:
    parties = [
        Party(
            role=str(p.get("role", "counterparty")),
            first_name=str(p.get("first_name", "")).strip(),
            last_name=str(p.get("last_name", "")).strip(),
            email=str(p.get("email", "")).strip(),
            organisation=str(p.get("organisation", "")).strip(),
        )
        for p in raw.get("parties", [])
    ]
    assumptions = [
        Assumption(
            field=str(a.get("field", "")),
            value=str(a.get("value", "")),
            why=str(a.get("why", "")),
        )
        for a in raw.get("assumptions", [])
    ]
    terms = {str(k): str(v) for k, v in (raw.get("terms") or {}).items()}
    request = DocumentRequest(
        doc_type=str(raw.get("doc_type", "document")),
        title=str(raw.get("title", "Agreement")),
        parties=parties,
        terms=terms,
        assumptions=assumptions,
        source_prompt=prompt,
    )
    _validate(request)
    return request


def _validate(request: DocumentRequest) -> None:
    signers = [p for p in request.parties if p.role != "sender"]
    if not signers:
        raise ValueError(
            "no counterparty was identified - the pipeline refuses to build a "
            "document with nobody to sign it"
        )
    for party in signers:
        if "@" not in party.email:
            raise ValueError(
                f"signer {party.full_name or party.role!r} has no usable email address "
                f"({party.email!r}); refusing to guess one"
            )
