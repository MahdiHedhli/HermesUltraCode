---
name: scope-first
description: "Plan-first discipline for builds: before delegating or writing code for a build/feature request, establish scope — target directory, acceptance criteria, and the minimal first version — asking clarifying questions for material ambiguity. The default behavior; bypassed only when the user says 'yolo' / 'skip planning' / 'just build it'. Pairs with the HermesUltraCode gate, which tightens every file-writing delegation to a target directory."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [scope-first, planning, scoping, discipline, delegation, target-directory, ultracode]
    related_skills: [neckbeard]
---

# Scope-first (plan before you build)

Apply this **before delegating a build/feature task or writing code for one**. It turns a
generic request into a scoped one, so the build is disciplined and lands where it should.
It is the **default**; skip it only on an explicit bypass (see below).

## When it applies

A request to **build / implement / create / add / scaffold / set up** something — anything
that will write files or spawn a worker to do so. It does **not** apply to questions,
read-only investigation, or a request the user already scoped precisely.

## Do this first (one short round, not an interrogation)

1. **Target directory — required.** Decide *where the code will live* before any file is
   written, and state it. Propose a concrete path and confirm it. Never start writing into
   an unstated or ambiguous location.
2. **Minimal first version + out of scope.** Name the smallest thing that satisfies the
   request, and what you are explicitly *not* doing yet.
3. **Acceptance criteria.** How will we both know it's done?
4. **Stack / constraints.** Language, framework, dependencies, versions — only where it
   materially changes the build.
5. **Clarifying questions — only the ones that change the outcome.** If the request is
   already precise, skip straight to building. Don't ask for the sake of asking.

Then proceed to build (delegating as needed). The [[neckbeard]] ruleset governs *how* the
code is written; this skill governs *what* and *where* before it starts.

## Bypass

If the user says **"yolo"**, **"skip planning"**, **"just build it"**, or otherwise signals
they want to skip scoping, go straight to building — but still state the target directory in
one line before writing. The gate is never bypassed.

## How it pairs with the gate

The HermesUltraCode gate independently tightens every file-writing delegation with a
*declare-and-stay-within-a-target-directory* directive. This skill makes you establish that
directory **up front** (and interactively), so the gate's directive is satisfied by design
rather than discovered late. Planning is also the default for the `/ultracode <task>`
command; `/ultracode yolo <task>` is the command-level bypass.
