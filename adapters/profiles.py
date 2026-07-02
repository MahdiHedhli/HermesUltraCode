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

# Marker file dropped in a profile dir at creation so reap can only ever touch OUR profiles.
_MANAGED_MARKER = ".ultracode-managed"


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


# A task in any of these statuses is a LIVE reference to its routed profile. `blocked` is
# included on purpose: an enqueued task is created blocked and only promoted to ready after
# validation, so a blocked-pre-promotion task must still protect its profile from reaping.
NON_TERMINAL_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "running",
                                   "blocked", "review"})


def referenced_profiles(task_rows: Iterable[tuple[str, str | None]]) -> set[str]:
    """Profiles referenced by a NON-TERMINAL task. ``task_rows`` is (status, routed_profile)
    pairs — the routed profile comes from each task's route tag. ``done``/``archived`` tasks
    are terminal and release their reference; everything else (incl. ``blocked``) holds it."""
    return {prof for status, prof in task_rows if prof and status in NON_TERMINAL_STATUSES}


def reap_managed(managed: Iterable[str], declared_static: Iterable[str],
                 referenced: Iterable[str], backend: ProfileBackend) -> list[str]:
    """Return the managed dynamic profiles safe to reap: only ones NOT declared static and
    NOT referenced by any non-terminal task (refcount, ``blocked`` included). Never reaps a
    declared static profile or one an open task routes to. (Actual deletion is the caller's;
    this is the pure, testable decision.)"""
    static = set(declared_static)
    live = set(referenced)
    return [m for m in managed if m not in static and m not in live and backend.profile_exists(m)]


# ---------------------------------------------------------------------------
# Real backend (lazy Hermes; only constructed in a live Hermes process)
# ---------------------------------------------------------------------------


# Provider credential / base-url env vars that could shadow a profile's own .env.
_CRED_SUFFIXES = ("_API_KEY", "_BASE_URL", "_API_BASE", "_API_MODE", "_API_SECRET",
                  "_SECRET_KEY", "_TOKEN")


def _is_provider_cred_env(key: str) -> bool:
    return key.upper().endswith(_CRED_SUFFIXES)


def _parse_env_file(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE .env parse (comments/blanks ignored, surrounding quotes stripped)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip():
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def isolated_env(base_environ: dict, home: str, dotenv_text: str) -> dict:
    """A subprocess env in which the ONLY provider credentials are the profile's own .env:
    every ambient provider cred is stripped, then the profile's .env is layered on. So an
    empty-.env profile can never borrow OPENAI_API_KEY (etc.) from the parent process
    (invariant #7 — a profile authenticates from ITS OWN secrets or not at all)."""
    env = {k: v for k, v in base_environ.items() if not _is_provider_cred_env(k)}
    env["HERMES_HOME"] = str(home)
    env.update(_parse_env_file(dotenv_text))
    return env


class HermesProfileBackend:
    """The production backend. Every method lazily imports ``hermes_cli`` so this module
    imports cleanly outside Hermes (tests never touch this class). ``served_models`` probes the
    live catalog in a SUBPROCESS whose env has ambient provider creds scrubbed and only the
    profile's own .env layered on, so it fails closed to ``None`` and can't be shadowed."""

    def __init__(self) -> None:
        self._served_cache: dict[str, "list[str] | None"] = {}

    def list_profiles(self) -> list[str]:
        from hermes_cli import profiles as _p
        return [getattr(i, "name", "") for i in _p.list_profiles()]

    def profile_exists(self, name: str) -> bool:
        from hermes_cli import profiles as _p
        return bool(_p.profile_exists(name))

    def create_profile(self, name: str, *, clone_from: str | None, description: str) -> None:
        import os
        from hermes_cli import profiles as _p
        # clone_config=False + no clone_all: bootstraps dirs + an EMPTY .env; copies NO secret.
        _p.create_profile(name, clone_from=clone_from, clone_config=False, description=description)
        # Drop a marker so reap can only ever touch profiles WE created (never a user profile).
        try:
            with open(os.path.join(str(_p.get_profile_dir(name)), _MANAGED_MARKER), "w") as fh:
                fh.write("created by hermesultracode\n")
        except OSError:
            pass

    def managed_profiles(self) -> list[str]:
        """Only profiles HermesUltraCode created (carry the marker) are reap candidates."""
        import os
        from hermes_cli import profiles as _p
        out = []
        for name in self.list_profiles():
            try:
                if os.path.exists(os.path.join(str(_p.get_profile_dir(name)), _MANAGED_MARKER)):
                    out.append(name)
            except OSError:
                continue
        return out

    def delete_profile(self, name: str) -> bool:
        """Delete a MANAGED profile only (guarded by the marker). Best-effort, never raises."""
        import os
        from hermes_cli import profiles as _p
        try:
            if not os.path.exists(os.path.join(str(_p.get_profile_dir(name)), _MANAGED_MARKER)):
                return False                       # refuse to delete an unmanaged (user) profile
            remove = getattr(_p, "remove_profile", None) or getattr(_p, "delete_profile", None)
            if remove:
                remove(name)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def set_description(self, name: str, text: str) -> None:
        try:
            from hermes_cli import profiles as _p
            setter = getattr(_p, "set_profile_description", None)
            if setter:
                setter(name, text)
        except Exception:  # noqa: BLE001 - description refresh is best-effort
            pass

    def auth_ok(self, name: str) -> bool:
        # A profile is authenticated iff it serves a live catalog from ITS OWN creds.
        return bool(self._served_cached(name))

    def served_models(self, name: str) -> list[str] | None:
        return self._served_cached(name)

    # -- internals --------------------------------------------------------

    def _served_cached(self, name: str) -> list[str] | None:
        if name not in self._served_cache:
            self._served_cache[name] = self._served(name)
        return self._served_cache[name]

    def _served(self, name: str) -> list[str] | None:
        """Live GET <base>/models for the profile, probed in a SUBPROCESS whose env has ambient
        provider creds SCRUBBED and only the profile's own .env layered on (``isolated_env``), so
        an empty-.env profile can't be shadowed by OPENAI_API_KEY (etc.) in the parent. None on
        any failure — the fail-closed source of truth (NOT the curated/models.dev/openrouter merge)."""
        import json
        import os
        import subprocess
        import sys
        try:
            from hermes_cli import profiles as _p
            home = str(_p.get_profile_dir(name))
            pid = self._provider_id(name)
        except Exception:  # noqa: BLE001
            return None
        try:
            with open(os.path.join(home, ".env"), "r", encoding="utf-8") as fh:
                dotenv = fh.read()
        except OSError:
            dotenv = ""
        probe = (
            "import json;"
            "from hermes_cli import auth, models;"
            f"c=auth.resolve_api_key_provider_credentials({pid!r});"
            "m=models.fetch_api_models(c.get('api_key') or '', c.get('base_url') or '', timeout=5.0);"
            "print(json.dumps(list(m) if m else []))"
        )
        try:
            r = subprocess.run([sys.executable, "-c", probe],
                               env=isolated_env(os.environ, home, dotenv),
                               capture_output=True, text=True, timeout=12)
            if r.returncode != 0:
                return None
            served = json.loads((r.stdout or "").strip() or "[]")
            return list(served) or None
        except Exception:  # noqa: BLE001
            return None

    def _provider_id(self, name: str) -> str:
        from hermes_cli import profiles as _p
        info = next((i for i in _p.list_profiles() if getattr(i, "name", "") == name), None)
        return getattr(info, "provider", "") or name
