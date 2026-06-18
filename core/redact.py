"""Secret redaction. Applied on write to the audit store and to any config the
read API surfaces (invariant: secrets redacted on write; the audit trail is ISO
evidence, not a credential leak).

neckbeard: regex-based redactor over a curated pattern set. Upgrade path is a
detect-secrets / entropy-based scanner if the corpus widens; the seam is
``redact()`` + ``REDACTORS`` so swapping the engine touches one module.
"""

from __future__ import annotations

import re
from typing import Any

# (kind, compiled-pattern). Order matters: more specific first.
REDACTORS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openrouter_key", re.compile(r"sk-or-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_key", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}")),
    ("basic_auth_url", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s:@]+)@")),
    # key=value assignments for sensitive-looking names
    ("assigned_secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*([^\s,'\"]{6,})"
    )),
]

REDACTION_MARK = "«REDACTED:{kind}»"


def redact(text: str) -> str:
    """Return ``text`` with recognised secrets replaced by a typed marker."""
    if not text:
        return text
    out = text
    for kind, pat in REDACTORS:
        if kind == "basic_auth_url":
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}:«REDACTED:url_password»@", out)
        elif kind == "assigned_secret":
            out = pat.sub(lambda m: f"{m.group(1)}=«REDACTED:assigned_secret»", out)
        else:
            out = pat.sub(REDACTION_MARK.format(kind=kind), out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists/tuples; pass-through scalars."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj
