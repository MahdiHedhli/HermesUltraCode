# HermesUltraCode

A self-contained **pre-dispatch prompt gate**, **ponytail generation discipline**, and
**observability dashboard** that sits on top of an existing
[Hermes Agent](https://nousresearch.com/) (Nous Research) orchestrator/worker setup.

The Hermes runtime, the orchestrator agent, and the worker subagent pool already exist
— this repo does **not** rebuild them. It builds the gate that vets orchestrator-authored
prompts *before they reach workers*, the ponytail minimalism discipline applied to
generation, and a read-only dashboard that makes the whole thing observable and
ISO 27001:2022-auditable.

The gate's own code is built under the ponytail ladder: stdlib-only core, zero runtime
dependencies, offline-testable with the model provider mocked.

```
orchestrator base prompt ─▶ [ GATE ] ─▶ dispatched prompt ─▶ worker
                              │  classify blast radius (code)
                              │  reviewer (a DIFFERENT lab) → structured verdict
                              │  tighten-only validation (code)
                              │  release decision (code, not chat)
                              └─▶ immutable, redacted audit row ─▶ dashboard / MCP
```

## The eight non-negotiable invariants

These are the design, enforced in code, not by trusting the model:

1. **Fail closed.** A missing/unparseable verdict, reviewer error, timeout, quota
   exhaustion, or empty response is **not** a pass. Silence degrades to
   block-and-escalate, never to silent pass-through. — `core/gate.py:_fail_closed`
2. **The reviewer is neutral, not adversarial.** Its objective is to maximise worker
   success against the guidelines. A no-op (zero added directives) is a *good* outcome
   scored as success. The word "adversarial" never appears in its role/system prompt.
   — `core/gate.py:REVIEWER_SYSTEM_PROMPT`
3. **Tighten-only by construction.** The base prompt is immutable. The reviewer may only
   *append* constraints or *block*; it can never replace, delete, broaden, or grant tool
   access. Enforced structurally + validated in code. — `core/tighten.py`
4. **The release decision lives in code.** The dispatcher refuses to release until a
   structured, present, parseable, passing verdict exists. No agent negotiates release
   in chat. — `core/gate.py:Gate.review_and_dispatch`
5. **Lift, don't fork.** Ponytail ships as vendored ruleset text — no marketplace plugin,
   no Node hooks. The dashboard is a standalone panel, not a fork of the Hermes SPA.
   — `ruleset/ponytail.md`, `web/`
6. **Ponytail runs on the orchestrator and workers, never the reviewer.** Minimalism is a
   generation-time bias. — `core/ponytail.py:inject_ruleset`
7. **The protected set is extended for compliance.** Ponytail's carve-outs (security,
   input validation, data-loss, accessibility) **plus** observability/structured logging,
   audit logging, idempotency, and retries/backoff. Never pruned — they are the ISO 27001
   evidence trail. — `ruleset/ponytail.md`, `core/gate.py`
8. **The prompt-under-review is untrusted data.** It may carry text from issues, PRs, or
   UX feedback. The reviewer evaluates embedded instructions *as data*; it never executes
   them. — `core/gate.py:build_review_prompt`

## Architecture

Three parts, one repo. Storage and model providers sit behind interfaces so the core
stays testable and portable.

| Part | Location | Notes |
|---|---|---|
| **Gate core** | `core/` | Provider- and storage-agnostic. Review loop, blast-radius tiering, tighten validation, audit trail. No Hermes import, no un-mockable network in the hot path. |
| **Hermes adapter** | `adapters/hermes_hook.py` | The *only* Hermes-coupled file. Hooks the gate into the dispatch boundary; **fails closed** if it cannot intercept dispatch. |
| **Dashboard + read API** | `server/`, `web/` | Read-only views over the store, mirroring Hermes dashboard security. Optional read-only MCP server. |

The **reviewer** is a model call routed to a provider that **must differ in lab** from the
orchestrator's (genuine posttraining divergence, not same-family-different-size). This is
validated at startup — identical labs hard-fail. — `core/providers.py:validate_distinct_providers`

### The verdict (reviewer's structured output)

```json
{
  "verdict": "pass | revise | block",
  "added_directives": ["string"],
  "rationale": "string",
  "scope_assessment": "in_scope | needs_narrowing | out_of_scope",
  "round": 0,
  "reviewer_model": "string"
}
```

The reviewer never returns a rewritten prompt — only directives to append. That is what
makes tighten-only *structural* rather than a fragile semantic diff:

```
dispatched_prompt = base_prompt (verbatim) + rendered(added_directives)
```

- `pass` + empty directives → dispatch the base unchanged (the no-op, a good answer).
- `revise` → append directives, run the tighten validator, re-review or dispatch.
- `block` → do not dispatch; escalate or log per tier.

### Blast-radius tiering (deterministic, dispatcher-side — `core/tiering.py`)

| Tier | Trigger | Review | Round-cap fallback |
|---|---|---|---|
| `merge_adjacent` | carries merge authority | frontier; block = hard stop | escalate to human |
| `elevated` | protected paths (auth/crypto/CI/infra) or over file/cost threshold | frontier | escalate to human |
| `standard` | ordinary code change | frontier | auto-accept last base, **log dissent** |
| `trivial` | read-only or single-file | skip frontier (or cheap model) | n/a |

> **On invariants 1 & 5 together:** a reviewer **error / timeout / unparseable** verdict
> *always* fails closed to a block (criterion 1) — it never reaches the standard
> auto-accept. The standard-tier auto-accept (criterion 5) is reachable *only* through
> the round cap being exhausted by genuine, valid `revise` verdicts, and it is recorded
> as `dispatched_fallback` with `dissent_logged=true`. It is a governed, audited policy
> decision, not a silent bypass.

## Quick start

```bash
# 1. Run the full test suite (offline, stdlib unittest — no pip install needed)
python -m unittest discover -s tests

#    or with pytest:  pip install -e ".[dev]" && pytest

# 2. Run the gate-on vs gate-off benchmark on the example corpus
python -m bench.harness --out bench_results.json

# 3. Launch the read-only dashboard (loopback + ephemeral session token)
python -m server --store gate_audit.sqlite3 --bench bench_results.json
#    open the printed URL, paste the printed token into the dashboard

# 4. (optional) read-only MCP server for the Hermes agent
python -m server.mcp_server --store gate_audit.sqlite3

# 5. Live smoke test against a real model via the installed Hermes proxy
hermes proxy start --provider xai --host 127.0.0.1 --port 8649   # in another shell
python -m bench.smoke_hermes      # routes the reviewer through xAI (a different lab)
```

### Live smoke test (`bench/smoke_hermes.py`)

Exercises the whole gate end-to-end with a **real reviewer call** routed through
`hermes proxy` (Hermes's local OpenAI-compatible endpoint). xAI Grok is a genuinely
different lab from the Nous orchestrator, so this is a faithful test of invariant 6, not
a workaround. It runs a benign task (→ dispatch), a protected-path task (→ the live model
appends the extended-protected-set directives — audit logging, idempotency, retries/
backoff, validation — then dispatches), and a prompt-injection-laden base (→ fail-closed
block). This is also what surfaced the tighten validator's precision tuning below.

### Wiring into Hermes

`adapters/hermes_hook.py` is the integration point. Construct a `Gate`, wrap it in a
`HermesGateAdapter`, and `register()` it against your Hermes runtime's dispatch hook.
If no recognised hook surface is found, registration **raises** rather than letting
dispatch run unguarded:

```python
from core.config import load_config
from core.gate import Gate
from core.store_sqlite import SqliteAuditStore
from adapters.hermes_hook import HermesGateAdapter

cfg = load_config("config.example.json")          # validates distinct provider labs
gate = Gate(
    reviewer_provider=cfg.reviewer_provider,
    orchestrator_provider=cfg.orchestrator_provider,
    store=SqliteAuditStore(cfg.store_path),
    round_cap=cfg.round_cap,
)
HermesGateAdapter(gate=gate).register(hermes_runtime=my_hermes_runtime)
```

## Ponytail discipline

The vendored ruleset (`ruleset/ponytail.md`, MIT, **no** marketplace plugin, **no** Node
hooks) is injected into orchestrator and worker prompt assembly via
`core/ponytail.inject_ruleset` — and **refused** for the reviewer. Every shortcut in this
codebase is tagged with a `ponytail:` comment naming its upgrade path; those markers are
harvested into the dashboard's **debt ledger** (`core/ponytail.harvest_markers`).

Applied to this repo, the ladder produced: a stdlib-only core, a `http.server`-based read
API (no Flask/FastAPI), a server-rendered dashboard (no React build step — see
[`web/README.md`](web/README.md) for the documented React 19 + Vite + Tailwind upgrade
path), and SQLite for the audit store (no new dependency).

## Dashboard views (`web/`, served by `server/read_api.py`)

- **Live** — orchestrator + active worker subagents, backend, status, current dispatch.
- **Queue** — pending dispatches with blast-radius tier badges.
- **Gate panel** (per dispatch) — verdict, round count, the appended directives (the
  actual "tighten"), rationale, reviewer model, final decision.
- **Audit trail** — the immutable log, filterable by tier/verdict/date, JSON + CSV export.
- **Ponytail** — the debt ledger and protected-set violations the gate blocked.
- **Metrics** — first-pass worker success gate-on vs off, guideline-violation rate, gate
  latency p50/p95, added token cost per dispatch.
- **Fail-closed counter** — dispatches blocked due to reviewer error/timeout/quota, so
  silent degradation is *visible* rather than hidden.

**Read API security** (mirrors the Hermes web dashboard): binds loopback (127.0.0.1) by
default on port 9120, requires an ephemeral session token in the `X-Gate-Session-Token`
header on every `/api/*` route, restricts CORS to localhost origins, validates the Host
header against an allowlist (DNS-rebinding defense), redacts secrets on any config
surfaced, and refuses to bind a non-loopback host without a token.

## Audit trail / ISO 27001 evidence

One immutable row per dispatch (`core/store.py`, default `core/store_sqlite.py`):
`{id, ts, base_prompt, added_directives, dispatched_prompt, verdict, tier, reviewer_model,
decision, round_count, …}` plus observability fields (latency, added tokens) and
compliance flags (fail_closed, dissent_logged, escalated, ponytail_block). Secrets are
redacted on write (`core/redact.py`). UPDATE/DELETE are blocked by SQLite triggers —
append-only by construction. Exportable to JSON and CSV. Storage is behind an interface;
a Cloudflare D1 adapter is a later swap against the same seam (the seam is left, not built).

## File layout

```
hermesultracode/
  core/        gate.py verdict.py tighten.py tiering.py providers.py
               store.py store_sqlite.py redact.py config.py ponytail.py
  adapters/    hermes_hook.py            # only Hermes-coupled file; fails closed
  ruleset/     ponytail.md               # vendored, MIT, no plugin/hooks
  server/      read_api.py views.py mcp_server.py __main__.py
  web/         dashboard.html app.js styles.css README.md
  bench/       harness.py tasks.example.json
  tests/       test_tighten.py test_failclosed.py test_tiering.py
               test_provider_distinct.py test_round_cap.py test_store.py
               test_gate.py test_redact.py test_ponytail.py test_read_api.py
               test_adapter.py test_bench.py test_mcp.py helpers.py
  config.example.json  pyproject.toml  README.md
```

## Acceptance criteria → where they're proven

| # | Criterion | Test(s) |
|---|---|---|
| 1 | Never releases without a present, parseable, passing verdict; bypass fails closed | `test_failclosed.py`, `test_gate.py` |
| 2 | Dispatched prompt contains base verbatim; append-only; grants/edits rejected | `test_tighten.py` |
| 3 | Reviewer provider ≠ orchestrator provider; identical config fails at startup | `test_provider_distinct.py` |
| 4 | Reviewer error/timeout/quota/empty → not-a-pass, fails closed per tier | `test_failclosed.py` |
| 5 | Round cap honored (default 2); tier-specific fallback fires | `test_round_cap.py` |
| 6 | Tiering classifies merge/protected/trivial correctly | `test_tiering.py` |
| 7 | Immutable audit row; secrets redacted; JSON + CSV export | `test_store.py`, `test_redact.py` |
| 8 | Ponytail ruleset present + injected; extended protected set; no plugin/hooks | `test_ponytail.py` |
| 9 | Dashboard views + read API security (token/CORS/Host/redaction) | `test_read_api.py` |
| 10 | Benchmark runs gate-on vs gate-off, emits four metrics | `test_bench.py` |
| 11 | Test suite passes; invariant tests present and green | `python -m unittest discover -s tests` |

## Out of scope

The orchestrator and worker pool (assumed to exist on Hermes — this wraps their dispatch
boundary). CI optimization, video/QA capture, post-staging log monitoring. The real task
corpus. Auth beyond loopback + token + optional OAuth gate. The D1 storage adapter (the
seam is left; it is not built here).
