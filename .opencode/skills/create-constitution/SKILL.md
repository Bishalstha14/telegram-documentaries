---
name: create-constitution
description: Guide for authoring the project constitution. Use when the SPECS/ directory does not exist yet or is incomplete, or when asked to create, initialize, author, or update the project constitution / MISSION.md, TECH.md, ROADMAP.md. This is the first deliverable of a greenfield project.
---

# Create Constitution

You are helping the user author the **project constitution**: the single source
of truth that every other document and agent defers to. It lives in `SPECS/`
and consists of exactly three files:

- `SPECS/MISSION.md` — why the project exists and what it must do.
- `SPECS/TECH.md` — the technical contract: stack, architecture, policies.
- `SPECS/ROADMAP.md` — the ordered plan of what gets built, phase by phase.

Additional feature specs live in `SPECS/<YYYY-MM-DD>-<feature-name>/` and are
created later by the `plan` agent through the `feature-specification` skill. You
create the constitution only — not feature specs, not code.

## Before you write anything

**You must ask the user for clarifications before writing to disk.** The
constitution is the contract every later stage is judged against, so ambiguity
here is expensive. Surface, at minimum:

- Product scope: what the system does, and just as importantly what it does
  **not** do (YAGNI).
- Any change to the target stack, models, or pipeline stages listed below.
- Backward-compatibility decisions, if any — do not assume compatibility should
  be kept.
- Anything you are tempted to assume about the architecture.

When in doubt about an impactful decision, ask rather than assume. Do not create
files until the user has answered.

## Grounding: what this project is

This is **The Telegram Documentaries** — a greenfield Telegram bot built with
Google's Agent Development Kit (ADK). A user uploads a portrait and receives a
narrated, comedy-wildlife-documentary about themselves. The pipeline stages are:

1. **The Bouncer** — Gemini 3.1 Flash Lite vision gate. Confirms a human is
   present; cheekily rejects non-human images and resets state. (No TTS, no
   video — text/image classification only.)
2. **The Interviewer** — Gemini 3.1 Flash Lite. Doubles as the **orchestrator**;
   asks 5–7 sequential questions, one at a time, and accumulates a behavioural
   dossier tied to the user's `chat_id`. It also outputs a suggested animal.
3. **The Converter** — Gemini 3.1 Flash Image. Native multimodal fusion of the
   original photo + interview dossier into a hybrid animal portrait, returned
   directly to Telegram with no intermediate text hop.
4. **The Scripter** — Gemini 3.1 Flash Lite. One-paragraph (~60–90 words),
   dramatic British-documentary narration built from the dossier.
5. **The Narrator** — *not an agent*. The script is routed directly to Gemini
   TTS (`gemini-3.1-flash-tts-preview`), rendered to a Telegram-compatible audio
   format (OGG/MP3), and sent to the chat.

Transport is Telegram **long polling** (no webhooks, no public URL). Session
state is held in memory keyed by `chat_id`; `/start` and `/restart` must purge
that state and temporary media without restarting the process. Secrets live in
`.env` (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) and must never be committed.

Confirm this grounding with the user before enshrining it. If they want a
different narrator persona, an extra agent, or a different model, that belongs
in the constitution.

## What to put in each file

### `SPECS/MISSION.md`

The product contract. Keep it short and unambiguous.

- One-paragraph vision, and the end-to-end user experience.
- In-scope behaviour (the pipeline above) and explicit **out-of-scope** items.
- Success criteria: what "working" means for the graded rubric (happy path plus
  the `/restart` reset and graceful handling of out-of-order text/media).
- Non-negotiables: never leak another user's session, never hardcode secrets.

### `SPECS/TECH.md`

The technical contract. This is where policies live, so the other agents can
enforce them.

- **Stack:** Python, Telegram Bot API (long polling), Google ADK, Gemini
  models as listed above, Gemini TTS.
- **Architecture:** ADK hub-and-spoke; the Interviewer is the orchestrator;
  each stage is a discrete agent/module; explicit state-machine phases.
- **Contracts at boundaries:** parse Telegram updates, Gemini responses, and
  TTS output into typed models (Pydantic) at the edge; never pass raw dicts or
  unvalidated payloads across module boundaries. Treat all external input as
  untrusted and arbitrary.
- **Logging & error policy:** comprehensive structured logging; prefer
  decorators over mixing logging into business logic. "Fail loudly and log" for
  non-critical, user-invisible work; on a validated user's conversation path,
  catch errors and degrade gracefully so the conversation continues (logged
  loudly, never raising into the user's flow). No bare `except: pass`, no
  swallowed exceptions, no un-logged fallbacks.
- **Session state:** versioned, per-`chat_id` schema; one shared state driver;
  reset semantics for `/start` and `/restart`.
- **Testing:** Red/Green TDD. Tests are written before code. Dev scripts live
  in `scripts/` (e.g. `scripts/test`, `scripts/hooks`) and are the ground truth
  for tests, lint, and type checks — document them here and in the README.
- **Repo hygiene:** `.env` in `.gitignore`; reproducible environment/
  dependencies.
- **README policy:** what developer-facing behaviour must be documented and
  kept in sync.

### `SPECS/ROADMAP.md`

The ordered build plan, one phase per pipeline capability. A sensible default
ordered from the guide:

1. **Repository & gateway** — project skeleton, `.env`, polling loop that
   echoes a hardcoded reply.
2. **Bouncer** — vision gate + rejection/reset routing.
3. **Interviewer** — sequential stateful Q&A + dossier summary + suggested
   animal.
4. **Converter** — multimodal hybrid portrait sent to Telegram.
5. **Scripter** — one-paragraph narration.
6. **Narrator** — TTS synthesis + audio delivery.
7. **Resilience** — `/restart`/`/start` reset (purge state and temp files),
   wrong-payload-at-wrong-stage guards, API-timeout fallbacks.

Each phase should note its acceptance criteria and which rubric criterion it
serves. Mark phases as complete only when verified.

## Conventions to honour

- Prefer simple, elegant, general solutions; cut scope by default (YAGNI).
- Prefer schemas over regexes.
- Do not include hypothetical future features or "just in case" config.
- Keep the three files tight — the constitution is a contract, not an essay.

## Finishing up

1. Present the drafted constitution to the user in simple, jargon-free terms:
   what the project does, what the technical rules are, and the phase order.
   Ask for changes before finalising.
2. Write the files only after the user is satisfied.
3. **Do not commit.** Leave the `SPECS/` files for the user to review and
   commit themselves.
4. Once written, the `read-constitution` command should load them cleanly;
   offer to run through it to confirm the constitution reads back correctly.

## What not to do

- Do not create feature specs under `SPECS/<date>-<name>/` — that is the
  `plan` agent's job via `feature-specification`.
- Do not write or edit production code.
- Do not invent architecture or scope the user has not confirmed.
- Do not commit or push.
