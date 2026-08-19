"""Scripted walkthrough, for the demo video and for anyone reproducing it.

Runs the whole pipeline with a caption before each step, so the recording
explains itself without narration.

    python demo.py                 # real Foxit calls, --draft-only (no email)
    python demo.py --offline       # no credentials needed at all
    python demo.py --pause         # wait for Enter between steps

Nothing here is special-cased: every step shells out to the same CLI a user
would type.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def fresh_workspace() -> Path:
    """A new directory per run, so each recording starts with an empty ledger.

    Numbered rather than cleaned: a demo script should not be in the business
    of deleting directories on someone else's machine.
    """
    n = 1
    while (ROOT / f"demo-workspace-{n}").exists():
        n += 1
    return ROOT / f"demo-workspace-{n}"


WORKSPACE = fresh_workspace()

PROMPT = (
    "Draft a mutual NDA between Ai-Q Labs and Northwind Labs. Send it to "
    "Alice Nakamura at alice@northwind-labs.example for signature. Effective "
    "September 1 2026, two-year term, covering our upcoming logistics "
    "integration project."
)

WIDTH = 78


def caption(number: str, title: str, *lines: str) -> None:
    # ASCII only: box-drawing characters turn into mojibake on a console still
    # running a legacy code page, which is exactly where a demo gets recorded.
    print()
    print("-" * WIDTH)
    print(f"  {number}  {title}")
    for line in lines:
        print(f"      {line}")
    print("-" * WIDTH)
    print()


def run(args: list[str], expect_failure: bool = False) -> str:
    # The prompt is a paragraph; printing it inline makes the command
    # unreadable on screen, so long arguments are elided in the echo only.
    shown = [a if len(a) < 56 else f'"{a[:52]} ..."' for a in args]
    print("$ " + " ".join(["consent-gate", *shown]) + "\n")
    proc = subprocess.run(
        [sys.executable, "-m", "consent_gate.cli", "--workspace", str(WORKSPACE), *args],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output.rstrip())
    if expect_failure and proc.returncode == 0:
        raise SystemExit("expected this step to be refused, and it was not")
    if not expect_failure and proc.returncode != 0:
        raise SystemExit(f"step failed with {proc.returncode}")
    return output


def hold(args: argparse.Namespace, seconds: float = 2.0) -> None:
    if args.pause:
        input("\n      [Enter to continue]")
    else:
        time.sleep(seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip Foxit entirely")
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    parser.add_argument("--backend", default="claude-code", choices=["claude-code", "anthropic", "mock"])
    args = parser.parse_args()

    print()
    print("=" * WIDTH)
    print("  CONSENT GATE")
    print("  From a plain prompt to a signed contract - and the agent cannot sign it.")
    print("=" * WIDTH)
    hold(args, 3)

    # ------------------------------------------------------------------ 1
    caption(
        "STEP 1",
        "One sentence of input.",
        "The agent will extract intent, check the counterparty, draft the",
        "document, render it through Foxit, and audit the result.",
    )
    draft_args = ["draft", PROMPT, "--backend", args.backend, "--no-verify-counterparty"]
    if args.offline:
        draft_args.append("--offline")
    output = run(draft_args)

    match = re.search(r"--doc ([0-9a-f]{16}) --token ([0-9a-f]{32})", output)
    if not match:
        raise SystemExit("could not read the review packet from the output")
    digest, token = match.group(1), match.group(2)
    hold(args, 6)

    # ------------------------------------------------------------------ 2
    caption(
        "STEP 2",
        "The agent stopped on its own.",
        "It has a finished PDF and nowhere to send it. Watch what happens",
        "when we ask it to send anyway.",
    )
    run(["send", "--dry-run"], expect_failure=True)
    hold(args, 4)

    # ------------------------------------------------------------------ 3
    caption(
        "STEP 3",
        "A human authorises these exact bytes.",
        "The token was printed to the terminal in step 1 and stored nowhere,",
        "so the agent could not have produced it.",
    )
    run(["approve", "--doc", digest, "--token", token, "--approver", "K. Sato"])
    hold(args, 3)

    # ------------------------------------------------------------------ 4
    caption(
        "STEP 4",
        "Now change one byte of the approved document.",
        "The approval named a SHA-256, not a filename.",
    )
    pdf = WORKSPACE / "document.pdf"
    original = pdf.read_bytes()
    pdf.write_bytes(original + b" ")
    print("$ printf ' ' >> demo-workspace/document.pdf\n")
    run(["send", "--dry-run"], expect_failure=True)
    pdf.write_bytes(original)
    print("\n      (restored)")
    hold(args, 4)

    # ------------------------------------------------------------------ 5
    caption(
        "STEP 5",
        "Restored to the approved bytes, it goes out.",
        "--draft-only creates the envelope in Foxit eSign without emailing",
        "anyone. The signature happens there, under the signer's own login.",
    )
    if args.offline:
        run(["send", "--dry-run"])
    else:
        run(["send", "--draft-only"])
    hold(args, 4)

    # ------------------------------------------------------------------ 6
    caption(
        "STEP 6",
        "The record, afterwards.",
        "Each line carries the hash of the one before it. The dispatch entry",
        "carries the same digest the human authorised.",
    )
    run(["ledger"])

    print()
    print("=" * WIDTH)
    print("  github.com/ai-q-labs/consent-gate")
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
