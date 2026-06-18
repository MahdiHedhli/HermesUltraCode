"""Acceptance criterion 10: the benchmark harness runs a corpus gate-on vs gate-off
and emits the four metrics."""

import os
import unittest

from bench.harness import DEFAULT_TASKS, WorkerSim, load_tasks, run_benchmark


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tasks = load_tasks(DEFAULT_TASKS)

    def test_example_corpus_loads(self):
        self.assertGreaterEqual(len(self.tasks), 3)

    def test_emits_four_metrics(self):
        r = run_benchmark(self.tasks)
        for side in ("gate_on", "gate_off"):
            self.assertIn("first_pass_success_rate", r[side])
            self.assertIn("guideline_violation_rate", r[side])
        self.assertIn("added_latency_ms_per_dispatch", r)
        self.assertIn("added_cost_usd_per_dispatch", r)

    def test_gate_on_beats_gate_off(self):
        r = run_benchmark(self.tasks)
        # Tightening should raise first-pass success and cut guideline violations.
        self.assertGreater(r["gate_on"]["first_pass_success_rate"],
                           r["gate_off"]["first_pass_success_rate"])
        self.assertLess(r["gate_on"]["guideline_violation_rate"],
                       r["gate_off"]["guideline_violation_rate"])

    def test_added_cost_nonnegative(self):
        r = run_benchmark(self.tasks)
        self.assertGreaterEqual(r["added_cost_usd_per_dispatch"], 0.0)

    def test_added_cost_is_delta(self):
        # Audit regression: added_cost must be on - off, consistent with other deltas.
        r = run_benchmark(self.tasks)
        on = r["gate_on"]["avg_added_cost_usd_per_dispatch"]
        off = r["gate_off"]["avg_added_cost_usd_per_dispatch"]
        self.assertAlmostEqual(r["added_cost_usd_per_dispatch"], round(on - off, 6), places=6)

    def test_worker_sim_deterministic(self):
        sim = WorkerSim()
        risks = ["input_validation", "missing_tests"]
        prompt_addressed = "do x. Validate inputs. Add tests."
        prompt_bare = "do x."
        self.assertEqual(sim.run(risks, prompt_addressed), (True, 0))
        self.assertEqual(sim.run(risks, prompt_bare), (False, 2))


if __name__ == "__main__":
    unittest.main()
