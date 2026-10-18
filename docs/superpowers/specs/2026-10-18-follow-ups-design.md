# Follow-ups: the home remembers the conversation

Status: reviewed, for approval, 2026-10-19.

## Intent and the why

Today every spoken sentence starts from nothing. "Which lights are on?" gets a good
short answer since 1.22.3, but "turn them off" straight after it goes to the fallback
agent, because "them" names nothing.

The goal is a home that feels intelligent and fast. A follow-up is where a voice
assistant feels most like a person or most like a machine. This step makes the
agent remember what the last turn was about, for a few minutes, and read the next
sentence with that in mind.

It is the first of several steps. Later steps (a cloud model for reasoning, nightly
patterns and alerts, several commands in one sentence, learning from corrections)
all read the same memory, so this step also builds that memory as a part of its
own.

## What the user hears

All examples are one speaker, one satellite or one Assist dialog, within five
minutes of the last turn.

| Turn 1 | Turn 2 | What happens |
|---|---|---|
| Which lights are on? → "Shelf and study table are on." | Turn them off. | The shelf and the study table light turn off. Nothing else does. |
| Turn on the kitchen light. → "Turned on the light" | Turn it off. | The kitchen light turns off. |
| Turn on the kitchen light. | Set it to 40%. | The kitchen light goes to 40%. |
| Are any lights on in the kids room? → "Yes, two: ..." | And the living room? | The same question, for the living room: "Yes, one: living room lamp." |
| Turn off the lights in the kitchen. | And the office. | Changed before release (decided by the user, 2026-10-19): a command is not repeated, so this goes on as today. Only a question is repeated, because every way found to repeat a command let a misread question switch a device. |
| Which curtains are closed? → "Study room sheer curtain is closed." | Open it. | That curtain opens. |
| Turn on the kitchen light. | Is the office light on? | A new, unrelated question. Memory is not used. |

After five minutes with no turn, or from a different satellite, "turn them off" is
handled exactly as it is today (it goes to the fallback agent).

## Goals

- "it", "them", "those", "that", "they", "the same", "both" and similar words point
  back to the devices of the last turn.
- "and the living room", "and the office", "same for the kitchen" repeat the last
  request for a new target.
- No extra call to Jev and no extra wait. The memory goes into the one request the
  sentence already makes.
- Every action still goes through Home Assistant's intents, with its exposure
  check. A remembered device that is no longer exposed is not acted on.
- The trace and the Assist dialog show when memory was used and what it pointed to.

## Non-goals for this step

- Relative brightness ("make it brighter", "a bit dimmer"). It goes to the fallback
  agent today and still does.
- "The others", "all except that one", "the second one".
- Undo ("undo that", "never mind, turn it back on").
- Memory across satellites, across rooms or longer than five minutes.
- Memory from turns Home Assistant handled itself before this agent saw them.
- Memory stored on disk. It lives in memory and is gone after a restart.
- A cloud model. Nothing in this step calls one.
- Languages other than English for the "and the X" repeat. Pronouns work in any
  language through the Jev questions, because those read meaning, not English words.
  The English word cue below only helps; it does not decide on its own.

## Anti-goals (a failure even if it works)

- Acting on a device the user did not mean, because memory was used for a sentence
  that was not a follow-up. A wrong action is worse than a fallback. When unsure,
  do what happens today.
- A follow-up that is slower than the first turn.
- Memory that leaks between people: one satellite's turn changing what another
  satellite's "turn them off" means.
- Saying "them" back in a confusing way. The reply names the devices or uses Home
  Assistant's own sentence, as today.

## Constraints

- AGENTS.md applies in full: tests never spend tokens, the 13 translation files,
  the full gate, the `quality_scale.yaml` numbers, and a measured or honestly
  unmeasured reason for each limit in `const.py` or beside the code.
- One Jev call per sentence stays the rule. A level said in words still makes its
  second call, as today.
- The whole-house gate, the exposure rule, the confidence floor, the "which device"
  question and the Sonnet fallback keep working as today.
- The budget check already counts every byte of the request through
  `payload_bytes`, so the memory's bytes are counted with no special code.

## How it works (decided)

### The key and the window

Memory is keyed by the conversation id. Home Assistant keeps one conversation id
per satellite (and per Assist dialog) and reuses it at each wake word for five
minutes after the last turn (`chat_session.CONVERSATION_TIMEOUT`). A new satellite
or a new dialog gets a new id, so memory cannot pass between them. The window is the
same five minutes the "which device" question already uses.

### What is remembered

After each turn that ran an intent, the agent keeps one record for that
conversation id. It replaces the previous one.

- The action and the intent that ran, with its slots.
- The target as it was named: a device, a room, two rooms, a floor or the house.
- The kind of device (domain).
- The shape and asked state, for a status question.
- **The devices the answer was about.** For an action, the devices it changed. For a
  status question, the devices the short answer named (for "which lights are on",
  the ones that are on, not every light read). For a full-list answer, every device
  listed.
- The time, for the window.

A turn that answered "already on" or "already off" also writes a record, with that
one device, so "turn on the kitchen light" → "it is already on" → "turn it off"
works. (Builder's call, agreed in review.)

A turn that went to the fallback agent, was refused, or asked "which device" does not
replace the record. (Decided so that a misheard sentence does not wipe the memory.
The "which device" question keeps working as it does today.)

### How the next sentence uses it

Only when a record exists for this conversation id:

1. The request gets a short description of the last turn in its state, for example:
   `"last_turn": {"said": "which lights are on", "about": ["Kids Room Shelf Light",
   "Kids Room Study Table Light"], "action": "get_state", "where": "Kids Room"}`.
2. Two yes/no questions are added to the same request:
   - `refers_back`: does the sentence point back to the devices of the last turn
     ("them", "it", "those"), rather than naming its own?
   - `same_again`: does the sentence ask for the same thing as the last turn, for a
     different room or device ("and the office")?
3. With no record, neither is added, so a first sentence costs exactly what it costs
   today.

Reading the answers:

- **refers_back** at or above its floor: the target is the remembered devices. The
  action comes from this sentence, as Jev reads it now ("turn them off" is
  turn_off). The intent runs once per device, by its name, its domain and its area,
  the same way the two-room read runs once per room. If the remembered set is one
  device, it runs once.
- **same_again** at or above its floor: the action, the domain and the shape come
  from the record. The target comes from this sentence, as Jev reads it now (the
  area, floor or device question). If this sentence has no target Jev is sure of,
  it is not a repeat and goes on as today.
- **Both, or neither, at or above their floors:** neither is used, and the sentence
  goes on as today. (Decided: a sentence that is read both ways is not clear enough
  to act on.)
- An English word cue helps, in the same way the shape cue does: "it", "them",
  "those", "that one", "both" for refers_back, and a sentence that starts "and the"
  or "same for" for same_again. A cue alone never acts. It can only lift an answer
  that is just under the floor, as the design for the shape question does.

### A remembered device that is gone

Before anything runs, the remembered set is filtered to the devices that are still
exposed and still exist. The rest are acted on. If none are left, the sentence goes
on as today. The agent never acts on some devices and then stops with an error
half-way. (Builder's call, agreed in review.)

### Safety

- A remembered device goes through the intent like any other, so Home Assistant's
  exposure check decides again at act time.
- The whole-house gate applies to a follow-up action exactly as to any action: if
  the remembered set came from a house-wide question and the follow-up is
  "turn them on", the gate decides as it would for "turn everything on".
- Locks and entrance covers are still never controlled.

### Commands Home Assistant handles itself

When a pipeline has "Prefer handling commands locally" on, Home Assistant handles
simple commands itself and this agent never sees them, so they leave no record. The
user turns that option off on their pipeline (decided by the user). The docs say to
do the same, and a troubleshooting row explains a follow-up that does not work after
a simple command when the option is on. Building records from turns Home Assistant
handled itself is out of scope.

### No wake word for the follow-up

After a status answer that names devices (any, all, which, how many, with at least
one device named), the reply sets `continue_conversation`, so a satellite such as
the Voice PE listens for one more sentence with no wake word (decided by the user).
After an action, after a full-list answer and after a status answer that names no
device, it does not. The "which device" question keeps doing so, as today.

### What the user sees

- The trace records `follow_up` (`refers_back`, `same_again` or none), the two
  answers, and the remembered devices used.
- The Assist dialog's reasoning gets one line, for example
  `Follow-up: refers back to Kids Room Shelf Light, Kids Room Study Table Light`.
- For one device, the reply is the same sentence the action or question would get
  today.
- For several devices, Home Assistant has no sentence. The agent says its own, for
  example "Turned off shelf and study table." and "Opening sheer and main.". The
  names use `status.short_name`. These are new keys in `strings.json` and all 13
  translation files, with English text. (Builder's call, agreed in review.)

## Measured and not measured

- The added bytes per follow-up request are to be measured locally with
  `payload_bytes`, as was done for the status questions, and recorded in both
  copies of `measurements.md`.
- The floors for `refers_back` and `same_again` have no measurement. Start at 0.8,
  the same unmeasured floor as the shape question, and say so beside the constant.
  No paid calibration run is part of this step.

## Tests that prove it (mocked Jev answers only)

At least, end to end through `async_converse`:

- The seven rows of the table above.
- "turn them off" after "which lights are on" changes only the lights that were on.
- A follow-up after five minutes behaves as today.
- A follow-up from a different conversation id behaves as today.
- A remembered device that was unexposed in between is not acted on.
- `refers_back` and `same_again` both sure: behaves as today.
- No record: the two questions are not in the request.
- A fallback, a refusal or a "which device" turn does not replace the record.
- A follow-up still makes exactly one Jev call.
- The whole-house gate still refuses "turn them on" after a house-wide question.
- A status answer that names devices sets `continue_conversation`. An action, a
  full-list answer and a status answer that names no device do not.
- "already on" writes a record; "turn it off" after it acts on that device.
- A set with one unexposed device acts on the others and never on that one.
- Several devices get the agent's own sentence, which names each of them.

Each guard is broken once and its test watched to fail, as AGENTS.md asks.

## Release

Commits in small steps, the full gate after each, then `Release 1.23.0`, a push to
`main` and a GitHub release on `spsneo/HA-Jev`. The user asked for the release
("build 1 first and release it"). The user installs it through HACS and tries it
for a while before the next step starts.

## Left to the builder

- Where the record lives in code (a small module of its own is fine) and its exact
  fields.
- The exact wording of the two questions and of the `last_turn` state.
- How the per-device intent run and the per-room run share code.
- The exact English cue patterns.
