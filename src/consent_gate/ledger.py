"""Append-only, hash-chained audit ledger.

Every stage of a run appends one line.  Each line carries the hash of the
previous line, so a reviewer can prove after the fact that nothing was
inserted, reordered or edited between "the human approved X" and "X was sent
for signature".

Format: one JSON object per line (JSONL).

    {"seq": 0, "ts": "...", "event": "...", "data": {...},
     "prev": "<sha256 of previous line or 64 zeros>", "hash": "<sha256 of this line without the hash field>"}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import canonical_json, sha256_hex

GENESIS = "0" * 64


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ read

    def entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def last(self) -> dict[str, Any] | None:
        last = None
        for entry in self.entries():
            last = entry
        return last

    # ----------------------------------------------------------------- write

    def append(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.last()
        entry = {
            "seq": 0 if previous is None else previous["seq"] + 1,
            "ts": utcnow(),
            "event": event,
            "data": data,
            "prev": GENESIS if previous is None else previous["hash"],
        }
        entry["hash"] = sha256_hex(canonical_json(entry).encode("utf-8"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # ---------------------------------------------------------------- verify

    def verify(self) -> tuple[bool, str]:
        """Re-walk the chain.  Returns (ok, human-readable reason)."""
        expected_prev = GENESIS
        expected_seq = 0
        count = 0
        for entry in self.entries():
            if entry.get("seq") != expected_seq:
                return False, f"line {count}: seq is {entry.get('seq')}, expected {expected_seq}"
            if entry.get("prev") != expected_prev:
                return False, f"line {count}: prev does not match the previous line's hash"
            body = {k: v for k, v in entry.items() if k != "hash"}
            recomputed = sha256_hex(canonical_json(body).encode("utf-8"))
            if recomputed != entry.get("hash"):
                return False, f"line {count}: contents were edited after the fact"
            expected_prev = entry["hash"]
            expected_seq += 1
            count += 1
        return True, f"{count} entries, chain intact"

    def find(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("event") == event]
