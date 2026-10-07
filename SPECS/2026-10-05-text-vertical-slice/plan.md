# Plan — Text vertical slice (Bouncer, Interviewer, Scripter)

Red/Green TDD throughout (TECH.md). Every task group writes its tests first, runs
them, and **watches them fail for the right reason** before any production code
is written. Checks are run through `scripts/test` and `scripts/hooks` only.

Branch: `feature/2026-10-05-text-vertical-slice`, cut from
`feature/2026-10-05-repository-and-gateway`. Nothing is committed by the
implementer; the spec is committed, the code is reviewed and verified, then a PR
is opened against `main`.

Rules that apply to every group:

- No `except: pass`, no bare `except`, no un-logged fallback (R9.5, TECH.md).
- No test may touch the network or a real credential (R10).
- New exception names carry the `Error` suffix; ruff `N818` stays enabled.
- The logging module is `observability.py`. Never add a `logging.py`.
- `scripts/test` is the ground truth and must be green at the end of every group
  that changes `src/`.

## Task group 1 — Branch and baseline

1. Create the branch off the current commit (`ead88e4`):
   `git switch -c feature/2026-10-05-text-vertical-slice`.
2. Run `scripts/test` and confirm the Phase 1 baseline is green before touching
   anything. Record the test count (107 at the time of writing) as the number
   every later group is measured against.
3. Create the empty test modules `test_gemini.py`, `test_state.py`,
   `test_media.py`, `test_bouncer.py`, `test_interviewer.py`, `test_scripter.py`,
   `test_pipeline.py`, so the layout is visible before the code exists.

## Task group 2 — `validation_error_fields` moves and `@logged` gains `extra`  (RED → GREEN)

Two small changes to Phase 1 modules that everything else depends on. Done
first so the Gemini boundary can reuse the leak guard (D7) and so stage records
carry `stage` (D8).

RED:

- `test_validation_error_fields_returns_names_only` — build a failing model with
  a sibling secret in the input, assert the helper returns field **names** and
  that the secret is not among them.
- `test_logged_merges_static_extra_into_every_record`
- `test_logged_static_extra_cannot_overwrite_a_reserved_record_attribute` — a
  static key named `message` or `levelname` must not corrupt the record.
- `test_contracts_no_longer_expose_settings_error_fields` and
  `test_config_exposes_validation_error_fields` — the move, asserted so the old
  name cannot creep back (no shim, per D7).

GREEN:

- Move `settings_error_fields` from `config.py` to `contracts.py`, renamed
  `validation_error_fields`, with its docstring generalised from `.env` keys to
  "any model's field names". Update `__main__.py` to import the new name.
  Keep the behaviour byte-identical; the Phase 1 R2.3 guard is not being
  weakened, and `test_settings_validation_error_text_never_contains_the_secret_value`
  in `test_config.py` must still pass unmodified.
- Add `extra: Mapping[str, object] | None = None` to
  `observability.logged(...)`, merged into every record alongside the computed
  `event` / `chat_id` / `update_id` / `duration_ms`, and filtered against
  `_RESERVED_RECORD_ATTRS` exactly as `extra=` already is.

## Task group 3 — The Gemini boundary  (RED → GREEN)

The highest-risk module in the phase. Every R1.4 rejection path gets its own
test, driven through `GeminiClient.generate` with a stubbed `generate_content`.

RED — in `test_gemini.py`:

- `test_generate_returns_the_typed_model_when_the_reply_validates`
- `test_generate_requests_json_with_the_stage_schema` — assert the captured
  `GenerateContentConfig` sets `response_mime_type="application/json"` and the
  caller's `response_schema`. **This is the schema-first guard (R1.3).**
- `test_generate_sends_the_image_bytes_as_inline_data` — the Bouncer path.
- `test_generate_raises_when_there_are_no_candidates`
- `test_generate_raises_when_finish_reason_is_max_tokens` — the truncated-reply
  guard. **It must not attempt to parse the partial JSON.**
- `test_generate_raises_when_a_part_is_not_text`
- `test_generate_raises_when_the_text_is_not_json`
- `test_generate_raises_field_names_when_the_json_fails_the_schema` — assert the
  error names the offending field and that the reply body is **not** in the
  message.
- `test_generate_raises_when_the_schema_error_has_no_locatable_field`
- `test_generate_raises_unavailable_on_timeout`
- `test_generate_raises_unavailable_on_server_error`
- `test_generate_raises_unavailable_on_rate_limit` — 429.
- `test_generate_raises_unavailable_on_transport_error` — `httpx.HTTPError`.
- `test_gemini_failures_never_log_the_api_key` — sweep every record.
- `test_gemini_failures_never_log_the_response_body` — the `APIError.details`
  guard (R1.7). Build a `ServerError` whose details contain a sentinel string and
  assert the sentinel appears in no record and in no exception message.
- `test_generate_applies_the_timeout_to_the_client` — assert
  `HttpOptions.timeout == GEMINI_TIMEOUT_MS` on the constructed client (R1.5).
- `test_the_model_constant_is_the_verified_model_id` — guards D1/R1.8.
- `test_gemini_client_is_a_protocol` — assert `isinstance(GenAiGeminiClient(...),
  GeminiClient)` via a `runtime_checkable` protocol, so a future signature change
  to the interface without the implementation is a test failure.

GREEN — implement `gemini.py`: `Stage`, `GeminiRequest`, the `runtime_checkable`
`GeminiClient` protocol, `GenAiGeminiClient`, `MODEL_ID`, `GEMINI_TIMEOUT_MS`,
and the three exception classes. Parse in the order given by R1.4 and raise
`GeminiResponseError` / `GeminiUnavailableError` accordingly.

## Task group 4 — Session state and the state machine  (RED → GREEN)

RED — in `test_state.py`:

- `test_a_new_chat_loads_a_fresh_awaiting_photo_state`
- `test_save_then_load_round_trips_the_state`
- `test_purge_removes_the_chat_and_leaves_others_untouched` — **the isolation
  guard.**
- `test_a_mismatched_version_is_discarded_rather_than_loaded` — save a state,
  mutate its `version`, assert `SessionVersionError` and that the hub's fallback
  is a fresh state (R3.3).
- `test_session_state_is_frozen`
- `test_begin_interview_moves_to_awaiting_answer`
- `test_record_answer_appends_the_asked_question_and_the_answer`
- `test_record_answer_rejects_an_answer_to_a_different_question` — R3.5.
- `test_record_answer_rejects_the_final_answer_beyond_the_plan` — a 6th answer
  against a 5-question plan.
- `test_complete_interview_moves_to_scripted_and_stores_the_script`
- `test_an_illegal_transition_raises_session_transition_error` — assert
  `from_phase`, `to_phase` and `event` are all reported, and that nothing was
  mutated.
- `test_begin_interview_from_scripted_is_illegal` — `SCRIPTED` is terminal.
- `test_every_legal_transition_in_the_table_is_accepted` — drive the declared
  table so a row cannot be listed and left unimplemented.
- `test_every_transition_not_in_the_table_is_rejected` — the table is exhaustive,
  not advisory.

GREEN — implement `state.py` per R3 and R4. Transitions are functions returning
new frozen states, not field assignments.

## Task group 5 — Temporary media  (RED → GREEN)

RED — in `test_media.py`, using `tmp_path` as the base:

- `test_save_photo_writes_the_bytes_and_returns_a_stored_photo`
- `test_save_photo_rejects_empty_bytes`
- `test_save_photo_rejects_a_declared_size_over_the_cap_before_writing` — assert
  nothing was written (R5.2).
- `test_purge_deletes_the_chat_directory_including_the_photo`
- `test_purge_on_a_chat_with_no_directory_logs_at_debug_and_does_not_raise`
- `test_purge_failure_is_logged_at_exception_level_and_re_raised` — R5.3's
  fail-loud rule, simulated by making the directory undeletable.
- `test_two_chats_purge_independently` — the isolation guard at the media layer.
- `test_the_session_directory_path_contains_the_integer_chat_id` — the path
  segment is `str(StrictInt)`, never attacker-shaped (R5.1).

GREEN — implement `media.py`.

## Task group 6 — Bouncer  (RED → GREEN)

RED — in `test_bouncer.py`:

- `test_judge_returns_a_typed_verdict_and_never_a_parsed_string` — the roadmap's
  criterion, asserted as a `Verdict` instance.
- `test_judge_passes_the_photo_bytes_to_gemini`
- `test_judge_includes_the_model_subject_in_the_verdict`
- `test_a_human_verdict_is_accepted`
- `test_an_unsure_verdict_is_accepted_and_logged_as_bouncer_unsure` — D6.
- `test_a_non_human_verdict_carries_the_models_cheeky_line` — R6.2.
- `test_a_blank_or_oversized_line_falls_back_to_the_local_rejection` — R6.2's
  guarantee.
- `test_a_non_human_verdict_leaves_the_session_untouched` — the stage itself
  does not reset; the hub does (R6.4). Assert the stage is pure apart from the
  Gemini call.
- `test_a_gemini_unavailable_error_leaves_the_session_untouched` — R6.5.
- `test_a_gemini_response_error_is_logged_loudly_and_not_swallowed` — R6.5.

GREEN — implement `bouncer.py`: the system instruction, the `BouncerVerdict`
schema with its length bounds, and `judge()`.

## Task group 7 — Interviewer  (RED → GREEN)

RED — in `test_interviewer.py`:

- `test_plan_returns_a_typed_plan_with_questions_and_an_animal`
- `test_plan_rejects_a_count_under_five` and `test_plan_rejects_a_count_over_seven`
  — the schema-level 5–7 rule (R7.2).
- `test_plan_rejects_a_blank_question` and `test_plan_rejects_an_over_long_question`
  — the 280-character bound.
- `test_plan_rejects_a_blank_suggested_animal`
- `test_plan_passes_the_bouncer_subject_into_the_prompt` — R7.1.
- `test_next_question_returns_the_first_question_then_advances` — R7.3.
- `test_next_question_returns_none_once_the_plan_is_exhausted`
- `test_asking_a_question_makes_no_gemini_call` — **the call-count guard.** Assert
  the fake client recorded exactly one call for a whole interview's worth of
  questions. This is what makes D2's cost claim true rather than aspirational.
- `test_an_empty_answer_does_not_advance_the_plan` — R7.5.

GREEN — implement `interviewer.py`: the `InterviewPlan` / `Question` schemas,
`plan()`, and the local question cursor.

## Task group 8 — Scripter  (RED → GREEN)

RED — in `test_scripter.py`:

- `test_write_returns_a_script_with_a_locally_counted_word_count` — R8.1: assert
  the count is computed from the text, not read from the model.
- `test_write_accepts_exactly_sixty_words` and
  `test_write_accepts_exactly_ninety_words` — both boundaries inclusive.
- `test_write_rejects_fifty_nine_words`
- `test_write_rejects_ninety_one_words`
- `test_write_rejects_empty_or_whitespace_output`
- `test_write_rejects_text_longer_than_the_telegram_message_limit`
- `test_write_retries_once_and_reports_the_actual_count` — the corrective retry
  (D5/R8.3); assert the second prompt carries the count that was rejected.
- `test_write_gives_up_after_one_retry_and_logs_script_rejected_at_exception_level`
- `test_write_never_pads_truncates_or_falls_back_to_local_text` — **the no-coercion
  guard.** Assert that after two failures the bot has produced no script at all.
- `test_write_passes_the_plan_and_the_answers_to_gemini` — R7.4/R8.2, including
  the suggested animal.
- `test_a_gemini_unavailable_error_is_not_retried` — transport failures are not
  this phase's business (Phase 7).

GREEN — implement `scripter.py`: the `Script` schema, the local word count, the
single corrective retry, and `ScriptRejectedError`.

## Task group 9 — The hub and the rewired adapter  (RED → GREEN)

The largest group. The decision table (R9.1) is the specification, and every row
gets a test.

First, the **explicit Phase 1 update**, because these are existing tests and
existing call sites:

- `test_bot.py`: every `bot.build_application(FAKE_BOT_TOKEN)` becomes
  `build_application(FAKE_BOT_TOKEN, pipeline)` with a fake or real `ConversationPipeline`.
- `test_bot.py`: the Phase 1 `/start` assertions
  (`test_start_command_sends_the_greeting_to_the_integer_chat_id`,
  `test_start_reply_text_is_the_documented_greeting`) are rewritten against the
  hub's new `/start` text. The integer-`chat_id` assertion **survives unchanged**
  — it is still load-bearing.
- `test_main.py`: the wiring tests gain the pipeline construction step, and
  `test_main_names_only_the_missing_field_in_its_message` must still pass
  unmodified after the D7 move.
- `test_bot.py`: `test_the_greeting_constant_carries_no_credential` moves with the
  constant, retargeted at the hub's `/start` text and the rejection fallback, so
  neither string can become a place to hide a token.
- Delete the `GREETING` constant and its test. Do not leave a shim (D10).

Then the new tests — RED, in `test_pipeline.py`:

- `test_start_purges_state_and_media_and_asks_for_a_photo` — the `/start` row.
- `test_restart_purges_state_and_media_mid_interview` — the roadmap's
  "`/restart` mid-interview, no process restart" criterion.
- `test_text_before_a_photo_asks_for_a_photo_without_calling_gemini` — the
  out-of-order row.
- `test_a_sticker_before_a_photo_is_refused_by_kind` — the unsupported row;
  assert the reply names the kind.
- `test_an_accepted_photo_starts_the_interview_with_the_first_question`
- `test_a_rejected_photo_resets_the_session_and_purges_the_media` — Phase 2's
  "playful rejection + state reset", with the media assertion.
- `test_each_answer_sends_exactly_one_message` — **the one-at-a-time guard.**
  Count `FakeTelegramBot.sent` after three answers: exactly three replies, each
  the next question.
- `test_the_interview_completes_after_five_to_seven_answers_and_sends_the_script`
  — the happy path, end to end, asserting the narration text is the last message
  sent to the same integer `chat_id`.
- `test_a_photo_mid_interview_starts_a_fresh_interview` — D4.
- `test_text_after_the_script_nudges_towards_a_new_photo` — the `SCRIPTED` row.
- `test_a_gemini_failure_mid_interview_preserves_the_state_and_re_asks` — **D3,
  the most important degradation test.** Fail the call on the third answer, assert
  the state is byte-identical to before except for the reply, and that the next
  message is recorded as the answer to the *same* question with nothing skipped.
- `test_a_gemini_timeout_at_each_of_the_three_call_sites_never_raises` — bouncer,
  planner, scripter.
- `test_a_rejected_script_twice_degrades_to_a_restart_prompt` — D5 at the hub
  level.
- `test_two_chats_interleaved_never_see_each_others_answers_or_script` — **the
  isolation rubric item.** Drive both chats through a full interview with
  interleaved updates and assert no message and no state field crosses over.
- `test_every_illegal_transition_is_answered_not_raised` — R4.3 through R9.5.
- `test_every_send_uses_an_integer_chat_id`
- `test_no_log_record_anywhere_contains_the_bot_token` — the Phase 1 sweep,
  re-run across the whole vertical slice.
- `test_no_log_record_anywhere_contains_the_gemini_api_key` — its sibling (R1.7).

And in `test_bot.py`, for the adapter:

- `test_three_handlers_are_registered_plus_one_error_handler` — `/start`,
  `/restart`, the catch-all message handler, `on_error`.
- `test_the_message_handler_serves_text_photos_and_other_media` — one handler,
  one decision table (R9.3).
- `test_a_malformed_inbound_update_logs_a_warning_and_sends_nothing` — the Phase 1
  contract still holds.
- `test_handler_failure_is_logged_at_exception_level_with_context` — unchanged.
- `test_a_telegram_send_failure_never_leaks_into_the_session` — a send failure is
  logged, and the state is not advanced on a reply that was never delivered.

GREEN — implement `pipeline.py` (`PhotoFetcher`, `ConversationPipeline`, the three
entry points, the decision table) and rewrite `bot.py` (the three thin handlers,
`TelegramPhotoFetcher`, `build_application(token, pipeline)`), then update
`__main__.py` to construct and inject the pipeline.

## Task group 10 — Documentation sync

Per TECH.md's README policy, in the same change as the behaviour.

1. `README.md` "Use": describe the real flow end to end, and state plainly that
   the hybrid image and the voice note are later phases.
2. `README.md` "Status": Phases 2, 3 and text-5 shipped; 4 and 6 outstanding.
3. `ROADMAP.md`: mark Phase 2 and Phase 3 delivered with details; mark Phase 5
   delivered **for the text half only** and say what remains; update the Status
   table.
4. `TECH.md`: record the `google.genai` divergence (D1) and the `PhotoFetcher`
   port, so the next phase does not rediscover them.
5. `MISSION.md`: confirm it needs no change — product scope is unchanged.
6. Update this spec folder if the implementation diverged, with the deviation and
   its reason.

## Task group 11 — Final gates

1. `scripts/test` — full output, exit `0`.
2. `scripts/hooks` — exit `0` with staged files and with none.
3. `grep` proving zero `except: pass` and zero bare `except` in `src/`.
4. `grep` proving no `google.genai` import outside `gemini.py` — the boundary
   holds.
5. `grep` proving no reference anywhere in `src/` to image generation, TTS,
   audio, `.ogg`, `.mp3`, `send_photo`, or the two out-of-scope model ids —
   YAGNI, verified rather than asserted.
6. `git status` showing only intended files, and no secret.
7. Confirm `.env` is still ignored.
