"""Gate configuration + startup validation.

ponytail: stdlib only — a dataclass loaded from a dict / JSON / env. No pydantic,
no PyYAML (rung 1: doesn't need to exist; rung 2: stdlib does it). Upgrade path is
pydantic-settings if config grows a schema worth validating richly.

Startup validation enforces invariant 6: the reviewer provider must be a different
lab from the orchestrator, or ``load_config`` raises ``ProviderConfigError`` before
any dispatch can run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any

from .providers import (
    HermesProvider,
    OpenRouterProvider,
    Provider,
    ProviderConfigError,
    validate_distinct_providers,
)
from .tiering import TieringConfig

PONYTAIL_LITE = "lite"
PONYTAIL_FULL = "full"


@dataclass(frozen=True)
class ReadApiConfig:
    """Read API / dashboard security knobs. Loopback + token by default."""

    host: str = "127.0.0.1"
    port: int = 9120
    session_token: str = ""  # filled at startup if blank (ephemeral)
    allowed_hosts: tuple[str, ...] = ()  # filled from host/port if empty
    require_auth_when_nonloopback: bool = True

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class GateConfig:
    """The whole gate's runtime configuration."""

    orchestrator_provider: Provider
    reviewer_provider: Provider
    round_cap: int = 2
    reviewer_timeout_s: float = 30.0
    max_directives: int = 12
    max_directive_len: int = 2000
    tiering: TieringConfig = field(default_factory=TieringConfig)
    store_path: str = "gate_audit.sqlite3"
    ponytail_orchestrator_mode: str = PONYTAIL_FULL  # lite|full, full default
    ponytail_worker_mode: str = PONYTAIL_FULL  # full on workers
    read_api: ReadApiConfig = field(default_factory=ReadApiConfig)
    # cheap model used for trivial-tier reviews (optional; None -> skip review).
    cheap_reviewer_provider: Provider | None = None

    def __post_init__(self) -> None:
        if self.round_cap < 1:
            raise ProviderConfigError("round_cap must be >= 1")
        if self.ponytail_orchestrator_mode not in (PONYTAIL_LITE, PONYTAIL_FULL):
            raise ProviderConfigError("ponytail_orchestrator_mode must be lite|full")
        if self.ponytail_worker_mode != PONYTAIL_FULL:
            # Workers are always full (invariant 6: minimalism is generation-time on
            # orchestrator+workers; workers stay strict).
            raise ProviderConfigError("ponytail_worker_mode must be 'full' for workers")
        # Invariant 6 / startup rule — the headline check.
        validate_distinct_providers(self.orchestrator_provider, self.reviewer_provider)


_PROVIDER_REGISTRY: dict[str, type[Provider]] = {
    "openrouter": OpenRouterProvider,
    "hermes": HermesProvider,
}


def _build_provider(spec: Any) -> Provider:
    """Build a provider from a dict spec or accept an already-constructed Provider."""
    if isinstance(spec, Provider):
        return spec
    if not isinstance(spec, dict):
        raise ProviderConfigError(f"provider spec must be a dict or Provider, got {type(spec)}")
    kind = spec.get("kind")
    if kind not in _PROVIDER_REGISTRY:
        raise ProviderConfigError(
            f"unknown provider kind {kind!r}; known: {sorted(_PROVIDER_REGISTRY)}"
        )
    cls = _PROVIDER_REGISTRY[kind]
    kwargs = {k: v for k, v in spec.items() if k != "kind"}
    # api_key may reference an env var: {"api_key_env": "OPENROUTER_KEY"}
    if "api_key_env" in kwargs:
        kwargs["api_key"] = os.environ.get(kwargs.pop("api_key_env"), "")
    return cls(**kwargs)


def load_config(source: dict[str, Any] | str) -> GateConfig:
    """Load and validate config from a dict or a path to a JSON file.

    Providers may be given as constructed ``Provider`` objects (tests) or as dict
    specs ``{"kind": "openrouter", "model": "...", "lab": "..."}``.
    """
    if isinstance(source, str):
        with open(source, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = dict(source)

    if "orchestrator_provider" not in data or "reviewer_provider" not in data:
        raise ProviderConfigError(
            "config requires 'orchestrator_provider' and 'reviewer_provider'"
        )

    orch = _build_provider(data["orchestrator_provider"])
    rev = _build_provider(data["reviewer_provider"])
    cheap = data.get("cheap_reviewer_provider")
    cheap_provider = _build_provider(cheap) if cheap else None

    tiering_data = data.get("tiering")
    tiering = TieringConfig(**tiering_data) if isinstance(tiering_data, dict) else TieringConfig()

    read_api_data = data.get("read_api", {})
    read_api = ReadApiConfig(**read_api_data) if isinstance(read_api_data, dict) else ReadApiConfig()

    cfg = GateConfig(
        orchestrator_provider=orch,
        reviewer_provider=rev,
        round_cap=int(data.get("round_cap", 2)),
        reviewer_timeout_s=float(data.get("reviewer_timeout_s", 30.0)),
        max_directives=int(data.get("max_directives", 12)),
        max_directive_len=int(data.get("max_directive_len", 2000)),
        tiering=tiering,
        store_path=data.get("store_path", "gate_audit.sqlite3"),
        ponytail_orchestrator_mode=data.get("ponytail_orchestrator_mode", PONYTAIL_FULL),
        ponytail_worker_mode=data.get("ponytail_worker_mode", PONYTAIL_FULL),
        read_api=read_api,
        cheap_reviewer_provider=cheap_provider,
    )
    return cfg


def ensure_session_token(read_api: ReadApiConfig) -> ReadApiConfig:
    """Return a copy with an ephemeral session token if none was set."""
    if read_api.session_token:
        return read_api
    import secrets

    token = secrets.token_urlsafe(32)
    allowed = read_api.allowed_hosts or (
        f"{read_api.host}:{read_api.port}",
        f"localhost:{read_api.port}",
        f"127.0.0.1:{read_api.port}",
    )
    return replace(read_api, session_token=token, allowed_hosts=tuple(allowed))
