# Handover — The Telegram Documentaries

This file tells a new reader (tutor, reviewer, or future maintainer) everything
they need to evaluate and continue the project. The authoritative details live
in `SPECS/` — this is the map, not the territory.

## The project, in one paragraph

A Telegram bot (`@BishalTech_bot`) that turns a portrait photo into a narrated
wildlife documentary. A user sends a selfie; the bot checks it is a human
portrait, interviews them with 5–7 behavioural questions, writes a 60–90 word
British-documentary-style narration about them, and delivers it as a **voice
note**. The one unimplemented stage — fusing the portrait with an animal image —
is blocked on **API billing**, not on code.

## Where everything is

- **Repository:** `github.com/Bishalstha14/telegram-documentaries`
- **Default position of all work:** branch `feature/2026-10-05-text-vertical-slice`
  (merged into `main` on handover; `main` is the whole project)
- **Spec-driven:** every feature lives in `SPECS/<date>-<name>/` with
  `requirements.md`, `plan.md`, `validation.md`, built with Red/Green TDD.
  `SPECS/MISSION.md`, `TECH.md`, `ROADMAP.md` are the constitution.

## Status table

| # | Stage | Model | Status |
|---|-------|-------|--------|
| 1 | Gateway (`/start`, long polling) | — | **Done**, verified live |
| 2 | Bouncer (vision gate) | Gemini 3.1 Flash Lite | **Done** |
| 3 | Interviewer (5–7 Q&A, dossier) | Gemini 3.1 Flash Lite | **Done** |
| 4 | Converter (animal image) | Gemini image models | **Blocked — image-generation quota** (all image models return `429`; free tier unavailable for images; needs billing, ~$0.067/portrait) |
| 5 | Scripter (narration) | Gemini 3.1 Flash Lite | **Done** |
| 6 | Narrator (TTS voice note) | `gemini-3.1-flash-tts-preview` | **Done, verified live** |
| 7 | Resilience (timeouts, 429 retry, guards) | — | **Done** |

Suite: **608 unit tests** (pytest), **ruff clean**, **mypy strict clean**,
**zero network access in tests** (all Gemini/Telegram calls are fake seams).

## What "done" means here — the evidence

- Voice note **verified live**: the bot logged `narration_delivered` then
  `voice_note_sent`, and the received voice note played.
- Voice is MP3, encoded **in-process** with `lameenc` (no FFmpeg, no system
  binary); voice `Kore`, pinned behind a `Literal`.
- Timeouts are **per call class**: text calls 20 s, synthesis 60 s — a
  synthesis call at text latency would time out a good voice note.
- A rate-limited request (`429`) is retried at most 3 times (1 s then 2 s
  backoff) and only it is retried; a spent throttle answers *"give me a moment
  and send that again"* — never `/restart`, because the session is held.
- A **matrix test** proves every (phase × payload) cell of the decision table
  answers *and* logs; an **AST guard** prevents any silent `except: pass` from
  ever being committed.
- Secrets never reach logs (sweep-tested: token and API key, plus a planted
  Gemini response body, appear in no record).

## How to run it

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # fill in TELEGRAM_BOT_TOKEN and GEMINI_API_KEY
.venv/bin/python -m telegram_documentaries    # long-polls Telegram, no public URL
```

Checks:

```bash
./scripts/test      # ground truth: pytest + ruff + mypy strict
./scripts/hooks     # pre-commit gate, staged-scoped
```

## How to verify it live

1. Start the bot (above). It dials **out** to Telegram — no ports, no webhook.
2. In Telegram, message `@BishalTech_bot`: `/start` → send a portrait photo.
3. Answer the 5–7 questions one at a time.
4. Receive the voice note reading the narration. Send `/restart` to wipe it.
5. Send a non-photo (sticker/document) to see the polite refusal.

## Points worth checking for review

- The decision table in `src/telegram_documentaries/pipeline.py` is the single
  place that decides what any update means; the bot adapter (`bot.py`) contains
  no conversational text.
- `gemini.py` is the **only** module that imports `google.genai`; every stage
  builds a typed request and gets a validated model or a typed error.
- Note the recorded divergences in `SPECS/TECH.md` (D1, D2) and
  `SPECS/2026-10-08-narrator-voice-note/requirements.md` (D-V1–D-V4): the
  project calls `google.genai` directly rather than ADK `Runner`, and the
  Narrator widened `Reply` to `str | VoiceNote`.

## Repo hygiene

- `.env` and `GITHUB-SSH-KEY.txt` are gitignored; nothing sensitive is
  committed. A credential sweep of all log records is part of the suite.
- Branches: `main` (integration), `feature/phase1-repository-and-gateway`
  (superseded by the slice branch), `feature/2026-10-05-text-vertical-slice`
  (all work).
- The bot is long-polling and has no supervisor: it dies when its machine or
  workspace restarts and must be restarted by hand (`python -m
  telegram_documentaries`). `/tmp` logs are not durable.

## Open items

1. **Phase 4 (Converter):** blocked on image-generation quota for the API key.
   Billing must be enabled on the key; then the already-written call path runs
   unchanged.
2. **Live maintenance:** re-verify the voice note after the key changes, and
   restart the bot after any machine/workspace restart.