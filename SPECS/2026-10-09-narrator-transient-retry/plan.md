# Narrator transient server-error retry — Plan

Three task groups. Each is Red/Green TDD: the test is written first, watched to
fail for the right reason, then the smallest change that makes it pass. Every
group ends with `./scripts/test` green before the next begins.

Run the dev scripts from `scripts/` — `./scripts/test` is the ground truth
(pytest → ruff → mypy strict, exit 0 required). Baseline on entry: **608
passed**, 14 source files.

The `investigate-bug` report (H0: synthesis `504` on 2026-10-09, x3; H1/H2
sweeps: no other terminal-delivery site) is recorded in the implementation
report accompanying the branch.

---

## TG1 — Mark transient server errors on the typed error

**Sweep:** Horizon 1 — every reader of `GeminiUnavailableError` (the narrator's
D6 catch in `pipeline.py`, the hub's `_respond`, the throttle tests) must be
unaffected by the new field. Confirm `_from_api_error` and `_unavailable` are
the only builders of the type.

**Tests first** (`tests/unit/test_gemini.py`):
- A fake transport raising `ServerError(500, ...)` through `_generate` raises
  `GeminiUnavailableError` with `transient is True`.
- A `ServerError(504, ...)` also carries `transient is True`.
- A `ServerError(501, ...)` carries `transient is False` — the transient set is
  exactly `{500, 502, 503, 504}`.
- An `httpx.ReadTimeout` carries `transient is False` — timeouts are never
  marked transient.
- A `ClientError(429, ...)` still produces `GeminiThrottledError`, whose
  `transient` is `False` and whose subtype relationship is unchanged.

**Code:** in `gemini.py`, add `_TRANSIENT_SERVER_CODES = frozenset({500, 502,
503, 504})` beside `_THROTTLED_CODE`; add a `transient: bool = False` field to
`GeminiUnavailableError`; pass the flag through `_unavailable` (new kwarg,
default `False`) and set it `True` in the server branch of `_from_api_error`
only when `_code_of(exc) in _TRANSIENT_SERVER_CODES`. Additive log field:
`gemini_call_failed` gains `"transient"` in its extra (additive; no existing
test asserts its absence).

**Done when:** the five tests pass, every existing `GeminiUnavailableError`
construction elsewhere still compiles untouched, and `./scripts/test` is green.

---

## TG2 — The opt-in bounded retry at the seam

**Sweep:** Horizon 2 — confirm `_call_transport` is still the *only* seam
through which the SDK is called (`_generate_content` and `_synthesize` both
route through it). If a second seam exists, the flag must be threaded through
both.

**Tests first** (`tests/unit/test_gemini.py`, reusing the existing
`FakeTransport` + `no_wait` recording-`_sleep` fixtures):
- **Synthesis, 504 then success:** the transport is called **twice**, the call
  returns successfully, and exactly **one** `gemini_transient_retry` record was
  emitted with `attempt == 1`, `delay == 2.0`, `error_code == 504` and
  `stage == NARRATOR`. The first attempt's `gemini_call_failed` record is still
  present (loud failure preserved).
- **Synthesis, 504 then 504:** `GeminiUnavailableError` is raised, the
  transport was called exactly **twice** (the bound holds), exactly one
  `gemini_transient_retry` record.
- **Synthesis, timeout:** the transport is called **once** — a timeout is not
  in the transient set (R1.5).
- **Synthesis, 429:** still uses the throttle path — called up to
  `_THROTTLE_MAX_ATTEMPTS`, and no `gemini_transient_retry` record appears
  (the two retry rules do not interfere, R1.6).
- **Generate (steps), 500:** the transport is called **once** — the flag is
  default-off; the existing `test_a_server_error_is_not_retried` and
  `test_a_timeout_is_not_retried` (which drive `_generate`) pass **unchanged**
  (R2.1).
- The delays are asserted against `_TRANSIENT_BASE_DELAY_SECONDS`, never a
  re-derived number.

**Code:** add `_TRANSIENT_MAX_ATTEMPTS = 2` and
`_TRANSIENT_BASE_DELAY_SECONDS = 2.0` beside the throttle constants; add
`retry_transient_server_errors: bool = False` to `_call_transport`; inside the
existing `except genai_errors.APIError` branch, classify via `_from_api_error`
and, only when the flag is set **and** the classified error is
`GeminiUnavailableError` with `transient is True` **and** attempts remain,
log `gemini_transient_retry` (WARNING with stage/attempt/delay/error_code,
`error_code = classified.error_code`), `await _sleep(delay)`, and `continue`;
otherwise `raise classified from None`. `_synthesize` passes
`retry_transient_server_errors=True`; `_generate_content` stays on the default.

**Test seam:** reuse the existing `_sleep` monkeypatch (`no_wait`) — no new
monkeypatch, no real sleep.

**Done when:** all retry tests pass, the step tests are byte-for-byte
unchanged and green, and the suite is green.

---

## TG3 — Delivery-level proof through the pipeline

**Tests first** (`tests/unit/test_pipeline.py`, driving the hub with the
existing fake-client pattern):
- A client whose `synthesize` raises a `transient` `GeminiUnavailableError`
  once and then returns a valid audio payload: the final answer yields a
  `VoiceNote` reply and exactly one `narration_delivered` record (R1.8) — the
  retry is invisible to the user.
- A client whose `synthesize` raises it **twice**: the reply is the narration
  **text** (D6 preserved), exactly **one** `narration_voice_failed` record with
  `reason == "synthesis"`, and **no** `narration_delivered` record (R1.7).
  The hub-level failure record count is one — retries happened inside the
  transport and are counted by `gemini_transient_retry`, not by this event.
- A client whose `synthesize` raises a **non-transient** unavailable error
  (e.g. `error_code == 501`): the reply is the narration text and the transport
  (fake client) was called once — nothing outside the transient set retries.

**Code:** none expected — this group proves the wiring end to end. If the
pipeline tests force a change, it is a naming/wiring fix in `gemini.py`, and
the report says so.

**Done when:** all three delivery outcomes are green and the existing D6
synthesis tests (throttled/encoding failures) are untouched.

---

## TG4 — Final gates

- `./scripts/test` green: pytest, ruff, mypy strict, exit 0.
- `./scripts/hooks` green.
- The `investigate-bug` H1/H2 records from TG1/TG2 restated in the
  implementation report.
- `git status --porcelain` empty; everything committed on
  `feature/2026-10-05-text-vertical-slice`.
- No secret rendered anywhere; `.env` and `GITHUB-SSH-KEY.txt` untouched.
- Docs deltas recorded for the verifier: `SPECS/2026-10-09-resilience/`
  (R1.3 gains a cross-reference to this carve-out), `TECH.md` (the transient
  retry line), `validation.md` (this spec's F-section reconciliation).

**Done when:** the suite is green, the working tree is clean, and the branch
carries the commits.