Neckbeard ruleset (vendored). Forked from the Ponytail ruleset (MIT) and renamed;
the marketplace plugin and Node lifecycle hooks are deliberately NOT carried over —
this is vendored text only. Credit and thanks to the original Ponytail authors.

Before writing code, stop at the first rung that holds:
  1. Does this need to exist?   -> no: skip it (YAGNI)
  2. Stdlib does it?            -> use it
  3. Native platform feature?   -> use it
  4. Installed dependency?      -> use it
  5. One line?                  -> one line
  6. Only then: the minimum that works

Lazy, not negligent. Never on the chopping block:
  - trust-boundary validation
  - data-loss handling
  - security
  - accessibility
Extended protected set (compliance evidence, never pruned as "unnecessary"):
  - observability / structured logging
  - audit logging
  - idempotency
  - retries / backoff with limits

Mark every shortcut taken with a `neckbeard:` comment naming its upgrade path.
This ruleset applies to the orchestrator and the workers. It does NOT apply to the reviewer.
