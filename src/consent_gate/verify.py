"""Stage 3 - is the other party a real thing.

A prompt-to-contract agent will happily draft a watertight agreement with an
entity that does not exist, because nothing in the pipeline ever leaves the
model's head. This stage goes and looks.

Two checks, and the cheap one runs always:

*   **The signer's domain** - does it resolve, does it serve HTTPS, and how old
    is its certificate. No key, no account, no network service in between: just
    DNS and a TLS handshake from the standard library. A counterparty whose
    domain does not resolve, or whose certificate was issued last week, is
    worth a second look before you are bound to them.

*   **The organisation** - live search results via SerpApi, returned as
    evidence with the URL each fact came from, so the reviewer sees sources
    rather than a model's recollection. Optional: without a key this half is
    skipped and the audit says so.

Absent evidence is reported as absent. Neither check ever silently passes.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .models import Evidence, VerificationResult

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# A certificate younger than this on a counterparty's domain is not proof of
# anything, but it is the sort of thing a person should see before signing.
YOUNG_CERTIFICATE_DAYS = 60


class VerificationError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# the keyless half


def inspect_domain(domain: str, timeout: float = 8.0) -> list[Evidence]:
    """DNS + TLS facts about a domain. No credentials, no third-party service."""
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return []

    evidence: list[Evidence] = []

    try:
        infos = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        evidence.append(
            Evidence(
                claim="dns",
                value=f"resolves to {len(addresses)} address(es)",
                source_url=f"dns:{domain}",
                source_title="DNS lookup",
            )
        )
    except socket.gaierror as exc:
        evidence.append(
            Evidence(
                claim="dns",
                value=f"does not resolve ({exc.strerror or exc})",
                source_url=f"dns:{domain}",
                source_title="DNS lookup",
            )
        )
        return evidence

    context = ssl.create_default_context()
    try:
        with socket.create_connection((domain, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as tls:
                cert = tls.getpeercert() or {}
    except (ssl.SSLError, socket.timeout, OSError) as exc:
        evidence.append(
            Evidence(
                claim="tls",
                value=f"no usable HTTPS certificate ({type(exc).__name__})",
                source_url=f"https://{domain}",
                source_title="TLS handshake",
            )
        )
        return evidence

    issuer = _rdn(cert.get("issuer", ()), "organizationName") or _rdn(
        cert.get("issuer", ()), "commonName"
    )
    raw_not_before = cert.get("notBefore")
    not_before = _parse_cert_time(raw_not_before if isinstance(raw_not_before, str) else None)
    age_days = (datetime.now(timezone.utc) - not_before).days if not_before else None

    value = f"valid certificate issued by {issuer or 'an unnamed CA'}"
    if age_days is not None:
        value += f", {age_days} days old"
        if age_days < YOUNG_CERTIFICATE_DAYS:
            value += " (recent)"
    evidence.append(
        Evidence(
            claim="tls",
            value=value,
            source_url=f"https://{domain}",
            source_title="TLS certificate",
        )
    )
    return evidence


def _rdn(rdns: Any, key: str) -> str:
    for rdn in rdns or ():
        for pair in rdn:
            if len(pair) == 2 and pair[0] == key:
                return str(pair[1])
    return ""


def _parse_cert_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
    """Look the counterparty up and return what the open web actually says.

    The domain check runs unconditionally. The search half runs only if a
    SerpApi key is configured; without one the result says so rather than
    claiming the organisation could not be found.
    """
    key = (api_key or os.environ.get("SERPAPI_API_KEY", "")).strip()
    domain_evidence = inspect_domain(email_domain) if email_domain else []

    if not key:
        resolves = any(e.claim == "dns" and "does not resolve" not in e.value for e in domain_evidence)
        return VerificationResult(
            counterparty=organisation,
            verified=resolves,
            evidence=domain_evidence,
            note=(
                "Domain checked. The organisation itself was not looked up: no "
                "SERPAPI_API_KEY is configured."
            ),
        )
    if not organisation:
        return VerificationResult(
            counterparty="",
            verified=False,
            note="the request named no organisation to check",
        )

    evidence: list[Evidence] = list(domain_evidence)

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
