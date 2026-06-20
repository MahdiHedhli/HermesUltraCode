# Upstream proposal: per-task model/provider selection in Hermes `delegate_task`

**Status:** draft for a Hermes PR. HermesUltraCode ships the routing *brain* today as an
advisory layer (`HERMESULTRACODE_ROUTING=1`); this is the one upstream change that lets it
*bind* per task instead of only annotating the audit row.

## The gap

Hermes can already run a subagent on a different `provider:model` pair, including a local
OpenAI-compatible endpoint (LM Studio, Ollama). But the binding is resolved from **one
global `delegation:` config block**, the same value for every subagent:

- `tools/delegate_tool.py` → `_resolve_delegation_credentials(cfg, parent_agent)` reads
  `delegation.base_url / model / provider / api_mode` and returns one credential bundle.
- The agent-facing tool handler extracts `goal / context / toolsets / tasks / role /
  background` from the call args (`delegate_tool.py` ~`:3112`) — there is **no `model`**
  read from the per-call args, so a caller (or a middleware like the UltraCode gate) cannot
  request a model per dispatch.
- The spawn path already *accepts* a model (`spawn(..., model=...)` ~`:779/:826`) and
  resolves `creds["model"] / creds["provider"]` (~`:2228`), so the plumbing exists — it is
  just not reachable per task.

Net: an orchestrator that wants cheap tasks on a local box and hard tasks on a frontier
model has no per-task lever. Cost-aware routing can decide, but cannot act.

## Proposed change (minimal, additive, backward-compatible)

Let a single delegation be overridden per call, falling back to the global block when
absent. Two equally small options:

1. **Per-task override (preferred).** Accept optional `model` (and optional `provider`) on
   a task and on the single-task form, threaded into `_resolve_delegation_credentials` as
   an override that wins over `cfg` but inherits everything else (api_key, base_url):
   ```python
   delegate_task(tasks=[
     {"goal": "...", "model": "local/gemma-4-31b"},      # cheap/free
     {"goal": "...", "model": "anthropic/claude-opus"},  # hard
   ])
   ```
2. **Role → delegation profile map.** Add `delegation.profiles: {role: {provider, model,
   base_url}}` and select by the existing `role` arg (already read at `:3119`). Routers set
   `role`; no new arg surface.

Either way: when no override/profile is given, behavior is **identical to today** (the
global `delegation:` block), so it is safe to land dark.

## How HermesUltraCode would use it

The gate's `tool_request` middleware already rewrites the `delegate_task` args per task. On
the day this lands, `RouteDecision.model_id` (from `core/router.py:choose_worker`) is written
onto each task (`args["model"]` or the chosen `role`) right where the directory/coordination
directives are already appended — a one-line change in `adapters/hermes_hook.py`. The
advisory `est_cost_usd / est_savings_usd` columns then describe *real* routing instead of a
counterfactual.

## Safety notes for reviewers

- The change is **additive and opt-in**; the default path is unchanged.
- It does not touch the cross-lab **reviewer ≠ orchestrator** rule (that is resolved
  separately at gate/CLI startup), and HermesUltraCode keeps risk-gating local models off
  elevated / merge-authority work regardless of cost (`core/router.py`).
- Hermes already inherits a **fallback provider chain** for subagents (`delegate_tool.py`
  ~`:1183`), so a down local box recovers without any new cascade logic in the plugin.
