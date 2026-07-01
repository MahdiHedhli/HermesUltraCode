"""Reconciler logic over a fake backend (offline). Covers the invariants: create-if-absent
idempotent, match-before-create, never fabricates credentials, unauthenticated/unserved is
fail-closed not-ready, refcount reap never touches a referenced or static profile."""

import unittest

from adapters import profiles
from adapters.profiles import (
    ReconcileReport,
    ensure,
    reap_managed,
    reconcile,
    valid_profile_name,
    validate_ready,
)
from core.roster import Roster


class FakeBackend:
    def __init__(self, existing=(), auth=(), served=None):
        self._profiles = set(existing)
        self._auth = set(auth)
        self._served = dict(served or {})
        self.created = []            # (name, clone_from, description) — NO credential field exists
        self.descriptions = {}

    def list_profiles(self):
        return sorted(self._profiles)

    def profile_exists(self, name):
        return name in self._profiles

    def create_profile(self, name, *, clone_from, description):
        self.created.append((name, clone_from, description))
        self._profiles.add(name)

    def set_description(self, name, text):
        self.descriptions[name] = text

    def auth_ok(self, name):
        return name in self._auth

    def served_models(self, name):
        return self._served.get(name)


def roster():
    return Roster.from_dict({
        "orchestrator": {"profile": "orch", "lab": "anthropic"},
        "reviewer": None,
        "reviewer_mode": "off",
        "providers": [
            {"profile": "claude", "provider": "anthropic-x", "lab": "anthropic"},
            {"profile": "gemma-local", "provider": "local", "lab": "local", "model": "gemma-4-31b"},
        ],
        "routing": {"trivial": ["gemma-local"], "standard": ["claude"]},
    })


class EnsureTest(unittest.TestCase):
    def test_create_if_absent_then_idempotent(self):
        b = FakeBackend()
        self.assertTrue(ensure("claude", b, description="frontier"))
        self.assertEqual(b.created, [("claude", None, "frontier")])
        self.assertFalse(ensure("claude", b))          # already exists -> no-op
        self.assertEqual(len(b.created), 1)

    def test_match_before_create(self):
        b = FakeBackend(existing={"claude"})
        self.assertFalse(ensure("claude", b))
        self.assertEqual(b.created, [])                # matched an existing one, never created

    def test_never_fabricates_credentials(self):
        # Structural: the backend create interface has no credential parameter, and ensure
        # passes only (name, clone_from, description) — no secret can be written.
        b = FakeBackend()
        ensure("claude", b, description="x")
        name, clone_from, desc = b.created[0]
        self.assertIsNone(clone_from)                  # non-secret bootstrap, no config/env clone
        self.assertNotIn("key", (desc or "").lower())

    def test_invalid_or_reserved_name_rejected(self):
        for bad in ("default", "Root", "has space", "", "-lead"):
            with self.assertRaises(ValueError):
                ensure(bad, FakeBackend())

    def test_valid_profile_name(self):
        self.assertTrue(valid_profile_name("gemma-local"))
        self.assertFalse(valid_profile_name("sudo"))
        self.assertFalse(valid_profile_name("A"))


class ReadinessTest(unittest.TestCase):
    def test_unauthenticated_not_ready(self):
        r = validate_ready("claude", set(), FakeBackend(existing={"claude"}))
        self.assertFalse(r.ready)
        self.assertEqual(r.reason, "unauthenticated")

    def test_no_catalog_blocks(self):
        # authenticated but the live catalog is None (unreachable) or [] -> fail closed
        for served in (None, []):
            b = FakeBackend(existing={"claude"}, auth={"claude"}, served={"claude": served})
            self.assertFalse(validate_ready("claude", set(), b).ready)

    def test_unserved_model_rejected(self):
        b = FakeBackend(existing={"claude"}, auth={"claude"}, served={"claude": ["a", "b"]})
        r = validate_ready("claude", {"c"}, b)
        self.assertFalse(r.ready)
        self.assertIn("c", r.missing)

    def test_ready_when_authenticated_and_served(self):
        b = FakeBackend(existing={"claude"}, auth={"claude"}, served={"claude": ["a", "b"]})
        r = validate_ready("claude", {"a"}, b)
        self.assertTrue(r.ready)
        self.assertEqual(r.served_count, 2)


class ReapTest(unittest.TestCase):
    def test_refcount_never_reaps_referenced_or_static(self):
        b = FakeBackend(existing={"m1", "m2", "static1"})
        out = reap_managed(managed=["m1", "m2", "static1"], declared_static=["static1"],
                           open_assignees=["m2"], backend=b)
        self.assertEqual(out, ["m1"])   # m2 referenced, static1 declared -> only m1 reapable

    def test_reap_skips_absent(self):
        b = FakeBackend(existing={"m1"})
        self.assertEqual(reap_managed(["m1", "ghost"], [], [], b), ["m1"])


class ReconcileTest(unittest.TestCase):
    def test_creates_absent_and_reports_readiness(self):
        # claude authenticated+served; gemma-local absent auth -> not ready
        b = FakeBackend(auth={"claude"}, served={"claude": ["a"]})
        rep = reconcile(roster(), b)
        self.assertIsInstance(rep, ReconcileReport)
        self.assertIn("claude", rep.created)
        self.assertIn("gemma-local", rep.created)
        self.assertIn("claude", rep.ready)
        self.assertTrue(any(r.profile == "gemma-local" for r in rep.not_ready))
        self.assertFalse(rep.ok())

    def test_no_hermes_import_at_module_load(self):
        # adapters.profiles must import without Hermes present (backend is lazy).
        import sys
        self.assertNotIn("hermes_cli", sys.modules)
        self.assertTrue(hasattr(profiles, "HermesProfileBackend"))


if __name__ == "__main__":
    unittest.main()
