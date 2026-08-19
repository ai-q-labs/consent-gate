"""Core data structures shared across the pipeline.

Everything that crosses a stage boundary is a frozen dataclass that can be
serialised to plain JSON.  The audit ledger stores those JSON blobs verbatim,
so a reviewer can replay any run without the original process being alive.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """Stable JSON used wherever a value gets hashed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _asdict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _asdict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_asdict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


@dataclass(frozen=True)
class Party:
    """A human who will be asked to sign.  Never the agent."""

    role: str  # "sender" | "counterparty"
    first_name: str
    last_name: str
    email: str
    organisation: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(frozen=True)
class Assumption:
    """Something the model filled in that the human never said.

    The whole point of stage 2 is that these are enumerated rather than
    silently folded into the draft.  Stage 6 refuses to let an assumption
    become a binding obligation without the human seeing it first.
    """

    field: str
    value: str
    why: str


@dataclass(frozen=True)
class DocumentRequest:
    """Structured intent extracted from the plain prompt."""

    doc_type: str
    title: str
    parties: list[Party]
    terms: dict[str, str]
    assumptions: list[Assumption] = field(default_factory=list)
    source_prompt: str = ""

    def to_json(self) -> dict[str, Any]:
        return _asdict(self)


@dataclass(frozen=True)
class Evidence:
    """A fact about a counterparty, with the URL it came from."""

    claim: str
    value: str
    source_url: str
    source_title: str = ""


@dataclass(frozen=True)
class VerificationResult:
    counterparty: str
    verified: bool
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return _asdict(self)


@dataclass(frozen=True)
class Clause:
    """One numbered clause of the drafted document."""

    heading: str
    text: str
    # Where this clause came from: a phrase from the prompt, an assumption id,
    # or "" when the model produced it from nothing.  Stage 6 keys off this.
    traces_to: str = ""


@dataclass(frozen=True)
class Draft:
    title: str
    preamble: str
    clauses: list[Clause]
    signature_block: str = ""

    def to_json(self) -> dict[str, Any]:
        return _asdict(self)


@dataclass(frozen=True)
class Finding:
    """One thing the gate wants a human to look at before signing."""

    severity: str  # "block" | "warn" | "info"
    code: str
    message: str
    where: str = ""

    def to_json(self) -> dict[str, Any]:
        return _asdict(self)


@dataclass(frozen=True)
class AuditReport:
    """The verdict on one exact sequence of PDF bytes."""

    document_sha256: str
    findings: list[Finding]
    checked_at: str

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def to_json(self) -> dict[str, Any]:
        return _asdict(self)
