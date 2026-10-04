---
name: investigate-bug
description: Guide for investigating bugs via RCA and category sweeps. Use whenever a bug, regression, silent failure, or suspected defect is reported — before patching. Provides narrow blast-radius and codebase-wide category analysis. Accessible to all agents.
---

# Investigate Bug

You are investigating a bug. Your job is to diagnose the **instance**, classify the **category**, and patch the **class** — not just the symptom. Follow the three horizons in order. **Report your findings** to your caller (the orchestrating agent if you are a subagent in a loop, otherwise the user). Do not write spec files to disk — crafting `SPECS/` specifications is the plan agent's job. Your report informs that next step.

## When to invoke

Invoke this skill whenever:
- A bug report, reproduction, log, or failing test is handed to you.
- You observe a silent failure, empty result, type mismatch, data leak, or invariant violation that looks like a bug class.
- The code-reviewer or verifier suspects a pattern that may have siblings.

If you are not sure whether something is a bug-class, treat it as one and ask the caller.

## The three horizons

Investigate in this order. Do not skip a horizon. Each horizon must produce an explicit finding — even "nothing found" is a finding.

### Horizon 0 — The instance (RCA)

1. **Reproduce.** Obtain a minimal reproduction that fails for the right reason: a failing test (`scripts/test`), a log trace, or a step-by-step emulator run. Confirm the failure is about the bug, not a typo or fixture error.
2. **Mechanism.** Explain *why* it fails — the data shape, type mismatch, missing guard, false assumption, lifecycle ordering, or state-key drift — not just *where*. Quote the line(s) and the underlying mechanism (e.g. a text update dispatched to a handler that only expects media → the stage guard is missing; a Telegram `chat_id` stored as `str` but looked up as `int` → session silently not found; a TTS/vision timeout caught and ignored → the pipeline is left mid-state).
3. **Silence analysis.** Why did this stay silent? Missing fail-loud invariant, swallowed exception, un-logged fallback, type-check that sees `str` not the filter expression, hook that doesn't cover this path? Name the gap.

**Exit criterion:** You can state the root cause in one sentence with evidence linked (file:line, reproduction, log).

### Horizon 1 — Narrow blast radius (siblings next door)

Same pattern, same root cause, close by.

1. **Define the radius.** The narrow radius is: the same file, the same module/package (e.g. `agents/`, `gateway/`, `pipeline/`, `state/`), the same Telegram update handler or ADK agent stage, and directly neighbouring call sites that were copy-pasted or share the helper.
2. **Sweep.** Run a systematic search — `grep` for the literal pattern, AST scan where appropriate (mirror a `scripts/check-*` style static checker or state-schema audit), and manual read of neighbours. Do not eyeball a single file and declare "no siblings".
3. **Classify each hit.** For every hit record: `file:line | snippet | affected? (yes/no/unsure) | impact`. When unsure whether two findings are the same class, ask the caller.

**Exit criterion:** A table listing every H1 sibling found (or "none found after sweeping X") with verdict and line references. Every affected sibling must be flagged for fixing alongside the instance.

### Horizon 2 — Codebase-wide category (the class)

The same root cause across modules and features.

1. **Name the category.** Give the class a stable name (e.g. `media sent mid-interview`, `text sent during photo intake`, `silently-swallowed API error`, `unvalidated Telegram update used as state key`, `session stuck after unhandled timeout`). Reuse names from `SPECS/TECH.md` / `SPECS/ROADMAP.md` when they exist. A stable name lets future findings reference the class.
2. **Codify the signature.** Write down the searchable signature of the class: field names, type pattern, state-stage guard, helper name, or literal shape you are sweeping for.
3. **Sweep the codebase.** Repeat the systematic search across the entire repo (all modules, all agents, all Telegram handlers and state transitions). Use the same grep/AST approach as H1 but repo-wide. Check sibling modules that own the same boundary (e.g. every ADK agent definition if the instance was an agent; every update handler if it was a gateway bug; every state transition if it was a session-state bug).
4. **Report.** Produce a second table — same columns as H1 but for H2. Include false positives you eliminated and why. "No codebase-wide siblings found after sweeping Y" is a valid result, but you must state what you swept.

**Exit criterion:** A category sweep table with stable category name, signature, scope swept, and every codebase-wide sibling classified. No silent "I didn't look".

## From findings to fixes

After the three horizons, present findings and potential fixes grouped by horizon:

| Horizon | Finding | Proposed fix | Prevention |
|---|---|---|---|
| H0 — instance | one-line RCA | direct patch | — |
| H1 — narrow radius | N siblings at file:line | fix each sibling alongside instance | shared helper / guard at module boundary |
| H2 — codebase-wide | M siblings at file:line | fix each sibling (or file a follow-up spec if scope is large) | general mechanism (see below) |

**Prevention (required).** Every bug-fixing report must propose at least one *general, mechanism-level* prevention so the class cannot silently recur. Prefer over one-off patches:

- Typed contracts at the boundary (Pydantic `model_cls` validation at write/update time; parse Telegram/Gemini/TTS payloads at the edge).
- Fail-loud invariant (a versioned, per-`chat_id` session-state schema; a `validate_session_state` guard run at each pipeline transition; CRITICAL log + raise on contract violation).
- Deterministic session derivation (`get_or_create_session(chat_id)` / a single state-machine driver shared by every handler).
- Static cross-check script (e.g. `scripts/check-state-schemas`, `scripts/check-handler-consistency`) wired into `scripts/hooks` so CI blocks the regression.

Cite the repo-precedented option you recommend and where it would be wired.

**Test lock-in (propose).** Propose tests that would lock the class:
- One test per sibling instance (H1 + H2) proving it no longer reproduces.
- At least one test asserting the prevention mechanism actually fires (fail-loud log, rejected input, hook failure).
- Cross-chat / cross-user isolation test when the class is a data leak (one `chat_id`'s session state must not bleed into another's).

Do not implement the tests unless your caller asks — you are reporting, not taking over the implementer's job.

## How to report

- **In a loop (subagent → orchestrator):** Return a structured report to the calling agent via your final message. The orchestrator decides whether to hand it to the plan agent (new spec), the spec-implementer (patch loop), or the user. Do not write `SPECS/...` files yourself.
- **Invoked directly by the user:** Report to the user in the same structured format (RCA → H1 table → H2 table → proposed fixes → prevention → test proposals → open questions).
- Keep the report concise but evidence-rich: cite `file:line`, the grep/sweep you ran, and link to reproduction/logs.
- End with **Open questions** — genuine ambiguities (is X the same class? should we fix H2 siblings now or in a follow-up spec?) — so the caller can decide rather than you assuming. Consult on any impactful decision or spec drift.

## What not to do

- Do not patch only the reported call site.
- Do not declare "no siblings" without stating what you swept.
- Do not invent a spec folder or edit production code — report, then let the plan/build pipeline act.
- Do not bypass the dev scripts (`scripts/test`, `scripts/hooks`) when you need to confirm a reproduction.

## Reference precedents

- Text sent during photo intake (Bouncer stage takes media; text arrives) — category: wrong-payload-at-wrong-stage; prevention: per-stage input contract plus a handler-level guard that routes text back to the correct state instead of ignoring it.
- Media sent mid-interview (Interviewer awaits text reply; a photo arrives) — category: wrong-payload-at-wrong-stage, same class as the intake case; sweep every state transition for the same gap.
- Silently-swallowed Gemini/Telegram/TTS API error (vision or TTS timeout caught and ignored, session left stuck) — category: silently-swallowed API error; prevention: fail-loud wrapper that notifies the user, offers `/restart`, and never leaves the pipeline mid-state.

Use these as templates for category naming, sweep breadth, and prevention wiring.
