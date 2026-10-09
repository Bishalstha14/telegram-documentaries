# Phase 7 — Resilience

Hardening across the whole pipeline. This phase adds no capability to the
documentary: it makes the capability that already exists survive a hostile API
and refuse to fail quietly.

## Context

`ROADMAP.md` names four acceptance criteria for Phase 7. Three freedoms and one
gap are the whole of the work:

| Criterion | State on entry |
|---|---|
| `/start` and `/restart` both purge state and temp files | Met (text slice) |
| Wrong-payload-at-wrong-stage guards on every state | Present in `state.py` + `pipeline.py`; unproven cell by cell |
| API timeout and rate-limit fallbacks | **Timeout met** (per-call budgets, `fb2b675`); **rate-limit missing** |
| No `except: pass`; no un-logged failure path | Holds today by inspection; no guard keeps it holding |

The gap is concrete and was written down in the code before this spec existed.
`scripter.py` says, of a transport failure: *"Not retried here — transport
failures are Phase 7's concern, and retrying a timeout inside one handler would
hold the chat open for nothing."* This phase is where that concern is discharged.

A `429` is already classified as environmental (`GeminiUnavailableError`), and
already degrades gracefully — but to the wrong line. The hub answers every
environmental failure with `GENERIC_FAILURE`:

> "Something went wrong on my side. Send /restart to begin again."

That is right for a broken turn and wrong for a throttle. A throttle is
*transient* and the user did nothing wrong: telling them to throw away a
half-finished interview is the worst available advice. Live evidence of `429` on
this project is real — every image-model probe in this session returned it.

## Scope

### R1 — Rate-limit fallback

- **R1.1** A `429` is classified as its own failure: `GeminiThrottledError`, a
  **subclass** of `GeminiUnavailableError`. Subclassing keeps every existing
  `except GeminiUnavailableError` / `except GeminiError` correct, including the
  narrator's degrade-to-text path; it only lets a caller that cares tell a
  throttle apart from an outage.
- **R1.2** A `429` is retried at the **shared transport seam**
  (`_call_transport`), so `generate` and `synthesize` inherit it once. At most
  `_THROTTLE_MAX_ATTEMPTS` attempts in total, with exponential backoff of
  `_THROTTLE_BASE_DELAY_SECONDS * 2**attempt`.
- **R1.3** **Only `429` is retried.** A timeout, a transport failure, a 5xx and a
  rejected reply are classified and raised on the first attempt, exactly as
  today. Retrying a timeout would hold the chat open — the reason the scripter
  refused to do it.
- **R1.4** The backoff sleep is a module-level seam (`gemini._sleep`, default
  `asyncio.sleep`) so tests never wait. It is the only monkeypatched symbol in
  the phase.
- **R1.5** When the attempts are exhausted the `GeminiThrottledError` propagates.
- **R1.6** The hub maps `GeminiThrottledError` to a dedicated `RATE_LIMITED`
  line that asks the user to try again shortly. It **never** says `/restart` and
  is never the generic failure line.
- **R1.7** The session is untouched by that failure (the existing D3 rule: a
  failed call holds the state), so resending the same answer is consumed as the
  same answer to the same question. This is what makes "try again" true rather
  than polite.
- **R1.8** A throttled **voice delivery** still degrades to the narration text
  via the existing D6 path — a failed delivery is not a failed step — and does
  **not** produce the `RATE_LIMITED` line. The user keeps the whole interview
  and gains the text.
- **R1.9** Logging: `gemini_throttled_retry` (WARNING) before each retry, with
  the attempt number and delay; `gemini_throttled` (ERROR) when the attempts are
  spent; `rate_limited` (WARNING) when the hub answers with the line. No
  exception's `str()` is rendered anywhere (the standing R1.7 rule).

### R2 — Wrong-payload guards, proven for every cell

- **R2.1** Every (payload kind × phase) pair has a **defined** outcome: an
  answer, a nudge, a restart, or a typed rejection. No pair crashes, and no pair
  silently no-ops.
- **R2.2** Illegal transitions continue to raise `SessionTransitionError`
  explicitly (R4.3 of the text slice), never coerced.
- **R2.3** A parametrized matrix test asserts every cell of the table, so a
  future arm added to `InboundAttachment` or a new `Phase` fails the suite
  rather than the user.

### R3 — No silent failure path

- **R3.1** No bare `except:` in `src/`.
- **R3.2** No `except` whose entire body is `pass` or `...`.
- **R3.3** Both are enforced by an AST-walking test over `src/`, not by
  inspection — the rule must fail the build, not a review.

### R4 — Constitution and README sync

- **R4.1** `ROADMAP.md`: Phase 5 recorded as delivered (voice), Phase 6 recorded
  as delivered and live-verified, Phase 7 recorded as delivered; the status
  table and the "live smoke test pending" paragraph corrected; Phase 4 recorded
  as **blocked on image-generation quota**, not merely "not started".
- **R4.2** `TECH.md`: records the narrator divergences (D-V1 `Reply = str |
  VoiceNote`, D-V2 two-method `GeminiClient`), the `lameenc` encoder, the
  per-class timeout policy (text 20 s, synthesis 60 s) and the throttle retry.
- **R4.3** `README.md`: the module tree and the phase line reflect the code.
- **R4.4** The text-slice spec (`2026-10-05-text-vertical-slice/`) cross-refers
  to D-V1/D-V2 where it claimed `Reply = str` and a one-method client.

## Out of scope (YAGNI)

- **Phase 4 itself.** It is blocked on image-generation quota, not on code.
  Nothing here should be read as building it.
- **Retrying timeouts or 5xx.** R1.3 is deliberate. A retry on a 20-second call
  is a worse experience than an honest failure.
- **Jitter on the backoff.** One bot, one user, one key: a thundering herd is not
  a thing here, and jitter would make the retry untestable for no gain.
- **Locking `SessionStore`.** The store is single-threaded by design; revisiting
  it belongs to raising `max_concurrent_updates`, which nothing asks for.
- **A retry queue, persistence, circuit breakers, or metrics.** No current
  requirement needs them.

## Decisions

- **D1 — retry `429` only.** Chosen by the user. The transient, cheap-to-retry
  failure is the throttle; everything else fails once and degrades.
- **D2 — bounded: three attempts, 1 s then 2 s.** A throttle fails *fast*
  (measured: 0.1–0.6 s for every image probe), so the backoff dominates and the
  whole fallback costs at most ~3 s. Bounded because an unbounded retry is a
  hang.
- **D3 — classify at the transport, decide at the hub.** The transport knows a
  `429` when it sees one; only the hub knows that a throttle deserves different
  *words* than an outage. Keeping the two apart is what lets each be tested
  alone.
- **D4 — `GeminiThrottledError` subclasses `GeminiUnavailableError`.** A throttle
  *is* an environmental failure; it differs only in the advice. Subclassing
  keeps every existing catch correct while letting the hub specialise.
- **D5 — the sleep is a module-level seam, not a constructor argument.**
  Threading a sleep through two decorated call protocols would widen three
  signatures for a test convenience. One module attribute is the smaller,
  more general change.
- **D6 — the silent-except guard is a test, not new tooling.** An AST walk is
  twenty lines, needs no dependency and fails the build. A ruff plugin or a new
  `scripts/check-*` would be more machinery for the same guarantee.

## The category, and why the fix is general

- **Category fixed:** *a transient, self-healing API condition reported to the
  user as a fatal step failure.* The instance is `429`; the general rule is that
  an environmental failure which is cheap to retry must be retried before it is
  narrated, and must never be narrated as "start over".
- **Sibling swept (Horizon 1/2):** the whole `GeminiTransport` seam — one retry
  loop covers both `generate` and `synthesize`, which is the reason it lives
  there rather than beside either call. `narrator.py` and the scripter already
  route their failures through this seam, so they inherit the fallback with no
  change.
- **Prevention (mechanism-level):** the retry bound is a named constant with a
  test asserting the attempts and the delays; the throttle has its own error
  type, so a future *new* catch that forgets it fails to compile as intended;
  the `RATE_LIMITED` line is asserted never to contain the string `/restart`;
  and the silent-except rule is enforced by the suite.
