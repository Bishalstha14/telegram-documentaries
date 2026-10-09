# The Telegram Documentaries

A Telegram bot that turns a portrait photo into a narrated, comedic wildlife
documentary about the person in it. Built with Google's Agent Development Kit
(ADK).

Send a selfie. Get interviewed. Receive your own face fused with an animal,
plus a voice note reading a dramatic British-nature-documentary narration about
you.

## How it works

Five stages, orchestrated by the Interviewer:

| # | Stage | Model | Job |
|---|-------|-------|-----|
| 1 | **Bouncer** | Gemini 3.1 Flash Lite | Vision gate. Accepts human portraits, cheekily rejects everything else, resets state. |
| 2 | **Interviewer** | Gemini 3.1 Flash Lite | Orchestrator. Asks 5–7 questions one at a time, builds a behavioural dossier, suggests an animal. |
| 3 | **Converter** | Gemini 3.1 Flash Image | Fuses your real photo with the dossier into a hybrid animal portrait. |
| 4 | **Scripter** | Gemini 3.1 Flash Lite | Writes a 60–90 word dramatic narration. |
| 5 | **Narrator** | Gemini TTS | *Not an agent* — speaks the script and sends a voice note. |

Transport is Telegram **long polling**. No webhooks, no public URL, no open
ports — the bot dials out to Telegram.

> **Current state:** stages 1, 2, 4 and 5 — Bouncer, Interviewer, Scripter and
> Narrator — are wired end to end: send a portrait photo, answer 5–7 questions,
> get a voice note reading the narration (text if synthesis fails). The hybrid
> image (Converter) is the one outstanding stage — **blocked on image-generation
> quota** on the project's API key, a billing matter, not a code one. Scroll to
> [Status](#status).

## Setup

### 1. Prerequisites

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
```

Then fill in `.env`:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
```

`.env` is gitignored and must never be committed.

### 4. Run

```bash
python -m telegram_documentaries
```

### 5. Use

```
/start            greet you and ask for a photo
<portrait photo>  pass the Bouncer, then answer 5–7 questions
                  -> receive a voice note reading the narration
                  (if the speech call fails, the narration arrives as text)
/restart          at any time: wipe the session and start over
```

Everything else is declined with one short line. A sticker, a video or a voice
note gets `unsupported_media`; a photo that fails the vision gate gets the
Bouncer's rejection; a text message arriving before the photo gets a nudge back
to the conversation. One reply per message, always.

The hybrid animal image is not built yet — the Converter is blocked on
image-generation quota for the project's API key, a billing matter. When the
interview finishes you get the narration as a **voice note** (or as text if
the speech call fails). See `SPECS/ROADMAP.md`.

## How it is built

The interesting part is not the five stages above. It is the shape of the code
behind them.

### One module is allowed to talk to Gemini

```
bot.py ──▶ pipeline.py ──┬──▶ bouncer.py ─────┐
          (the decision  ├──▶ interviewer.py ─┼──▶ gemini.py ──▶ google.genai
           table)        ├──▶ scripter.py ────┘
                         └──▶ narrator.py ────────────▶ gemini.py (TTS)
```

No stage imports the SDK. A stage builds a typed `GeminiRequest` and gets back
either a validated Pydantic model or one of two errors. Everything in between is
ordinary, testable Python — which is why the whole suite runs with zero network
access.

`bot.py` is only an adapter: it parses the update, calls
`pipeline.handle_start` / `handle_restart` / `handle_message`, and sends the one
string that comes back. It has no conversational text of its own, and it never
constructs a hub — `__main__` builds the collaborators and injects them. The
consequences live in `state.py`, a plain class with no I/O, so the decision
table is testable without Telegram, Gemini or a filesystem.

### Three kinds of Gemini failure, kept apart

| Error | Means | What the user experiences |
|---|---|---|
| `GeminiThrottledError` | The API is rate-limiting (429) and the bounded retry ran out | "Give me a moment and send that again." The session survives — the resend is consumed as the answer already on screen. |
| `GeminiUnavailableError` | Timeout, network failure, 5xx | Retry later. The session survives. |
| `GeminiResponseError` | A reply arrived and was unusable | Something is wrong with the model's output. Escalate. |

Collapsing these into one exception would force a choice between losing a
half-finished interview on a rate limit, and hiding a broken reply behind a
friendly "try again". Throttles are retried briefly (three attempts, 1 s then
2 s); nothing else is, because a retry on a slow call holds the chat open for
nothing.

### Rejected, never repaired

A malformed reply is treated as a broken reply, never patched up:

- **Truncated** (`MAX_TOKENS`) is rejected *before* parsing. The text of a
  truncated reply is a valid JSON prefix, so parsing it would mean inventing
  content the model never produced.
- **Off-schema** payloads are rejected, not coerced. The schemas are strict, so
  a model that invents a field is refused rather than quietly believed.

### No secret ever reaches a log

`google.genai`'s `APIError.__str__` interpolates the raw HTTP response body into
its own message, so printing any failed traceback would print it verbatim. This
is the same leak class the project already neutralised for Pydantic's
`ValidationError` in Phase 1 — a different library, the same defence.

Records carry the exception's *class name* and error code. Messages are assembled
from the stage, a fixed reason and field *names*. The leaky originals are chained
with `from None` so the stdlib formatter cannot reach them. Tests sweep for a
planted key and a planted response body appearing in no log record, rendered the
way a human would actually see them.

### Configuration fails fast, and quietly

Both secrets load as `SecretStr`. A blank secret is rejected as missing, because
`Field(min_length=1)` silently does *not* enforce on `SecretStr` — a
whitespace-only token would otherwise load fine and die later as `InvalidToken`
deep inside the Telegram library. A config error names only the offending
*fields*, never their values, because Pydantic renders the sibling secret inside
its error text.

## Development

### Checks

```bash
scripts/test    # pytest + ruff + mypy over the whole tree — the ground truth
scripts/hooks   # pre-commit: ruff + pytest scoped to staged .py files,
                # mypy always over all of src/
```

`scripts/test` is the gate that matters. mypy runs strict, over `src/`, with
`warn_unreachable` on. It earns its place: the type checker is what caught a real
bug in the Gemini boundary that 49 passing tests had hidden — the SDK's async
call lives at `client.aio.models.generate_content`, not on the client, so the
first wiring type-checked and passed everything and would still have crashed on
the first live photo.

### Layout

```
src/telegram_documentaries/
  __main__.py     entry point; builds the hub and injects it
  bot.py          the Telegram adapter: parse -> hub -> one send
  pipeline.py     the decision table; the only place that decides
  state.py        SessionStore, phases, transitions — no I/O
  contracts.py    InboundUpdate, the typed Telegram boundary
  bouncer.py      the vision gate
  interviewer.py  5–7 questions, one at a time
  scripter.py     the 60–90 word narration, one corrective retry
  narrator.py     waveform -> MP3 voice note (lameenc, voice Kore) — not an agent
  media.py        MediaStore, the local file tree
  gemini.py       the only module that imports google.genai
  config.py       Settings from .env, both secrets as SecretStr
  observability.py  configure_logging, get_logger, @logged

tests/unit/       608 tests, no network, no real credentials
SPECS/            the constitution, and one folder per feature
```

### Contributing

This project is spec-driven. Work flows:

**constitution → feature spec → implementation → review → verification → PR**

1. Read `SPECS/MISSION.md`, `SPECS/TECH.md`, `SPECS/ROADMAP.md`.
2. A feature spec lives in `SPECS/<YYYY-MM-DD>-<feature-name>/` on its own
   `feature/<YYYY-MM-DD>-<feature-name>` branch.
3. Implement with Red/Green TDD — tests before code.
4. Review, verify, then open a PR against `main`.

Never implement directly on `main`.

## Documentation

- `SPECS/MISSION.md` — what the project is and must do
- `SPECS/TECH.md` — the technical contract: stack, architecture, policies
- `SPECS/ROADMAP.md` — the ordered build plan
- `SPECS/2026-10-05-repository-and-gateway/` — Phase 1, shipped
- `SPECS/2026-10-05-text-vertical-slice/` — Phases 2/3/5 text slice, shipped
- `SPECS/2026-10-08-narrator-voice-note/` — Phase 6, shipped and live-verified
- `SPECS/2026-10-09-resilience/` — Phase 7, shipped
- `.guides/img/` — wildlife mascot reference art

## Status

| Phase | What it delivers | State |
|---|---|---|
| 1 | Repository and `/start` gateway | **Done** — verified against the live bot |
| 2–5 | Photo → Bouncer → Interviewer → Scripter → Narrator | **Done** — 608 tests green; voice note delivered live (`narration_delivered` + `voice_note_sent`) |
| 3 (img) | Converter: the hybrid animal portrait | **Blocked** — image-generation quota on the project's API key (`429` on every image model); resumes when billing is enabled |
| 7 | Hardening and polish | **Done** — per-class timeouts, bounded 429 retry, wrong-payload matrix, no-silent-except guard |

Phases 2–4 of the roadmap were built together as one vertical slice so that a
real narration arrives early, then the Narrator (Phase 6) gave it a voice. The
riskiest part — a stateful multi-turn conversation — stays small and provable
on its own, and the voice note was verified live before Phase 7's hardening.

**What you can do today:** `/start`, send a portrait photo, answer 5–7
questions, hear the narration as a **voice note** (or read it as text if the
speech call fails), `/restart`. **What you cannot do yet:** see the hybrid
animal image — the Converter is blocked on image-generation quota, a billing
matter, not a code one.

See `SPECS/ROADMAP.md` for the full plan.

## License

MIT — see [LICENSE](LICENSE).