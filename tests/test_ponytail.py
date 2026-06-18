"""Acceptance criterion 8: the ponytail ruleset is present and injected into the
orchestrator and worker prompt assembly; the extended protected set is enumerated;
the marketplace plugin and its Node hooks are neither installed nor referenced."""

import os
import unittest

from core.ponytail import (
    MODE_FULL,
    MODE_LITE,
    ROLE_ORCHESTRATOR,
    ROLE_REVIEWER,
    ROLE_WORKER,
    PonytailError,
    harvest_markers,
    inject_ruleset,
    load_ruleset,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENDED_PROTECTED = [
    "observability", "structured logging", "audit logging", "idempotency",
    "retries", "backoff",
]


class PonytailRulesetTest(unittest.TestCase):
    def test_ruleset_present(self):
        text = load_ruleset()
        self.assertIn("Ponytail ruleset (vendored)", text)
        self.assertIn("Does this need to exist?", text)

    def test_extended_protected_set_enumerated(self):
        text = load_ruleset().lower()
        for item in EXTENDED_PROTECTED:
            self.assertIn(item, text, msg=item)

    def test_ruleset_excludes_reviewer(self):
        self.assertIn("does NOT apply to the reviewer", load_ruleset())

    def test_inject_into_orchestrator_full(self):
        out = inject_ruleset("BASE TASK", ROLE_ORCHESTRATOR, MODE_FULL)
        self.assertIn("Ponytail ruleset (vendored)", out)
        self.assertIn("BASE TASK", out)

    def test_inject_into_orchestrator_lite(self):
        out = inject_ruleset("BASE TASK", ROLE_ORCHESTRATOR, MODE_LITE)
        self.assertIn("Ponytail (lite)", out)
        self.assertIn("BASE TASK", out)
        # protected set still enumerated even in lite
        self.assertIn("idempotency", out.lower())

    def test_worker_always_full_even_if_lite_requested(self):
        out = inject_ruleset("WORK", ROLE_WORKER, MODE_LITE)
        self.assertIn("Ponytail ruleset (vendored)", out)  # full text, not lite

    def test_reviewer_injection_forbidden(self):
        with self.assertRaises(PonytailError):
            inject_ruleset("X", ROLE_REVIEWER, MODE_FULL)

    def test_no_marketplace_plugin_or_node_hooks_referenced(self):
        # Criterion 8: the marketplace plugin and its Node hooks are neither installed
        # nor *used*. Disclaimers that say we do NOT use them are allowed; an actual
        # install/usage reference is not. We check per-line: any line mentioning a
        # banned token must also carry a negation, else it's a real reference.
        negations = ("no ", "not ", "never", "neither", "without", "don't", "do not",
                     "must not", "no marketplace", "no node", "vendored", "instead of")
        banned = ("marketplace plugin", "marketplace", "npx ponytail", "ponytail-plugin",
                  "lifecycle hook", "node lifecycle")
        offenders = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__"}]
            for name in filenames:
                if name == "test_ponytail.py":
                    continue  # this test file names the banned tokens to check for them
                if not name.endswith((".py", ".md", ".json", ".toml", ".js", ".html")):
                    continue
                full = os.path.join(dirpath, name)
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, start=1):
                        low = line.lower()
                        if any(b in low for b in banned) and not any(n in low for n in negations):
                            offenders.append(f"{os.path.relpath(full, REPO_ROOT)}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], msg=f"non-disclaimer plugin refs: {offenders}")

    def test_no_package_json_depends_on_ponytail_plugin(self):
        # No node manifest installs the plugin (criterion 8: not installed).
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules"}]
            for name in filenames:
                if name in ("package.json", "package-lock.json"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                        self.assertNotIn("ponytail", fh.read().lower())

    def test_harvest_markers_finds_debt(self):
        items = harvest_markers(REPO_ROOT)
        # We deliberately mark shortcuts with `ponytail:`; at least a few exist.
        self.assertTrue(len(items) >= 1)
        for it in items:
            self.assertTrue(it.note)
            self.assertTrue(it.file)
            self.assertGreater(it.line, 0)


if __name__ == "__main__":
    unittest.main()
