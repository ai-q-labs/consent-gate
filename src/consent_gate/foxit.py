"""Foxit clients: PDF Services (stage 5) and eSign (stage 8).

Written against ``urllib`` rather than ``requests`` so the core of this project
has no third-party dependencies at all - a reviewer can clone it and run it
with nothing but a Python interpreter.

Both APIs authenticate the same way: the raw key pair goes in named headers,
``client_id`` and ``client_secret``. There is no OAuth exchange.

Credentials are read from the environment and never from this repository:

    FOXIT_CLIENT_ID        required
    FOXIT_CLIENT_SECRET    required
    FOXIT_HOST             default https://na1.fusion.foxit.com
    FOXIT_ESIGN_HOST       defaults to FOXIT_HOST
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOST = "https://na1.fusion.foxit.com"

# Foxit's edge rejects urllib's default agent string with a 403 before the
# request ever reaches the API. This names the client honestly rather than
# imitating a browser - the point is to be identifiable, not to look like
# something we are not.
USER_AGENT = "consent-gate/0.1.0 (+https://github.com/aiqlabs/consent-gate)"


class FoxitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    host: str = DEFAULT_HOST
    esign_host: str = ""

    @classmethod
    def from_env(cls) -> "Credentials":
        client_id = os.environ.get("FOXIT_CLIENT_ID", "").strip()
        client_secret = os.environ.get("FOXIT_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise FoxitError(
                "FOXIT_CLIENT_ID and FOXIT_CLIENT_SECRET must be set. "
                "Create a free developer account at app.developer-api.foxit.com "
                "and export the pair (see .env.example)."
            )
        host = os.environ.get("FOXIT_HOST", DEFAULT_HOST).rstrip("/")
        esign = os.environ.get("FOXIT_ESIGN_HOST", host).rstrip("/")
        return cls(client_id, client_secret, host, esign)

    @property
    def headers(self) -> dict[str, str]:
        return {"client_id": self.client_id, "client_secret": self.client_secret}


# --------------------------------------------------------------------------
# transport


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 120,
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        raise FoxitError(
            f"{method} {_redact(url)} -> HTTP {exc.code}: {body[:500].decode('utf-8', 'replace')}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FoxitError(f"{method} {_redact(url)} failed to connect: {exc.reason}") from exc


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


def _json_request(url: str, credentials: Credentials, payload: dict[str, Any], method: str = "POST") -> dict[str, Any]:
    headers = {**credentials.headers, "Content-Type": "application/json"}
    body = json.dumps(payload).encode("utf-8")
    _, raw, _ = _request(url, method=method, headers=headers, data=body)
    return json.loads(raw or b"{}")


def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----consent-gate-{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for name, path in files.items():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
        ).encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# PDF Services - stage 5


class PdfServices:
    """Upload HTML, convert it to PDF, download the bytes.

    Four calls, exactly as the Foxit guides document them:
        POST /pdf-services/api/documents/upload
        POST /pdf-services/api/documents/create/pdf-from-html
        GET  /pdf-services/api/tasks/{taskId}
        GET  /pdf-services/api/documents/{documentId}/download
    """

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.base = f"{credentials.host}/pdf-services/api"

    def upload(self, path: Path) -> str:
        body, content_type = _multipart({}, {"file": path})
        headers = {**self.credentials.headers, "Content-Type": content_type}
        _, raw, _ = _request(f"{self.base}/documents/upload", method="POST", headers=headers, data=body)
        data = json.loads(raw)
        document_id = data.get("documentId") or data.get("id")
        if not document_id:
            raise FoxitError(f"upload succeeded but returned no documentId: {data}")
        return document_id

    def html_to_pdf(self, document_id: str) -> str:
        data = _json_request(
            f"{self.base}/documents/create/pdf-from-html",
            self.credentials,
            {"documentId": document_id},
        )
        task_id = data.get("taskId") or data.get("id")
        if not task_id:
            raise FoxitError(f"conversion request returned no taskId: {data}")
        return task_id

    def wait(self, task_id: str, timeout: int = 180, interval: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            _, raw, _ = _request(
                f"{self.base}/tasks/{task_id}", headers=self.credentials.headers
            )
            last = json.loads(raw)
            status = str(last.get("status", "")).upper()
            if status == "COMPLETED":
                result = last.get("resultDocumentId") or last.get("documentId")
                if not result:
                    raise FoxitError(f"task completed without a result document: {last}")
                return result
            if status == "FAILED":
                raise FoxitError(f"conversion failed: {last.get('error') or last}")
            time.sleep(interval)
        raise FoxitError(f"task {task_id} did not finish within {timeout}s (last: {last})")

    def download(self, document_id: str) -> bytes:
        _, raw, _ = _request(
            f"{self.base}/documents/{document_id}/download", headers=self.credentials.headers
        )
        return raw

    def render_html(self, html_path: Path) -> bytes:
        """The whole stage 5 in one call."""
        document_id = self.upload(html_path)
        task_id = self.html_to_pdf(document_id)
        result_id = self.wait(task_id)
        return self.download(result_id)


# --------------------------------------------------------------------------
# eSign - stage 8


class ESign:
    """Send a document out for a human to sign.

    This class deliberately exposes no method that signs anything. It creates
    the envelope and hands control to a person; the signature happens in
    Foxit's UI, under that person's own authentication, and comes back to us
    as a completed document we can only read.
    """

    # Foxit's own guides show two prefixes for the same routes. The default
    # matches the endpoint list published on developer-api.foxit.com/esign/;
    # FOXIT_ESIGN_PREFIX overrides it, and the client retries with the other
    # one on a 404 rather than making the operator guess.
    PREFIXES = ("/esign/api/v1", "/api")

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.host = (credentials.esign_host or credentials.host).rstrip("/")
        override = os.environ.get("FOXIT_ESIGN_PREFIX", "").strip().rstrip("/")
        self.prefixes = (override,) if override else self.PREFIXES

    @property
    def base(self) -> str:
        return f"{self.host}{self.prefixes[0]}"

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        last: FoxitError | None = None
        for prefix in self.prefixes:
            try:
                return _json_request(f"{self.host}{prefix}{route}", self.credentials, payload)
            except FoxitError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                last = exc
        raise FoxitError(
            f"no eSign route matched {route}; tried {', '.join(self.prefixes)}. "
            f"Set FOXIT_ESIGN_PREFIX to the prefix your account uses. Last error: {last}"
        )

    def create_folder(
        self,
        folder_name: str,
        file_names: list[str],
        parties: list[dict[str, Any]],
        file_urls: list[str] | None = None,
        base64_files: list[str] | None = None,
        send_now: bool = True,
        embedded: bool = False,
        embedded_emails: list[str] | None = None,
    ) -> dict[str, Any]:
        if not file_urls and not base64_files:
            raise FoxitError("createfolder needs either file_urls or base64_files")
        payload: dict[str, Any] = {
            "folderName": folder_name,
            "fileNames": file_names,
            "parties": parties,
            # text tags such as [sig|req|signer1] become real signature fields
            "processTextTags": True,
            "sendNow": send_now,
        }
        # inputType is explicit in both directions in Foxit's own sample; omitting
        # it on a base64 payload makes the API reject the files as empty.
        if base64_files:
            payload["inputType"] = "base64"
            payload["base64FileString"] = base64_files
        else:
            payload["inputType"] = "url"
            payload["fileUrls"] = file_urls
        if embedded:
            payload["createEmbeddedSigningSession"] = True
            payload["embeddedSignersEmailIds"] = embedded_emails or []
        return self._post("/folders/createfolder", payload)

    def download(self, folder_id: str) -> bytes:
        last: FoxitError | None = None
        for prefix in self.prefixes:
            url = f"{self.host}{prefix}/folders/download?folderId={folder_id}"
            try:
                _, raw, _ = _request(url, headers=self.credentials.headers)
                return raw
            except FoxitError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                last = exc
        raise FoxitError(f"could not download folder {folder_id}: {last}")

    def create_webhook(self, callback_url: str, events: list[str] | None = None) -> dict[str, Any]:
        payload = {
            "callbackUrl": callback_url,
            "events": events or ["FOLDER_COMPLETED", "PARTY_SIGNED"],
        }
        return self._post("/webhook/createwebhookchannel", payload)


def party_payload(first: str, last: str, email: str, sequence: int) -> dict[str, Any]:
    return {
        "firstName": first,
        "lastName": last,
        "emailId": email,
        "permission": "FILL_FIELDS_AND_SIGN",
        "sequence": sequence,
    }
