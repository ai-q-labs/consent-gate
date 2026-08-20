"""The consent gate.

This module is the reason the project exists.  Everything else - intent
extraction, drafting, rendering - is ordinary plumbing that many agents
already do.  What is usually missing is a boundary that a program cannot walk
through on its own.

Three properties, in increasing order of how much they actually hold:

1. **Hash binding.**  An approval authorises one exact sequence of bytes,
   identified by its SHA-256.  `assert_authorised` re-hashes the file that is
   about to be sent.  Re-render the document, change one comma, and the
   approval no longer applies.  This closes the time-of-check/time-of-use gap
   that a "click OK" dialog leaves wide open.

2. **Out-of-band nonce.**  `review` prints a random token to the terminal and
   stores only its hash.  `approve` will not mint an authorisation without the
   token.  A process that follows this module's API cannot approve its own
   work, because it never gets to see the plaintext.

3. **Append-only ledger.**  Every step is chained by hash.  Even if 1 and 2
   are defeated, the record of *which bytes a human authorised, and when*
   cannot be quietly rewritten afterwards.

Honest threat model: (2) is defeated by anything that can read the operator's
terminal, and all three are defeated by editing this file.  The guarantee that
survives both is (1) plus (3): after the fact you can always prove what was
authorised and what was sent, and whether they were the same thing.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledger import Ledger, utcnow
from .models import AuditReport, display_path, sha256_hex

EVENT_REVIEW = "review.requested"
EVENT_APPROVE = "human.authorised"
EVENT_REJECT = "human.rejected"
EVENT_SEND = "esign.dispatched"


class GateError(RuntimeError):
    """Raised whenever something tries to send an unauthorised document."""


@dataclass(frozen=True)
class ReviewPacket:
    document_sha256: str
    nonce: str  # shown once, on the terminal, never written to disk
    report: AuditReport


class ConsentGate:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    # --------------------------------------------------------------- review

    def open_review(self, pdf_path: Path, report: AuditReport) -> ReviewPacket:
        digest = sha256_file(pdf_path)
        if digest != report.document_sha256:
            raise GateError(
                "the audit report describes different bytes than the file on disk; "
                "re-run the audit"
            )
        nonce = secrets.token_hex(16)
        self.ledger.append(
            EVENT_REVIEW,
            {
                "document_sha256": digest,
                "document_path": str(pdf_path),
                "nonce_sha256": sha256_hex(nonce.encode("ascii")),
                "findings": [f.to_json() for f in report.findings],
                "blocking": len(report.blocking),
                "warnings": len(report.warnings),
            },
        )
        return ReviewPacket(document_sha256=digest, nonce=nonce, report=report)

    # -------------------------------------------------------------- approve

    def approve(
        self,
        document_sha256: str,
        nonce: str,
        approver: str,
        override_reason: str = "",
    ) -> dict[str, Any]:
        review = self._latest_review(document_sha256)
        if review is None:
            raise GateError(
                f"no open review for {document_sha256[:12]}...; run `consent-gate review` first"
            )
        if sha256_hex(nonce.encode("ascii")) != review["data"]["nonce_sha256"]:
            raise GateError("review token does not match; approval refused")
        if review["data"]["blocking"] and not override_reason:
            raise GateError(
                f"{review['data']['blocking']} blocking finding(s) are unresolved; "
                "pass --override-reason to authorise anyway"
            )
        return self.ledger.append(
            EVENT_APPROVE,
            {
                "document_sha256": document_sha256,
                "approver": approver,
                "at": utcnow(),
                "override_reason": override_reason,
                "acknowledged_findings": review["data"]["findings"],
            },
        )

    def reject(self, document_sha256: str, approver: str, reason: str) -> dict[str, Any]:
        return self.ledger.append(
            EVENT_REJECT,
            {"document_sha256": document_sha256, "approver": approver, "reason": reason},
        )

    # ----------------------------------------------------------- enforcement

    def assert_authorised(self, pdf_path: Path) -> dict[str, Any]:
        """The only door to the eSign client.  Re-hashes the actual bytes."""
        digest = sha256_file(pdf_path)
        for entry in reversed(list(self.ledger.entries())):
            if entry["event"] == EVENT_REJECT and entry["data"]["document_sha256"] == digest:
                raise GateError("this document was explicitly rejected by a human")
            if entry["event"] == EVENT_APPROVE and entry["data"]["document_sha256"] == digest:
                return entry
        raise GateError(
            f"refusing to send: no human authorisation for sha256:{digest[:12]}...\n"
            f"  file      {display_path(pdf_path)}\n"
            "  Any edit to the document invalidates an earlier approval by design."
        )

    # --------------------------------------------------------------- helpers

    def _latest_review(self, document_sha256: str) -> dict[str, Any] | None:
        found = None
        for entry in self.ledger.entries():
            if (
                entry["event"] == EVENT_REVIEW
                and entry["data"]["document_sha256"] == document_sha256
            ):
                found = entry
        return found


def sha256_file(path: Path) -> str:
    return sha256_hex(Path(path).read_bytes())
