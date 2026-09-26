"""Pluggable model backends.

Three of them, for three different situations:

``mock``
    No model, no network, no key.  Reads canned JSON from a fixtures
    directory.  The whole pipeline - including the gate and the audit - runs
    under this backend, which is how the test suite covers end-to-end
    behaviour without anybody's credentials.

``claude-code``
    Shells out to the local ``claude`` CLI in headless mode.  Useful when you
    already have Claude Code on the machine and would rather not mint an API
    key just to try this.

``anthropic``
    The official Anthropic SDK (an optional extra: ``pip install
    consent-gate[anthropic]``).  Defaults to ``claude-opus-5`` with adaptive
    thinking, because drafting and auditing a contract is exactly the kind of
    work that benefits from it.

Every backend answers the same question - "given this instruction and this
payload, return a JSON object" - so the stages above never learn which one is
in use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-opus-5"

JSON_CONTRACT = (
    "Respond with a single JSON object and nothing else. No prose, no code "
    "fences, no explanation before or after."
)


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Tolerant on purpose: a fenced block, a leading sentence, or trailing
    whitespace should not take down a signing pipeline.  Anything more
    creative than that is an error worth surfacing.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model did not return usable JSON: {exc}") from exc
    raise LLMError("model response contained no JSON object")


class Backend(ABC):
    name = "abstract"

    @abstractmethod
    def complete_json(self, instruction: str, payload: str) -> dict[str, Any]:
        """Return the model's JSON answer to ``instruction`` about ``payload``."""


# --------------------------------------------------------------------------


class MockBackend(Backend):
    """Deterministic responses loaded from disk. No model involved."""

    name = "mock"

    def __init__(self, fixtures: str | os.PathLike[str] | None = None) -> None:
        self.fixtures = Path(fixtures or os.environ.get("CONSENT_GATE_FIXTURES", "fixtures"))

    def complete_json(self, instruction: str, payload: str) -> dict[str, Any]:
        key = _fixture_key(instruction)
        path = self.fixtures / f"{key}.json"
        if not path.exists():
            raise LLMError(
                f"mock backend has no fixture for {key!r} (looked in {self.fixtures}/)"
            )
        return json.loads(path.read_text(encoding="utf-8"))


def _fixture_key(instruction: str) -> str:
    first = instruction.strip().splitlines()[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", first).strip("-")
    return slug[:60] or "response"


# --------------------------------------------------------------------------


class ClaudeCodeBackend(Backend):
    """Headless Claude Code. No API key needed if the CLI is already set up."""

    name = "claude-code"

    def __init__(self, executable: str = "claude", timeout: int = 300) -> None:
        self.executable = executable
        self.timeout = timeout
        self._workdir: str | None = None

    def _neutral_cwd(self) -> str:
        """An empty directory to run the CLI in.

        Claude Code loads the configuration of whatever directory it starts in -
        project instructions, hooks, MCP servers. Inheriting the host project's
        setup makes this backend's behaviour depend on where consent-gate happens
        to be invoked from, and a project with slow MCP servers will hang it.
        A scratch directory keeps the drafting step hermetic.
        """
        workdir = self._workdir
        if workdir is None:
            workdir = tempfile.mkdtemp(prefix="consent-gate-llm-")
            self._workdir = workdir
        return workdir

    # Claude Code is an agent, not a completion endpoint. Left to its defaults it
    # will happily write files, explain itself in prose, and obey whatever
    # instructions the operator keeps in their own config - including which
    # language to answer in. These flags turn it back into one hermetic call:
    #   --system-prompt      replaces the agent persona with our own contract
    #   --setting-sources    loads no user/project/local settings
    #   --strict-mcp-config  connects to no MCP servers
    #   --disallowedTools    leaves it nothing to do but answer
    ISOLATION = [
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disallowedTools",
        "Write",
        "Edit",
        "Bash",
        "Task",
        "WebFetch",
        "WebSearch",
    ]
    SYSTEM_PROMPT = (
        f"You turn a request into structured JSON. {JSON_CONTRACT} "
        "Always answer in English."
    )
    # Everything long goes over stdin. A multi-line argument through cmd.exe on
    # Windows loses its newlines and quoting, and the model then sees a mangled
    # instruction - which is not a failure that announces itself.
    DIRECTIVE = (
        "The input contains an INSTRUCTION section followed by a PAYLOAD section. "
        "Follow the instruction, applied to the payload."
    )

    def _argv(self, instruction: str) -> list[str]:
        """Resolve the CLI to something CreateProcess/execve will actually run.

        On Windows npm installs the CLI as ``claude.CMD``; passing the bare name
        to subprocess raises WinError 2, and a batch file cannot be handed to
        CreateProcess directly - it has to go through the command interpreter.
        """
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise LLMError(
                f"{self.executable!r} is not on PATH. Install Claude Code, or use "
                "--backend anthropic / --backend mock."
            )
        argv = [resolved, "-p", self.DIRECTIVE, "--system-prompt", self.SYSTEM_PROMPT]
        argv += self.ISOLATION
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
        return argv

    def complete_json(self, instruction: str, payload: str) -> dict[str, Any]:
        stdin = "\n".join(
            ["=== INSTRUCTION ===", instruction, "", "=== PAYLOAD ===", payload, ""]
        )
        try:
            proc = subprocess.run(
                self._argv(instruction),
                input=stdin,
                cwd=self._neutral_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
        return extract_json(proc.stdout)


# --------------------------------------------------------------------------


class AnthropicBackend(Backend):
    """Official Anthropic SDK. Optional extra: pip install consent-gate[anthropic]."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 16000) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on env
                raise LLMError(
                    "the anthropic backend needs the SDK: pip install 'consent-gate[anthropic]'"
                ) from exc
            # Credentials resolve from the environment (ANTHROPIC_API_KEY, or an
            # `ant auth login` profile). Nothing is read from this repo.
            self._client = anthropic.Anthropic()
        return self._client

    def complete_json(self, instruction: str, payload: str) -> dict[str, Any]:
        client = self._get_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=f"{instruction}\n\n{JSON_CONTRACT}",
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": payload}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError("the model declined this request")
        text = "".join(block.text for block in response.content if block.type == "text")
        return extract_json(text)


# --------------------------------------------------------------------------


NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
# Three tiers, used for three different jobs (see cli._stage_backends):
#   fast    - reading the prompt into structured intent; a small model is enough
#   default - drafting the clauses
#   review  - the independent second opinion; the largest model, because it has
#             to catch what the drafter did not
NEBIUS_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
NEBIUS_FAST_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
NEBIUS_REVIEW_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"


class NebiusBackend(Backend):
    """NVIDIA Nemotron on Nebius Token Factory (OpenAI-compatible endpoint).

    Standard library only - no SDK to install. The key resolves from
    ``NEBIUS_API_KEY`` or, failing that, ``~/.secrets/nebius/api_key.txt``;
    nothing is read from this repo.
    """

    name = "nebius"

    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = 16000,
        timeout: int = 300,
    ) -> None:
        self.model = model or os.environ.get("CONSENT_GATE_NEBIUS_MODEL", NEBIUS_DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.timeout = timeout
        # One entry per call, from the usage block Token Factory returns.
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _api_key() -> str:
        key = os.environ.get("NEBIUS_API_KEY", "").strip()
        if not key:
            path = Path.home() / ".secrets" / "nebius" / "api_key.txt"
            if path.exists():
                key = path.read_text(encoding="utf-8").strip()
        if not key:
            raise LLMError("the nebius backend needs NEBIUS_API_KEY (or ~/.secrets/nebius/api_key.txt)")
        return key

    def complete_json(self, instruction: str, payload: str) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": f"{instruction}\n\n{JSON_CONTRACT} Always answer in English."},
                {"role": "user", "content": payload},
            ],
        }
        request = urllib.request.Request(
            f"{NEBIUS_BASE_URL}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"Token Factory returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach Token Factory: {exc.reason}") from exc
        usage = data.get("usage") or {}
        self.calls.append(
            {
                "model": self.model,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            }
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected Token Factory response shape: {str(data)[:300]}") from exc
        # Reasoning models may emit a <think> block before the answer.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        return extract_json(text)


def nebius_prices(timeout: int = 30) -> dict[str, tuple[float, float]]:
    """USD per token (prompt, completion) for every model, as Token Factory reports it.

    Read live from ``/v1/models?verbose=true`` rather than hard-coded, so the
    cost line never drifts from the price list. Returns {} if it cannot be read.
    """
    import urllib.request

    try:
        request = urllib.request.Request(
            f"{NEBIUS_BASE_URL}/models?verbose=true",
            headers={"Authorization": f"Bearer {NebiusBackend._api_key()}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - the cost line is a courtesy, never a failure
        return {}
    prices: dict[str, tuple[float, float]] = {}
    for model in data.get("data", []):
        pricing = model.get("pricing") or {}
        try:
            prices[model["id"]] = (float(pricing["prompt"]), float(pricing["completion"]))
        except (KeyError, TypeError, ValueError):
            continue
    return prices


def usage_summary(
    calls: list[dict[str, Any]], prices: dict[str, tuple[float, float]]
) -> dict[str, Any]:
    """Tokens and USD per model for one run. Cost is None where no price is known."""
    per_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = per_model.setdefault(
            call["model"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        row["calls"] += 1
        row["prompt_tokens"] += call["prompt_tokens"]
        row["completion_tokens"] += call["completion_tokens"]
    total: float | None = 0.0
    for model, row in per_model.items():
        price = prices.get(model)
        if price is None:
            row["usd"] = None
            total = None
            continue
        row["usd"] = row["prompt_tokens"] * price[0] + row["completion_tokens"] * price[1]
        if total is not None:
            total += row["usd"]
    return {"models": per_model, "total_usd": total}


# --------------------------------------------------------------------------

BACKENDS = {
    "mock": MockBackend,
    "claude-code": ClaudeCodeBackend,
    "anthropic": AnthropicBackend,
    "nebius": NebiusBackend,
}


def get_backend(name: str, **kwargs: Any) -> Backend:
    try:
        factory = BACKENDS[name]
    except KeyError:
        raise LLMError(
            f"unknown backend {name!r}; choose one of {', '.join(sorted(BACKENDS))}"
        ) from None
    return factory(**kwargs)
