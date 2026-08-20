"""The .env loader.

The point of these is the precedence rule: a file on disk is a default, an
exported variable is a decision, and the decision wins. Getting that backwards
is how a stale checkout quietly sends a run to the wrong account.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consent_gate.env import load_env  # noqa: E402

KEY = "CONSENT_GATE_TEST_KEY"
OTHER = "CONSENT_GATE_TEST_OTHER"


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in (KEY, OTHER)}
        for k in (KEY, OTHER):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        (self.dir / ".env").write_text(text, encoding="utf-8")
        return self.dir

    def test_reads_a_plain_pair(self) -> None:
        applied = load_env(self.write(f"{KEY}=sk-from-file\n"))
        self.assertEqual(applied, [KEY])
        self.assertEqual(os.environ[KEY], "sk-from-file")

    def test_environment_wins_over_file(self) -> None:
        os.environ[KEY] = "sk-from-shell"
        applied = load_env(self.write(f"{KEY}=sk-from-file\n"))
        self.assertEqual(applied, [])
        self.assertEqual(os.environ[KEY], "sk-from-shell")

    def test_skips_comments_blanks_and_junk(self) -> None:
        applied = load_env(
            self.write(
                "\n"
                "# a comment mentioning KEY=not-this\n"
                "   \n"
                "a line with no equals sign\n"
                f"{KEY}=kept\n"
            )
        )
        self.assertEqual(applied, [KEY])
        self.assertEqual(os.environ[KEY], "kept")

    def test_strips_quotes_and_export_prefix(self) -> None:
        load_env(self.write(f'export {KEY}="quoted value"\n' f"{OTHER}='single'\n"))
        self.assertEqual(os.environ[KEY], "quoted value")
        self.assertEqual(os.environ[OTHER], "single")

    def test_searches_upwards(self) -> None:
        self.write(f"{KEY}=found-above\n")
        nested = self.dir / "src" / "consent_gate"
        nested.mkdir(parents=True)
        load_env(nested)
        self.assertEqual(os.environ[KEY], "found-above")

    def test_no_env_file_is_not_an_error(self) -> None:
        empty = self.dir / "nowhere"
        empty.mkdir()
        # An absent .env is the normal case in CI: it must not raise, and it
        # must not invent a value. Asserted on the variable rather than the
        # return value, because the search walks up into directories this test
        # does not control.
        load_env(empty)
        self.assertNotIn(KEY, os.environ)


if __name__ == "__main__":
    unittest.main()
