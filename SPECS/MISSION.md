# MISSION

## Vision

**The Telegram Documentaries** is a Telegram bot that turns a single portrait
photo into a narrated, comedic wildlife documentary about the person in it. A
user sends a selfie; the bot interviews them, imagines them as a hybrid animal,
writes a dramatic British-nature-documentary narration about them, and replies
with the hybrid portrait plus a voice note reading the narration aloud.

The delight is the collision: a mundane selfie answered with the gravitas of a
National Geographic voiceover.

## End-to-end user experience

1. User sends `/start`. The bot greets them and asks for a portrait photo.
2. User sends a photo. **Bouncer** checks a human is present. If the image is
   an animal, object, or empty scene, the bot cheerfully rejects it and resets
   state so the user can try again.
3. Bot asks **5–7 interview questions, one at a time**, waiting for each answer.
   Answers accumulate into a behavioural dossier tied to the user's `chat_id`.
4. Bot produces a **hybrid animal portrait**: the user's actual photo fused with
   an animal, judged from the dossier.
5. Bot writes a **60–90 word dramatic narration** about the user.
6. Bot replies with the hybrid image **and a voice note** speaking the narration.

## In scope

- The five pipeline stages below.
- Telegram **long polling** transport (no webhooks, no public URL).
- In-memory session state keyed by `chat_id`.
- Graceful degradation so a validated user's conversation never dies mid-flow.
- `/start` and `/restart` purging state and temporary media without a restart.

## Out of scope (YAGNI)

- Webhooks, public URLs, or any inbound network exposure.
- Multiple concurrent users beyond per-`chat_id` isolation.
- Video generation, image galleries, or multi-frame output.
- Voice cloning, user-chosen voices, or streaming audio.
- A database or any persistence beyond process memory.
- Payment, accounts, or admin tooling.
- Localisation / non-English output.

## The pipeline

| # | Stage | Model | Job |
|---|-------|-------|-----|
| 1 | **Bouncer** | Gemini 3.1 Flash Lite | Vision gate. Confirms a human is present; cheekily rejects non-human images and resets state. Text/image classification only — no TTS, no video. |
| 2 | **Interviewer** | Gemini 3.1 Flash Lite | Orchestrator. Asks 5–7 sequential questions, one at a time, accumulating a behavioural dossier tied to `chat_id`. Also outputs a suggested animal. |
| 3 | **Converter** | Gemini 3.1 Flash Image | Native multimodal fusion of the original photo + dossier into a hybrid animal portrait. Returned straight to Telegram — no intermediate text hop. |
| 4 | **Scripter** | Gemini 3.1 Flash Lite | One-paragraph (~60–90 word) dramatic British-documentary narration built from the dossier. |
| 5 | **Narrator** | *not an agent* | The script is routed directly to Gemini TTS (`gemini-3.1-flash-tts-preview`), rendered to a Telegram-compatible audio format (OGG/MP3), and sent to the chat. |

The Interviewer is the orchestrator; the Narrator is deliberately not an agent.

## Success criteria

The feature is **working** when all of the following hold:

- **Happy path:** a portrait photo produces a hybrid image and a voice note,
  with the interview asking one question at a time and never requiring the user
  to answer several at once.
- **`/restart` reset:** `/restart` purges that `chat_id`'s session state and any
  temporary media, and the bot is immediately ready for a new portrait — with
  no process restart.
- **Out-of-order input:** a text message or media arriving at the wrong stage is
  handled with a helpful prompt or a safe reset — never a crash, never a wrong
  state transition.
- **Isolation:** two users conversing simultaneously never see each other's
  dossier, image, script, or audio.
- **Failure containment:** an upstream Gemini or Telegram error never surfaces as
  a crash or an unhandled exception in the user's conversation.

## Non-negotiables

1. **Never leak another user's session.** All state is scoped to `chat_id`.
2. **Never hardcode secrets.** Tokens and API keys come from `.env` only, and
   `.env` stays in `.gitignore` forever.
3. **Never treat untrusted input as trusted.** Telegram payloads and model output
   are arbitrary and must be validated before use.
4. **Never silence a failure.** Every error is either handled gracefully for the
   user or logged loudly — never both skipped.