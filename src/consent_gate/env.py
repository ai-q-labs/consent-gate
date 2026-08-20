"""Read a .env file, because .env.example implies one works.

Deliberately not python-dotenv: the core carries no third-party dependencies,
and the format we need is a dozen lines of parsing. A real environment
variable always wins over the file, so a shell export or a CI secret is never
silently overridden by a stale checkout.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(start: Path | None = None) -> list[str]:
    """Apply the nearest .env at or above `start` (default: the cwd).

    Returns the names that were set — never the values, so a caller can report
    what it picked up without putting a credential on screen.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return _apply(candidate)
    return []


def _apply(path: Path) -> list[str]:
    applied: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
