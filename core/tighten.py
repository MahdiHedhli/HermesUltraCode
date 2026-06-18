"""Deterministic tighten validator (invariant 3: tighten-only by construction).

The orchestrator's base prompt is immutable. The reviewer may only APPEND
constraints/clarifications. This module assembles the dispatched prompt and proves,
in code, that:

  * the base prompt is present verbatim and untouched,
  * no directive encodes a tool / permission / scope-broadening grant,
  * the directive count is within a sane cap,
  * no directive tries to negate, replace, or delete the base.

On any violation it raises ``TightenError`` and the gate fails closed. This is
enforcement, not trust in the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_DIRECTIVES = 12
DEFAULT_MAX_DIRECTIVE_LEN = 2000

_DIRECTIVE_HEADER = "\n\n--- Appended review directives (tighten-only, append-only) ---\n"


class TightenError(ValueError):
    """Raised when assembled directives would violate the tighten-only contract."""


# Defense-in-depth note: the reviewer can never reach the dispatcher allowlist, so a
# directive that *says* "use the shell" grants nothing — the allowlist is the real
# capability boundary. These patterns are a SECONDARY textual-hygiene check that rejects
# directives which talk like a grant/tamper, so such text never even reaches a worker
# prompt.
#
# Design bias: PRECISION over recall. A false positive here fails the gate closed on a
# *legitimate* tightening directive (a real reviewer once wrote "...a flag that can
# disable the grace window..." — innocent), which undermines invariant 2; a false
# negative is backstopped by the allowlist. So we:
#   * normalise separators (underscores/hyphens/newlines/punctuation -> single space) and
#     use DOTALL, which defeats the realistic obfuscations without adding false positives;
#   * suppress a match that is NEGATED right before it ("do not enable shell", "never
#     grant tool access"), so restrictive directives pass.
# Residual limitation (accepted): pathological NO-separator concatenation
# ("ignoretheprevious") is not caught by this text check — a neutral reviewer never emits
# it, and the allowlist neutralises it anyway. We do not chase it with a de-spaced
# denylist, which empirically false-matched innocent phrases ("disable the grace window").

# Underscores are word chars, so they defeat \b ("call_the_Bash_tool") — replace them
# with spaces before matching. Other separators (hyphens, newlines, punctuation, runs of
# whitespace) are already spanned by the patterns' .{0,N} under DOTALL, so they need no
# special handling — and keeping sentence punctuation lets us scope negation to its clause.
_UNDERSCORE = re.compile(r"_+")

# Clause boundaries — a negation only suppresses a match inside the SAME clause.
_CLAUSE_SEP = re.compile(r"[.;:!?\n]")

# Negation tokens; a grant/tamper match with one of these earlier in its clause is
# restrictive ("do not enable shell", "never grant tool access"), not a grant.
_NEGATION = re.compile(
    r"\b(not|never|cannot|without|disallow\w*|forbid\w*|prevent\w*|refuse\w*|"
    r"prohibit\w*|deny|denies|avoid\w*|reject\w*)\b",
    re.IGNORECASE,
)

# Patterns that smell like a tool / permission / capability grant.
_TOOL_GRANT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        r"\bgrant(?:s|ing|ed)?\b.{0,40}\b(access|permission|tool|scope|capabilit)",
        r"\b(allow|enable|permit|authoriz\w*)\b.{0,40}\b(tool|access|permission|command|shell|exec|network|internet|filesystem|sudo)",
        r"\byou (?:may|can|are allowed to|are permitted to)\b.{0,40}\b(use|run|access|invoke|call)\b.{0,40}\b(tool|shell|bash|command|network|api|mcp)",
        r"\b(add|append|extend)\b.{0,30}\b(to the )?allowlist\b",
        r"\benable\b.{0,20}\b(tool|permission|access)\b",
        r"\bgive\b.{0,30}\b(access|permission|the ability)\b",
        r"\b(use|invoke|call)\b.{0,20}\b(the )?(Bash|Shell|Exec|WebFetch|Write|Edit|MCP)\b.{0,20}\btool\b",
        r"\bsudo\b",
        r"\bchmod\b|\bchown\b",
        # "--dangerously-skip-permissions" normalises to "dangerously skip permissions"
        r"\bdangerously\b",
        r"\bbypass\b.{0,30}\b(gate|review|permission|allowlist|sandbox|guard)",
        r"\bdisable\b.{0,30}\b(sandbox|guard|permission|safety|review)",
        r"\bnew (tool|permission|capabilit|scope)\b",
        # turn/switch off a safety control (unambiguous; no legit tightening does this)
        r"\b(turn|switch)\s+off\b.{0,30}\b(sandbox|guard|guardrail|safety|safeguard|permission|review|gate|restriction|protection)\b",
        # permission / privilege escalation, either word order
        r"\bescalat\w*\b.{0,30}\b(permission|privilege|access|right)s?\b",
        r"\b(permission|privilege|access)s?\b.{0,20}\bescalat\w*",
        # a PERMISSIVE modal ("feel free to", "you may", ...) granting shell / exec /
        # network. The modal is required so good *narrowing* directives ("do not run
        # shell commands") are NOT rejected — only granting phrasings are.
        r"\b(feel free to|free to|fine to|ok(?:ay)? to|go ahead and|you (?:may|can|are (?:allowed|permitted|free) to))\b.{0,40}\b(shell|bash|terminal|subprocess|sudo|network access|internet access|arbitrary (?:code|command)|shell command)",
        # a permissive modal granting work OUTSIDE the named scope/paths
        r"\b(feel free to|free to|fine to|ok(?:ay)? to|go ahead and|you (?:may|can|are (?:allowed|permitted|free) to))\b.{0,40}\b(outside|beyond|any (?:path|file|dir)|all (?:files|paths|dirs|directories))\b",
    )
)

# Patterns that try to overwrite / delete / broaden the immutable base.
_BASE_TAMPER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        r"\b(ignore|disregard|forget|override|overrule|supersede|replace|delete|remove|discard|cancel)\b.{0,40}\b(the )?(base|above|previous|prior|original|earlier|system)\b.{0,20}\b(prompt|instruction|directive|constraint|rule)",
        # "ignore all previous instructions" etc. — but only when it references the
        # SYSTEM's own instructions/rules, so "ignore any trailing whitespace" is fine.
        r"\b(ignore|disregard|forget)\b.{0,15}\b(all|any|every|everything)\b.{0,20}\b(instruction|directive|rule|guideline|constraint|prompt|previous|prior|above)\b",
        r"\binstead of (the )?(base|above|previous)\b",
        r"\bthe base (prompt )?(is|no longer) (wrong|void|invalid|cancell?ed)\b",
        r"\bbroaden\b.{0,30}\bscope\b",
        r"\bexpand\b.{0,30}\bscope\b.{0,30}\b(beyond|past)\b",
        r"\byou are no longer (bound|limited|restricted|constrained)\b",
    )
)


@dataclass(frozen=True)
class TightenResult:
    """Outcome of a successful tighten assembly."""

    dispatched_prompt: str
    base_prompt: str
    added_directives: tuple[str, ...]


def render_directives(added_directives: tuple[str, ...] | list[str]) -> str:
    """Render directives into the append block. Empty -> empty string (the no-op)."""
    cleaned = [d.strip() for d in added_directives if d and d.strip()]
    if not cleaned:
        return ""
    body = "\n".join(f"- {d}" for d in cleaned)
    return _DIRECTIVE_HEADER + body + "\n"


def assemble_dispatched_prompt(base_prompt: str, added_directives) -> str:
    """dispatched = base (verbatim) + rendered(added_directives). Pure, deterministic."""
    return base_prompt + render_directives(tuple(added_directives))


def _normalize_for_scan(text: str) -> str:
    """Replace underscores with spaces so they can't dodge \\b. Other separators are
    handled by DOTALL + .{0,N}; sentence punctuation is preserved for clause scoping."""
    return _UNDERSCORE.sub(" ", text)


def _negated_before(text: str, start: int) -> bool:
    """True if a negation token appears earlier in the SAME clause as the match — the
    directive is restrictive ("do not ignore rules or grant tools"), not a grant."""
    clause_start = 0
    for m in _CLAUSE_SEP.finditer(text, 0, start):
        clause_start = m.end()
    return _NEGATION.search(text, clause_start, start) is not None


def find_grant_violations(directive: str) -> list[str]:
    """Return the human-readable names of any grant/tamper patterns the directive hits.

    Scans a separator-normalised form (defeats underscore/newline/punctuation
    obfuscation) and ignores matches that are negated right before them (restrictive
    directives like "do not grant tool access" are legitimate tightening)."""
    hits: list[str] = []
    normalized = _normalize_for_scan(directive)
    for kind, patterns in (("tool/permission grant", _TOOL_GRANT_PATTERNS),
                           ("base tampering", _BASE_TAMPER_PATTERNS)):
        for pat in patterns:
            m = pat.search(normalized)
            if m and not _negated_before(normalized, m.start()):
                hits.append(f"{kind}: /{pat.pattern}/")
    return hits


def validate_tighten(
    base_prompt: str,
    added_directives,
    *,
    max_directives: int = DEFAULT_MAX_DIRECTIVES,
    max_directive_len: int = DEFAULT_MAX_DIRECTIVE_LEN,
) -> TightenResult:
    """Assemble and validate. Raises ``TightenError`` on any violation (fail closed).

    Guarantees on success:
      * ``result.dispatched_prompt`` starts with ``base_prompt`` byte-for-byte,
      * every directive is append-only and grant-free,
      * directive count <= ``max_directives``.
    """
    if base_prompt is None or not isinstance(base_prompt, str):
        raise TightenError("base_prompt must be a non-null string")
    if base_prompt.strip() == "":
        raise TightenError("base_prompt is empty; nothing to dispatch")

    directives = tuple(added_directives or ())

    if len(directives) > max_directives:
        raise TightenError(
            f"too many added_directives: {len(directives)} > cap {max_directives}"
        )

    cleaned: list[str] = []
    for raw in directives:
        if not isinstance(raw, str):
            raise TightenError(f"directive is not a string: {type(raw)}")
        d = raw.strip()
        if not d:
            continue  # blank directives are dropped, not an error
        if len(d) > max_directive_len:
            raise TightenError(f"directive exceeds max length {max_directive_len}")
        violations = find_grant_violations(d)
        if violations:
            raise TightenError(
                "directive rejected (tighten-only contract): "
                + "; ".join(violations)
                + f" :: {d!r}"
            )
        cleaned.append(d)

    dispatched = assemble_dispatched_prompt(base_prompt, cleaned)

    # Structural proof: the base survives verbatim at the head of the dispatch.
    if not dispatched.startswith(base_prompt):
        raise TightenError("assembled prompt does not begin with the base verbatim")
    # And the only thing after the base is our rendered, controlled append block.
    tail = dispatched[len(base_prompt):]
    if tail != render_directives(tuple(cleaned)):
        raise TightenError("assembled prompt has unexpected content after the base")

    return TightenResult(
        dispatched_prompt=dispatched,
        base_prompt=base_prompt,
        added_directives=tuple(cleaned),
    )
