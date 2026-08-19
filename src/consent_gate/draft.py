"""Stage 4 - structured intent to a clause-by-clause draft.

Each clause must declare what it traces back to.  That single extra field is
what lets stage 6 tell the difference between "you asked for a two-year term"
and "the model felt a two-year term was customary".  Both end up in the
document; only one of them should end up binding you without comment.
"""

from __future__ import annotations

from typing import Any

from .llm import Backend
from .models import Clause, Draft, DocumentRequest, VerificationResult

INSTRUCTION = """draft-document
You draft a plain-English legal document from structured intent.

Return JSON with exactly these keys:
  title             the document title
  preamble          the opening paragraph naming the parties and the date
  clauses           array of {heading, text, traces_to}
  signature_block   the closing text above the signature lines

`traces_to` must be ONE of:
  - a word or short phrase copied verbatim from the original request
  - the exact `field` name of one of the declared assumptions
  - an empty string, if this clause traces to neither

Do not stretch `traces_to`. An empty string is the correct, honest answer for
boilerplate you added on your own initiative, and the reviewer is shown those
clauses separately. A false trace is worse than an empty one.

Write in clear modern English. Do not leave placeholders, blanks, or bracketed
TODOs anywhere in the text - if a value is missing, say so plainly in the
clause instead of inserting a blank to be filled in later.

`signature_block` is the closing sentence above the signatures - one or two
lines of prose. Do not draw signature lines, do not add underscores, and do not
list the parties again: the signature fields are generated separately and
placed by the signing platform. Do not attempt to sign anything yourself."""


def draft_document(
    backend: Backend,
    request: DocumentRequest,
    verification: VerificationResult | None = None,
) -> Draft:
    payload = {
        "request": request.to_json(),
        "counterparty_verification": verification.to_json() if verification else None,
    }
    raw = backend.complete_json(INSTRUCTION, _dump(payload))
    return _to_draft(raw)


def _dump(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _to_draft(raw: dict[str, Any]) -> Draft:
    clauses = [
        Clause(
            heading=str(c.get("heading", "")).strip(),
            text=str(c.get("text", "")).strip(),
            traces_to=str(c.get("traces_to", "")).strip(),
        )
        for c in raw.get("clauses", [])
    ]
    if not clauses:
        raise ValueError("the model returned a document with no clauses")
    return Draft(
        title=str(raw.get("title", "Agreement")).strip(),
        preamble=str(raw.get("preamble", "")).strip(),
        clauses=clauses,
        signature_block=str(raw.get("signature_block", "")).strip(),
    )


# --------------------------------------------------------------------------
# rendering to HTML, which is what the Foxit PDF Services API consumes


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 22mm 20mm; }}
  body {{ font-family: Georgia, "Times New Roman", serif; font-size: 11pt;
         line-height: 1.55; color: #111; }}
  h1 {{ font-size: 16pt; text-align: center; margin: 0 0 6mm; }}
  .preamble {{ margin: 0 0 8mm; }}
  ol.clauses {{ padding-left: 6mm; }}
  ol.clauses > li {{ margin: 0 0 5mm; }}
  .heading {{ font-weight: bold; }}
  .sig {{ margin-top: 14mm; white-space: pre-wrap; }}
  .sigline {{ margin-top: 10mm; }}
  .sigline div {{ border-top: 1px solid #111; width: 70mm; padding-top: 2mm;
                  margin-bottom: 8mm; font-size: 9pt; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="preamble">{preamble}</p>
<ol class="clauses">
{clauses}
</ol>
<div class="sig">{signature_block}</div>
<div class="sigline">
{signature_lines}
</div>
</body>
</html>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def to_html(draft: Draft, request: DocumentRequest) -> str:
    """Render the draft as HTML, with Foxit eSign text tags on the signature lines.

    ``processTextTags`` on the eSign side turns ``[sig|req|signer1]`` into a real
    signature field bound to the first party, so the field placement lives in
    the document rather than in hard-coded coordinates.
    """
    clauses_html = "\n".join(
        f'  <li><span class="heading">{_escape(c.heading)}.</span> {_escape(c.text)}</li>'
        for c in draft.clauses
    )
    signers = [p for p in request.parties if p.role != "sender"] or request.parties
    lines = []
    for index, party in enumerate(signers, start=1):
        label = _escape(party.full_name or party.email)
        org = f" &mdash; {_escape(party.organisation)}" if party.organisation else ""
        lines.append(
            f"  <p>[sig|req|signer{index}]</p>\n"
            f"  <div>{label}{org}<br>Date: [date|req|signer{index}]</div>"
        )
    return HTML_TEMPLATE.format(
        title=_escape(draft.title),
        preamble=_escape(draft.preamble),
        clauses=clauses_html,
        signature_block=_escape(draft.signature_block),
        signature_lines="\n".join(lines),
    )
