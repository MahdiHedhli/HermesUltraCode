"""Wizard planning: a single authenticated provider is a COMPLETE valid setup; the reviewer
always resolves to a NAMED mode and a same-lab reviewer never presents as cross-lab. Offline."""

import unittest

from adapters.wizard import ProviderChoice, default_routing, plan_roster
from core.roster import (
    REVIEWER_CROSS_LAB,
    REVIEWER_OFF,
    REVIEWER_SAME_LAB_FLAGGED,
    Roster,
    RosterError,
)

CLAUDE = ProviderChoice("claude", "anthropic-x", "anthropic", flat_rate=True)
CLAUDE2 = ProviderChoice("claude2", "anthropic-y", "anthropic")
CODEX = ProviderChoice("codex", "openai-x", "openai", flat_rate=True)
LOCAL = ProviderChoice("gemma-local", "local-x", "local", model="gemma-4-31b", flat_rate=True)


class WizardTest(unittest.TestCase):
    def test_single_provider_is_a_complete_setup(self):
        d = plan_roster(CLAUDE, [CLAUDE], reviewer_profile=None)
        self.assertEqual(d["reviewer_mode"], REVIEWER_OFF)
        self.assertIsNone(d["reviewer"])
        r = Roster.from_dict(d)                       # loads clean => complete, valid
        self.assertIn("claude", r.routing["standard"])

    def test_two_labs_unlock_cross_lab(self):
        d = plan_roster(CLAUDE, [CLAUDE, CODEX], reviewer_profile="codex")
        self.assertEqual(d["reviewer_mode"], REVIEWER_CROSS_LAB)
        Roster.from_dict(d)

    def test_same_lab_reviewer_degrades_to_named_mode(self):
        d = plan_roster(CLAUDE, [CLAUDE, CLAUDE2], reviewer_profile="claude2")
        self.assertEqual(d["reviewer_mode"], REVIEWER_SAME_LAB_FLAGGED)   # named, not silent cross_lab
        Roster.from_dict(d)

    def test_cannot_force_cross_lab_on_same_lab_reviewer(self):
        with self.assertRaises(RosterError):
            plan_roster(CLAUDE, [CLAUDE, CLAUDE2], reviewer_profile="claude2",
                        reviewer_mode="cross_lab")

    def test_default_routing_risk_gates_local(self):
        rt = default_routing([LOCAL, CLAUDE])
        self.assertIn("gemma-local", rt["trivial"])
        self.assertNotIn("gemma-local", rt["elevated"])
        self.assertNotIn("gemma-local", rt["merge_adjacent"])
        self.assertIn("claude", rt["elevated"])


if __name__ == "__main__":
    unittest.main()
