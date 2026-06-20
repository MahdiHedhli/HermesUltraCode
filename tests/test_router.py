"""Cost-aware routing engine: catalog, the advisory router, the difficulty hint on the
verdict, and the non-raising local liveness probe. All offline, no provider calls."""

import unittest

from core import local_probe
from core.catalog import load_catalog
from core.router import choose_worker, effective_cost, real_cost_usd, required_tier
from core.tiering import TIER_ELEVATED, TIER_MERGE_ADJACENT, TIER_STANDARD, TIER_TRIVIAL
from core.verdict import parse_verdict

# default catalog: local/gemma(t2 local), haiku(t1), sonnet(t2), opus(t3)
CAT = load_catalog()


class CatalogTest(unittest.TestCase):
    def test_default_loads(self):
        self.assertTrue(CAT["local/gemma"].is_local)
        self.assertFalse(CAT["anthropic/claude-sonnet"].is_local)

    def test_local_trusted_tier_clamps(self):
        c = load_catalog({"models": {"local/x": {"lab": "local", "is_local": True,
                                                  "capability_tier": 3}}})
        self.assertEqual(c["local/x"].trusted_tier(local_trusted_tier=2), 2)   # local capped
        self.assertEqual(CAT["anthropic/claude-opus"].trusted_tier(2), 3)      # cloud keeps tier

    def test_unknown_keys_and_malformed_entry(self):
        c = load_catalog({"models": {"ok": {"lab": "x", "capability_tier": 1, "bogus": 9},
                                     "bad": "not-a-dict"}})
        self.assertEqual(c["ok"].lab, "x")
        self.assertNotIn("bad", c)


class RequiredTierTest(unittest.TestCase):
    def test_blast_radius_floor(self):
        self.assertEqual(required_tier(TIER_TRIVIAL), 1)
        self.assertEqual(required_tier(TIER_STANDARD), 2)
        self.assertEqual(required_tier(TIER_ELEVATED), 3)
        self.assertEqual(required_tier(TIER_MERGE_ADJACENT), 3)

    def test_difficulty_only_raises(self):
        self.assertEqual(required_tier(TIER_TRIVIAL, difficulty=3), 3)    # hint raises
        self.assertEqual(required_tier(TIER_ELEVATED, difficulty=1), 3)   # never below the floor
        self.assertEqual(required_tier(TIER_STANDARD, difficulty=0), 2)   # missing => floor, not 3
        self.assertEqual(required_tier(TIER_TRIVIAL, difficulty=99), 1)   # garbage ignored


class ChooseWorkerTest(unittest.TestCase):
    def test_standard_prefers_local_with_savings(self):
        d = choose_worker(TIER_STANDARD, CAT)
        self.assertEqual(d.model_id, "local/gemma")
        self.assertTrue(d.is_local)
        self.assertEqual(d.reason, "local")
        self.assertEqual(d.est_cost_usd, 0.0)
        self.assertGreater(d.baseline_cloud_usd, 0.0)
        self.assertGreater(d.est_savings_usd, 0.0)

    def test_trivial_prefers_local_over_haiku(self):
        self.assertTrue(choose_worker(TIER_TRIVIAL, CAT).is_local)

    def test_elevated_risk_gates_local(self):
        d = choose_worker(TIER_ELEVATED, CAT)
        self.assertFalse(d.is_local)
        self.assertEqual(d.model_id, "anthropic/claude-opus")

    def test_merge_authority_never_local(self):
        self.assertFalse(choose_worker(TIER_MERGE_ADJACENT, CAT).is_local)

    def test_local_down_falls_to_cloud(self):
        d = choose_worker(TIER_STANDARD, CAT, local_alive=False)
        self.assertFalse(d.is_local)
        self.assertEqual(d.model_id, "anthropic/claude-sonnet")   # cheapest tier-2 cloud

    def test_difficulty_bumps_off_local(self):
        d = choose_worker(TIER_STANDARD, CAT, difficulty=3)        # reviewer says hard
        self.assertFalse(d.is_local)
        self.assertEqual(d.model_id, "anthropic/claude-opus")

    def test_ctx_excludes_local(self):
        d = choose_worker(TIER_STANDARD, CAT, est_in=40000, est_out=10000)  # 50k > gemma 32768
        self.assertFalse(d.is_local)

    def test_no_candidate_names_fallback(self):
        small = load_catalog({"models": {"c/h": {"lab": "c", "capability_tier": 1, "ctx": 200000,
                                                  "usd_per_mtok_in": 1.0, "usd_per_mtok_out": 1.0}}})
        d = choose_worker(TIER_ELEVATED, small)
        self.assertEqual(d.reason, "fallback_no_candidate")
        self.assertEqual(d.model_id, "c/h")

    def test_cost_helpers(self):
        sonnet = CAT["anthropic/claude-sonnet"]
        self.assertAlmostEqual(real_cost_usd(sonnet, 1_000_000, 0), 3.0)       # in price
        self.assertEqual(real_cost_usd(CAT["local/gemma"], 1_000_000, 1_000_000), 0.0)
        self.assertLess(effective_cost(CAT["local/gemma"], 3000, 1500, _CFG()), 0.01)


def _CFG():
    from core.router import RouterConfig
    return RouterConfig()


class VerdictDifficultyTest(unittest.TestCase):
    def test_parsed_and_clamped(self):
        self.assertEqual(parse_verdict('{"verdict":"pass","difficulty":2}').difficulty, 2)
        self.assertEqual(parse_verdict('{"verdict":"pass","difficulty":7}').difficulty, 3)   # clamp up
        self.assertEqual(parse_verdict('{"verdict":"pass","difficulty":0}').difficulty, 0)   # below 1 => 0
        self.assertEqual(parse_verdict('{"verdict":"pass"}').difficulty, 0)                  # missing

    def test_garbage_difficulty_never_fails_closed(self):
        # a malformed difficulty must NOT raise (that would block the dispatch) — it degrades to 0
        for bad in ('"high"', "null", "[3]", '"x"'):
            v = parse_verdict('{"verdict":"pass","difficulty":%s}' % bad)
            self.assertTrue(v.is_pass)
            self.assertEqual(v.difficulty, 0)


class _FakeResp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class LocalProbeTest(unittest.TestCase):
    def test_alive_caches_within_ttl(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _FakeResp(b'{"data":[{"id":"gemma-3-31b"}]}')

        t = {"v": 100.0}
        probe = local_probe.LocalProbe("http://localhost:1234/v1", ttl=15.0,
                                       clock=lambda: t["v"])
        orig = local_probe.urllib.request.urlopen
        local_probe.urllib.request.urlopen = fake_urlopen
        try:
            self.assertTrue(probe.alive())
            self.assertEqual(probe.models, ("gemma-3-31b",))
            t["v"] = 105.0                       # within TTL -> cached, no new probe
            self.assertTrue(probe.alive())
            self.assertEqual(calls["n"], 1)
            t["v"] = 130.0                       # past TTL -> re-probe
            self.assertTrue(probe.alive())
            self.assertEqual(calls["n"], 2)
        finally:
            local_probe.urllib.request.urlopen = orig

    def test_unreachable_is_not_alive_never_raises(self):
        def boom(req, timeout=None):
            raise OSError("connection refused")

        probe = local_probe.LocalProbe("http://localhost:9/v1", clock=lambda: 0.0)
        orig = local_probe.urllib.request.urlopen
        local_probe.urllib.request.urlopen = boom
        try:
            self.assertFalse(probe.alive())
            self.assertEqual(probe.models, ())
        finally:
            local_probe.urllib.request.urlopen = orig

    def test_empty_base_url_not_alive(self):
        self.assertFalse(local_probe.LocalProbe("").alive())


def _rec(**kw):
    from core.store import DispatchRecord
    base = dict(id="x", ts="t", base_prompt="b", added_directives=(), dispatched_prompt="b",
                verdict="pass", tier="standard", reviewer_model="m", decision="dispatched",
                round_count=1)
    base.update(kw)
    return DispatchRecord(**base)


class MetricsRoutingTest(unittest.TestCase):
    def _store(self, *records):
        from core.store_sqlite import SqliteAuditStore
        s = SqliteAuditStore(":memory:")
        for r in records:
            s.append(r)
        return s

    def test_savings_block(self):
        from server.views import compute_metrics
        s = self._store(
            _rec(id="a", routed_model="local/gemma", routed_is_local=True,
                 est_cost_usd=0.0, est_savings_usd=0.042),
            _rec(id="b", tier="elevated", routed_model="anthropic/claude-opus",
                 routed_is_local=False, est_cost_usd=0.15, est_savings_usd=0.0),
        )
        m = compute_metrics(s)["routing"]
        self.assertEqual(m["advised_total"], 2)
        self.assertEqual(m["advised_local"], 1)
        self.assertAlmostEqual(m["est_savings_usd"], 0.042)
        self.assertAlmostEqual(m["baseline_cloud_usd"], 0.192)

    def test_absent_when_routing_off(self):
        from server.views import compute_metrics
        self.assertNotIn("routing", compute_metrics(self._store(_rec(id="a"))))


if __name__ == "__main__":
    unittest.main()
