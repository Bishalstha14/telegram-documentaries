# Narrator transient server-error retry

One bounded retry for the voice step only. When Gemini's speech model answers a
synthesis call with a *transient server error* (HTTP 500/502/503/504), the
voice step tries once more after a short delay before giving up and degrading to
the narration text. No other stage changes.

## Context

On 2026-10-09 the live bot failed the voice step **three times** with
`gemini_call_failed | error_code=504 error_type=ServerError stage=narrator`.
Each time the narration was delivered as **text** (the D6 fallback) and the
voice note was lost for that conversation: the session is terminal (SCRIPTED),
so the only recovery was redoing the whole interview.

Direct probes with the *identical* code path and key succeeded (4.2 s / 16.5 s /
25.4 s for 13/61/99-word texts), proving the endpoint is healthy but
intermittently slow (16–25 s) and that Google's own gateway answers `504`
when the preview model (`gemini-3.1-flash-tts-preview`) crosses roughly its
~20 s server-side window. This is upstream flakiness, not a code defect — it is
a *resilience gap*.

The gap is a decision asymmetry, not a missing mechanism. The resilience spec
recorded R1.3 — *only `429` is retried* — for the interview **steps**
(bouncer/interviewer/scripter), which are naturally re-triggerable: a failed
step is simply re-run by the user's next message answering the same question
(D3 holds the state). The narrator **synthesis** is a single-shot **delivery**:
its only chance is spent on the first attempt, and a transient upstream hiccup
permanently denies the user the deliverable they completed the interview for.

The `investigate-bug` H1/H2 sweeps (recorded in the implementation report)
found no other terminal-delivery site: the Telegram `send_voice` upload failure
fell back to text safely (`voice_note_send_failed`, R4.3), and step 5xx
failures are re-triggerable. Only the synthesis is single-shot.

## Scope

### R1 — One bounded retry for a transient server error on synthesis

- **R1.1** A synthesis call that raises `GeminiUnavailableError` for a
  **transient server error** — `error_code` in `{500, 502, 503, 504}` — is
  retried once, after a bounded delay, at the **shared transport seam**
  (`_call_transport`). At most `_TRANSIENT_MAX_ATTEMPTS` attempts in total.
- **R1.2** The transient marker lives on the typed error, classified **once**:
  `GeminiUnavailableError.transient` is `True` exactly when the code is in the
  transient set, set by the existing `_from_api_error` / `_unavailable`
  builders. No call site re-derives status codes.
- **R1.3** The retry is **opt-in** at the transport: `_call_transport` gains a
  parameter (e.g. `retry_transient_server_errors: bool = False`) so the
  generate path (steps) can never receive it. Only the narrator's
  `_synthesize` passes `True`. **The steps keep R1.3 of the resilience spec
  verbatim:** a 5xx on a step still fails once, exactly as today.
- **R1.4** Bounded backoff: `_TRANSIENT_MAX_ATTEMPTS = 2`, delay
  `_TRANSIENT_BASE_DELAY_SECONDS = 2.0` (one wait of 2 s between attempts,
  using the existing `_sleep` seam so tests never wait).
- **R1.5** A **timeout** and a **transport failure** on synthesis are **not**
  retried — the transient set is server errors only. Retrying a slow call
  holds the chat open for nothing (the recorded R1.3 rationale).
- **R1.6** A `429` on synthesis continues to use the **throttle** retry
  (`_THROTTLE_*`) — the two retry rules are independent and do not interfere.
- **R1.7** When the retry is spent (`GeminiUnavailableError` transient raised
  on both attempts), the call propagates `GeminiUnavailableError` and the
  pipeline's existing D6 path delivers the narration as text with **one**
  `narration_voice_failed` record — the user-visible failure line is unchanged.
- **R1.8** When the retry succeeds, the pipeline delivers the `VoiceNote` and
  logs `narration_delivered` as if the first attempt had succeeded — the retry
  is invisible to the user.
- **R1.9** Logging: each retry emits `gemini_transient_retry` (WARNING) with
  `stage`, `attempt`, `delay` and `error_code`, before sleeping. The failed
  first attempt still emits its `gemini_call_failed` (ERROR) record. No
  exception's `str()` is rendered anywhere (the standing rule).

### R2 — Step behaviour preserved

- **R2.1** The existing no-retry tests for steps keep passing **unchanged**:
  `test_a_server_error_is_not_retried` and `test_a_timeout_is_not_retried`
  (both drive `_generate`). No step, in any phase, retries a 5xx.
- **R2.2** The throttle tests (`test_a_throttle_is_retried_*`,
  `test_the_retry_bound_holds_*`) keep passing unchanged.

## Out of scope (YAGNI)

- **Retrying steps.** The carve-out is for the delivery only; step failures
  remain re-triggerable by the user's next message (D3), which is cheaper than
  any retry policy.
- **Retrying timeouts or transport failures.** Deliberate (R1.5).
- **Retrying encoding failures (`NarratorError`).** Those are ours, not
  upstream; retrying cannot fix them.
- **Jitter, exponential backoff beyond one step, circuit breakers, or a retry
  queue.** One bot, one user; two attempts and one 2 s wait are the whole policy.
- **Changing the user-visible failure line, `RATE_LIMITED`, or the D6
  fallback.**
- **Phase 4 (Converter).** Still blocked on image-generation quota.

## Decisions

- **D1 — retry the delivery, never the steps (chosen by the user).** The step
  retry is pointless — the user re-runs a step by answering again — while the
  delivery's single shot has no second chance. This is the asymmetry the spec
  closes.
- **D2 — transient set is `{500, 502, 503, 504}`, nothing else.** The classic
  retryable set: gateway/product-server hiccups. `501`/`505` are not retryable;
  timeouts and transport failures are exactly the "slow call" R1.3 refused to
  hold the chat open for.
- **D3 — classify once, on the type.** `_from_api_error` already splits a 5xx
  from a rejection; the marker is one boolean on `GeminiUnavailableError`
  populated there, so the transport loop tests `exc.transient` instead of
  re-reading status codes — the same "classify at the transport, decide at the
  hub" shape as the throttle (resilience D3).
- **D4 — opt-in parameter, default `False`.** The generate path never passes
  it; this is what keeps every step test byte-for-byte unchanged.
- **D5 — one flat 2 s wait, not exponential.** With max two attempts there is
  exactly one delay; exponential notation (`base * 2**attempt`) is kept for
  consistency with the throttle idiom, but no second term exists.
- **D6 — the retry lives in `_call_transport`, not `narrator.py`.**
  `_synthesize` already routes through the seam; the single loop inherits the
  existing classification and `_sleep` seam. `narrate` stays a plain function.

## The category, and why the fix is general

- **Category fixed:** *a transient upstream failure permanently denying a
  single-shot delivery whose recovery is redoing the whole conversation.* The
  instance is a synthesis `504`; the general rule is that a delivery reaches
  the user via a bounded retry before falling back, while re-triggerable steps
  keep failing once.
- **Sibling swept (Horizon 1):** `voice_note_send_failed` (bot.py) — already
  safe (text fallback, R4.3); step 5xx (bouncer/interviewer/scripter) — safe by
  re-triggerability; no other terminal delivery found.
- **Swept (Horizon 2):** all outbound calls in the repo — Gemini `generate`
  (3 stages), Gemini `synthesize` (narrator), Telegram sends. Only synthesis is
  both single-shot and currently retryless. Nothing else changes.
- **Prevention (mechanism-level):** the transient marker is classified in the
  one builder every caller passes through, so a future *new* call cannot decide
  retry-eligibility ad hoc; the opt-in flag is default-off, so a future *new*
  step cannot inherit the retry by accident; the existing step tests lock that
  guarantee.