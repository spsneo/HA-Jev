# Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The conversation agent remembers the last turn of a conversation for five minutes, so "turn them off", "set it to 40%" and "and the living room?" act on what the last turn was about, in the same one Jev call.

**Architecture:** A new `memory.py` holds one `Turn` per conversation id. `conversation.py` reads it before the call, puts a short `last_turn` in the request state and asks two more yes/no questions only when a record exists. `interpret()` reads those answers and either targets the remembered devices (`refers_back`) or repeats the last action for a new target (`same_again`). `_act` runs the intent once per room or once per device through Home Assistant's intents, then writes the new record.

**Tech Stack:** Python 3.14, Home Assistant 2026.9 custom integration, jevclient 1.3.1, pytest with pytest-homeassistant-custom-component 0.13.365.

**Spec:** `docs/superpowers/specs/2026-10-18-follow-ups-design.md`

## Global Constraints

- AGENTS.md applies: run the full gate after each task (ruff check, ruff format --check, mypy --strict, pytest with `--cov-fail-under=96`, `config_flow.py` at 100%, mkdocs --strict). Use `.venv/bin/*`. Tests never spend tokens. No em dashes in docs. Sentence-case headings.
- One Jev call per sentence. A level said in words keeps its second call.
- The two new questions are sent only when a record exists for the conversation id.
- Window: `chat_session.CONVERSATION_TIMEOUT` (5 minutes) since the record was written. Memory is in memory only, never on disk.
- `FOLLOW_UP_FLOOR = 0.8`, unmeasured, said so beside the constant. With an English cue, the floor is the configured `min_confidence` (default 0.6) instead. A cue alone never acts.
- Both `refers_back` and `same_again` at or above their floors: use neither.
- A remembered device is filtered to the current snapshot (exposed devices only) before anything runs.
- The whole-house gate, the exposure rule, the "which device" question, locks and entrance covers, and the Sonnet fallback behave as today.
- New spoken sentences go in `_FALLBACK`, `strings.json` and all 13 `translations/*.json`, English text in each.
- `continue_conversation=True` only after a short status answer (any, all, which, how_many) that names at least one device, and after the "which device" question as today.
- Each guard test is broken once and watched to fail.

## Review Focus

- A capped status answer ("lamp 0, lamp 1, lamp 2 and 6 more are on") remembers all nine devices, not the three it named aloud. Test in Task 1.
- A follow-up that asks a question ("are they still on?") reads the remembered devices and answers about them only. Test in Task 4.
- A conversation with no id (the `conversation.process` action without one) neither crashes nor remembers across calls. Test in Task 4.
- A full-list answer over two rooms, then "turn them off", acts across both rooms. Test in Task 4.
- A follow-up after a turn that went to the fallback agent uses the record from before that turn. Test in Task 4.

---

### Task 1: What a status answer was about, and a spoken list

**Files:**
- Modify: `custom_components/jev/status.py`
- Test: `tests/test_status.py`

**Interfaces:**
- Produces: `about(shape: Shape, asked: Bucket | None, states: Sequence[State]) -> list[State]`, the devices a follow-up's "them" means, after `without_groups`. ANY, WHICH, HOW_MANY: the devices in the asked state. ALL: the exceptions when the answer is no, all devices when it is yes. LIST, ONE, or `asked` not in `ASKABLE`: every device. Unreachable devices are never in it. The full set, not capped.
- Produces: `spoken_list(names: Sequence[str], lines: Mapping[str, str] | None = None) -> str`, the join that `answer()` uses today ("a", "a and b", "a, b and c", "a, b, c and N more"). `answer()` calls it, behaviour unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_about_any_is_the_ones_on():
    states = [light("a", "on", "A"), light("b", "off", "B"), light("c", "unavailable", "C")]
    assert [s.entity_id for s in about(Shape.ANY, Bucket.ON, states)] == ["light.a"]

def test_about_all_no_is_the_exceptions():
    states = [window("w1", "off", "W1"), window("w2", "on", "W2")]
    assert [s.entity_id for s in about(Shape.ALL, Bucket.CLOSED, states)] == ["binary_sensor.w2"]

def test_about_all_yes_is_everything():
    states = [window("w1", "off", "W1"), window("w2", "off", "W2")]
    assert len(about(Shape.ALL, Bucket.CLOSED, states)) == 2

def test_about_is_not_capped():
    states = [light(f"l{i}", "on", f"Lamp {i}") for i in range(9)]
    assert len(about(Shape.WHICH, Bucket.ON, states)) == 9

def test_about_a_list_is_every_device():
    states = [light("a", "on", "A"), light("b", "off", "B")]
    assert len(about(Shape.LIST, None, states)) == 2

def test_spoken_list():
    assert spoken_list(["a"]) == "a"
    assert spoken_list(["a", "b", "c"]) == "a, b and c"
    assert spoken_list([str(i) for i in range(6)]) == "0, 1, 2 and 3 more"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_status.py --no-cov -q`. Expected: ImportError on `about`.
- [ ] **Step 3: Implement both in `status.py`**; make `answer()` use `spoken_list` and share the yes/no split with `about` so the two cannot disagree.
- [ ] **Step 4: Run** the same command. Expected: all pass, `status.py` still 100% covered.
- [ ] **Step 5: Break once:** make `about` return the first `MAX_NAMES - 1` devices; `test_about_is_not_capped` fails. Restore.
- [ ] **Step 6: Full gate, then commit** `status.py` and `tests/test_status.py`: "A status answer says which devices it was about".

### Task 2: The memory

**Files:**
- Create: `custom_components/jev/memory.py`
- Test: `tests/test_memory.py`
- Modify: `quality_scale.yaml` (modules 27 → 28, test count)

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class Turn:
    text: str
    action: str                      # interpret action, e.g. "turn_off", "get_state"
    intent_type: str
    domains: tuple[str, ...]
    where: tuple[str, ...]           # room or floor names in scope; () for a device or the house
    everything: bool                 # the target was the whole house
    devices: tuple[str, ...]         # entity ids the turn was about
    names: tuple[str, ...]           # their names, same order
    shape: str | None = None
    asked_state: str | None = None
    at: datetime = field(default_factory=dt_util.utcnow)

    def describe(self) -> dict[str, Any]:
        """The last_turn entry of the request state: said, action, about (names), where."""

class Memory:
    def __init__(self, window: timedelta = CONVERSATION_TIMEOUT) -> None: ...
    def get(self, conversation_id: str | None) -> Turn | None: ...   # None id -> None; drops expired
    def remember(self, conversation_id: str | None, turn: Turn) -> None: ...  # None id -> no-op; replaces
```

- `describe()` omits empty fields. `about` lists at most 20 names; past that it adds `"and_more": N` (unmeasured cap, said so in a comment, to keep a house-wide record from growing the request).

- [ ] **Step 1: Write the failing tests** in `tests/test_memory.py`: `test_a_turn_is_kept_for_its_conversation`, `test_another_conversation_sees_nothing`, `test_a_turn_expires_after_the_window` (use `freezegun` via `homeassistant.util.dt` patch or pass `at` 6 minutes old), `test_a_new_turn_replaces_the_old`, `test_no_conversation_id_remembers_nothing`, `test_describe_names_the_devices` (asserts `{"said": "which lights are on", "action": "get_state", "about": ["Shelf", "Study table"], "where": ["Kids Room"]}`), `test_describe_caps_a_long_list` (25 devices → 20 names, `"and_more": 5`).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_memory.py --no-cov -q`. Expected: ImportError.
- [ ] **Step 3: Implement `memory.py`.**
- [ ] **Step 4: Run** again. Expected: pass, 100% of `memory.py`.
- [ ] **Step 5: Break once:** skip the expiry check in `get`; the expiry test fails. Restore.
- [ ] **Step 6: Update `quality_scale.yaml`** (27 → 28 modules, new test count). Full gate. Commit: "A memory of the last turn, per conversation, for five minutes".

### Task 3: Reading a follow-up

**Files:**
- Modify: `custom_components/jev/interpret.py`
- Test: `tests/test_follow_up_interpret.py` (calls `build_questions` and `interpret` directly with `build_response` from `tests/conftest.py` and a hand-built `HomeSnapshot`; no Home Assistant needed)

**Interfaces:**
- Consumes: `Turn` from Task 2.
- Produces:
  - `build_questions(text, snapshot, max_options, *, follow_up: bool = False)`: with `follow_up`, adds `refers_back` and `same_again` as `Noul`s. `refers_back`: "Does the command point back to the devices of the last turn, with words such as them, it or those, rather than naming its own?" `same_again`: "Does the command ask for the same thing as the last turn, for a different room or device?" Wording is the implementer's to tune.
  - `FOLLOW_UP_FLOOR = 0.8` (comment: not measured, same as SHAPE_FLOOR).
  - `refers_back_cue(text: str) -> bool` (English: `\b(it|them|those|they|that one|these|both)\b`), `same_again_cue(text: str) -> bool` (English: starts with "and the", "and in the", "and for the", "same for", "what about the").
  - `interpret(..., last: Turn | None = None)`.
  - New `Interpretation` fields: `follow_up: str | None = None` (`"refers_back"` or `"same_again"`), `remembered: list[str]` (entity ids used), `each: list[dict[str, Any]]` (one slot set per remembered device, empty otherwise), `already_entity: str | None = None` (the entity id behind `already_satisfied`).
- Rules:
  - A follow-up is read only when `last` is given. Each answer counts at `FOLLOW_UP_FLOOR`, or at `min_confidence` when its cue matches. Both counting: neither is used.
  - `refers_back`: the action is this sentence's action answer. The target is `last.devices` filtered by `snapshot.by_id`; none left means no follow-up. One device: the same slots a named device gets today (`name`, `domain`, `preferred_area_id`), so every existing rule (brightness, already done) applies. Several: `slots["domain"]` is the devices' domains, `each` holds one slot set per device. `targets_everything` is `last.everything`.
  - `same_again`: the action, `shape` and `asked_state` are taken from `last`, and the domain answer becomes `last.domains` when Jev's own is not sure. The target comes from this sentence's answers, read as today. No sure target: goes on as today.
  - `_already_done` also returns the entity id, into `already_entity`.

- [ ] **Step 1: Write the failing tests** (fixtures: a snapshot with `light.shelf`, `light.study_table`, `light.ceiling` in Kids Room, `light.lamp` in Living Room; a `Turn` about shelf and study table):
  - `test_no_record_asks_nothing_new`: `build_questions(...)` has neither key.
  - `test_a_record_adds_the_two_questions`: `follow_up=True` has both.
  - `test_them_targets_the_remembered_devices`: action turn_off 0.95, refers_back 0.9 → `intent_type == "HassTurnOff"`, `follow_up == "refers_back"`, `[s["name"]["value"] for s in each] == ["Shelf", "Study table"]`.
  - `test_it_with_one_device_is_a_named_device`: Turn about shelf only, "set it to 40%", set_brightness → `slots["name"]["value"] == "Shelf"`, `slots["brightness"]["value"] == 40`, `each == []`.
  - `test_and_the_living_room_repeats_the_last_question`: Turn get_state any/on in Kids Room; this sentence area Living Room 0.9, same_again 0.9, action answer turn_on 0.4 → `intent_type == "HassGetState"`, `slots["area"]["value"] == "Living Room"`, `shape == "any"`, `asked_state == "on"`.
  - `test_a_cue_lifts_an_answer_under_the_floor`: refers_back 0.7, text "turn them off" → follow-up used; text "turn the shelf off" with 0.7 → not used.
  - `test_both_read_is_neither`: both 0.9 → `follow_up is None`.
  - `test_a_remembered_device_no_longer_exposed_is_dropped`: Turn includes `light.gone` → `each` names only the two present.
  - `test_them_after_a_house_wide_turn_targets_everything`: Turn `everything=True` → `targets_everything is True`.
  - `test_already_done_names_its_entity`: existing already-on case → `already_entity == "light.shelf"`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_follow_up_interpret.py --no-cov -q`. Expected: failures on the new parameters.
- [ ] **Step 3: Implement** in `interpret.py`. Read the follow-up right after the action checks, before the target branches, so a follow-up skips the target questions and a same_again re-enters them with the replaced answers (`dataclasses.replace(response, answers=...)`).
- [ ] **Step 4: Run** again, then the whole suite: existing tests pass unchanged (no `last` given anywhere yet).
- [ ] **Step 5: Break once each:** (a) drop the "both read is neither" rule; (b) drop the snapshot filter; (c) remove the cue lift. Each makes its test fail. Restore.
- [ ] **Step 6: Measure** with `payload_bytes`: a 20-entity snapshot with and without `follow_up=True` and a two-device `last_turn`. Note the bytes for Task 5.
- [ ] **Step 7: Full gate, update test count, commit:** "A follow-up is read from the same one Jev call".

### Task 4: The agent remembers and acts on follow-ups

**Files:**
- Modify: `custom_components/jev/conversation.py`, `custom_components/jev/strings.json`, `custom_components/jev/translations/*.json` (13)
- Test: `tests/test_follow_ups.py` (end to end through `conversation.async_converse`, reusing the fixture pattern of `tests/test_status_conversation.py`: Kids Room shelf/study table/ceiling/night lamp, Living Room lamp, Kitchen light, Study Room curtains, all exposed except one hidden light; a helper `turn(hass, text, conversation_id=None) -> ConversationResult`)

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces in `conversation.py`:
  - `JevConversationEntity._memory: Memory`, created in `__init__`.
  - Before the call: `last = self._memory.get(chat_log.conversation_id)`; `build_questions(..., follow_up=last is not None)`; state gets `"last_turn": last.describe()` when there is one; `interpret(..., last=last)`.
  - `_read_rooms` becomes `_run(user_input, decision, text) -> tuple[ha_intent.IntentResponse, list[State]]`: one run per room (`decision.rooms`), per device (`decision.each`), or once. Reads skip a run that fails to match, as today. Actions skip a device whose intent raises `MatchFailedError` or `IntentHandleError` after another succeeded; the second element is the states acted on. No run succeeding raises as today.
  - Several devices acted on: the reply is the agent's own line, by action: `many_turned_on` "Turned on {names}.", `many_turned_off` "Turned off {names}.", `many_toggled` "Toggled {names}.", `many_opening` "Opening {names}.", `many_closing` "Closing {names}.", `many_brightness` "Set {names} to {level}%.". Covers use opening/closing. `{names}` is `status.spoken_list` of `status.short_name` with the rooms in scope. Each key goes in `_FALLBACK`, `strings.json` and the 13 translations, English text.
  - After a turn that ran an intent or answered already-on/off: `self._memory.remember(chat_log.conversation_id, Turn(...))`. `devices`: the status short answer's `about(...)`; else for a read, every matched state; for an action, the states acted on; for already-done, `already_entity`. Fallbacks, refusals and the "which device" question write nothing.
  - `_status_answer` returns `tuple[str, list[State]] | None` (the sentence and `about`). When it returned an answer with at least one device, the `ConversationResult` has `continue_conversation=True`.
  - `_reasoning` adds `Follow-up: refers back to A, B` or `Follow-up: same again as "<last text>"` when `trace["follow_up"]` is set.
  - Every `ConversationResult` the agent returns carries `chat_log.conversation_id` where it now carries `user_input.conversation_id`, so a client can continue the conversation.

- [ ] **Step 1: Write the failing tests** (each uses one conversation id across its turns unless it says otherwise):
  - `test_turn_them_off_after_which_lights_are_on`: turn 1 which/on Kids Room; turn 2 refers_back 0.9, action turn_off → `light.turn_off` called for exactly `{"light.kids_shelf", "light.kids_study_table"}`; reply `"Turned off shelf and study table."`.
  - `test_turn_it_off_after_turn_it_on` and `test_set_it_to_40_after_turn_it_on`: Kitchen light; second turn acts on `light.kitchen` only; brightness_pct 40.
  - `test_and_the_living_room`: turn 1 any/on Kids Room; turn 2 same_again 0.9, area Living Room → `"Yes, one: lamp."`.
  - `test_and_the_office_after_turn_off_the_kitchen` (area action repeat).
  - `test_open_it_after_which_curtains_are_closed` → `cover.open_cover` for the sheer curtain only.
  - `test_an_unrelated_question_does_not_use_memory`: both follow-up answers 0.1 → behaves as a first turn.
  - `test_after_five_minutes_there_is_no_memory` (move time with `async_fire_time_changed`/`freezer` 6 minutes) → the request has no `refers_back` key and the reply is the fallback.
  - `test_another_conversation_has_no_memory`.
  - `test_a_device_unexposed_in_between_is_not_acted_on`: unexpose study table after turn 1 → only shelf turned off.
  - `test_both_read_behaves_as_today`.
  - `test_a_fallback_turn_keeps_the_record` and `test_a_which_device_turn_keeps_the_record`.
  - `test_already_on_then_turn_it_off`.
  - `test_a_follow_up_is_one_jev_call` (count asks excluding setup's probe, as `test_status_conversation.py` does).
  - `test_the_whole_house_gate_still_asks_before_turn_them_on` after a house-wide which/on.
  - `test_a_status_answer_keeps_the_microphone_open`; `test_an_action_does_not`; `test_a_full_list_does_not`; `test_a_status_answer_naming_nothing_does_not` ("None of the curtains are closed.").
  - Review Focus: `test_are_they_still_on_reads_the_remembered_devices`; `test_no_conversation_id_neither_crashes_nor_remembers` (call the agent's handler with `conversation_id=None` twice through `conversation.async_converse` and assert the second gets the fallback); `test_them_after_a_two_room_list_acts_in_both_rooms`; `test_a_fallback_between_keeps_the_earlier_record`.
  - In `tests/test_conversation.py`: `test_the_assist_dialog_shows_a_follow_up` asserting the `Follow-up: refers back to` line.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_follow_ups.py --no-cov -q`. Expected: the follow-up tests fail; the "does not use memory" ones may already pass.
- [ ] **Step 3: Implement** in `conversation.py`; add the six `many_*` keys to `_FALLBACK`, `strings.json` and all 13 translations with a script that keeps each file's 2-space JSON layout unchanged.
- [ ] **Step 4: Run** the new file, then the whole suite. Expected: all pass; `tests/test_translations.py` passes (keys and placeholders in all 13 files).
- [ ] **Step 5: Break once each:** (a) write a record on a fallback turn; (b) remove the per-conversation key (use one global record); (c) set `continue_conversation` after every reply; (d) remove the snapshot filter's effect by acting on `last.devices` directly. Each makes at least one test fail. Restore.
- [ ] **Step 6: Full gate, update test count, commit:** "\"Turn them off\" acts on what the last answer was about".

### Task 5: Docs and measurements

**Files:**
- Modify: `site-docs/conversation.md` (new section "Follow-ups" after "Status questions": the table of examples from the spec, the five-minute window per satellite, the open microphone after a status answer, what stays as today, and that "Prefer handling commands locally" must be off for follow-ups after simple commands), `site-docs/troubleshooting.md` (rows: "'Turn it off' after a simple command goes to the fallback" → the pipeline handles simple commands locally, turn that off; "A follow-up acts on nothing" → more than five minutes passed or a different satellite heard it; the trace records `follow_up`), `site-docs/install.md` (one line in the Assist setup: turn off "Prefer handling commands locally"), `README.md` (one clause in the conversation bullet), `docs/measurements.md` and `site-docs/measurements.md` (section "### The follow-up questions" with the Task 3 numbers, dated, local, no API call, tokens not measured), `quality_scale.yaml` (test count).

- [ ] **Step 1: Write the docs.** Every example sentence must be one a Task 4 test produces; copy it from the test.
- [ ] **Step 2: Run** `.venv/bin/mkdocs build --strict`. Expected: no warnings.
- [ ] **Step 3: Full gate, commit:** "Docs: follow-ups".

### Task 6: Release 1.23.0

- [ ] **Step 1:** `manifest.json` version `1.23.0`. Full gate. Commit "Release 1.23.0".
- [ ] **Step 2:** Merge `follow-ups` into `main` fast-forward only; `git push origin main`.
- [ ] **Step 3:** `gh release create 1.23.0 --repo spsneo/HA-Jev --target main --title 1.23.0 --notes-file <notes>`; notes in the 1.22.3 style, examples copied from tests.
- [ ] **Step 4:** Report to the user in ASD-STE100: what changed, test counts before and after, what was not measured, the install step (HACS update, restart, turn off "Prefer handling commands locally").
