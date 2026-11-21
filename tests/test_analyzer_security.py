import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from analyzer import AIBudget, Analyzer, AnalyzerError


class AnalyzerSecurityTests(unittest.TestCase):
    def test_provider_error_does_not_expose_secret(self):
        secret = "test-secret-not-a-real-key"
        analyzer = object.__new__(Analyzer)
        analyzer._keys = [secret]
        analyzer._active = 0
        analyzer._exhausted = set()
        analyzer._client = Mock(side_effect=RuntimeError(f"request contained {secret}"))
        analyzer.budget = AIBudget(per_run=2, daily=2, used_today=0)

        with self.assertRaises(AnalyzerError) as raised:
            analyzer._generate("news", system="test", temperature=0)

        self.assertNotIn(secret, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_triage_logs_do_not_expose_provider_error_details(self):
        secret = "test-secret-not-a-real-key"
        analyzer = object.__new__(Analyzer)
        analyzer._keys = [secret]
        analyzer._active = 0
        analyzer._exhausted = set()
        analyzer._client = Mock(side_effect=RuntimeError(f"request contained {secret}"))
        analyzer.budget = AIBudget(per_run=2, daily=2, used_today=0)
        item = SimpleNamespace(id=1, title="Security update", source_label="test")

        with self.assertLogs("analyzer", level="WARNING") as logs:
            self.assertIsNone(analyzer.triage([item]))

        self.assertNotIn(secret, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
