"""The gate's guarantees, as tests.

Run with:  python -m unittest discover -s tests
No API keys, no network, no third-party packages.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consent_gate.gate import ConsentGate, GateError, sha256_file  # noqa: E402
from consent_gate.ledger import Ledger  # noqa: E402
from consent_gate.models import AuditReport, Finding, sha256_hex  # noqa: E402


def report_for(data: bytes, findings: list[Finding] | None = None) -> AuditReport:
    return AuditReport(
        document_sha256=sha256_hex(data),
        findings=findings or [],
        checked_at="2026-01-01T00:00:00+00:00",
    )


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_chain_verifies(self) -> None:
        for i in range(5):
            self.ledger.append("test.event", {"i": i})
        ok, reason = self.ledger.verify()
        self.assertTrue(ok, reason)
        self.assertIn("5 entries", reason)

    def test_first_entry_links_to_genesis(self) -> None:
        entry = self.ledger.append("first", {})
        self.assertEqual(entry["prev"], "0" * 64)
        self.assertEqual(entry["seq"], 0)

    def test_editing_a_line_breaks_the_chain(self) -> None:
        self.ledger.append("a", {"value": "original"})
        self.ledger.append("b", {})
        lines = self.ledger.path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["data"]["value"] = "tampered"
        lines[0] = json.dumps(entry, ensure_ascii=False)
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, reason = self.ledger.verify()
        self.assertFalse(ok)
        self.assertIn("edited after the fact", reason)

    def test_deleting_a_line_breaks_the_chain(self) -> None:
        for i in range(3):
            self.ledger.append("e", {"i": i})
        lines = self.ledger.path.read_text(encoding="utf-8").splitlines()
        del lines[1]
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, _ = self.ledger.verify()
        self.assertFalse(ok)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pdf = root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-1.7 pretend this is a contract")
        self.gate = ConsentGate(Ledger(root / "ledger.jsonl"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- the headline guarantee ------------------------------------------

    def test_send_is_refused_without_an_approval(self) -> None:
        with self.assertRaises(GateError) as ctx:
            self.gate.assert_authorised(self.pdf)
        self.assertIn("no human authorisation", str(ctx.exception))

    def test_approval_lets_the_document_through(self) -> None:
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes()))
        self.gate.approve(packet.document_sha256, packet.nonce, "A Human")
        entry = self.gate.assert_authorised(self.pdf)
        self.assertEqual(entry["data"]["approver"], "A Human")

    def test_one_changed_byte_invalidates_the_approval(self) -> None:
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes()))
        self.gate.approve(packet.document_sha256, packet.nonce, "A Human")
        self.gate.assert_authorised(self.pdf)  # fine so far

        self.pdf.write_bytes(self.pdf.read_bytes() + b" ")

        with self.assertRaises(GateError):
            self.gate.assert_authorised(self.pdf)

    # -- the out-of-band token -------------------------------------------

    def test_wrong_token_cannot_approve(self) -> None:
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes()))
        with self.assertRaises(GateError) as ctx:
            self.gate.approve(packet.document_sha256, "0" * 32, "Impostor")
        self.assertIn("token does not match", str(ctx.exception))

    def test_the_plaintext_token_is_never_written_to_disk(self) -> None:
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes()))
        on_disk = self.gate.ledger.path.read_text(encoding="utf-8")
        self.assertNotIn(packet.nonce, on_disk)
        self.assertIn(sha256_hex(packet.nonce.encode("ascii")), on_disk)

    def test_approval_without_a_review_is_refused(self) -> None:
        with self.assertRaises(GateError) as ctx:
            self.gate.approve(sha256_file(self.pdf), "whatever", "A Human")
        self.assertIn("no open review", str(ctx.exception))

    # -- blocking findings ------------------------------------------------

    def test_blocking_findings_stop_approval_unless_overridden(self) -> None:
        finding = Finding(severity="block", code="PLACEHOLDER", message="x", where="y")
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes(), [finding]))

        with self.assertRaises(GateError) as ctx:
            self.gate.approve(packet.document_sha256, packet.nonce, "A Human")
        self.assertIn("blocking finding", str(ctx.exception))

        entry = self.gate.approve(
            packet.document_sha256, packet.nonce, "A Human", override_reason="counsel reviewed"
        )
        self.assertEqual(entry["data"]["override_reason"], "counsel reviewed")

    # -- rejection is sticky ----------------------------------------------

    def test_rejection_survives_a_later_approval_attempt(self) -> None:
        packet = self.gate.open_review(self.pdf, report_for(self.pdf.read_bytes()))
        self.gate.reject(packet.document_sha256, "A Human", "wrong counterparty")
        with self.assertRaises(GateError) as ctx:
            self.gate.assert_authorised(self.pdf)
        self.assertIn("rejected", str(ctx.exception))

    # -- report/file mismatch ---------------------------------------------

    def test_review_refuses_a_report_about_different_bytes(self) -> None:
        stale = report_for(b"some other document")
        with self.assertRaises(GateError) as ctx:
            self.gate.open_review(self.pdf, stale)
        self.assertIn("different bytes", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
