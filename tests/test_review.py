"""The second opinion can add concerns but can never raise or clear a block."""

from __future__ import annotations

import unittest

from consent_gate.llm import Backend, LLMError
from consent_gate.models import Clause, Draft, DocumentRequest
from consent_gate.review import MAX_FINDINGS, _to_findings, second_opinion


class _Canned(Backend):
    name = "canned"

    def __init__(self, answer=None, error: str | None = None) -> None:
        self.answer = answer
        self.error = error

    def complete_json(self, instruction, payload):
        if self.error:
            raise LLMError(self.error)
        return self.answer


def _request() -> DocumentRequest:
    return DocumentRequest(
        doc_type="mutual-nda",
        title="Mutual NDA",
        parties=[],
        terms={},
        assumptions=[],
        source_prompt="Draft a mutual NDA.",
    )


def _draft() -> Draft:
    return Draft(title="Mutual NDA", preamble="", clauses=[Clause(heading="Term", text="Two years.")])


class ReviewTests(unittest.TestCase):
    def test_block_from_reviewer_is_downgraded_to_warn(self):
        found = _to_findings({"findings": [{"severity": "block", "clause": "Term", "concern": "Too long."}]})
        self.assertEqual([f.severity for f in found], ["warn"])
        self.assertEqual(found[0].code, "REVIEWER")

    def test_only_warn_and_info_survive(self):
        raw = {"findings": [{"severity": s, "concern": "x"} for s in ("warn", "info", "BLOCK", "critical")]}
        self.assertEqual({f.severity for f in _to_findings(raw)}, {"warn", "info"})

    def test_findings_are_capped(self):
        raw = {"findings": [{"severity": "info", "concern": f"c{i}"} for i in range(30)]}
        self.assertEqual(len(_to_findings(raw)), MAX_FINDINGS)

    def test_malformed_answers_yield_nothing(self):
        for raw in (None, [], {"findings": "no"}, {"findings": [1, "x", {"concern": ""}]}):
            self.assertEqual(_to_findings(raw), [])

    def test_reviewer_failure_is_informational_not_blocking(self):
        found = second_opinion(_Canned(error="timeout"), _request(), _draft())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "info")
        self.assertEqual(found[0].code, "REVIEWER_UNAVAILABLE")

    def test_empty_review_adds_nothing(self):
        self.assertEqual(second_opinion(_Canned(answer={"findings": []}), _request(), _draft()), [])


if __name__ == "__main__":
    unittest.main()
