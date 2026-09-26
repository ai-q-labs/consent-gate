"""Per-run token and cost accounting for the Nebius backend."""

from __future__ import annotations

import unittest

from consent_gate.llm import usage_summary


class UsageTests(unittest.TestCase):
    def test_sums_per_model_and_prices_them(self):
        calls = [
            {"model": "a", "prompt_tokens": 1000, "completion_tokens": 100},
            {"model": "a", "prompt_tokens": 500, "completion_tokens": 50},
            {"model": "b", "prompt_tokens": 200, "completion_tokens": 20},
        ]
        summary = usage_summary(calls, {"a": (1e-6, 3e-6), "b": (0.0, 1e-6)})
        self.assertEqual(summary["models"]["a"]["calls"], 2)
        self.assertEqual(summary["models"]["a"]["prompt_tokens"], 1500)
        self.assertAlmostEqual(summary["models"]["a"]["usd"], 1500e-6 + 150 * 3e-6)
        self.assertAlmostEqual(summary["total_usd"], 1500e-6 + 450e-6 + 20e-6)

    def test_unknown_price_makes_total_unknown_not_zero(self):
        summary = usage_summary(
            [{"model": "x", "prompt_tokens": 10, "completion_tokens": 10}], {}
        )
        self.assertIsNone(summary["models"]["x"]["usd"])
        self.assertIsNone(summary["total_usd"])


if __name__ == "__main__":
    unittest.main()
