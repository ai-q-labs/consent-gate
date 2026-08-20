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
    # A model that has nothing to put in a field returns JSON null, and
    # str(None) is the four-character string "None" - which then prints on the
    # signature line of a real document as "Kazuma Sato - None".
    def text(value: Any, default: str = "") -> str:
        return default if value is None else str(value).strip()

    parties = [
        Party(
            role=text(p.get("role"), "counterparty"),
            first_name=text(p.get("first_name")),
            last_name=text(p.get("last_name")),
            email=text(p.get("email")),
            organisation=text(p.get("organisation")),
        )
        for p in raw.get("parties", [])
    ]
    assumptions = [
        Assumption(
            field=text(a.get("field")),
            value=text(a.get("value"), "(left blank)"),
            why=text(a.get("why")),
        )
        for a in raw.get("assumptions", [])
    ]
    terms = {str(k): text(v) for k, v in (raw.get("terms") or {}).items()}
    request = DocumentRequest(
        doc_type=text(raw.get("doc_type"), "document"),
        title=text(raw.get("title"), "Agreement"),
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
