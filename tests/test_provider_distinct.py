"""Acceptance criterion 3: reviewer provider != orchestrator provider. Identical
lab config fails at startup with a clear error."""

import unittest

from core.config import load_config
from core.providers import (
    HermesProvider,
    MockProvider,
    OpenRouterProvider,
    ProviderConfigError,
    validate_distinct_providers,
)


class ProviderDistinctTest(unittest.TestCase):
    def test_same_lab_rejected(self):
        a = MockProvider(lab="openai", model="gpt-4o")
        b = MockProvider(lab="openai", model="gpt-4o-mini")  # same lab, different size
        with self.assertRaises(ProviderConfigError) as ctx:
            validate_distinct_providers(a, b)
        self.assertIn("DIFFERENT lab", str(ctx.exception))

    def test_same_lab_case_insensitive(self):
        a = MockProvider(lab="OpenAI", model="x")
        b = MockProvider(lab="openai", model="y")
        with self.assertRaises(ProviderConfigError):
            validate_distinct_providers(a, b)

    def test_different_lab_ok(self):
        a = MockProvider(lab="nous", model="hermes-4")
        b = MockProvider(lab="anthropic", model="claude")
        validate_distinct_providers(a, b)  # no raise

    def test_empty_lab_rejected(self):
        a = MockProvider(lab="", model="x")
        b = MockProvider(lab="anthropic", model="y")
        with self.assertRaises(ProviderConfigError):
            validate_distinct_providers(a, b)

    def test_whitespace_only_lab_rejected(self):
        # Audit regression: "  " is truthy but strips to empty — must be rejected.
        for blank in ("  ", "\t", "\n", " \t "):
            with self.assertRaises(ProviderConfigError):
                validate_distinct_providers(
                    MockProvider(lab=blank, model="x"),
                    MockProvider(lab="anthropic", model="y"),
                )

    def test_load_config_rejects_same_lab(self):
        cfg = {
            "orchestrator_provider": MockProvider(lab="nous", model="hermes"),
            "reviewer_provider": MockProvider(lab="nous", model="hermes-small"),
        }
        with self.assertRaises(ProviderConfigError):
            load_config(cfg)

    def test_load_config_accepts_distinct(self):
        cfg = {
            "orchestrator_provider": MockProvider(lab="nous", model="hermes"),
            "reviewer_provider": MockProvider(lab="anthropic", model="claude"),
        }
        loaded = load_config(cfg)
        self.assertEqual(loaded.orchestrator_provider.lab, "nous")
        self.assertEqual(loaded.reviewer_provider.lab, "anthropic")

    def test_default_real_providers_are_distinct_labs(self):
        # The shipped real-provider defaults must not collide.
        orch = HermesProvider()
        rev = OpenRouterProvider()
        self.assertNotEqual(orch.lab.lower(), rev.lab.lower())
        validate_distinct_providers(orch, rev)


if __name__ == "__main__":
    unittest.main()
