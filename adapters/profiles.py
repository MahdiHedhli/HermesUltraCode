"""Profile manager / reconciler — thin, Hermes-coupled, and strictly OUT OF BAND (runs at
setup and roster change, NEVER on the LLM path; not registered as an agent tool — invariant
"control-plane stays out of the model's reach").

All Hermes coupling is quarantined behind a ``ProfileBackend`` seam so the reconciler LOGIC
(create-if-absent, match-before-create, fail-closed readiness, refcount reap) is fully
offline-testable with a fake backend. The real backend lazily imports ``hermes_cli`` and
scopes every per-profile check to that profile's home, so ambient provider env can't shadow
a profile's ``.env`` (invariant #7 — validate the tagged model against the profile's
AUTHENTICATED catalog; never trust the openrouter fallback).

Two hard rules this module enforces:
  * **Never writes a credential.** ``ensure`` creates only non-secret dimensions
    (``clone_config=False``); reconciliation automates only the non-secret side (invariant #10).
  * **Match, don't make.** ``ensure`` is create-if-absent; it matches an existing profile
    or creates one — it never overwrites (invariant #6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from core.roster import ProviderProfile, Roster


# ---------------------------------------------------------------------------
# Backend seam (all Hermes coupling lives behind this)
# ---------------------------------------------------------------------------


class ProfileBackend(Protocol):
    def list_profiles(self) -> list[str]: ...
    def profile_exists(self, name: str) -> bool: ...
    # create-if-absent, NON-SECRET only. Must NOT copy any .env/credential.
    def create_profile(self, name: str, *, clone_from: str | None, description: str) -> None: ...
    def set_description(self, name: str, text: str) -> None: ...
    def auth_ok(self, name: str) -> bool: ...
    # live authenticated catalog for the profile; None on unauth/unreachable (fail-closed).
    def served_models(self, name: str) -> list[str] | None: ...


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    profile: str
    ready: bool
    reason: str = ""
    served_count: int = 0
    missing: tuple[str, ...] = ()


@dataclass
class ReconcileReport:
    created: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    not_ready: list[Readiness] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.not_ready and not self.errors


# Names the router/reconciler must never reap or collide with.
_RESERVED = frozenset({"default", "hermes", "test", "tmp", "root", "sudo"})


def valid_profile_name(name: str) -> bool:
    """`_PROFILE_ID_RE` = ^[a-z0-9][a-z0-9_-]{0,63}$ and not reserved (mirrors Hermes)."""
    import re
    if not name or name in _RESERVED:
        return False
    return re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name) is not None


# ---------------------------------------------------------------------------
# Reconciler logic (pure over the backend — offline-testable)
# ---------------------------------------------------------------------------


def ensure(profile: str, backend: ProfileBackend, *, clone_from: str | None = None,
           description: str = "") -> bool:
    """Create-if-absent (invariant #6, match-don't-make). Returns True if it created the
    profile, False if it already existed. NEVER copies credentials (the backend's
    ``create_profile`` uses non-secret dimensions only, invariant #10)."""
    if not valid_profile_name(profile):
        raise ValueError(f"invalid or reserved profile name: {profile!r}")
    if backend.profile_exists(profile):
        return False
    backend.create_profile(profile, clone_from=clone_from, description=description)
    return True


def validate_ready(profile: str, required_models: Iterable[str], backend: ProfileBackend) -> Readiness:
    """Fail-closed readiness (invariant #7). Not authenticated -> not ready. No served
    catalog (None/[] — unauthenticated OR unreachable) -> not ready; we NEVER treat a
    missing catalog as "assume served" and never trust the openrouter fallback. A routed
    model absent from the live catalog -> not ready."""
    if not backend.auth_ok(profile):
        return Readiness(profile, False, "unauthenticated")
    served = backend.served_models(profile)
    if not served:                                   # None or [] both BLOCK
        return Readiness(profile, False, "no authenticated served catalog (unauth or unreachable)")
    served_set = set(served)
    required = {m for m in required_models if m}
    missing = tuple(sorted(m for m in required if m not in served_set))
    if missing:
        return Readiness(profile, False, "routed model(s) not served: " + ", ".join(missing),
                         len(served_set), missing)
    return Readiness(profile, True, "ready", len(served_set), ())


def routed_models(roster: Roster, profile: str) -> set[str]:
    """The concrete model slugs a profile must serve: its operator-supplied tier slots plus
    any pinned default. Empty until the operator fills the roster (stub phase validates auth
    + a non-empty catalog only)."""
    p = roster.provider(profile)
    if p is None:
        return set()
    models = {v for v in p.tiers.values() if v and not str(v).startswith("<")}  # ignore <SLOT> placeholders
    if p.model:
        models.add(p.model)
    return models


def reconcile(roster: Roster, backend: ProfileBackend) -> ReconcileReport:
    """Match declared vs actual, create-if-absent, refresh descriptions, validate readiness.
    Match before create; never spawn unnecessary profiles. Out of band only."""
    report = ReconcileReport()
    for p in roster.providers:
        try:
            if ensure(p.profile, backend, description=p.description):
                report.created.append(p.profile)
            elif p.description:
                backend.set_description(p.profile, p.description)
                report.refreshed.append(p.profile)
            r = validate_ready(p.profile, routed_models(roster, p.profile), backend)
            (report.ready.append(p.profile) if r.ready else report.not_ready.append(r))
        except Exception as exc:  # noqa: BLE001 - reconciliation reports, never crashes setup
            report.errors.append(f"{p.profile}: {exc}")
    return report


def reap_managed(managed: Iterable[str], declared_static: Iterable[str],
                 open_assignees: Iterable[str], backend: ProfileBackend) -> list[str]:
    """Return the managed dynamic profiles safe to reap: only ones NOT declared static and
    NOT referenced by any non-terminal task's assignee (refcount). Never reaps a declared
    static profile or one with an open assignee. (Actual deletion is the caller's; this is
    the pure, testable decision.)"""
    static = set(declared_static)
    referenced = set(open_assignees)
    return [m for m in managed if m not in static and m not in referenced and backend.profile_exists(m)]


# ---------------------------------------------------------------------------
# Real backend (lazy Hermes; only constructed in a live Hermes process)
# ---------------------------------------------------------------------------


class HermesProfileBackend:
    """The production backend. Every method lazily imports ``hermes_cli`` so this module
    imports cleanly outside Hermes (tests never touch this class). ``served_models`` scopes
    the auth+catalog probe to the profile's home via the HERMES_HOME override the map
    verified, and fails closed to ``None`` on any error."""

    def list_profiles(self) -> list[str]:
        from hermes_cli import profiles as _p
        return [getattr(i, "name", "") for i in _p.list_profiles()]

    def profile_exists(self, name: str) -> bool:
        from hermes_cli import profiles as _p
        return bool(_p.profile_exists(name))

    def create_profile(self, name: str, *, clone_from: str | None, description: str) -> None:
        from hermes_cli import profiles as _p
        # clone_config=False + no clone_all: bootstraps dirs + an EMPTY .env; copies NO secret.
        _p.create_profile(name, clone_from=clone_from, clone_config=False, description=description)

    def set_description(self, name: str, text: str) -> None:
        try:
            from hermes_cli import profiles as _p
            setter = getattr(_p, "set_profile_description", None)
            if setter:
                setter(name, text)
        except Exception:  # noqa: BLE001 - description refresh is best-effort
            pass

    def auth_ok(self, name: str) -> bool:
        return self._served(name) not in (None,) and self._auth_status(name)

    def served_models(self, name: str) -> list[str] | None:
        return self._served(name)

    # -- internals --------------------------------------------------------

    def _auth_status(self, name: str) -> bool:
        try:
            from hermes_cli import auth, hermes_constants, profiles as _p
            tok = hermes_constants.set_hermes_home_override(str(_p.get_profile_dir(name)))
            try:
                pid = self._provider_id(name)
                return bool(auth.get_auth_status(pid).get("logged_in"))
            finally:
                hermes_constants.reset_hermes_home_override(tok)
        except Exception:  # noqa: BLE001
            return False

    def _served(self, name: str) -> list[str] | None:
        """Live GET <base>/models scoped to the profile home. None on any failure — the
        fail-closed source of truth (NOT the curated/models.dev/openrouter merge)."""
        try:
            from hermes_cli import auth, hermes_constants, models, profiles as _p
            tok = hermes_constants.set_hermes_home_override(str(_p.get_profile_dir(name)))
            try:
                pid = self._provider_id(name)
                creds = auth.resolve_api_key_provider_credentials(pid)
                served = models.fetch_api_models(creds["api_key"], creds["base_url"], timeout=5.0)
                return list(served) if served else None
            finally:
                hermes_constants.reset_hermes_home_override(tok)
        except Exception:  # noqa: BLE001
            return None

    def _provider_id(self, name: str) -> str:
        from hermes_cli import profiles as _p
        info = next((i for i in _p.list_profiles() if getattr(i, "name", "") == name), None)
        return getattr(info, "provider", "") or name
