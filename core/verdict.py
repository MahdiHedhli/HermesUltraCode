"""Reviewer verdict: the structured output of a gate review.

The reviewer NEVER returns a rewritten prompt. It returns directives to append
(tighten-only) plus a verdict. Parsing fails closed: a missing field, an unknown
enum value, malformed JSON, or an empty body raises ``VerdictParseError`` and the
gate treats that as not-a-pass (invariant 1: silence is never success).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VERDICT_PASS = "pass"
VERDICT_REVISE = "revise"
VERDICT_BLOCK = "block"
VALID_VERDICTS = frozenset({VERDICT_PASS, VERDICT_REVISE, VERDICT_BLOCK})

SCOPE_IN = "in_scope"
SCOPE_NARROW = "needs_narrowing"
SCOPE_OUT = "out_of_scope"
VALID_SCOPES = frozenset({SCOPE_IN, SCOPE_NARROW, SCOPE_OUT})


class VerdictParseError(ValueError):
    """Raised when a reviewer response cannot be parsed into a valid Verdict.

    The gate maps this to a fail-closed block. Never swallow it into a pass.
    """


@dataclass(frozen=True)
class Verdict:
    """A reviewer's structured decision. Immutable once parsed."""

    verdict: str
    added_directives: tuple[str, ...] = ()
    rationale: str = ""
    scope_assessment: str = SCOPE_IN
    round: int = 0
    reviewer_model: str = ""
    # Advisory routing hint (1=trivial .. 3=hard); 0 = unspecified. NEVER fail-closed:
    # a malformed difficulty must not block a dispatch, so it is clamped to 0..3 at parse
    # time and never raises. The router maps 0 -> max tier (fail-safe-up).
    difficulty: int = 0

    def __post_init__(self) -> None:
        if self.verdict not in VALID_VERDICTS:
            raise VerdictParseError(f"unknown verdict value: {self.verdict!r}")
        if self.scope_assessment not in VALID_SCOPES:
            raise VerdictParseError(f"unknown scope_assessment: {self.scope_assessment!r}")
        if not isinstance(self.round, int) or self.round < 0:
            raise VerdictParseError(f"round must be a non-negative int, got {self.round!r}")
        for d in self.added_directives:
            if not isinstance(d, str):
                raise VerdictParseError(f"added_directives must be strings, got {type(d)}")

    @property
    def is_pass(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def is_revise(self) -> bool:
        return self.verdict == VERDICT_REVISE

    @property
    def is_block(self) -> bool:
        return self.verdict == VERDICT_BLOCK

    @property
    def is_noop(self) -> bool:
        """A pass with zero added directives — a valid, good outcome (invariant 2)."""
        return self.is_pass and len(self.added_directives) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "added_directives": list(self.added_directives),
            "rationale": self.rationale,
            "scope_assessment": self.scope_assessment,
            "round": self.round,
            "reviewer_model": self.reviewer_model,
            "difficulty": self.difficulty,
        }


def _coerce_directives(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        # A single string is tolerated but wrapped; a bare string is not a list.
        raise VerdictParseError("added_directives must be a list, not a bare string")
    if not isinstance(raw, (list, tuple)):
        raise VerdictParseError(f"added_directives must be a list, got {type(raw)}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise VerdictParseError("each added_directive must be a string")
        item = item.strip()
        if item:
            out.append(item)
    return tuple(out)


def parse_verdict(raw: Any, *, default_round: int = 0, default_model: str = "") -> Verdict:
    """Parse a reviewer response into a Verdict, failing closed on any defect.

    ``raw`` may be a JSON string or an already-decoded mapping. Empty/blank input,
    invalid JSON, missing ``verdict`` field, or an unknown enum all raise
    ``VerdictParseError`` (invariant 1: not-a-pass).
    """
    if raw is None:
        raise VerdictParseError("empty reviewer response (None)")

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise VerdictParseError("empty reviewer response (blank)")
        text = _strip_code_fence(text)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise VerdictParseError(f"reviewer response is not valid JSON: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise VerdictParseError(f"unsupported reviewer response type: {type(raw)}")

    if not isinstance(data, dict):
        raise VerdictParseError("reviewer response did not decode to an object")

    if "verdict" not in data:
        raise VerdictParseError("reviewer response missing required 'verdict' field")

    verdict_value = data.get("verdict")
    if not isinstance(verdict_value, str):
        raise VerdictParseError("'verdict' must be a string")

    return Verdict(
        verdict=verdict_value.strip().lower(),
        added_directives=_coerce_directives(data.get("added_directives")),
        rationale=str(data.get("rationale", "")),
        scope_assessment=str(data.get("scope_assessment", SCOPE_IN)).strip().lower(),
        round=int(data.get("round", default_round)),
        reviewer_model=str(data.get("reviewer_model") or default_model),
        difficulty=_coerce_difficulty(data.get("difficulty")),
    )


def _coerce_difficulty(raw: Any) -> int:
    """Advisory routing hint, clamped to 0..3. Returns 0 (unspecified) on anything
    invalid. NEVER raises — a malformed hint must not fail-close a dispatch (it is
    advisory to the router, not part of the pass/block decision)."""
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return 0
    if d < 1:
        return 0
    return 3 if d > 3 else d


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json ... ``` fence around the JSON body. neckbeard: minimal,
    upgrade path is a real markdown/JSON extractor if reviewers wander further."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
