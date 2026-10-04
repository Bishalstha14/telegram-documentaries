---
name: write-tests
description: Guide for writing tests in this repository. The spec-implementer MUST invoke this skill every time it writes tests. Covers Red/Green TDD, happy-path and edge-case coverage, and behaviour-first testing.
---

# Write Tests

This skill is the mandatory guide for writing tests in The Telegram
Documentaries codebase. **Invoke it every time you write, extend, or modify
tests.** It applies to the tests written for any feature, bug fix, or
refactor, in every phase of the Red/Green TDD loop.

This is a **Red/Green TDD** repository (see `SPECS/TECH.md` and the
`feature-specification` skill). Tests are written **before** the code that makes
them pass, and no feature is considered done without a passing test suite.

---

## 1. Red/Green TDD — the loop

Follow this order strictly. Do not skip ahead.

1. **RED — write a failing test first.** Write a test that expresses the desired
   behaviour. Run it and confirm it **fails for the right reason** (the behaviour
   does not exist yet), not because of a typo, an import error, or a broken
   fixture. The failure should be about the missing/incorrect behaviour.
2. **GREEN — make it pass.** Write the smallest amount of code needed to make the
   test pass. No speculative features, no premature abstraction (YAGNI).
3. **REFACTOR — clean up.** Improve the code you just wrote and its tests while
   keeping the suite green. Run the full checks afterwards.

Rules:

- Never write the implementation before the test for it. If you find yourself
  reaching for production code first, stop and write the test.
- A test whose RED state was never demonstrated is suspect — flag it.
- Run the tests via the dev scripts (consult the README): `scripts/test` to run
  the suite, `scripts/hooks` for the full pre-commit suite. Do not bypass them.

---

## 2. Cover the happy path

Every feature or fix must have a test proving the **primary, expected success
path** works. The happy-path test is what demonstrates the feature actually does
its job when everything behaves.

- The happy path must assert the **observable outcome**, not just that a function
  was called without error.
- Where the feature has an output contract (a returned value, a persisted
  document, a logged event, a sent message), assert on the real output.

---

## 3. Cover the edge cases

Happy-path coverage alone is not enough. For each piece of behaviour, add tests
for the **edge cases** and **failure modes**. Ask yourself: *what else could
happen, and is it handled correctly?*

Common edge cases to consider (choose the ones that apply to your code):

- **Empty inputs** — empty lists, empty strings, missing optional fields.
- **Boundary values** — minimum/maximum values, off-by-one thresholds, the exact
  value at a limit versus just past it.
- **Malformed / unexpected input** — wrong types, missing required fields,
  extra fields, non-ASCII or special characters (this project treats all
  external boundaries — Telegram updates, Gemini model/output payloads, TTS
  audio, uploaded media — as untrusted and arbitrary).
- **Errors and fallbacks** — what happens when a dependency fails, a retry is
  exhausted, a query returns nothing, or a fallback model is used. Assert the
  degraded behaviour, not just that "no exception propagates" (unless that is
  the contract).
- **Idempotency** — running the same operation twice produces the same result.
- **Isolation / scoping** — where data is per-user or per-bot (e.g. in-memory
  session state keyed by `chat_id`), assert one entity's data does **not**
  leak into another's.
- **State transitions** — first vs. later turns, counter increments/decrements,
  the Nth occurrence versus the (N+1)th.

For each edge case you identify, decide whether it is:
- **Covered** — add a test that asserts the correct behaviour, or
- **Explicitly out of scope** — note it, and why (it cannot occur, or YAGNI).

Do not leave edge cases unexamined.

---

## 4. Test behaviour, not implementation

Prefer tests that assert **what the code does** (its externally observable
behaviour and outcomes) over tests that assert **how it does it** (internal
implementation details). This keeps tests resilient to refactoring and honest
about what actually works.

Prefer asserting:

- The **return value / output** (the Pydantic model, the string, the list).
- The **side effect that matters** (a session-state field updated, a message
  sent via Telegram, a hybrid image or audio file rendered, a structured log
  record emitted).
- The **resulting state** (a counter's value, a session-state key's contents).
- The **failure behaviour** (an exception raised, a fallback delivered, a
  CRITICAL log fired).

Avoid asserting:

- **Internal call sequences** that are incidental — e.g. that a specific private
  method was called in a specific order, unless that ordering is the contract.
- **Tight coupling to a particular implementation** (a specific internal helper,
  a mock's internal wiring) where a behavioural assertion is possible.

That said, mocks and test doubles have a legitimate place — this repo uses them
for non-deterministic external calls (e.g. recorded LLM responses) and in
`tests/component/` for multi-dependency mock interaction tests. When you use a
mock, use it to make the **behaviour** observable and deterministic, not to
assert implementation trivia. Where a real dependency is cheap and deterministic
(the Google ADK runtime run in-process, or a real Telegram/Gemini call behind a
guarded env flag), prefer it over a mock so the test exercises real behaviour.

Respect the repository's test-directory philosophy (from `SPECS/ROADMAP.md`):

- `tests/unit/` — isolated deterministic logic, test doubles, no real I/O.
- `tests/integration/` — exercise at least one real external dependency (e.g. the
  Telegram Bot API or Gemini API, with credentials supplied via env vars).
- `tests/component/` — mock multiple dependencies, no real I/O (honestly labelled).

---

## 5. Naming and structure

- Name tests to describe the **behaviour** they verify, e.g.
  `test_get_session_by_chat_id_returns_only_that_chats_state`, not
  `test_repo_function`.
- Group related tests sensibly; a new behaviour gets a new test, not an overloaded
  "kitchen sink" test.
- Keep each test focused on one behaviour. If a test asserts many unrelated
  things, split it.
- Do **not** remove existing tests unless they are replaced by a better, more
  comprehensive test covering the same behaviour plus additional cases (per the
  repo's testing philosophy). Redundant tests are harmless; missing tests are
  dangerous.

---

## 6. Category lock-in (bug-fixing tests)

When you write tests for a bug fix, in addition to the reported instance, add
tests that lock in the **category** and the **prevention mechanism** (see the
`feature-specification` skill's "Root cause, category & prevention" guidance):

- At least one test per **sibling instance** found in the sweep.
- A test asserting the **prevention mechanism actually fires** (the fail-loud
  log, the rejected input, the static-hook failure).
- A **cross-chat / cross-user isolation** test when the bug is a data leak.

This ensures the class of bug cannot silently recur, not just the one instance.
