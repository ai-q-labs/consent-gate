"""Command line interface.

Five verbs, and the split between them is the design:

    consent-gate draft "..."     stages 2-6. Ends at the gate, never past it.
    consent-gate approve         the human, out of band, with the review token.
    consent-gate send            stage 8. Refuses unless the bytes were approved.
    consent-gate collect         fetch the signed document back once a human signed.
    consent-gate ledger          replay and verify the chain afterwards.

`draft` cannot send. `send` cannot approve. That separation is not politeness -
it is the only reason the word "authorisation" means anything here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from .audit import audit as run_audit
from .draft import draft_document, to_html
from .env import load_env
from .foxit import Credentials, ESign, FoxitError, PdfServices, party_payload
from .gate import ConsentGate, GateError, sha256_file
from .intent import extract_intent
from .ledger import Ledger
from .llm import LLMError, get_backend
from .models import (
    AuditReport,
    DocumentRequest,
    Draft,
    Finding,
    Party,
    VerificationResult,
    display_path,
)
from .verify import VerificationError, serpapi_available, verify_counterparty

# Before anything reads the environment: .env.example tells the reader to copy
# it to .env, so .env has to actually take effect. Looked up from the cwd
# first, then from the installed package, so it works from a clone and from an
# editable install alike.
if not load_env():
    load_env(Path(__file__).resolve().parent)

DEFAULT_WORKSPACE = Path(os.environ.get("CONSENT_GATE_WORKSPACE", "workspace"))

SEVERITY_MARK = {"block": "BLOCK", "warn": "WARN ", "info": "info "}


# --------------------------------------------------------------------------
# small helpers


def _workspace(args: argparse.Namespace) -> Path:
    path = Path(args.workspace)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger(args: argparse.Namespace) -> Ledger:
    return Ledger(_workspace(args) / "ledger.jsonl")


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _print_findings(findings: list[Finding]) -> None:
    if not findings:
        print("  (nothing flagged)")
        return
    order = {"block": 0, "warn": 1, "info": 2}
    for finding in sorted(findings, key=lambda f: order.get(f.severity, 3)):
        mark = SEVERITY_MARK.get(finding.severity, finding.severity)
        print(f"  [{mark}] {finding.code:<24} {finding.where}")
        print(f"          {finding.message}")


def _rehydrate_request(data: dict) -> DocumentRequest:
    from .models import Assumption

    return DocumentRequest(
        doc_type=data["doc_type"],
        title=data["title"],
        parties=[Party(**p) for p in data["parties"]],
        terms=data.get("terms", {}),
        assumptions=[Assumption(**a) for a in data.get("assumptions", [])],
        source_prompt=data.get("source_prompt", ""),
    )


# --------------------------------------------------------------------------
# draft


def cmd_draft(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    ledger = _ledger(args)
    gate = ConsentGate(ledger)
    backend = get_backend(args.backend)

    ledger.append("run.started", {"prompt": args.prompt, "backend": backend.name})

    print(f"[1/6] intent        ({backend.name})")
    request = extract_intent(backend, args.prompt)
    _write_json(workspace / "intent.json", request.to_json())
    ledger.append("intent.extracted", request.to_json())
    print(f"      {request.doc_type}: {request.title}")
    print(f"      {len(request.assumptions)} value(s) the model supplied on your behalf")

    verification: VerificationResult | None = None
    if args.verify_counterparty:
        org = next(
            (p.organisation for p in request.parties if p.role != "sender" and p.organisation),
            "",
        )
        domain = next(
            (p.email.split("@")[-1] for p in request.parties if p.role != "sender" and "@" in p.email),
            "",
        )
        how = "domain + search" if serpapi_available() else "domain only (no SERPAPI_API_KEY)"
        print(f"[2/6] verify        {org or '(no organisation named)'} - {how}")
        try:
            verification = verify_counterparty(org, domain)
            _write_json(workspace / "verification.json", verification.to_json())
            ledger.append("counterparty.checked", verification.to_json())
            print(f"      verified={verification.verified}  {len(verification.evidence)} source(s)")
            for item in verification.evidence[:3]:
                print(f"      - {item.claim}: {item.value[:80]}")
        except VerificationError as exc:
            print(f"      skipped: {exc}")
    else:
        print("[2/6] verify        skipped (disabled)")

    print("[3/6] draft")
    document = draft_document(backend, request, verification)
    _write_json(workspace / "draft.json", document.to_json())
    ledger.append("draft.written", document.to_json())
    print(f"      {len(document.clauses)} clause(s)")

    print("[4/6] render        Foxit PDF Services")
    html_path = workspace / "document.html"
    html_path.write_text(to_html(document, request), encoding="utf-8")
    pdf_path = workspace / "document.pdf"
    if args.offline:
        pdf_path.write_bytes(html_path.read_bytes())
        print("      --offline: using the HTML bytes as a stand-in for the PDF")
    else:
        credentials = Credentials.from_env()
        pdf_bytes = PdfServices(credentials).render_html(html_path)
        pdf_path.write_bytes(pdf_bytes)
        print(f"      {display_path(pdf_path)} ({len(pdf_bytes):,} bytes)")
    ledger.append(
        "document.rendered",
        {"path": str(pdf_path), "sha256": sha256_file(pdf_path), "offline": args.offline},
    )

    print("[5/6] audit")
    report = run_audit(document, request, pdf_path.read_bytes(), verification)
    _write_json(workspace / "audit.json", report.to_json())
    _print_findings(report.findings)

    print("[6/6] gate")
    packet = gate.open_review(pdf_path, report)
    print()
    print("=" * 72)
    print("  STOP. This is as far as the agent goes.")
    print("=" * 72)
    print(f"  document   {display_path(pdf_path)}")
    print(f"  sha256     {packet.document_sha256}")
    print(f"  blocking   {len(report.blocking)}    warnings {len(report.warnings)}")
    print()
    print("  Read the document, then authorise it yourself:")
    print()
    print(
        f"    consent-gate approve --doc {packet.document_sha256[:16]} "
        f"--token {packet.nonce} --approver \"your name\""
    )
    print()
    print("  The token above is printed here and nowhere else - it is not written")
    print("  to disk. The approval binds to these exact bytes; re-render the")
    print("  document and it stops applying.")
    print("=" * 72)
    return 0


# --------------------------------------------------------------------------
# approve / reject


def _resolve_hash(ledger: Ledger, prefix: str) -> str:
    matches = {
        entry["data"]["document_sha256"]
        for entry in ledger.entries()
        if entry["event"] == "review.requested"
        and entry["data"]["document_sha256"].startswith(prefix)
    }
    if not matches:
        raise GateError(f"no review found whose document hash starts with {prefix!r}")
    if len(matches) > 1:
        raise GateError(f"{prefix!r} matches {len(matches)} documents; use more characters")
    return matches.pop()


def cmd_approve(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    gate = ConsentGate(ledger)
    digest = _resolve_hash(ledger, args.doc)
    entry = gate.approve(digest, args.token, args.approver, args.override_reason)
    print(f"authorised sha256:{digest[:16]}...  by {args.approver}")
    print(f"ledger entry #{entry['seq']}  {entry['hash'][:16]}...")
    if args.override_reason:
        print(f"blocking findings overridden: {args.override_reason}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    gate = ConsentGate(ledger)
    digest = _resolve_hash(ledger, args.doc)
    gate.reject(digest, args.approver, args.reason)
    print(f"rejected sha256:{digest[:16]}...  reason: {args.reason}")
    return 0


# --------------------------------------------------------------------------
# send


def cmd_send(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    ledger = _ledger(args)
    gate = ConsentGate(ledger)

    pdf_path = Path(args.document) if args.document else workspace / "document.pdf"
    if not pdf_path.exists():
        print(f"no document at {pdf_path}", file=sys.stderr)
        return 2

    # The single enforcement point. Everything else in this file is convenience.
    approval = gate.assert_authorised(pdf_path)
    print(f"authorisation found: {approval['data']['approver']} at {approval['data']['at']}")

    request = _rehydrate_request(_read_json(workspace / "intent.json"))
    signers = [p for p in request.parties if p.role != "sender"]
    parties = [
        party_payload(p.first_name, p.last_name, p.email, index)
        for index, p in enumerate(signers, start=1)
    ]

    if args.dry_run:
        print("--dry-run: not calling Foxit. Envelope that would be created:")
        print(json.dumps({"folderName": request.title, "parties": parties}, indent=2))
        return 0

    credentials = Credentials.from_env()
    esign = ESign(credentials)
    encoded = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    response = esign.create_folder(
        folder_name=request.title,
        file_names=[pdf_path.name],
        parties=parties,
        base64_files=[encoded],
        send_now=not args.draft_only,
    )
    # createfolder nests the envelope under "folder"
    folder = response.get("folder") or {}
    folder_id = folder.get("folderId") or response.get("folderId") or ""
    status = folder.get("folderStatus", "")
    ledger.append(
        "esign.dispatched",
        {
            "document_sha256": approval["data"]["document_sha256"],
            "folder_id": folder_id,
            "folder_status": status,
            "signers": [p.email for p in signers],
            "response": response,
        },
    )
    print(f"envelope created. folder {folder_id or '(id not returned)'}  status {status or '?'}")
    for party in signers:
        print(f"  -> {party.full_name} <{party.email}>")
    if args.draft_only:
        print("\n--draft-only: the envelope exists but nobody was emailed.")
    print()
    print("The agent's part is over. The signature happens in Foxit, under the")
    print("signer's own authentication, and this program cannot produce it.")
    return 0


# --------------------------------------------------------------------------
# collect


def cmd_collect(args: argparse.Namespace) -> int:
    """Fetch the signed document back and close the chain."""
    workspace = _workspace(args)
    ledger = _ledger(args)

    dispatched = ledger.find("esign.dispatched")
    if not dispatched and not args.folder:
        print("nothing has been dispatched from this workspace", file=sys.stderr)
        return 2
    last = dispatched[-1]["data"] if dispatched else {}
    folder_id = args.folder or str(last.get("folder_id", ""))
    if not folder_id:
        print("no folder id recorded; pass --folder", file=sys.stderr)
        return 2

    credentials = Credentials.from_env()
    signed = ESign(credentials).download(folder_id)
    if not signed.startswith(b"%PDF"):
        preview = signed[:200].decode("utf-8", "replace")
        print(f"folder {folder_id} did not return a PDF - is it signed yet?\n  {preview}")
        return 1

    out = Path(args.out) if args.out else workspace / "document.signed.pdf"
    out.write_bytes(signed)
    signed_digest = sha256_file(out)
    approved = str(last.get("document_sha256", ""))

    ledger.append(
        "signature.collected",
        {
            "folder_id": folder_id,
            "approved_sha256": approved,
            "signed_sha256": signed_digest,
            "path": str(out),
            "bytes": len(signed),
        },
    )
    print(f"signed document retrieved: {out} ({len(signed):,} bytes)")
    print(f"  approved  sha256:{approved[:16]}...")
    print(f"  signed    sha256:{signed_digest[:16]}...")
    print()
    print("  These differ, and they should: signing adds content to the file. The")
    print("  ledger holds both, so the chain reads approved -> dispatched -> signed")
    print("  with one folder id tying them together. This program does not claim to")
    print("  prove cryptographically that the signed file contains the approved")
    print("  bytes - that is the signing platform's job, and its own signature.")
    return 0


# --------------------------------------------------------------------------
# ledger


def cmd_ledger(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    ok, reason = ledger.verify()
    entries = list(ledger.entries())
    for entry in entries:
        summary = ""
        data = entry["data"]
        if entry["event"] == "human.authorised":
            summary = f"{data['approver']} -> {data['document_sha256'][:12]}"
        elif entry["event"] == "review.requested":
            summary = f"{data['blocking']} blocking, {data['warnings']} warnings"
        elif entry["event"] == "esign.dispatched":
            summary = f"folder {data.get('folder_id', '')}"
        elif entry["event"] == "document.rendered":
            summary = data["sha256"][:12]
        elif entry["event"] == "signature.collected":
            summary = f"signed {data['signed_sha256'][:12]} <- approved {data['approved_sha256'][:12]}"
        elif entry["event"] == "counterparty.checked":
            summary = f"verified={data.get('verified')}"
        print(f"#{entry['seq']:<3} {entry['ts']}  {entry['event']:<22} {summary}")
    print()
    print(f"chain: {'OK' if ok else 'BROKEN'} - {reason}")
    return 0 if ok else 1


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consent-gate",
        description="From a plain prompt to a signed document, with a gate the agent cannot open.",
    )
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help="directory for artefacts and the ledger (default: ./workspace)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_draft = sub.add_parser("draft", help="prompt -> intent -> draft -> PDF -> audit -> stop")
    p_draft.add_argument("prompt", help="what you want, in plain language")
    p_draft.add_argument(
        "--backend",
        default=os.environ.get("CONSENT_GATE_BACKEND", "claude-code"),
        choices=["claude-code", "anthropic", "mock"],
    )
    p_draft.add_argument(
        "--no-verify-counterparty",
        dest="verify_counterparty",
        action="store_false",
        help="skip the SerpApi check on the other party",
    )
    p_draft.add_argument(
        "--offline",
        action="store_true",
        help="skip Foxit PDF Services and hash the HTML instead (for testing the gate)",
    )
    p_draft.set_defaults(func=cmd_draft, verify_counterparty=True)

    p_approve = sub.add_parser("approve", help="authorise one exact document (a human does this)")
    p_approve.add_argument("--doc", required=True, help="document sha256, or a unique prefix")
    p_approve.add_argument("--token", required=True, help="the review token printed by `draft`")
    p_approve.add_argument("--approver", required=True, help="who is taking responsibility")
    p_approve.add_argument("--override-reason", default="", help="required if findings block")
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="refuse a document permanently")
    p_reject.add_argument("--doc", required=True)
    p_reject.add_argument("--approver", required=True)
    p_reject.add_argument("--reason", required=True)
    p_reject.set_defaults(func=cmd_reject)

    p_send = sub.add_parser("send", help="hand the approved document to Foxit eSign")
    p_send.add_argument("--document", default="", help="path to the PDF (default: workspace/document.pdf)")
    p_send.add_argument("--dry-run", action="store_true", help="check the gate, print the envelope, send nothing")
    p_send.add_argument(
        "--draft-only",
        action="store_true",
        help="create the eSign envelope but do not email the signer (sendNow=false)",
    )
    p_send.set_defaults(func=cmd_send)

    p_collect = sub.add_parser("collect", help="fetch the signed document back from Foxit eSign")
    p_collect.add_argument("--folder", default="", help="folder id (default: the last one dispatched)")
    p_collect.add_argument("--out", default="", help="where to write the signed PDF")
    p_collect.set_defaults(func=cmd_collect)

    p_ledger = sub.add_parser("ledger", help="replay the run and verify the hash chain")
    p_ledger.set_defaults(func=cmd_ledger)

    return parser


def main(argv: list[str] | None = None) -> int:
    # A model drafting an agreement will produce en dashes, curly quotes and the
    # occasional non-breaking space. On a Windows console still defaulting to a
    # legacy code page, printing those raises UnicodeEncodeError and takes the
    # run down after the PDF has already been created and billed.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - unusual consoles
                pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GateError as exc:
        print(f"\nrefused: {exc}", file=sys.stderr)
        return 3
    except (FoxitError, LLMError, VerificationError, ValueError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
