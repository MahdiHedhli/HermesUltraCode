"""Ponytail ruleset injection + debt-ledger harvesting.

The vendored ruleset (``ruleset/ponytail.md``, MIT, no marketplace plugin, no Node
hooks) is injected into the ORCHESTRATOR and WORKER prompt assembly. It is NEVER
injected into the reviewer (invariant 6: minimalism is a generation-time bias, not a
review-time one). ``inject_ruleset`` raises if asked to inject into the reviewer.

``harvest_markers`` scans the codebase for ``ponytail:`` comments — every shortcut
taken names its upgrade path — and surfaces them as the dashboard's debt ledger.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_WORKER = "worker"
ROLE_REVIEWER = "reviewer"

MODE_LITE = "lite"
MODE_FULL = "full"

_RULESET_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ruleset", "ponytail.md")

# Lite mode keeps the ladder + the never-prune set but trims the prose. The protected
# set is NEVER trimmed (invariant 7) regardless of mode.
_LITE_HEADER = (
    "Ponytail (lite): climb the ladder — YAGNI, then stdlib, then platform, then an "
    "installed dep, then one line, then the minimum that works. Never prune: "
    "trust-boundary validation, data-loss handling, security, accessibility, "
    "observability/structured logging, audit logging, idempotency, retries/backoff. "
    "Mark every shortcut with a `ponytail:` comment naming its upgrade path."
)


class PonytailError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_ruleset() -> str:
    """Return the vendored full ruleset text."""
    with open(_RULESET_PATH, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def inject_ruleset(prompt: str, role: str, mode: str = MODE_FULL) -> str:
    """Prepend the ponytail ruleset to a prompt for the orchestrator or a worker.

    Raises ``PonytailError`` for the reviewer role — invariant 6 is enforced in code,
    not left to discipline.
    """
    if role == ROLE_REVIEWER:
        raise PonytailError(
            "ponytail must NOT be injected into the reviewer (invariant 6): "
            "minimalism is a generation-time bias, not a review-time one"
        )
    if role not in (ROLE_ORCHESTRATOR, ROLE_WORKER):
        raise PonytailError(f"unknown role for ponytail injection: {role!r}")
    # Workers are always full; the orchestrator may run lite.
    effective_mode = MODE_FULL if role == ROLE_WORKER else mode
    if effective_mode == MODE_LITE:
        block = _LITE_HEADER
    elif effective_mode == MODE_FULL:
        block = load_ruleset()
    else:
        raise PonytailError(f"unknown ponytail mode: {mode!r}")
    return f"{block}\n\n---\n\n{prompt}"


# ---------------------------------------------------------------------------
# Debt ledger
# ---------------------------------------------------------------------------

# `ponytail:` marker followed by the upgrade-path note. Matches in any comment style.
_MARKER_RE = re.compile(r"ponytail:\s*(?P<note>.+?)\s*$", re.IGNORECASE)
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
_SCAN_EXT = {".py", ".md", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".toml"}


@dataclass(frozen=True)
class DebtItem:
    file: str
    line: int
    note: str


def harvest_markers(root: str) -> list[DebtItem]:
    """Walk ``root`` and collect every ``ponytail:`` marker as a debt-ledger item.

    Skips the ruleset definition itself and this module's own pattern, so the ledger
    reflects real shortcuts, not the machinery that defines them.
    """
    items: list[DebtItem] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in _SCAN_EXT:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            # Don't harvest the ledger machinery or the ruleset definition.
            if rel.endswith(os.path.join("core", "ponytail.py")) or rel.endswith(
                os.path.join("ruleset", "ponytail.md")
            ):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, start=1):
                        m = _MARKER_RE.search(line)
                        if m:
                            items.append(DebtItem(file=rel, line=i, note=m.group("note").strip()))
            except OSError:
                continue
    items.sort(key=lambda it: (it.file, it.line))
    return items
