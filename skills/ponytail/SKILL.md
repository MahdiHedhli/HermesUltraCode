---
name: ponytail
description: "Ponytail minimalism for code generation — climb the ladder (YAGNI → stdlib → platform → installed dep → one line → minimum that works) while never pruning the protected set. Applies to the orchestrator and workers, never to a reviewer."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ponytail, minimalism, yagni, code-generation, lazy-not-negligent, iso27001, compliance]
    related_skills: []
---

# Ponytail ruleset (vendored)

Apply this as a **generation-time** bias when writing code. It makes solutions lean
without making them negligent. It is vendored text (MIT) — not a marketplace plugin and
not a set of Node lifecycle hooks.

Before writing code, stop at the first rung that holds:

1. **Does this need to exist?** → no: skip it (YAGNI)
2. **Stdlib does it?** → use it
3. **Native platform feature?** → use it
4. **Installed dependency?** → use it
5. **One line?** → one line
6. **Only then:** the minimum that works

## Lazy, not negligent — never on the chopping block

- trust-boundary validation
- data-loss handling
- security
- accessibility

## Extended protected set (compliance evidence — never pruned as "unnecessary")

- observability / structured logging
- audit logging
- idempotency
- retries / backoff with limits

These are the ISO 27001:2022 evidence trail. Treat them as required, not optional.

## How to apply

- Mark every shortcut you take with a `ponytail:` comment naming its upgrade path, so the
  debt is visible and can be harvested into a ledger.
- Prefer the lowest rung that fully solves the problem; do not add layers, abstractions,
  dependencies, or configurability that nothing yet needs.
- When a protected-set concern applies (handling external input, deleting data, auth,
  user-facing UI, anything that needs an audit trail or must be retried safely), implement
  it — the ladder never lets you skip these.

## Scope

This ruleset applies to the **orchestrator and the workers**. It does **NOT** apply to a
prompt reviewer/gate: minimalism is a generation-time bias, not a review-time one. A
reviewer's job is to maximize success against the guidelines, which sometimes means
*adding* protective constraints — the opposite of pruning.
