"""Blast-radius tiering (invariant: deterministic, computed by the dispatcher,
never by the reviewer).

Tiers, in precedence order:
  * merge_adjacent : the dispatch carries merge authority. Frontier review; a
                     block is a hard stop; round-cap fallback escalates to a human.
  * elevated       : touches protected paths (auth/crypto/CI/infra) or exceeds a
                     file/cost threshold. Frontier review; fallback escalates.
  * standard       : ordinary code change. Frontier review; fallback auto-accepts
                     the last base with the dissent logged.
  * trivial        : read-only or single-file. Skip frontier review (or cheap model).

All thresholds are configurable via ``TieringConfig``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIER_MERGE_ADJACENT = "merge_adjacent"
TIER_ELEVATED = "elevated"
TIER_STANDARD = "standard"
TIER_TRIVIAL = "trivial"

# Tiers that escalate to a human on a block verdict or round-cap exhaustion.
ESCALATING_TIERS = frozenset({TIER_MERGE_ADJACENT, TIER_ELEVATED})

# Default protected-path globs: auth, crypto, CI config, infra. Matched case-
# insensitively against any touched path. Extend via config, never narrow silently.
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    r"(^|/)auth",
    r"(^|/)login",
    r"(^|/)session",
    r"(^|/)(crypto|cipher|encrypt|secret|key|token|password|credential)",
    r"(^|/)\.github/workflows/",
    r"(^|/)\.gitlab-ci",
    r"(^|/)(Jenkinsfile|\.circleci|\.drone)",
    r"(^|/)(terraform|\.tf$|pulumi|cloudformation|k8s|kubernetes|helm|ansible)",
    r"(^|/)(Dockerfile|docker-compose|\.dockerignore)",
    r"(^|/)infra(structure)?/",
    r"(^|/)deploy",
    r"(^|/)(iam|rbac|policy|policies)/",
)


@dataclass(frozen=True)
class TieringConfig:
    """Thresholds for blast-radius classification. All configurable."""

    protected_patterns: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS
    elevated_file_threshold: int = 10
    elevated_cost_threshold_usd: float = 1.0
    trivial_max_files: int = 1

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p, re.IGNORECASE) for p in self.protected_patterns)


@dataclass(frozen=True)
class DispatchMeta:
    """The dispatcher's view of a pending dispatch. Drives deterministic tiering.

    None of these come from the reviewer — they are facts the orchestrator/dispatcher
    knows about the unit of work before any model call.
    """

    carries_merge_authority: bool = False
    touched_paths: tuple[str, ...] = ()
    file_count: int | None = None
    read_only: bool = False
    estimated_cost_usd: float = 0.0
    task_id: str = ""
    description: str = ""

    @property
    def effective_file_count(self) -> int:
        if self.file_count is not None:
            return self.file_count
        return len(self.touched_paths)


def matches_protected(paths, config: TieringConfig) -> list[str]:
    """Return the subset of paths that hit a protected pattern."""
    pats = config.compiled()
    hits: list[str] = []
    for p in paths:
        for pat in pats:
            if pat.search(p):
                hits.append(p)
                break
    return hits


def classify(meta: DispatchMeta, config: TieringConfig | None = None) -> str:
    """Deterministically classify a dispatch into a blast-radius tier.

    Precedence (highest blast radius wins): merge_adjacent > elevated > trivial >
    standard. A merge-carrying dispatch is always merge_adjacent even if it is also
    a single file — merge authority dominates.
    """
    config = config or TieringConfig()

    if meta.carries_merge_authority:
        return TIER_MERGE_ADJACENT

    if matches_protected(meta.touched_paths, config):
        return TIER_ELEVATED
    if meta.effective_file_count > config.elevated_file_threshold:
        return TIER_ELEVATED
    if meta.estimated_cost_usd > config.elevated_cost_threshold_usd:
        return TIER_ELEVATED

    # Trivial only after we've ruled out protected/over-threshold work above.
    if meta.read_only:
        return TIER_TRIVIAL
    if meta.effective_file_count <= config.trivial_max_files:
        return TIER_TRIVIAL

    return TIER_STANDARD


def skips_frontier_review(tier: str) -> bool:
    """Trivial dispatches skip frontier review (or use the cheap model)."""
    return tier == TIER_TRIVIAL
