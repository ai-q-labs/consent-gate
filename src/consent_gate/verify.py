"""Stage 3 - is the other party a real thing.

A prompt-to-contract agent will happily draft a watertight agreement with an
entity that does not exist, because nothing in the pipeline ever leaves the
model's head. This stage goes and looks.

It uses SerpApi for live, structured search results, and returns evidence with
the URL each fact came from, so the reviewer sees sources rather than a
model's recollection.

The stage is optional by design: no key means no verification, and the audit
raises UNVERIFIED_COUNTERPARTY rather than silently pretending the check
passed. Absent evidence is reported as absent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import Evidence, VerificationResult

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


class VerificationError(RuntimeError):
    pass


def serpapi_available() -> bool:
    return bool(os.environ.get("SERPAPI_API_KEY", "").strip())


def _search(query: str, api_key: str, engine: str = "google", num: int = 8) -> dict[str, Any]:
    params = {
        "engine": engine,
        "q": query,
        "num": str(num),
        "api_key": api_key,
        "output": "json",
    }
    url = f"{SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise VerificationError(f"SerpApi returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise VerificationError(f"SerpApi unreachable: {exc.reason}") from exc


def verify_counterparty(
    organisation: str,
    email_domain: str = "",
    api_key: str | None = None,
) -> VerificationResult:
    """Look the counterparty up and return what the open web actually says."""
    key = (api_key or os.environ.get("SERPAPI_API_KEY", "")).strip()
    if not key:
        raise VerificationError("SERPAPI_API_KEY is not set")
    if not organisation:
        return VerificationResult(
            counterparty="",
            verified=False,
            note="the request named no organisation to check",
        )

    evidence: list[Evidence] = []

    data = _search(f'"{organisation}" official site', key)

    kg = data.get("knowledge_graph") or {}
    if kg:
        for field, claim in (
            ("title", "entity name"),
            ("type", "entity type"),
            ("website", "website"),
            ("headquarters", "headquarters"),
            ("founded", "founded"),
        ):
            value = kg.get(field)
            if value:
                evidence.append(
                    Evidence(
                        claim=claim,
                        value=str(value),
                        source_url=str(kg.get("website") or kg.get("source", {}).get("link", "")),
                        source_title="Google Knowledge Graph",
                    )
                )

    for result in (data.get("organic_results") or [])[:5]:
        link = str(result.get("link", ""))
        if not link:
            continue
        evidence.append(
            Evidence(
                claim="search result",
                value=str(result.get("snippet", ""))[:300],
                source_url=link,
                source_title=str(result.get("title", "")),
            )
        )

    if email_domain:
        domain_hits = [e for e in evidence if email_domain.lower() in e.source_url.lower()]
        if not domain_hits:
            extra = _search(f'"{organisation}" site:{email_domain}', key)
            for result in (extra.get("organic_results") or [])[:3]:
                evidence.append(
                    Evidence(
                        claim="signer domain",
                        value=str(result.get("snippet", ""))[:300],
                        source_url=str(result.get("link", "")),
                        source_title=str(result.get("title", "")),
                    )
                )

    verified = bool(kg) or len(evidence) >= 3
    note = (
        ""
        if verified
        else "fewer than three independent results and no knowledge-graph entry"
    )
    return VerificationResult(
        counterparty=organisation,
        verified=verified,
        evidence=evidence,
        note=note,
    )
