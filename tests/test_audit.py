"""Audit, intent validation, response parsing and rendering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consent_gate.audit import audit  # noqa: E402
from consent_gate.draft import to_html  # noqa: E402
from consent_gate.intent import _to_request  # noqa: E402
from consent_gate.llm import LLMError, extract_json  # noqa: E402
from consent_gate import verify as verify_module  # noqa: E402
from consent_gate.models import (  # noqa: E402
    Assumption,
    Clause,
    DocumentRequest,
    Draft,
    Evidence,
    Party,
    VerificationResult,
)

PDF = b"%PDF-1.7 bytes"

SENDER = Party("sender", "Kazuma", "Sato", "sender@example.com", "Ai-Q Labs")
SIGNER = Party("counterparty", "Alice", "Nakamura", "alice@northwind-labs.example", "Northwind Labs")


def make_request(**kwargs) -> DocumentRequest:
    base = dict(
        doc_type="mutual-nda",
        title="Mutual Non-Disclosure Agreement",
        parties=[SENDER, SIGNER],
        terms={"term_length": "2 years"},
        assumptions=[],
        source_prompt="Draft a mutual NDA with Northwind Labs, two-year term.",
    )
    base.update(kwargs)
    return DocumentRequest(**base)  # type: ignore[arg-type]


def make_draft(clauses: list[Clause]) -> Draft:
    return Draft(
        title="Mutual Non-Disclosure Agreement",
        preamble="Between the parties.",
        clauses=clauses,
        signature_block="Signed by the parties.",
    )


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


class AuditTests(unittest.TestCase):
    def test_placeholder_blocks(self) -> None:
        draft = make_draft(
            [Clause("Fee", "The fee is [TBD] per month.", "term_length")]
        )
        report = audit(draft, make_request(), PDF)
        self.assertIn("PLACEHOLDER", codes(report))
        self.assertTrue(report.blocking)

    def test_a_blank_line_in_a_clause_blocks(self) -> None:
        draft = make_draft(
            [Clause("Fee", "The monthly fee is ______________ per month.", "term_length")]
        )
        report = audit(draft, make_request(), PDF)
        self.assertIn("BLANK_TO_FILL_IN", codes(report))
        self.assertTrue(report.blocking)

    def test_signature_lines_are_not_treated_as_blanks(self) -> None:
        # A real model draft put "______________________________" in the signature
        # block; underscores belong there and must not block the document.
        draft = Draft(
            title="Mutual Non-Disclosure Agreement",
            preamble="Between the parties.",
            clauses=[Clause("Term", "The term is two years.", "term_length")],
            signature_block="______________________________\nAi-Q Labs\n\n______________________________\nNorthwind Labs",
        )
        report = audit(draft, make_request(), PDF)
        self.assertNotIn("BLANK_TO_FILL_IN", codes(report))
        self.assertNotIn("PLACEHOLDER", codes(report))

    def test_bracketed_placeholders_still_block_in_the_signature_block(self) -> None:
        draft = Draft(
            title="Mutual Non-Disclosure Agreement",
            preamble="Between the parties.",
            clauses=[Clause("Term", "The term is two years.", "term_length")],
            signature_block="Signed by [INSERT NAME HERE]",
        )
        self.assertIn("PLACEHOLDER", codes(audit(draft, make_request(), PDF)))

    def test_blank_amount_blocks(self) -> None:
        draft = make_draft([Clause("Fee", "The monthly fee is $____.", "term_length")])
        report = audit(draft, make_request(), PDF)
        self.assertIn("BLANK_AMOUNT", codes(report))

    def test_uncapped_liability_blocks(self) -> None:
        draft = make_draft(
            [Clause("Liability", "The provider accepts unlimited liability.", "term_length")]
        )
        report = audit(draft, make_request(), PDF)
        self.assertIn("UNCAPPED_LIABILITY", codes(report))
        self.assertEqual(report.blocking[0].where, "Liability")

    def test_auto_renewal_warns(self) -> None:
        draft = make_draft(
            [Clause("Renewal", "This agreement will automatically renew each year.", "term_length")]
        )
        self.assertIn("AUTO_RENEWAL", codes(audit(draft, make_request(), PDF)))

    def test_untraced_clause_that_binds_is_an_obligation_warning(self) -> None:
        draft = make_draft(
            [
                Clause("Term", "The term is two years.", "term_length"),
                Clause(
                    "Non-Solicitation",
                    "Neither party shall solicit the other party's employees.",
                    "",
                ),
            ]
        )
        report = audit(draft, make_request(), PDF)
        self.assertIn("UNREQUESTED_OBLIGATION", codes(report))
        finding = next(f for f in report.findings if f.code == "UNREQUESTED_OBLIGATION")
        self.assertEqual(finding.where, "Non-Solicitation")
        self.assertEqual(finding.severity, "warn")

    def test_untraced_clause_without_obligation_is_only_info(self) -> None:
        draft = make_draft(
            [Clause("Recitals", "The parties have met and spoken.", "")]
        )
        report = audit(draft, make_request(), PDF)
        self.assertIn("UNREQUESTED_CLAUSE", codes(report))
        finding = next(f for f in report.findings if f.code == "UNREQUESTED_CLAUSE")
        self.assertEqual(finding.severity, "info")

    def test_a_clause_tracing_to_a_prompt_word_is_not_flagged(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "Northwind")])
        self.assertNotIn("UNREQUESTED_OBLIGATION", codes(audit(draft, make_request(), PDF)))

    def test_a_clause_tracing_to_an_assumption_field_is_not_flagged(self) -> None:
        request = make_request(
            assumptions=[Assumption("governing_law", "Delaware", "not stated")]
        )
        draft = make_draft(
            [Clause("Governing Law", "Governed by the laws of Delaware.", "governing_law")]
        )
        report = audit(draft, request, PDF)
        self.assertNotIn("UNREQUESTED_CLAUSE", codes(report))
        self.assertIn("ASSUMPTION", codes(report))

    def test_every_assumption_produces_a_warning(self) -> None:
        request = make_request(
            assumptions=[
                Assumption("governing_law", "Delaware", "not stated"),
                Assumption("notice_period", "30 days", "not stated"),
            ]
        )
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        report = audit(draft, request, PDF)
        self.assertEqual(len([f for f in report.findings if f.code == "ASSUMPTION"]), 2)

    def test_missing_standard_sections_warn(self) -> None:
        draft = make_draft([Clause("Purpose", "To exchange information.", "term_length")])
        found = codes(audit(draft, make_request(), PDF))
        self.assertIn("MISSING_GOVERNING_LAW", found)
        self.assertIn("MISSING_TERMINATION", found)

    def test_a_clause_heading_satisfies_a_required_section(self) -> None:
        # A real model produced a clause headed "Effective Date and Term" whose
        # text never used the phrase "term of this agreement". That is a term
        # clause, and flagging it as missing is a false positive.
        draft = make_draft(
            [
                Clause(
                    "Effective Date and Term",
                    "This agreement starts on 1 September 2026 and runs for two years.",
                    "term_length",
                )
            ]
        )
        self.assertNotIn("MISSING_TERM", codes(audit(draft, make_request(), PDF)))

    def test_no_verification_warns_rather_than_passing_silently(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        report = audit(draft, make_request(), PDF, verification=None)
        self.assertIn("UNVERIFIED_COUNTERPARTY", codes(report))

    def test_counterparty_not_found_blocks(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        verification = VerificationResult("Northwind Labs", verified=False, note="nothing found")
        report = audit(draft, make_request(), PDF, verification)
        self.assertIn("COUNTERPARTY_NOT_FOUND", codes(report))
        self.assertTrue(report.blocking)

    def test_email_domain_absent_from_evidence_warns(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        verification = VerificationResult(
            "Northwind Labs",
            verified=True,
            evidence=[Evidence("website", "northwind.test", "https://northwind.test", "")],
        )
        report = audit(draft, make_request(), PDF, verification)
        self.assertIn("EMAIL_DOMAIN_UNCONFIRMED", codes(report))

    def test_report_is_bound_to_the_pdf_bytes(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        a = audit(draft, make_request(), b"one")
        b = audit(draft, make_request(), b"two")
        self.assertNotEqual(a.document_sha256, b.document_sha256)


class IntentTests(unittest.TestCase):
    def test_missing_counterparty_is_refused(self) -> None:
        raw = {
            "doc_type": "nda",
            "title": "NDA",
            "parties": [{"role": "sender", "first_name": "K", "last_name": "S", "email": "k@e.com"}],
        }
        with self.assertRaises(ValueError) as ctx:
            _to_request(raw, "prompt")
        self.assertIn("no counterparty", str(ctx.exception))

    def test_signer_without_an_email_is_refused(self) -> None:
        raw = {
            "doc_type": "nda",
            "title": "NDA",
            "parties": [
                {"role": "counterparty", "first_name": "A", "last_name": "N", "email": "not-an-email"}
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            _to_request(raw, "prompt")
        self.assertIn("refusing to guess", str(ctx.exception))


class DomainCheckTests(unittest.TestCase):
    """The keyless half of stage 3. No network: DNS is stubbed."""

    def test_blank_or_bare_names_are_not_domains(self) -> None:
        self.assertEqual(verify_module.inspect_domain(""), [])
        self.assertEqual(verify_module.inspect_domain("localhost"), [])

    def test_a_domain_that_does_not_resolve_is_reported(self) -> None:
        original = verify_module.socket.getaddrinfo

        def boom(*_args, **_kwargs):
            raise verify_module.socket.gaierror(11001, "getaddrinfo failed")

        verify_module.socket.getaddrinfo = boom
        try:
            evidence = verify_module.inspect_domain("northwind-labs.example")
        finally:
            verify_module.socket.getaddrinfo = original

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].claim, "dns")
        self.assertIn("does not resolve", evidence[0].value)

    def test_an_unresolvable_domain_makes_the_counterparty_unverified(self) -> None:
        original = verify_module.socket.getaddrinfo

        def boom(*_args, **_kwargs):
            raise verify_module.socket.gaierror(11001, "getaddrinfo failed")

        verify_module.socket.getaddrinfo = boom
        try:
            result = verify_module.verify_counterparty(
                "Northwind Labs", "northwind-labs.example", api_key=""
            )
        finally:
            verify_module.socket.getaddrinfo = original

        self.assertFalse(result.verified)
        # and that is a blocking finding, not a note nobody reads
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        self.assertIn("COUNTERPARTY_NOT_FOUND", codes(audit(draft, make_request(), PDF, result)))

    def test_certificate_timestamps_parse(self) -> None:
        parsed = verify_module._parse_cert_time("Jun 10 00:00:00 2026 GMT")
        assert parsed is not None
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 6, 10))
        self.assertIsNone(verify_module._parse_cert_time("not a date"))
        self.assertIsNone(verify_module._parse_cert_time(None))


class ResponseParsingTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_a_leading_sentence(self) -> None:
        self.assertEqual(extract_json('Sure, here you go:\n{"a": 1}'), {"a": 1})

    def test_no_json_at_all_raises(self) -> None:
        with self.assertRaises(LLMError):
            extract_json("I would rather not.")


class RenderingTests(unittest.TestCase):
    def test_signature_text_tags_are_emitted_per_signer(self) -> None:
        draft = make_draft([Clause("Term", "Two years.", "term_length")])
        html = to_html(draft, make_request())
        self.assertIn("[sig|req|signer1]", html)
        self.assertIn("[date|req|signer1]", html)
        self.assertNotIn("[sig|req|signer2]", html)

    def test_content_is_escaped(self) -> None:
        draft = make_draft([Clause("Term", "5 < 6 & 7 > 2", "term_length")])
        html = to_html(draft, make_request())
        self.assertIn("5 &lt; 6 &amp; 7 &gt; 2", html)


if __name__ == "__main__":
    unittest.main()
