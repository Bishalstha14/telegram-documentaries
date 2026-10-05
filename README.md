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

Send `/start`. That is the entire feature today. The bot replies with a short
greeting stating that it is alive and talking to Telegram, and that the
portrait-photo pipeline is not built yet.

Only `/start` is handled. A photo, or any other message, is ignored — the bot
registers no other command and no catch-all.

The interview, the hybrid animal portrait, the narration, the voice note and
`/restart` all arrive in later phases. See `SPECS/ROADMAP.md`.

## Development

### Checks

```bash
scripts/test    # pytest + ruff + mypy over the whole tree — the ground truth
scripts/hooks   # pre-commit: ruff + pytest scoped to staged .py files,
                # mypy always over all of src/
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
- `.guides/img/` — wildlife mascot reference art

## Status

Phase 1 of 7 — repository and gateway. See `SPECS/ROADMAP.md` for the full plan.

## License

Not yet specified.