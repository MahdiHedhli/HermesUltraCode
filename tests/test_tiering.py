"""Acceptance criterion 6: tiering classifies merge-touching, protected-path,
over-threshold, and trivial dispatches correctly (deterministic, dispatcher-side)."""

import unittest

from core.tiering import (
    TIER_ELEVATED,
    TIER_MERGE_ADJACENT,
    TIER_STANDARD,
    TIER_TRIVIAL,
    DispatchMeta,
    TieringConfig,
    classify,
    matches_protected,
    skips_frontier_review,
)


class TieringTest(unittest.TestCase):
    def test_merge_authority_dominates(self):
        meta = DispatchMeta(carries_merge_authority=True, touched_paths=("readme.md",),
                            read_only=True)
        self.assertEqual(classify(meta), TIER_MERGE_ADJACENT)

    def test_protected_paths_elevate(self):
        for p in [
            "src/auth/session.py",
            "lib/crypto/cipher.go",
            ".github/workflows/deploy.yml",
            "infra/terraform/main.tf",
            "Dockerfile",
            "k8s/deployment.yaml",
            "config/iam/policy.json",
        ]:
            meta = DispatchMeta(touched_paths=(p,), read_only=False)
            self.assertEqual(classify(meta), TIER_ELEVATED, msg=p)

    def test_file_count_threshold_elevates(self):
        cfg = TieringConfig(elevated_file_threshold=3)
        meta = DispatchMeta(touched_paths=tuple(f"src/f{i}.py" for i in range(5)))
        self.assertEqual(classify(meta, cfg), TIER_ELEVATED)

    def test_cost_threshold_elevates(self):
        cfg = TieringConfig(elevated_cost_threshold_usd=0.5)
        meta = DispatchMeta(touched_paths=("src/a.py", "src/b.py"), estimated_cost_usd=0.9)
        self.assertEqual(classify(meta, cfg), TIER_ELEVATED)

    def test_read_only_is_trivial(self):
        meta = DispatchMeta(touched_paths=("src/a.py", "src/b.py"), read_only=True)
        self.assertEqual(classify(meta), TIER_TRIVIAL)

    def test_single_file_is_trivial(self):
        meta = DispatchMeta(touched_paths=("src/a.py",), read_only=False)
        self.assertEqual(classify(meta), TIER_TRIVIAL)

    def test_ordinary_multifile_is_standard(self):
        meta = DispatchMeta(touched_paths=("src/a.py", "src/b.py", "src/c.py"), read_only=False)
        self.assertEqual(classify(meta), TIER_STANDARD)

    def test_protected_beats_trivial(self):
        # A single protected file is elevated, not trivial.
        meta = DispatchMeta(touched_paths=("src/auth.py",), read_only=False)
        self.assertEqual(classify(meta), TIER_ELEVATED)

    def test_merge_beats_protected(self):
        meta = DispatchMeta(carries_merge_authority=True, touched_paths=("src/auth.py",))
        self.assertEqual(classify(meta), TIER_MERGE_ADJACENT)

    def test_trivial_skips_frontier_review(self):
        self.assertTrue(skips_frontier_review(TIER_TRIVIAL))
        self.assertFalse(skips_frontier_review(TIER_STANDARD))
        self.assertFalse(skips_frontier_review(TIER_ELEVATED))
        self.assertFalse(skips_frontier_review(TIER_MERGE_ADJACENT))

    def test_matches_protected_returns_hits(self):
        hits = matches_protected(("README.md", "src/auth/x.py", "src/util.py"), TieringConfig())
        self.assertEqual(hits, ["src/auth/x.py"])

    def test_determinism(self):
        meta = DispatchMeta(touched_paths=("src/a.py", "src/b.py", "src/c.py"))
        self.assertEqual({classify(meta) for _ in range(50)}, {TIER_STANDARD})


if __name__ == "__main__":
    unittest.main()
