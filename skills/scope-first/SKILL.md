---
name: scope-first
description: "Plan-first discipline for builds: before delegating or writing code for a build/feature request, scope it INTERACTIVELY using the clarify tool — ask one decision at a time (target directory, approach, v1 scope) with button choices, then build. The default behavior; bypassed only when the user says 'yolo' / 'skip planning' / 'just build it'. Pairs with the HermesUltraCode gate, which tightens every file-writing delegation to a target directory."
version: 1.2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [scope-first, planning, scoping, discipline, clarify, delegation, target-directory, ultracode]
    related_skills: [neckbeard, subagent-driven-development, plan]
---

# Scope-first (plan before you build)

Apply this **before delegating a build/feature task or writing code for one**. Turn a
generic request into a scoped one **interactively**, so the build is disciplined and lands
where it should. It is the **default**; skip it only on an explicit bypass (below).

## When it applies

A request to **build / implement / create / add / scaffold / set up** something that will
write files or spawn a worker. It does **not** apply to questions, read-only investigation,
or a request the user already scoped precisely.

## Ask interactively with the `clarify` tool — do NOT dump a list of questions

A wall of questions the user can't act on is the failure mode to avoid. Instead use the
**`clarify` tool**, which renders each question as a blocking, button-based prompt:

- Put the question in `question`; put each option as its own element of `choices` (up to 4).
  **Never** enumerate options inside the question text — the UI renders `choices` as
  selectable rows and auto-appends an "Other (type your answer)" row.
- Ask **one decision at a time**, most important first. Wait for the answer, then ask the
  next. Stop as soon as you know enough to build.
- Only `clarify` decisions with real trade-offs. For low-stakes details, pick a sensible
  default and say so instead of asking.

### The decisions worth a `clarify`, in order

1. **Target directory — confirm before any file is written.** Propose a concrete path:
   `clarify(question="Where should this live?", choices=["~/projects/html5-chess", "./html5-chess", "the current directory"])`
2. **Approach / stack**, where it forks the build:
   `clarify(question="Chess AI engine?", choices=["Stockfish (WASM)", "simple minimax", "no AI — player vs player only"])`
3. **Scope of v1:**
   `clarify(question="What's in the first version?", choices=["PvP only", "PvP + basic AI", "full rules + AI"])`

Anything still open after that, choose a reasonable default and note it in one line. The
[[neckbeard]] ruleset governs *how* the code is written; this skill governs *what* and
*where*, decided **with** the user, before it starts.

## Fan out independent work to parallel subagents

Once the scope is settled, look at the plan: if it has **2+ independent components**
(e.g. backend / frontend / tests), delegate them as **parallel subagents in a single
batched `delegate_task`** (`tasks=[{goal, …}, …]`) rather than building them one at a time.
The HermesUltraCode gate reviews and tightens **each task independently** (target-directory
directive included), so the fan-out stays disciplined. Keep work sequential only when the
steps genuinely depend on each other.

For the execute-and-review loop itself — fresh subagent per task, spec-then-quality review —
defer to the bundled **[[subagent-driven-development]]** skill; don't reinvent it here. This
skill's job ends at *scoping + the target directory*; that one runs the delegated work. For a
plain markdown plan with no execution, the bundled **[[plan]]** skill is the right tool.

## Bypass

If the user says **"yolo"**, **"skip planning"**, or **"just build it"**, skip the clarify
round — state the target directory in one line and build. The gate is never bypassed.

## How it pairs with the rest of HermesUltraCode

The gate independently tightens every file-writing delegation with a
*declare-and-stay-within-a-target-directory* directive, so the directory you confirm via
`clarify` satisfies it by design. The `/ultracode plan <task>` **command** is only a one-shot
text plan — a slash command runs in a worker with no agent loop, so it cannot ask interactive
questions. For the interactive, button-based version, the user sends the build request as a
**normal message** and this skill takes over with `clarify`.
