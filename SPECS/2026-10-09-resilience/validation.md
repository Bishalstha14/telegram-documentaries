# Phase 7 — Validation

How we know the hardening is real and the phase can be merged. Each item is
checkable by a command or by reading one named file — not by trusting a summary.

---

## A. Phase 7 acceptance criteria

| # | Criterion (`ROADMAP.md`) | How it is verified |
|---|---|---|
| A1 | `/start` and `/restart` both purge state and temp files | Already-met: existing tests + the code path `pipeline._command` → `sessions.purge` + `media.purge`. Re-asserted by the TG4 matrix. |
| A2 | Wrong-payload-at-wrong-stage guards on every state | TG4 matrix: all nine (payload × phase) cells assert a defined reply **and** its log event. |
| A3 | API **timeout** fallback | Existing per-class budgets (`fb2b675`): text 20 s, synthesis 60 s. Re-asserted by the TG2 "timeout is not retried" test. |
| A4 | API **rate-limit** fallback | TG1 (throttle is its own type) + TG2 (bounded retry) + TG3 (`RATE_LIMITED` line, no `/restart`). |
| A5 | No `except: pass`; no un-logged failure path | TG5 AST guard, plus its own self-test proving the guard fires. |

---

## B. Requirement coverage

| Req | Where it is proven |
|---|---|
| R1.1 throttle type | `test_gemini.py`: 429 → `GeminiThrottledError`; issubclass assertions |
| R1.2 bounded retry | `test_gemini.py`: two-429s-then-success, three-429s exhausted |
| R1.3 only 429 retried | `test_gemini.py`: 500 and timeout each call the transport **once** |
| R1.4 sleep seam | tests monkeypatch `gemini._sleep`; suite runtime unchanged |
| R1.5 exhaustion propagates | three-429s test asserts the raise |
| R1.6 no `/restart` | `test_pipeline.py`: `"/restart" not in RATE_LIMITED` |
| R1.7 state held | `test_pipeline.py`: pending question survives a throttle; resend answers it |
| R1.8 voice degrades to text | `test_pipeline.py`: throttled `synthesize` returns the narration text |
| R1.9 logging | asserted in TG1/TG2/TG3 tests: `gemini_throttled_retry`, `gemini_throttled`, `rate_limited` |
| R2.1/R2.2/R2.3 matrix | TG4 |
| R3.1/R3.2/R3.3 no silent excepts | TG5 guard + self-test |
| R4.1–R4.4 doc sync | TG6; checked by reading the four files against the status table |

---

## C. Constitution compliance

- **Never silence a failure (MISSION §4).** A throttle is logged at WARNING on
  each retry and ERROR on exhaustion, then answered with a real line — handled
  *and* logged, never skipped.
- **Fail loudly vs degrade (TECH "Logging & error policy").** A throttle on a
  *step* degrades gently for the user and logs loudly; a throttle on *delivery*
  degrades to text. Both are the policy's stated shape.
- **No raw `str(exc)` anywhere (standing rule).** Every new record carries the
  class name, the stage and the code — never the rendered exception.
- **Schemas over regexes (TECH).** No new text parsing is introduced.
- **Contracts at boundaries.** The retry adds no new boundary; it hardens an
  existing one.
- **YAGNI.** No new dependency, no config toggle, no circuit breaker.

---

## D. Evidence to collect

```bash
# the ground truth
./scripts/test                          # expect: > 580 passed, ruff clean, mypy clean

# the silent-except guard, alone
.venv/bin/python -m pytest tests/unit/test_no_silent_excepts.py -v

# the retry, alone — and prove it is fast (no real sleeping)
.venv/bin/python -m pytest tests/unit/test_gemini.py -k throttle -v

# the matrix, alone
.venv/bin/python -m pytest tests/unit/test_resilience_matrix.py -v

# the retry bound is a named, testable constant
grep -n "_THROTTLE_MAX_ATTEMPTS\|_THROTTLE_BASE_DELAY_SECONDS\|_sleep" \
  src/telegram_documentaries/gemini.py

# the line the user sees on a throttle
grep -n "RATE_LIMITED" src/telegram_documentaries/pipeline.py

# the working tree is clean and everything is pushed
git status --porcelain                  # expect: empty
git log --oneline origin/feature/2026-10-05-text-vertical-slice..HEAD   # expect: empty
```

---

## E. Forced-failure check (optional, no network)

The retry path is proven against a fake transport in unit tests, which is the
ground truth. A manual check is possible without spending quota by pointing at a
model that returns `429` (every image model does), but it is **not required**:
the classification is exercised deterministically by the fake.

If run, expect: `gemini_throttled_retry` (×2), then `gemini_throttled`, then the
`RATE_LIMITED` line — and the user's pending question still on screen.

---

## F. Spec-vs-reality reconciliation

At verification time, record any difference between what this spec said and what
was actually built, and update this file, `plan.md`, `requirements.md`, `TECH.md`
and `ROADMAP.md` to match reality — the same discipline applied to the text
slice and the narrator.

Pre-registered candidates to check:

1. **Whether the retry lives in `_call_transport` as planned, or had to move.**
   If a second SDK seam is found in TG2's sweep, the loop moves and this file
   records where and why.
2. **Whether the matrix found an unguarded cell.** If so, the fix is recorded
   here as an instance of the same category.
3. **Whether the silent-except guard found anything.** If it did, both the
   finding and the fix are recorded; if it found nothing, that is stated
   explicitly with the count of handlers inspected.
4. **Whether the `429` is 0.1–0.6 s as measured.** If any throttle is slow, the
   "three attempts cost ~3 s" claim in `requirements.md` D2 is corrected.

## Merge criteria

The phase is ready when **all** hold:

- `./scripts/test` and `./scripts/hooks` exit 0 with the new tests present.
- Every A-row above has a named passing test.
- No `except: pass` remains and the guard that enforces it is itself tested.
- The four documents agree with the code and with each other.
- The working tree is clean and the branch is pushed.
- The user has confirmed whether they want this merged, given Phase 4 remains
  blocked on quota (the branch still stacks on the unmerged text-slice branch).
