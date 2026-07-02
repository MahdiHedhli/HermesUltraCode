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

# The dedicated Kanban board ultracode work is scoped to (hard isolation boundary). Declared
# here as the default; every surface reads roster.board — it is NEVER a literal in the poller
# or adapters.
DEFAULT_BOARD = "ultracode"


class RosterError(ValueError):
    """Raised when a roster is malformed or violates the reviewer-lab invariant."""


@dataclass(frozen=True)
class Council:
    """A MoA / council endpoint: several member labs plus an aggregator lab. The aggregator
    is the model that actually DECIDES, so it counts toward the endpoint's lab set — a diverse
    council whose aggregator shares the orchestrator's lab still hands the verdict back to the
    orchestrator's blind spots."""

    members: tuple[str, ...] = ()
    aggregator: str = ""


@dataclass(frozen=True)
class Endpoint:
    """The orchestrator or reviewer: a profile plus the lab(s) it runs on — a single ``lab``
    for a plain model, or a ``council`` (members + aggregator) for a MoA preset."""

    profile: str
    lab: str = ""
    council: Council | None = None


def labs_of(endpoint: "Endpoint | None") -> frozenset[str]:
    """The full set of labs an endpoint spans. A plain model is the singleton ``{lab}``; a
    COUNCIL is ``set(members) | {aggregator}`` — the aggregator is ALWAYS included."""
    if endpoint is None:
        return frozenset()
    labs: set[str] = set()
    if endpoint.council is not None:
        labs |= set(endpoint.council.members)
        if endpoint.council.aggregator:
            labs.add(endpoint.council.aggregator)
    if endpoint.lab:                       # union a plain lab too (defense in depth; never dropped)
        labs.add(endpoint.lab)
    return frozenset(x for x in labs if x)


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
    board: str = DEFAULT_BOARD

    def provider(self, name: str) -> ProviderProfile | None:
        for p in self.providers:
            if p.profile == name:
                return p
        return None

    @property
    def labs(self) -> set[str]:
        labs = set(labs_of(self.orchestrator)) | {p.lab for p in self.providers if p.lab}
        return {x for x in labs if x}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Roster":
        if not isinstance(d, dict):
            raise RosterError("roster must be a mapping")

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

        orchestrator = _parse_endpoint(d.get("orchestrator") or {}, providers)
        if orchestrator is None or not orchestrator.profile:
            raise RosterError("orchestrator.profile is required")
        reviewer = _parse_endpoint(d.get("reviewer") or {}, providers)

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
            board=(str(d.get("board") or "").strip() or DEFAULT_BOARD),
        )
        roster.validate()
        return roster

    def validate(self) -> None:
        if self.reviewer_mode not in REVIEWER_MODES:
            raise RosterError(f"reviewer_mode must be one of {sorted(REVIEWER_MODES)}")
        # Invariant #8, council-safe: the reviewer's lab SET must be disjoint from the
        # orchestrator's. labs_of() includes a council's aggregator, so a diverse council whose
        # aggregator matches the orchestrator's lab (the model that actually decides) fails —
        # it would hand the verdict straight back to the orchestrator's blind spots.
        if self.reviewer_mode == REVIEWER_CROSS_LAB:
            if self.reviewer is None:
                raise RosterError("reviewer_mode=cross_lab requires a reviewer")
            r_labs, o_labs = labs_of(self.reviewer), labs_of(self.orchestrator)
            collision = r_labs & o_labs
            # Fail closed on BOTH an intersection AND an unknown lab on either side — an
            # un-lab'd orchestrator or reviewer can never be *certified* disjoint.
            if not r_labs or not o_labs or collision:
                raise RosterError(
                    f"reviewer_mode=cross_lab but reviewer labs {sorted(r_labs)} vs orchestrator "
                    f"{sorted(o_labs)} — cross-lab requires BOTH sides to declare known, disjoint labs "
                    f"(collision: {sorted(collision)}). An unknown lab (incl. a council whose aggregator "
                    "matches) can never present as cross-lab."
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
    r_labs, o_labs = labs_of(reviewer), labs_of(orchestrator)
    # council-safe (aggregator included) AND fail-closed: BOTH sides must have a known lab.
    cross_ok = bool(r_labs) and bool(o_labs) and r_labs.isdisjoint(o_labs)
    if requested:
        req = requested.strip().lower()
        if req == REVIEWER_CROSS_LAB and not cross_ok:
            raise RosterError("cannot request cross_lab: reviewer labs intersect the orchestrator's")
        if req in REVIEWER_MODES:
            return req
    return REVIEWER_CROSS_LAB if cross_ok else REVIEWER_SAME_LAB_FLAGGED


def _parse_endpoint(d: dict[str, Any], providers: tuple[ProviderProfile, ...]) -> Endpoint | None:
    """Parse an orchestrator/reviewer entry: either a plain ``lab`` (inherited from its
    provider profile if omitted) or a ``council: {members: [...], aggregator: <lab>}`` block."""
    if not isinstance(d, dict) or not str(d.get("profile") or "").strip():
        return None
    profile = str(d["profile"]).strip()
    council_d = d.get("council")
    if isinstance(council_d, dict) and (council_d.get("members") or council_d.get("aggregator")):
        members = tuple(str(m).strip().lower() for m in (council_d.get("members") or []) if str(m).strip())
        aggregator = str(council_d.get("aggregator") or "").strip().lower()
        return Endpoint(profile=profile, council=Council(members=members, aggregator=aggregator))
    lab = str(d.get("lab") or "").strip().lower()
    if not lab:
        match = next((p for p in providers if p.profile == profile), None)
        lab = match.lab if match else ""
    return Endpoint(profile=profile, lab=lab)


def load_roster(path: str) -> Roster:
    """Load + validate roster.yaml. YAML is imported lazily so the pure logic above stays
    dependency-free and offline-testable via ``Roster.from_dict``."""
    import yaml  # lazy: Hermes ships PyYAML; core stays stdlib-only at import time

    with open(path, "r", encoding="utf-8") as fh:
        return Roster.from_dict(yaml.safe_load(fh) or {})
