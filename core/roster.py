"""Roster manifest: the declared source of truth for HermesUltraCode's provider topology.

Pure and offline — NO Hermes import, NO network — so it is fully unit-testable. The YAML
load is a thin, lazily-imported shell over ``Roster.from_dict``; all logic works on plain
dicts. This module owns exactly one hard invariant (#8): a ``cross_lab`` reviewer's lab MUST
differ from the orchestrator's, and a reviewer that shares the orchestrator's lab may never
present as cross-lab. The resolved ``reviewer_mode`` is always explicit and surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Risk tiers (mirror core.tiering). Roster.routing is keyed by these.
TIERS = ("trivial", "standard", "elevated", "merge_adjacent")

REVIEWER_CROSS_LAB = "cross_lab"
REVIEWER_SAME_LAB_FLAGGED = "same_lab_flagged"
REVIEWER_OFF = "off"
REVIEWER_MODES = frozenset({REVIEWER_CROSS_LAB, REVIEWER_SAME_LAB_FLAGGED, REVIEWER_OFF})


class RosterError(ValueError):
    """Raised when a roster is malformed or violates the reviewer-lab invariant."""


@dataclass(frozen=True)
class Endpoint:
    """The orchestrator or reviewer: a profile plus the lab it runs on."""

    profile: str
    lab: str = ""


@dataclass(frozen=True)
class ProviderProfile:
    """One durable provider connection (one per provider). ``tiers`` holds OPERATOR-SUPPLIED
    tier->model slots (never invented here). ``model`` is a pinned default (local/pareto).
    ``flat_rate`` marks a subscription seat, preferred under ``subscription-first``."""

    profile: str
    provider: str = ""
    lab: str = ""
    description: str = ""
    tiers: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    flat_rate: bool = False
    min_coding_score: float | None = None


@dataclass(frozen=True)
class Roster:
    orchestrator: Endpoint
    reviewer: Endpoint | None
    reviewer_mode: str
    providers: tuple[ProviderProfile, ...]
    budget_mode: str = "subscription-first"
    routing: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def provider(self, name: str) -> ProviderProfile | None:
        for p in self.providers:
            if p.profile == name:
                return p
        return None

    @property
    def labs(self) -> set[str]:
        labs = {self.orchestrator.lab} | {p.lab for p in self.providers if p.lab}
        return {x for x in labs if x}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Roster":
        if not isinstance(d, dict):
            raise RosterError("roster must be a mapping")
        orch_d = d.get("orchestrator") or {}
        orchestrator = Endpoint(profile=str(orch_d.get("profile") or "").strip(),
                                lab=str(orch_d.get("lab") or "").strip().lower())
        if not orchestrator.profile:
            raise RosterError("orchestrator.profile is required")

        providers = tuple(
            ProviderProfile(
                profile=str(p.get("profile") or "").strip(),
                provider=str(p.get("provider") or "").strip(),
                lab=str(p.get("lab") or "").strip().lower(),
                description=str(p.get("description") or ""),
                tiers=dict(p.get("tiers") or {}),
                model=(str(p["model"]).strip() or None) if p.get("model") else None,
                flat_rate=bool(p.get("flat_rate", False)),
                min_coding_score=(float(p["min_coding_score"]) if p.get("min_coding_score") is not None else None),
            )
            for p in (d.get("providers") or [])
            if isinstance(p, dict) and str(p.get("profile") or "").strip()
        )

        rev_d = d.get("reviewer")
        reviewer = None
        if isinstance(rev_d, dict) and str(rev_d.get("profile") or "").strip():
            rev_profile = str(rev_d["profile"]).strip()
            # reviewer lab: explicit, else inherited from its provider profile
            rev_lab = str(rev_d.get("lab") or "").strip().lower()
            if not rev_lab:
                match = next((p for p in providers if p.profile == rev_profile), None)
                rev_lab = match.lab if match else ""
            reviewer = Endpoint(profile=rev_profile, lab=rev_lab)

        routing: dict[str, tuple[str, ...]] = {}
        for tier, names in (d.get("routing") or {}).items():
            if tier in TIERS and isinstance(names, (list, tuple)):
                routing[tier] = tuple(str(n).strip() for n in names if str(n).strip())

        mode = str(d.get("reviewer_mode") or "").strip().lower()
        roster = cls(
            orchestrator=orchestrator,
            reviewer=reviewer,
            reviewer_mode=mode or resolve_reviewer_mode(orchestrator, reviewer, requested=None),
            providers=providers,
            budget_mode=str(d.get("budget_mode") or "subscription-first").strip(),
            routing=routing,
        )
        roster.validate()
        return roster

    def validate(self) -> None:
        if self.reviewer_mode not in REVIEWER_MODES:
            raise RosterError(f"reviewer_mode must be one of {sorted(REVIEWER_MODES)}")
        # Invariant #8: a cross-lab reviewer must exist and differ in lab from the orchestrator.
        if self.reviewer_mode == REVIEWER_CROSS_LAB:
            if self.reviewer is None:
                raise RosterError("reviewer_mode=cross_lab requires a reviewer")
            if not self.reviewer.lab or self.reviewer.lab == self.orchestrator.lab:
                raise RosterError(
                    "reviewer_mode=cross_lab but reviewer lab "
                    f"({self.reviewer.lab!r}) matches or is missing vs orchestrator "
                    f"({self.orchestrator.lab!r}) — a same-lab reviewer must never present as cross-lab"
                )
        # routing tiers must reference declared provider profiles
        declared = {p.profile for p in self.providers}
        for tier, names in self.routing.items():
            for n in names:
                if n not in declared:
                    raise RosterError(f"routing[{tier}] references undeclared profile {n!r}")


def resolve_reviewer_mode(orchestrator: Endpoint, reviewer: Endpoint | None, *,
                          requested: str | None) -> str:
    """Resolve the reviewer mode explicitly. Two+ labs available (a reviewer on a different
    lab) => cross_lab. A reviewer sharing the orchestrator's lab can only be
    ``same_lab_flagged`` (weaker) or ``off`` — never cross_lab. No reviewer => off. A
    ``requested`` mode is honored only if consistent with the labs; otherwise it is refused."""
    if reviewer is None or not reviewer.profile:
        return REVIEWER_OFF
    cross_ok = bool(reviewer.lab) and reviewer.lab != orchestrator.lab
    if requested:
        req = requested.strip().lower()
        if req == REVIEWER_CROSS_LAB and not cross_ok:
            raise RosterError("cannot request cross_lab: reviewer shares the orchestrator's lab")
        if req in REVIEWER_MODES:
            return req
    return REVIEWER_CROSS_LAB if cross_ok else REVIEWER_SAME_LAB_FLAGGED


def load_roster(path: str) -> Roster:
    """Load + validate roster.yaml. YAML is imported lazily so the pure logic above stays
    dependency-free and offline-testable via ``Roster.from_dict``."""
    import yaml  # lazy: Hermes ships PyYAML; core stays stdlib-only at import time

    with open(path, "r", encoding="utf-8") as fh:
        return Roster.from_dict(yaml.safe_load(fh) or {})
