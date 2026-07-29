# Decision record: per-task model routing lives in the ultracode lane, not upstream

**Status:** RESOLVED (July 2026). The original proposal below was overtaken by an explicit
upstream decision, and HermesUltraCode is designed around it.

## The upstream decision

- [NousResearch/hermes-agent#41190](https://github.com/NousResearch/hermes-agent/issues/41190)
  (unified plugin route selector for per-turn/per-call provider+model override) was **closed
  `not-planned`** by the maintainer (July 14, 2026).
- The sanctioned re-scope, verbatim: *"A focused contribution improving the existing
  **config-level** delegation routing path, without introducing per-call or per-task
  selection, would be an appropriate re-scope."*
- Sibling issues asking for a `model` param on `delegate_task` (#44900, #32711) were swept
  closed; the per-call PRs referenced there were not merged. **Per-call / per-task model
  selection is refused upstream as a category**, not just as one issue.

## What HermesUltraCode does about it

1. **The ultracode Kanban lane is the binding surface.** The lane's poller spawns
   `hermes -p <profile> -m <model>` itself, so per-task model binding needs **no upstream
   seam**. `core/routing.py::resolve()` (the tier→model floor lookup over the roster's
   operator-supplied `tiers:` slots) and `next_rung()` (model cascade within a provider,
   then provider cascade) are **live** — they were stubbed only while an upstream per-task
   field looked possible.
2. **`delegate_task` stays two-layer:** the gate **binds** (safety: tighten-only,
   fail-closed) and the cost router **annotates** (advisory: the model it *would* pick +
   dollars saved). It cannot bind — upstream refused the seam — so when binding routing
   matters, route the work through the **ultracode board** instead of `delegate_task`.
   That is the documented pattern, not a workaround.
3. **Stock profile lanes stay config-level** (the profile's default model), matching
   upstream's own design. The parked fork branch
   [`feat/kanban-task-model`](https://github.com/NousResearch/hermes-agent/compare/main...MahdiHedhli:feat/kanban-task-model)
   (the tiny `create_task` write path + surfaces for the existing `model_override` column)
   stays unopened unless upstream policy shifts.
4. **Upstream contributions target the sanctioned lane:** improvements to the
   **config-level delegation routing path** — e.g. open fail-open/robustness bugs in it
   (#49095: `delegation.provider` silently falls back to openrouter/free; #49417: subagents
   don't inherit the parent's `fallback_providers` chain). Fail-closed delegation routing
   is precisely this project's competence.

## Why this is fine (and even good)

The refusal costs the plugin nothing — every binding path it needs, it owns. And it makes
the ultracode lane the only per-task-model routing surface in the Hermes ecosystem: tier
floors, operator-supplied model slots, a verified-failure cascade, and a cross-lab-reviewed
gate in front of every dispatch.

---

*The original proposal (kept for history): add an optional per-task `model`/`provider`
override to `delegate_task` (or a `delegation.profiles` role→model map) threaded into
`_resolve_delegation_credentials`, letting the gate's `tool_request` middleware bind the
router's decision per task. Superseded by the decision above.*
