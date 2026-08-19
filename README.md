# Consent Gate

**An agent that goes from a plain prompt to a signed contract — and is structurally incapable of signing it itself.**

Built for the Foxit challenge at the DevNetwork [API + Cloud + AI] Hackathon 2026: *"Your Agent Shouldn't Sign That."*

```
consent-gate draft "Draft a mutual NDA with Northwind Labs and send it to
                    alice@northwind-labs.example. Effective September 1 2026,
                    two-year term, covering our logistics integration project."
```

```
[1/6] intent        (claude-code)
      mutual-nda: Mutual Non-Disclosure Agreement
      18 value(s) the model supplied on your behalf
[2/6] verify        skipped (disabled)
[3/6] draft
      20 clause(s)
[4/6] render        Foxit PDF Services
      workspace/document.pdf (70,558 bytes)
[5/6] audit
  [WARN ] ASSIGNMENT               15. Assignment
          Rights or obligations can be transferred to a third party.
  [WARN ] ASSUMPTION               governing_law
          governing_law was assumed to be 'State of Delaware, USA' (The request is
          silent on governing law; Delaware is a common default for US commercial
          contracts. Must be confirmed.).
  [WARN ] ASSUMPTION               jurisdiction_venue
          jurisdiction_venue was assumed to be 'Exclusive jurisdiction of the state
          and federal courts located in Delaware' (No forum was specified; paired
          with the assumed governing law.).
  [info ] UNREQUESTED_CLAUSE       14. No Obligation to Proceed
          This clause was added by the model and traces to nothing you said.
  ... 22 more findings
[6/6] gate

========================================================================
  STOP. This is as far as the agent goes.
========================================================================
  sha256     c058cac2ca2e9be18e71822170f4f8da2ebdafba0fe68a17ce4bd8867d35341f
  blocking   0    warnings 20
```

*(Real transcript, trimmed only where marked. One sentence of input; the model
returned twenty clauses and declared eighteen values it had supplied on the
operator's behalf — including the governing law and the forum, neither of which
the request mentioned. Every figure in this README comes from that run or from
the test suite. None of it is illustrative.)*

The agent stops there. It has drafted the document, rendered it, checked that
the other party exists, and listed everything it invented on your behalf — but
it cannot put the document in front of a signer. That takes a separate command,
run by a person, carrying a token that was printed to the terminal and stored
nowhere.

---

## Why this, and not another prompt-to-PDF pipeline

The obvious build for this challenge is *prompt → PDF → send for signature*.
That pipeline works, and it is exactly the thing the challenge title is warning
about. An agent that can draft an agreement and also dispatch it for signature
has no boundary in it at all; the "human in the loop" is a dialog box that a
retry, a race, or a rewritten draft walks straight past.

So the gate is the product here, and the drafting is the plumbing.

**The failure this prevents is specific.** A model asked for a two-year NDA
will produce a competent two-year NDA — plus a governing-law clause, a
survival period, a non-solicitation covenant, and an auto-renewal, none of
which you mentioned. Every one of those is defensible boilerplate. Together
they are a set of obligations that nobody chose and nobody read, arriving in a
document that looks finished. Consent Gate makes each of them visible, names
which ones trace back to something you actually said, and refuses to let the
document move until a person has signed off on those exact bytes.

---

## The three guarantees

### 1. An approval names bytes, not documents

Approval binds to the SHA-256 of the rendered PDF. `send` re-hashes the file it
is about to transmit and looks for an authorisation of *that* digest.

```console
$ consent-gate approve --doc c058cac2ca2e9be1 --token <token> --approver "K. Sato"
authorised sha256:c058cac2ca2e9be1...  by K. Sato

$ printf ' ' >> workspace/document.pdf     # one byte

$ consent-gate send
refused: refusing to send: no human authorisation for sha256:a68d227d07c7...
  Any edit to the document invalidates an earlier approval by design.
```

This closes the time-of-check/time-of-use gap that a confirmation prompt leaves
open. "The human approved it" and "this is what the human approved" become the
same statement.

### 2. The approval token never touches the disk

`draft` prints a random token and stores only its SHA-256. `approve` will not
mint an authorisation without the plaintext. A process that talks to this
program through its documented interface cannot approve its own output, because
the only copy of the token went to the operator's terminal.

### 3. The record cannot be quietly rewritten

Every stage appends one line to a hash-chained JSONL ledger. Editing or
deleting any line is detectable:

```console
$ consent-gate ledger
#0   2026-08-19T14:53:54+00:00  run.started
#1   2026-08-19T14:54:13+00:00  intent.extracted
#2   2026-08-19T14:56:25+00:00  draft.written
#3   2026-08-19T14:56:39+00:00  document.rendered      c058cac2ca2e
#4   2026-08-19T14:56:39+00:00  review.requested       0 blocking, 20 warnings
#5   2026-08-19T14:56:56+00:00  human.authorised       K. Sato -> c058cac2ca2e
#6   2026-08-19T14:57:13+00:00  esign.dispatched       folder 35443545

chain: OK - 7 entries, chain intact
```

Line #6 carries the same digest as #5. That is the whole point: the ledger
answers "was the document that went out the document that was approved?"
without asking you to trust the program that sent it.

### Threat model, stated honestly

Guarantee 2 is defeated by anything that can read the operator's terminal.
All three are defeated by editing the source. What survives both is the
combination of 1 and 3: after the fact you can always prove which exact bytes a
human authorised, when, and whether those were the bytes that got sent. That is
the property an auditor needs, and it is the one this design actually delivers.

---

## The audit

Two kinds of check, deliberately kept apart.

**Deterministic red flags** — regexes over the drafted text, no model involved,
so results are reproducible and unit-tested.

| Code | Severity | What it catches |
|---|---|---|
| `PLACEHOLDER` | block | `[TBD]`, `{{name}}`, `<insert …>` surviving into a binding document |
| `BLANK_TO_FILL_IN` | block | a rule of underscores left in the binding text (the signature block is exempt — underscores belong there) |
| `BLANK_AMOUNT` | block | a currency figure that was never filled in |
| `UNCAPPED_LIABILITY` | block | liability with no ceiling |
| `INDEMNITY` | warn | indemnity / hold-harmless obligations |
| `AUTO_RENEWAL` | warn | the agreement renews itself |
| `PERPETUAL_GRANT` | warn | perpetual or irrevocable grants |
| `ASSIGNMENT` | warn | obligations transferable to a third party |
| `ARBITRATION` | warn | jury waiver / forced arbitration |
| `MISSING_GOVERNING_LAW`, `MISSING_TERM`, `MISSING_TERMINATION` | warn | clauses a reader will expect and cannot find |

**Traceability** — the interesting half. Every clause the model writes must
declare what it traces back to: a phrase from your prompt, or a named
assumption. A clause that traces to nothing is a clause the model invented.
Inventing recitals is harmless; inventing obligations is not, so the severity
depends on whether the clause contains binding language.

| Code | Severity | Meaning |
|---|---|---|
| `UNREQUESTED_OBLIGATION` | warn | binds somebody, traces to nothing you said |
| `UNREQUESTED_CLAUSE` | info | added by the model, binds nobody |
| `ASSUMPTION` | warn | one per value the model supplied on your behalf |
| `UNVERIFIED_COUNTERPARTY` | warn | nobody checked that the other side exists |
| `COUNTERPARTY_NOT_FOUND` | block | it was checked, and nothing was found |
| `EMAIL_DOMAIN_UNCONFIRMED` | warn | the signer's domain appears in no confirming source |

Blocking findings stop `approve` unless the operator supplies
`--override-reason`, which is recorded in the ledger next to their name.

---

## Counterparty verification (SerpApi)

Stage 3 asks the open web whether the other party is a real entity, and returns
evidence with the URL each fact came from — knowledge-graph entries, organic
results, and a targeted check that the signer's email domain appears in a
source that confirms the organisation.

You are otherwise about to be bound to a name that only ever existed inside a
language model's context window.

The stage is optional and fails loudly rather than silently: no
`SERPAPI_API_KEY` produces an `UNVERIFIED_COUNTERPARTY` warning on the review
screen, never a quiet pass.

---

## Install and run

Python 3.11+. **The core has no third-party dependencies** — clone it and run.

```bash
git clone <this repo> && cd consent-gate
python -m unittest discover -s tests     # 39 tests, no keys, no network
```

Try the whole pipeline with no credentials at all:

```bash
PYTHONPATH=src CONSENT_GATE_FIXTURES=fixtures \
python -m consent_gate.cli draft "Draft a mutual NDA with Northwind Labs..." \
  --backend mock --offline --no-verify-counterparty
```

`--backend mock` reads canned model responses from `fixtures/`; `--offline`
skips Foxit and hashes the HTML instead. Everything else — the audit, the gate,
the ledger — is the real code path.

To watch the whole thing, including both refusals, in one go:

```bash
python demo.py --offline --backend mock     # no credentials
python demo.py                              # live Foxit, --draft-only, no email
python demo.py --pause                      # step through it yourself
```

That script is what the demo video records. It shells out to the same CLI you
would type — there is no separate demo path through the code.

### With credentials

```bash
pip install -e .                 # or: pip install -e ".[anthropic]"
cp .env.example .env             # then fill it in and source it
consent-gate draft "..."
consent-gate approve --doc <prefix> --token <token> --approver "Your Name"
consent-gate send
consent-gate ledger
```

| Variable | Needed for | Notes |
|---|---|---|
| `FOXIT_CLIENT_ID` / `FOXIT_CLIENT_SECRET` | stages 5 and 8 | free developer account, 500 credits/year, no card |
| `FOXIT_HOST` | — | defaults to `https://na1.fusion.foxit.com` |
| `FOXIT_ESIGN_PREFIX` | — | `/esign/api/v1` by default; falls back to `/api` on 404 |
| `SERPAPI_API_KEY` | stage 3 | optional; absence is reported, not hidden |
| `ANTHROPIC_API_KEY` | `--backend anthropic` | not needed for `claude-code` or `mock` |

Nothing is read from the repository. Credentials come from the environment.

---

## How it is put together

```
prompt
  │  intent.py       structured request + every value the model invented
  │  verify.py       SerpApi: does the counterparty exist, with sources
  │  draft.py        clause-by-clause draft, each clause declaring its origin
  │  foxit.py        PDF Services: upload HTML → convert → poll → download
  │  audit.py        red flags + traceability, bound to the PDF's SHA-256
  ▼
gate.py ───────────── a human, out of band, with a token and a hash
  │
  │  foxit.py        eSign: createfolder → the signer signs in Foxit's UI
  ▼
ledger.py            append-only, hash-chained, replayable
```

| File | Lines | Job |
|---|---|---|
| `gate.py` | ~160 | the boundary. The reason the project exists |
| `audit.py` | ~250 | what a person should look at before signing |
| `ledger.py` | ~100 | proof, after the fact |
| `foxit.py` | ~280 | PDF Services + eSign, over `urllib` |
| `llm.py` | ~200 | three interchangeable model backends |
| `intent.py` / `draft.py` | ~230 | the plumbing |

### Technical choices, and why

**REST calls to Foxit rather than the MCP server.** The challenge requires
"direct calls to the Foxit eSign API using their own credentials", and Foxit's
published MCP server wraps PDF Services only — eSign is not in it. Both APIs
authenticate the same way (`client_id` / `client_secret` in named headers, no
OAuth exchange), so the client is ~280 lines of `urllib`.

**Signature fields via text tags.** The rendered HTML carries
`[sig|req|signer1]` and `[date|req|signer1]`; `processTextTags: true` turns
them into real fields on the eSign side. Field placement lives in the document
instead of in hard-coded page coordinates, so it survives a re-draft that
changes the page count.

**Base64 upload to eSign.** `createfolder` takes either `fileUrls` or
`base64FileString` + `inputType: "base64"`. A locally generated PDF has no
public URL, and giving one a public URL to satisfy an API is the wrong
trade — so the bytes go up inline.

**Three model backends behind one interface.** `mock` (fixtures, no key, used
by the tests), `claude-code` (the local CLI, no API key), `anthropic` (the
official SDK, `claude-opus-5` with adaptive thinking). Every backend answers
the same question, so no stage knows which is in use — and the full pipeline is
testable with no credentials at all.

**No third-party dependencies in the core.** A signing pipeline is exactly the
kind of software where a reviewer should be able to read everything it does.

---

## Where this would go next

The gate generalises past documents. Anything an agent does that a person is
answerable for — a payment, a deployment, an email to a customer list — has the
same shape: the agent prepares an artefact, a human authorises *that artefact*,
and the record has to survive the argument afterwards. Hash-bound approval plus
an append-only ledger is a small, auditable primitive for all of it.

The commercial version of this is the compliance evidence, not the drafting: an
organisation that lets agents prepare binding documents will need to show, per
document, who approved what and that nothing changed in between. That artefact
does not exist today.

---

## Status

Built for the DevNetwork [API + Cloud + AI] Hackathon 2026 (submission deadline
3 September 2026).

Verified end to end against the live Foxit APIs on 19 August 2026:

| | |
|---|---|
| Model | a real drafting run through the local Claude CLI: **20 clauses, 18 declared assumptions** |
| PDF Services | upload → `create/pdf-from-html` → poll → download, **70,558 bytes**, `%PDF-` header |
| eSign | `folders/createfolder` → **folder 35443545**, `folderStatus: DRAFT`, party recorded |
| Gate | send before approval, wrong token, and one-byte edit after approval — **all three refused** |
| Ledger | 7 entries, chain intact, `esign.dispatched` carrying the digest that was approved |
| Tests | **39 passing**, no keys, no network, no third-party packages |

`--draft-only` was used for the live eSign run, so the envelope exists in Foxit
but no email was sent.

The SerpApi stage is implemented but has not yet been exercised against the live
API, and the `anthropic` backend is written from the SDK documentation but has
not been run. Both are stated here rather than implied to work.

MIT licensed.
