"""Router: tier->floor pick, subscription-first flat-rate preference, escalate cascade,
and the resolve() stub returning the profile default (tier ignored). Deterministic, offline."""

import unittest

from core.roster import Roster
from core.routing import Route, RoutingError, escalate, resolve, route


def roster(**over):
    d = {
        "orchestrator": {"profile": "orch", "lab": "anthropic"},
        "reviewer": {"profile": "xai", "lab": "xai"},
        "budget_mode": "subscription-first",
        "providers": [
            {"profile": "gemma-local", "provider": "local", "lab": "local",
             "model": "gemma-4-31b", "flat_rate": True},
            {"profile": "claude", "provider": "anthropic-x", "lab": "anthropic", "flat_rate": True},
            {"profile": "xai", "provider": "xai-x", "lab": "xai", "flat_rate": False},
        ],
        "routing": {
            "trivial": ["gemma-local", "xai"],
            "standard": ["xai", "claude"],   # metered listed FIRST, flat-rate second
            "elevated": ["xai"],
        },
    }
    d.update(over)
    return Roster.from_dict(d)


class RoutingTest(unittest.TestCase):
    def test_trivial_floors_to_local(self):
        self.assertEqual(route("trivial", roster()).profile, "gemma-local")

    def test_subscription_first_prefers_flat_rate(self):
        # xai is listed first but metered; the flat-rate claude is preferred under subscription-first
        self.assertEqual(route("standard", roster()).profile, "claude")

    def test_metered_mode_takes_cheapest_listed(self):
        self.assertEqual(route("standard", roster(), budget_mode="metered").profile, "xai")

    def test_fail_closed_when_no_candidate(self):
        with self.assertRaises(RoutingError):
            route("merge_adjacent", roster())     # no routing list => fail closed

    def test_escalate_next_then_exhausted(self):
        r = roster()
        nxt = escalate(Route("xai", "xai", "standard"), "standard", r)
        self.assertEqual(nxt.profile, "claude")
        self.assertIsNone(escalate(Route("claude", "anthropic", "standard"), "standard", r))

    def test_resolve_stub_returns_profile_default(self):
        r = roster()
        self.assertEqual(resolve(r.provider("gemma-local"), "elevated"), "gemma-4-31b")  # tier ignored
        self.assertIsNone(resolve(r.provider("claude"), "standard"))   # no pinned model => inherit
        self.assertIsNone(resolve(None, "standard"))


if __name__ == "__main__":
    unittest.main()
