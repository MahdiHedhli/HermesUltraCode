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

    def test_resolve_is_tier_aware(self):
        # LIVE (was stubbed while a per-task upstream field was pending; upstream refused
        # per-call/per-task selection, and the lane binds -m itself, so the table applies now).
        from core.roster import Roster as _R
        r = _R.from_dict({
            "orchestrator": {"profile": "orch", "lab": "anthropic"}, "reviewer": None,
            "reviewer_mode": "off",
            "providers": [
                {"profile": "claude", "provider": "a", "lab": "anthropic",
                 "tiers": {"cheap": "sonnet", "heavy": "opus", "standard": "sonnet-direct"}},
                {"profile": "slots-empty", "provider": "b", "lab": "openai",
                 "model": "pinned", "tiers": {"cheap": "<SLOT>", "heavy": "<SLOT>"}},
            ],
            "routing": {"standard": ["claude"]}})
        c = r.provider("claude")
        self.assertEqual(resolve(c, "trivial"), "sonnet")          # low risk -> cheap slot
        self.assertEqual(resolve(c, "elevated"), "opus")           # escalating -> heavy slot
        self.assertEqual(resolve(c, "merge_adjacent"), "opus")
        self.assertEqual(resolve(c, "standard"), "sonnet-direct")  # direct tier key overrides slot
        e = r.provider("slots-empty")
        self.assertEqual(resolve(e, "standard"), "pinned")         # <SLOT> ignored -> pinned default
        self.assertIsNone(resolve(None, "standard"))

    def test_resolve_without_tiers_falls_back_to_default(self):
        r = roster()   # providers here declare no tiers slots
        self.assertEqual(resolve(r.provider("gemma-local"), "elevated"), "gemma-4-31b")
        self.assertIsNone(resolve(r.provider("claude"), "standard"))  # no slots, no pin => inherit

    def test_next_rung_model_then_provider_cascade(self):
        from core.roster import Roster as _R
        from core.routing import next_rung
        r = _R.from_dict({
            "orchestrator": {"profile": "orch", "lab": "anthropic"}, "reviewer": None,
            "reviewer_mode": "off",
            "providers": [
                {"profile": "claude", "provider": "a", "lab": "anthropic",
                 "tiers": {"cheap": "sonnet", "heavy": "opus"}},
                {"profile": "xai", "provider": "x", "lab": "xai", "model": "grok"},
            ],
            "routing": {"standard": ["claude", "xai"]}})
        start = Route("claude", "anthropic", "standard")
        # rung 1: same provider, heavy slot (model cascade)
        r1 = next_rung(start, "sonnet", "standard", r)
        self.assertEqual((r1[0].profile, r1[1]), ("claude", "opus"))
        # rung 2: already on heavy -> next profile with ITS model (provider cascade)
        r2 = next_rung(r1[0], r1[1], "standard", r)
        self.assertEqual((r2[0].profile, r2[1]), ("xai", "grok"))
        # rung 3: ladder exhausted -> None (caller fails closed)
        self.assertIsNone(next_rung(r2[0], r2[1], "standard", r))


if __name__ == "__main__":
    unittest.main()
