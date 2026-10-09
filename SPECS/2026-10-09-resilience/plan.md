# Phase 7 — Plan

Seven task groups. Each is Red/Green TDD: the test is written first, watched to
fail for the right reason, then the smallest change that makes it pass. Every
group ends with `./scripts/test` green before the next begins.

Run the dev scripts from `scripts/` — `./scripts/test` is the ground truth
(pytest → ruff → mypy strict, exit 0 required). Baseline on entry: **580
passed**, 14 source files.

---

## TG1 — Classify a throttle as its own failure

**Sweep:** Horizon 1 (the blast radius of the classification) — find every
reader of `GeminiUnavailableError` and confirm the new subclass is a strict
refinement, not a replacement.

**Tests first** (`tests/unit/test_gemini.py`):
- A fake transport raising `APIError` with `code=429` makes `generate` raise
  `GeminiThrottledError`.
- `GeminiThrottledError` is a subclass of `GeminiUnavailableError`, and hence of
  `GeminiError` — the existing catches must keep working.
- A `500` still raises the plain `GeminiUnavailableError`, **not** the throttle
  type — the two must not be conflated.
- The raised error carries `stage`, `reason` and `error_code == 429`.
- The `gemini_throttled` record is emitted with `stage` and `error_code`, and
  carries no exception `str()`.

**Code:** in `gemini.py`, add `GeminiThrottledError(GeminiUnavailableError)`,
add a `_throttled(...)` builder beside `_unavailable`/`_reject`, and split the
429 branch out of `_from_api_error` ahead of the 5xx branch.

**Done when:** the four tests pass, the 5xx test still passes unchanged, and
`GeminiThrottledError` is exported in `__all__`.

---

## TG2 — Retry a throttle, bounded, before giving up

**Sweep:** Horizon 2 (codebase-wide) — confirm `_call_transport` is the *only*
seam through which the SDK is called, so one loop covers every stage. If a second
seam exists, the retry must live where both can share it.

**Tests first:**
- Two `429`s then a success: the call returns the success, the transport was
  called **three** times, and exactly two `gemini_throttled_retry` records were
  emitted with attempts and delays.
- Three `429`s in a row: `GeminiThrottledError` is raised and the transport was
  called exactly three times — the bound holds.
- A `500` on the first attempt: the transport is called **once** — nothing but a
  `429` is retried (guards R1.3).
- A timeout on the first attempt: called once, `GeminiUnavailableError` raised.
- The delays are `1.0` then `2.0` seconds — asserted against the constant, not a
  re-derived number.
- The same tests pass through `synthesize`, proving the seam is shared.

**Code:** add `_THROTTLE_MAX_ATTEMPTS`, `_THROTTLE_BASE_DELAY_SECONDS`, a
module-level `_sleep = asyncio.sleep` seam, `_is_throttle`, `_throttle_delay`,
and the retry loop in `_call_transport` (a `while True` whose only `continue` is
guarded by the attempt bound — no unreachable code, no infinite loop).

**Test seam:** the tests monkeypatch `gemini._sleep` with a recording no-op.
This is the **only** monkeypatch in the phase and is why the delay is an
injectable seam rather than a literal `asyncio.sleep` call.

**Done when:** all retry tests pass, the non-429 tests prove zero extra calls,
and the suite does not slow down measurably.

---

## TG3 — The hub says "try again", not "/restart"

**Tests first** (`tests/unit/test_pipeline.py`):
- A client whose call raises `GeminiThrottledError` makes the hub reply with
  `RATE_LIMITED`, not `GENERIC_FAILURE`.
- `RATE_LIMITED` does **not** contain the substring `/restart` — the regression
  guard for R1.6.
- The session is unchanged after the throttle (D3/R1.7): the same question is
  still pending and a resend is accepted as its answer.
- A throttled **synthesis** on the final answer still returns the narration text
  (D6/R1.8), **not** `RATE_LIMITED` — the delivery/step distinction holds.
- A `rate_limited` record is emitted with `chat_id` and `update_id`.

**Code:** add the `RATE_LIMITED` constant to `pipeline.py`, and catch
`GeminiThrottledError` in `_respond` **before** the existing
`(GeminiError, ...)` catch (it is a subclass, so order is the whole trick).

**Done when:** all four behaviours pass and the existing `GENERIC_FAILURE` tests
are untouched.

---

## TG4 — Prove the wrong-payload guards, cell by cell

**Tests first** (new `tests/unit/test_resilience_matrix.py`, or a parametrized
block in `test_pipeline.py`):
- A parametrized matrix over `{AWAITING_PHOTO, AWAITING_ANSWER, SCRIPTED} ×
  {text, photo, unsupported media}` asserting the **defined** outcome for all
  nine cells, driven through the real hub with a fake client.
- Every cell asserts its own log event (`out_of_order_input`,
  `unsupported_media`, `bouncer_*`, `answer_recorded`, …), so a cell cannot pass
  by silently doing nothing.
- The matrix is built from the enum members, so adding a `Phase` or an
  attachment arm makes the test fail rather than go untested.

**Code:** none expected. If the matrix finds a cell with no defined outcome, fix
it in `pipeline.py` and say so in the report — that would be an instance of the
same "unhandled payload" category, not a new one.

**Done when:** all nine cells are green and assert both the reply and its log.

---

## TG5 — Make silent failure impossible, not merely absent

**Tests first** (new `tests/unit/test_no_silent_excepts.py`):
- Walk every `.py` under `src/`, `ast.parse` each, and collect every
  `ast.ExceptHandler` whose `type is None` (bare `except:`) or whose body is a
  single `Pass` / `Expr(Ellipsis)`.
- Assert the collected set is empty, with a message naming file and line.
- A small unit test of the checker itself, over a source string containing each
  offender shape, so the guard is proven to *fire* — a guard that cannot fail is
  not a guard.

**Code:** none expected. A violation found in `src/` is fixed (logged or
re-raised), never added to an allowlist.

**Done when:** the guard passes over the real tree and its own self-test proves
it detects both shapes.

---

## TG6 — Bring the constitution and README back in line with the code

No new behaviour; the documents currently contradict shipped reality.

- `ROADMAP.md`: Phase 5 delivered (voice note, `lameenc`, `Kore`); Phase 6
  delivered and live-verified (cite the live log: `narration_delivered` +
  `voice_note_sent`, and the timeout fix `fb2b675`); Phase 7 delivered; Phase 4
  recorded as **blocked on image-generation quota** with the 429 evidence; the
  status table and the "live smoke test pending" paragraph corrected.
- `TECH.md`: add the narrator deviations D-V1 (`Reply = str | VoiceNote`) and
  D-V2 (two-method `GeminiClient`); record `lameenc` as the encoder; record the
  per-class timeout policy (text 20 s, synthesis 60 s) and why; record the
  throttle retry.
- `README.md`: module tree gains `narrator.py`; the phase line matches the
  status table.
- `SPECS/2026-10-05-text-vertical-slice/`: cross-refer R9.3 (`Reply = str`) and
  the one-method client to D-V1/D-V2, recorded as deviations rather than left
  as contradictions.

**Done when:** every claim in the four files can be checked against the code or
a commit, and no file names a phase status the repository contradicts.

---

## TG7 — Final gates

- `./scripts/test` green: pytest, ruff, mypy strict, exit 0.
- `./scripts/hooks` green.
- The `investigate-bug` H1/H2 sweeps recorded in the implementation report.
- `git status --porcelain` empty; everything committed.
- No secret rendered anywhere; `.env` and `GITHUB-SSH-KEY.txt` untouched.
- Push the branch.

**Done when:** the suite is green, the working tree is clean, and the branch is
pushed.
