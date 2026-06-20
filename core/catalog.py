"""Model catalog for cost-aware routing: capability tier + price (or ~0 for local) per
model. Pure data plus a stdlib-json loader, no provider calls. The router
(``core/router.py``) reads this to pick the cheapest model that clears a task's required
capability tier. Default-off feature: only consulted when routing is enabled.

A LOCAL model (e.g. Gemma on LM Studio / a Mac Studio) is clamped to ``local_trusted_tier``
no matter what it declares, so it can never be routed elevated/architectural work on price
alone — risk overrides cost (see the router).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Capability tiers mirror the blast-radius ladder the router maps onto:
#   1 = trivial / cheap-fast work, 2 = standard features & refactors, 3 = hard / architectural.
CAP_MIN, CAP_MAX = 1, 3


@dataclass(frozen=True)
class CatalogEntry:
    model_id: str
    lab: str
    capability_tier: int = 1          # declared ceiling of what this model is trusted to do
    ctx: int = 8192                   # context window (tokens)
    is_local: bool = False
    usd_per_mtok_in: float = 0.0      # API price; ~0 for local (costed via electricity estimate)
    usd_per_mtok_out: float = 0.0
    tok_per_s: float = 30.0           # local decode rate, for the electricity estimate
    max_concurrency: int = 1          # a local box is one finite resource

    def trusted_tier(self, local_trusted_tier: int = 2) -> int:
        """Effective capability ceiling. A LOCAL (often quantized) model is clamped to
        ``local_trusted_tier`` regardless of its declared tier."""
        ceiling = local_trusted_tier if self.is_local else CAP_MAX
        return max(CAP_MIN, min(int(self.capability_tier), ceiling))


# A small, honest default. Prices change; override via HERMESULTRACODE_MODEL_CATALOG (a JSON
# file: {"models": {model_id: spec, ...}}). The local entry models LM Studio's Gemma; its
# dollar price is 0 (the router costs it by electricity, see effective_cost).
DEFAULT_CATALOG: dict[str, dict[str, Any]] = {
    "local/gemma": {
        "lab": "local", "capability_tier": 2, "ctx": 32768, "is_local": True,
        "usd_per_mtok_in": 0.0, "usd_per_mtok_out": 0.0, "tok_per_s": 40.0, "max_concurrency": 1,
    },
    "anthropic/claude-haiku":  {"lab": "anthropic", "capability_tier": 1, "ctx": 200000,
                                "usd_per_mtok_in": 0.80, "usd_per_mtok_out": 4.0},
    "anthropic/claude-sonnet": {"lab": "anthropic", "capability_tier": 2, "ctx": 200000,
                                "usd_per_mtok_in": 3.0, "usd_per_mtok_out": 15.0},
    "anthropic/claude-opus":   {"lab": "anthropic", "capability_tier": 3, "ctx": 200000,
                                "usd_per_mtok_in": 15.0, "usd_per_mtok_out": 75.0},
}

_FIELDS = set(CatalogEntry.__dataclass_fields__) - {"model_id"}


def _entry(model_id: str, spec: dict[str, Any]) -> CatalogEntry:
    # Ignore unknown keys so the JSON can carry comments/extras without breaking.
    fields = {k: spec[k] for k in spec if k in _FIELDS}
    return CatalogEntry(model_id=model_id, **fields)


def load_catalog(data: dict[str, Any] | None = None) -> dict[str, CatalogEntry]:
    """Build the catalog from a {model_id: spec} mapping, or {"models": {...}}; defaults
    when ``data`` is None. Malformed entries are skipped, never raised."""
    src = data if data is not None else DEFAULT_CATALOG
    if not isinstance(src, dict):
        return {}
    models = src["models"] if isinstance(src.get("models"), dict) else src
    out: dict[str, CatalogEntry] = {}
    for mid, spec in models.items():
        if isinstance(spec, dict):
            try:
                out[str(mid)] = _entry(str(mid), spec)
            except (TypeError, ValueError):
                continue
    return out


def load_catalog_file(path: str) -> dict[str, CatalogEntry]:
    with open(path, "r", encoding="utf-8") as fh:
        return load_catalog(json.load(fh))
