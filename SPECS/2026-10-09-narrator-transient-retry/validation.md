# Narrator transient server-error retry — Validation

How we know the delivery retry is real and the branch can be merged. Each item
is checkable by a command or by reading one named file — not by trusting a
summary.

---

## A. Requirement coverage

| Req | Where it is proven |
|---|---|
| R1.1 one bounded retry on transient 5xx | `test_gemini.py`: 504-then-success calls the transport twice; 504-then-504 raises after two calls |
| R1.2 marker classified once on the type | `test_gemini.py`: `GeminiUnavailableError`-typed asserts of `transient` for 500/504 (`True`) and 501/timeout (`False`) |
| R1.3 opt-in; steps unchanged | `test_gemini.py`: `test_a_server_error_is_not_retried`, `test_a_timeout_is_not_retried` pass **unchanged** (drive `_generate`, transport called once); the flag is `False` by default on the generate path |
| R1.4 bound and delay | constants `_TRANSIENT_MAX_ATTEMPTS = 2`, `_TRANSIENT_BASE_DELAY_SECONDS = 2.0`; delay asserted against the constant, not re-derived |
| R1.5 timeout/transport not retried | `test_gemini.py`: synthesis timeout calls the transport once |
| R1.6 429 still uses the throttle path | `test_gemini.py`: 429 on synthesis produces throttle retries and **no** `gemini_transient_retry` record |
| R1.7 spent retry → D6 text, one record | `test_pipeline.py`: double-transient client → text reply, one `narration_voice_failed` (`reason == "synthesis"`), no `narration_delivered` |
| R1.8 successful retry → `VoiceNote` | `test_pipeline.py`: transient-then-success client → `VoiceNote` reply, one `narration_delivered` |
| R1.9 logging | `test_gemini.py`: `gemini_transient_retry` (WARNING) carries `stage`, `attempt`, `delay`, `error_code`; first attempt still logs `gemini_call_failed`; no `str(exc)` anywhere (standing rule) |
| R2.1/R2.2 step and throttle tests unchanged | baseline suite comparison: those tests are byte-for-byte the same and green |

---

## B. Constitution compliance

- **Never silence a failure (MISSION §4).** A transient failure is logged as
  `gemini_call_failed` (ERROR), then `gemini_transient_retry` (WARNING), and a
  spent retry still answers the user with the narration text — handled *and*
  logged, never skipped.
- **Fail loudly vs degrade (TECH "Logging & error policy").** A delivery
  failure degrades to text after a bounded retry; a step failure still fails
  once. Both are the policy's stated shape.
- **No raw `str(exc)` anywhere (standing rule).** Every new record carries the
  class name, stage, code and attempt — never the rendered exception.
- **Schemas over regexes (TECH).** No new text parsing is introduced.
- **Contracts at boundaries.** The retry hardens an existing boundary; it adds
  no new one.
- **YAGNI.** No new dependency, no config toggle, no jitter, no circuit
  breaker, no change to user-facing copy or to `RATE_LIMITED`.

---

## C. Evidence to collect

```bash
# the ground truth
./scripts/test                          # expect: > 608 passed, ruff clean, mypy clean

# the new retry, alone — and prove it is fast (no real sleeping)
.venv/bin/python -m pytest tests/unit/test_gemini.py -k transient -v

# the throttled path is untouched
.venv/bin/python -m pytest tests/unit/test_gemini.py -k throttle -v

# the step tests that must remain unchanged (drive generate, called once)
.venv/bin/python -m pytest tests/unit/test_gemini.py -k "not_retried" -v

# the delivery-level outcomes through the hub
.venv/bin/python -m pytest tests/unit/test_pipeline.py -k narration -v

# the bound is a named, testable constant and the flag is default-off
grep -n "_TRANSIENT_MAX_ATTEMPTS\|_TRANSIENT_BASE_DELAY_SECONDS\|_TRANSIENT_SERVER_CODES\|retry_transient_server_errors" \
  src/telegram_documentaries/gemini.py

# the working tree is clean and everything is pushed
git status --porcelain                  # expect: empty
git log --oneline origin/feature/2026-10-05-text-vertical-slice..HEAD   # expect: empty
```

---

## D. Forced-failure check (optional, no network)

The retry path is proven deterministically against the fake transport in unit
tests — the ground truth, offline. A manual check is possible by letting a live
conversation hit the real speech model, but it is **not required**: whether
Google answers `504` is outside our control, and the classification is exercised
deterministically by the fake.

If a live `504` does appear, expect in the log: `gemini_call_failed`
(`error_code=504`, `transient=true`) → `gemini_transient_retry`
(`attempt=1`, `delay=2.0`) → then either `narration_delivered` +
`voice_note_sent` (retry succeeded) or, if the second attempt also 504s,
`narration_voice_failed` and the narration text (spent retry).

---

## E. Spec-vs-reality reconciliation

Verified by the verifier on 2026-10-10 against the uncommitted working tree on
`feature/2026-10-05-text-vertical-slice`. All four pre-registered candidates
landed as planned; two implementation refinements were added after review.

**Pre-registered candidates — all CONFIRMED:**

1. **Retry lives in `_call_transport` behind the opt-in flag.** `_call_transport`
   gained `retry_transient_server_errors: bool = False`; `_synthesize` passes
   `True`; `_generate_content` leaves the default. Because the flag is
   default-off, the generate path (steps) cannot inherit the retry.
2. **No step test needed touching.** `git diff -U0 tests/unit/test_gemini.py`
   shows **zero removed lines** — the change is entirely additive.
   `test_a_server_error_is_not_retried` and `test_a_timeout_is_not_retried`
   are byte-for-byte unchanged and green (`-k not_retried` → 2 passed).
3. **`GeminiThrottledError` does not carry `transient=True`.** It is built by
   `_throttled`, which never passes `transient`, so it keeps the field's `False`
   default; `test_a_throttle_is_not_marked_transient` asserts
   `transient is False` and the subtype relationship. The throttle branch is
   checked ahead of the transient branch in the retry decision.
4. **Transient set unchanged.** `_TRANSIENT_SERVER_CODES` is
   `frozenset({500, 502, 503, 504})`, locked by
   `test_the_transient_retry_policy_is_named_constants`. No live
   `503`-vs-`504` divergence was observed — the feature is proven
   deterministically against the fake transport.

**Implementation refinements (post-review), now recorded:**

- **(a) Pipeline delivery tests use a module-local autouse `no_wait` fixture.**
  `tests/unit/test_pipeline.py` patches `gemini._sleep` through an **autouse,
  module-local** fixture (not the shared `tests/unit/conftest.py` one), kept
  local for narrow blast radius per the review. Those tests route synthesis
  through the *real* `gemini._synthesize` over a scripted fake transport — so
  the retry genuinely executes — and assert the requested delay equals
  `_TRANSIENT_BASE_DELAY_SECONDS`, not a re-derived number.
- **(b) Separate attempt budgets (`transient_attempts`).** After code review
  the throttle rule and the transient rule were given **independent** counters,
  because a shared counter let a `429` sequence consume the transient rule's
  single attempt. Locked by
  `test_synthesis_retries_a_transient_error_after_a_throttle_takes_its_turn`
  (429 → 504 → success: the transport is called 3×, with exactly one
  `gemini_throttled_retry` and one `gemini_transient_retry`). The throttle
  branch is evaluated first, so a `429` still takes the throttle path.

**Suite at verification.** `./scripts/test` → **630 passed**, ruff clean, mypy
strict clean (14 source files). Focused runs: `-k transient` 17 passed,
`-k throttle` 12 passed, `-k not_retried` 2 passed,
`tests/unit/test_pipeline.py -k narration` 5 passed. `./scripts/hooks` exits 0
(no staged Python files — the work is uncommitted, as instructed).
`git log origin/feature/2026-10-05-text-vertical-slice..HEAD` is empty: there
are no unpushed commits; the entire change is uncommitted in the working tree.

## F. Lasting notes

- **Where the marker is classified.** `GeminiUnavailableError.transient` is set
  only through `_unavailable(..., transient=...)`, and that argument is computed
  only in `_from_api_error`'s server branch (`code in _TRANSIENT_SERVER_CODES`).
  No other constructor path sets it, and no call site re-reads a status code to
  decide retry eligibility; a future builder goes through `_unavailable` or gets
  the conservative `False`.
- **The log lines.** `_unavailable` now always carries the additive `transient`
  boolean; each retry emits `gemini_transient_retry` (WARNING) with `stage`,
  `attempt`, `delay`, `error_code`, `chat_id` and `update_id` before sleeping.
  The failed first attempt still emits `gemini_call_failed` (ERROR). No
  `str(exc)` is rendered on any new path (the standing rule).
- **Deliberate non-goals (YAGNI).** No jitter, no second exponential term, no
  circuit breaker, no retry queue, and no change to the user-visible D6 text or
  `RATE_LIMITED`.
- **Merge-gate status.** Code, tests and documentation are complete and the
  suite is green. Two merge criteria are *pending by design* at verification
  time because the work is deliberately left uncommitted and unpushed:
  `git status --porcelain` is non-empty (three modified files plus this untracked
  spec folder) and `scripts/hooks` found nothing staged. They are discharged
  when the orchestrator commits on the feature branch.

## Merge criteria

The feature is ready when **all** hold:

- `./scripts/test` and `./scripts/hooks` exit 0 with the new tests present.
- Every A-row above has a named passing test.
- The step tests (`-k not_retried`) are unchanged and green — the careful
  carve-out holds.
- The documents agree with the code (resilience R1.3 cross-referenced,
  `TECH.md` and `ROADMAP.md` updated).
- The working tree is clean and the branch is pushed.
- The user has confirmed the walkthrough can proceed with the new build.