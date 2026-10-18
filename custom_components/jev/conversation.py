"""A conversation agent that routes spoken commands through Jev.

What this does that a sentence matcher cannot: it understands a command that was
not phrased the way the template expected. What it does that an LLM agent does not:
it runs one typed request, costs a fraction of a cent, and hands back a confidence
figure the router can refuse to act on.

Three things shape the design.

The house it sees is the Assist exposure list and nothing wider. The user already
decided which entities a voice assistant may touch.

Every command it understands runs a built-in intent, not a service call. Intents
carry Home Assistant's own matching, its own spoken responses in every supported
language, and its own permission checks. Reimplementing that would mean
reimplementing it worse.

Anything it is not sure about goes to the fallback agent whole, with no partial
action taken first. A voice assistant that half-acts is worse than one that says
it did not understand.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.components.conversation.models import AbstractConversationAgent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_CLASS, MATCH_ALL
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent as ha_intent
from homeassistant.helpers import template, translation
from homeassistant.helpers.chat_session import CONVERSATION_TIMEOUT
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util import language as language_util
from jevclient import (
    Choice,
    ChoiceAnswer,
    JevAuthError,
    JevError,
    JevResponse,
    Noul,
    NoulAnswer,
    Question,
)

from . import status
from .const import (
    CONF_ALLOW_WHOLE_HOME,
    CONF_FALLBACK_AGENT,
    CONF_MIN_CONFIDENCE,
    DEFAULT_MIN_CONFIDENCE,
    DOMAIN,
    MAX_CONVERSATION_ENTITIES,
)
from .coordinator import JevRuntimeData
from .entity import build_device_info
from .interpret import (
    ACTIONS,
    NONE,
    Interpretation,
    build_questions,
    interpret,
    level_questions,
    read_level,
    spoken_name,
)
from .memory import Memory, Turn
from .payload import payload_bytes
from .snapshot import HomeSnapshot, async_heard_in, async_snapshot

_LOGGER = logging.getLogger(__name__)

# Used when a translation is missing, so a missing key is still a sentence rather
# than a blank reply. Kept in step with strings.json by a test.
_FALLBACK = {
    "not_understood": "Sorry, I did not understand that.",
    "whole_house": (
        "That would affect the whole house. Say which room or which device you mean."
    ),
    "which_kind": (
        "Which kind of thing do you mean? Say the lights, or the switches, or name "
        "a room."
    ),
    "intent_failed": "Sorry, that did not work.",
    "already_on": "{name} is already on.",
    "already_off": "{name} is already off.",
    "done": "Done.",
    "query_not_found": "I could not find that.",
    "budget_spent": (
        "Not enough of the daily token budget is left for that, so I cannot do it today."
    ),
    "auth_failed": "TypeSafe rejected the API key. Check it in the Jev settings.",
    "unavailable": "TypeSafe did not answer. Try again in a moment.",
    "which_device": "Do you mean {first} or {second}?",
    # Several remembered devices run one intent each, and Home Assistant has no
    # sentence for a set it never saw whole.
    "many_turned_on": "Turned on {names}.",
    "many_turned_off": "Turned off {names}.",
    "many_toggled": "Toggled {names}.",
    "many_opening": "Opening {names}.",
    "many_closing": "Closing {names}.",
    "many_brightness": "Set {names} to {level}%.",
}

PARALLEL_UPDATES = 0


@dataclass(slots=True)
class _Pending:
    """A command held while the agent asks which device it meant."""

    text: str
    response: JevResponse
    snapshot: HomeSnapshot
    candidates: tuple[str, str]
    expires: datetime = field(
        default_factory=lambda: dt_util.utcnow() + CONVERSATION_TIMEOUT
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([JevConversationEntity(entry)])


class JevConversationEntity(conversation.ConversationEntity, AbstractConversationAgent):
    """Routes one sentence, then gets out of the way."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        runtime: JevRuntimeData = entry.runtime_data
        self._attr_device_info = build_device_info(entry.entry_id, runtime)
        # Conversation id to the command waiting on its reply.
        self._pending: dict[str, _Pending] = {}
        # Conversation id to what its last turn was about, for "turn them off".
        self._memory = Memory()

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Whatever the intents support.

        Jev reads the sentence and Home Assistant speaks the reply, so the limit is
        the intent layer's, not ours.
        """
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self._entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self._entry)
        await super().async_will_remove_from_hass()

    # --- options ---

    @property
    def _min_confidence(self) -> float:
        return float(self._entry.options.get(CONF_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE))

    @property
    def _fallback_agent(self) -> str | None:
        agent = self._entry.options.get(CONF_FALLBACK_AGENT)
        # Pointing the fallback at this entity would recurse until something gave
        # way. Refusing it here is cheaper than detecting the loop later.
        if not agent or agent == self.entity_id:
            return None
        # Another Jev agent is the same loop one step removed: two entries that fall
        # back to each other pass the sentence between them and pay each time.
        registered = er.async_get(self.hass).async_get(agent)
        entry = self.hass.config_entries.async_get_entry(agent)
        if (registered and registered.platform == DOMAIN) or (
            entry and entry.domain == DOMAIN
        ):
            return None
        return str(agent)

    @property
    def _allow_whole_home(self) -> bool:
        return bool(self._entry.options.get(CONF_ALLOW_WHOLE_HOME, False))

    # --- the router ---

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        result = await self._handle(user_input, chat_log)
        # Home Assistant passes on the id the caller sent, which is None or an
        # expired one when a client starts a conversation, while the chat session
        # holds the id that continues it. The reply carries that one, so the next
        # sentence reaches the same memory.
        result.conversation_id = chat_log.conversation_id
        return result

    async def _handle(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        runtime: JevRuntimeData = self._entry.runtime_data

        runtime.usage.roll_over(dt_util.now().date())
        if (pending := self._take_pending(chat_log.conversation_id)) is not None:
            resolved = await self._resolve(user_input, chat_log, pending)
            if resolved is not None:
                return resolved

        heard_in = async_heard_in(
            self.hass, user_input.satellite_id, user_input.device_id
        )
        # Read before the snapshot, so the devices "them" means are kept inside the
        # cap however few words the sentence shares with them.
        last = self._memory.get(chat_log.conversation_id)
        snapshot = async_snapshot(
            self.hass,
            MAX_CONVERSATION_ENTITIES,
            user_input.text,
            heard_in,
            remembered=frozenset(last.devices) if last is not None else (),
        )
        if not snapshot.entities:
            return await self._fall_back(
                user_input, chat_log, "no entities are exposed to Assist"
            )

        # The memory's bytes go into the same request and through the same budget
        # check, and a conversation with nothing to follow asks what it asked before.
        questions = build_questions(
            user_input.text,
            snapshot,
            MAX_CONVERSATION_ENTITIES,
            follow_up=last is not None,
        )
        state = snapshot.as_state() | {"command": user_input.text}
        if last is not None:
            state["last_turn"] = last.describe()
        response = await self._ask(user_input, chat_log, state, questions)
        if isinstance(response, conversation.ConversationResult):
            return response

        decision = interpret(
            response,
            user_input.text,
            snapshot,
            self._min_confidence,
            heard_in=heard_in,
            last=last,
        )
        followed: dict[str, Any] = {}
        if last is not None:
            followed = {
                "last_said": last.text,
                "remembered_names": [
                    described.name
                    for entity_id in decision.remembered
                    if (described := snapshot.by_id(entity_id)) is not None
                ],
            }
        self._trace(
            chat_log,
            response,
            {
                "text": user_input.text,
                "exposed_entities": len(snapshot.entities),
                "left_out_by_cap": snapshot.left_out,
                **asdict(decision),
                **followed,
            },
        )

        if decision.candidates is not None:
            return await self._ask_which(
                user_input,
                chat_log,
                _Pending(user_input.text, response, snapshot, decision.candidates),
            )
        return await self._act(user_input, chat_log, decision, user_input.text, snapshot)

    async def _ask(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        state: dict[str, Any],
        questions: dict[str, Question],
    ) -> JevResponse | conversation.ConversationResult:
        """One call to Jev, inside the budget. A result means it did not answer."""
        runtime: JevRuntimeData = self._entry.runtime_data

        # The budget covers voice as well as sensors, because a satellite that
        # mishears a wake word all night is exactly the runaway it exists to stop.
        # The check is on this command's estimate, the same as a context's, so the
        # last command of the day cannot take the total past the budget.
        request_bytes = payload_bytes(state, questions, runtime.model)
        estimate = runtime.usage.estimate_tokens(request_bytes)
        if runtime.usage.would_exceed_with(estimate):
            return await self._fall_back(
                user_input,
                chat_log,
                f"the daily token budget has {runtime.usage.remaining()} tokens left "
                f"and this command needs about {estimate}",
                "budget_spent",
            )

        try:
            with runtime.usage.reservation(estimate):
                response = await runtime.client.ask(state, questions)
        except JevAuthError as err:
            _LOGGER.error("TypeSafe rejected the API key: %s", err)
            self._entry.async_start_reauth(self.hass)
            return await self._fall_back(
                user_input, chat_log, "the API key was rejected", "auth_failed"
            )
        except JevError as err:
            _LOGGER.warning("TypeSafe did not answer: %s", err)
            return await self._fall_back(
                user_input, chat_log, f"TypeSafe did not answer: {err}", "unavailable"
            )

        runtime.usage.record(response.usage.input_tokens, request_bytes)
        runtime.model_version = response.model or runtime.model_version
        runtime.usage.notify()
        return response

    def _trace(
        self,
        chat_log: conversation.ChatLog,
        response: JevResponse,
        record: dict[str, Any],
    ) -> None:
        """Keep what one call decided, for diagnostics and the Assist dialog."""
        runtime: JevRuntimeData = self._entry.runtime_data
        trace = {
            "latency_ms": response.latency_ms,
            "input_tokens": response.usage.input_tokens,
            **record,
        }
        runtime.conversation_traces.appendleft(trace)
        # The pipeline records a chat log delta as an intent-progress event: the
        # Assist dialog shows its thinking_content under the reply, and the run's
        # debug events keep it. The delta goes to the listener only, not into the
        # log, so a fallback agent reading this conversation never takes it for
        # something said. It carries each answer's distribution, because "why did
        # it pick the office light" is answered by the entity question and by
        # nothing in the decision alone.
        if chat_log.delta_listener is not None:
            chat_log.delta_listener(
                chat_log,
                {"role": "assistant", "thinking_content": _reasoning(trace, response)},
            )

    # --- asking which device ---

    def _take_pending(self, conversation_id: str) -> _Pending | None:
        """The command waiting on this conversation's reply, if it is still live.

        A pending command lasts as long as Home Assistant keeps the chat session,
        so a reply that arrives in a new session is never read as an answer.
        """
        now = dt_util.utcnow()
        for key in [k for k, v in self._pending.items() if v.expires < now]:
            del self._pending[key]
        return self._pending.pop(conversation_id, None)

    async def _ask_which(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        pending: _Pending,
    ) -> conversation.ConversationResult:
        """Ask which of two devices the command meant, and keep the command."""
        first, second = (pending.snapshot.by_id(e) for e in pending.candidates)
        assert first is not None and second is not None
        names = spoken_name(first, second)
        assert names is not None
        language = user_input.language or self.hass.config.language
        text = (await self._lines(language))["which_device"].format(
            first=names[0], second=names[1]
        )
        self._pending[chat_log.conversation_id] = pending
        # In the chat log, the next turn in this conversation carries the question
        # it answers, and an LLM fallback agent reads the same history.
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=self.entity_id, content=text)
        )
        response = ha_intent.IntentResponse(language=user_input.language)
        response.async_set_speech(text)
        # A satellite opens the microphone again for the answer.
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=True,
        )

    async def _resolve(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        pending: _Pending,
    ) -> conversation.ConversationResult | None:
        """Run the kept command on the device the reply picked.

        None when the reply picked neither, so the reply is handled as a new
        command: "no, the kitchen light" and "never mind, lock up" both are one.
        """
        snapshot = pending.snapshot
        options: dict[str, Any] = {
            entity_id: described.as_option()
            for entity_id in pending.candidates
            if (described := snapshot.by_id(entity_id)) is not None
        }
        options[NONE] = "Neither of these, or a different request"
        questions: dict[str, Question] = {
            "which": Choice("Which device does the reply pick?", options),
            # "Never mind, turn off the lamp in the bedroom" names one of the two, so
            # the choice alone picks it and the kept command, turn on, runs on it.
            # Measured on a development instance, twelve replies to "turn on the
            # lamp": the six that only pick a device scored 0.08 to 0.26 here, and
            # the six that ask for something else, that one included, 0.91 to 0.97.
            "new_request": Noul(
                "Does the reply ask for something of its own, rather than only "
                "saying which device the command meant?",
                true="The reply is a new instruction, or changes what should happen",
                false="The reply only picks a device, however it is phrased",
            ),
        }
        state = {"command": pending.text, "reply": user_input.text}
        response = await self._ask(user_input, chat_log, state, questions)
        if isinstance(response, conversation.ConversationResult):
            return response

        answer = response.answers.get("which")
        new_request = response.answers.get("new_request")
        picked = (
            answer.choice
            if isinstance(answer, ChoiceAnswer)
            and answer.choice in pending.candidates
            and answer.confidence >= self._min_confidence
            and not (isinstance(new_request, NoulAnswer) and new_request.noul >= 0.5)
            else None
        )
        self._trace(
            chat_log,
            response,
            {"text": user_input.text, "answers_command": pending.text, "picked": picked},
        )
        if picked is None:
            return None

        # The first call's answers stand, with the entity question settled. The
        # command is read from its own sentence again, so a brightness it named
        # still comes from the text.
        settled = ChoiceAnswer(choice=picked, probabilities={picked: 1.0}, confidence=1.0)
        first = replace(
            pending.response, answers=pending.response.answers | {"entity": settled}
        )
        decision = interpret(
            first, pending.text, snapshot, self._min_confidence, ask_back=False
        )
        return await self._act(user_input, chat_log, decision, pending.text, snapshot)

    # --- a level said in words ---

    async def _ask_level(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        decision: Interpretation,
        text: str,
    ) -> Interpretation | conversation.ConversationResult:
        """Ask for the level of "set the lamp to forty percent", one more request.

        Only a brightness command with no digit in it sends this, so no other
        command pays for it. It sends the sentence alone: 403 to 568 input tokens.
        """
        response = await self._ask(
            user_input, chat_log, {"command": text}, level_questions()
        )
        if isinstance(response, conversation.ConversationResult):
            return response
        level = read_level(response, text, self._min_confidence, user_input.language)
        self._trace(chat_log, response, {"text": text, "level_for": level})
        if level is None:
            return replace(
                decision,
                fallback=True,
                reason="a brightness was asked for but no level was said",
                needs_level=False,
            )
        slots = decision.slots | {"brightness": {"value": level}}
        return replace(decision, slots=slots, needs_level=False)

    # --- acting ---

    async def _act(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        decision: Interpretation,
        text: str,
        snapshot: HomeSnapshot,
    ) -> conversation.ConversationResult:
        """Carry out one decision, or say why not."""
        if decision.already_satisfied is not None:
            name, settled = decision.already_satisfied
            # "It is already on" is about that device, so "turn it off" can follow.
            if decision.already_entity is not None and (
                already := self.hass.states.get(decision.already_entity)
            ):
                self._remember(chat_log, decision, text, [already])
            return await self._speak(user_input, f"already_{settled}", name=name)

        if decision.needs_level:
            read = await self._ask_level(user_input, chat_log, decision, text)
            if isinstance(read, conversation.ConversationResult):
                return read
            decision = read

        if decision.should_fall_back:
            return await self._fall_back(user_input, chat_log, decision.reason)

        # The whole-home gate. "Turn everything off" is a real command and a
        # harmless one. "Turn everything on" at 3am, from a sentence the model was
        # only somewhat sure about, is not something to do silently. Off is allowed
        # because its worst case is a dark house; anything else asks first. A
        # question changes nothing, so "which curtains are closed" is not asked back.
        if decision.targets_everything:
            if (
                not self._allow_whole_home
                and decision.action != "turn_off"
                and decision.intent_type != ha_intent.INTENT_GET_STATE
            ):
                return await self._speak(user_input, "whole_house")
            # Home Assistant refuses "all" with no kind of device beside it, and an
            # unbounded command is not something to infer from one sentence anyway.
            if "domain" not in decision.slots:
                return await self._speak(user_input, "which_kind")

        assert decision.intent_type is not None
        try:
            intent_response, acted = await self._run(user_input, decision, text)
        except ha_intent.MatchFailedError as err:
            # The model named something the intent layer could not find. That is a
            # miss, not a failure, so the fallback agent gets the sentence intact.
            _LOGGER.debug("intent %s matched nothing: %s", decision.intent_type, err)
            return await self._fall_back(
                user_input, chat_log, "the named target was not found"
            )
        except ha_intent.IntentHandleError as err:
            # Home Assistant raises this only when no entity succeeded, so nothing
            # changed. A media player with no turn_off does this, and the fallback
            # agent may know another way to do what was asked.
            _LOGGER.debug("intent %s failed: %s", decision.intent_type, err)
            return await self._fall_back(
                user_input, chat_log, "the intent failed on every target", "intent_failed"
            )
        except ha_intent.IntentError as err:
            _LOGGER.error("intent %s failed: %s", decision.intent_type, err)
            # An error, so a satellite does not hear it as a command that went through.
            return await self._speak(
                user_input,
                "intent_failed",
                error=ha_intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
            )

        # Loading our lines reads translations, so only a reply without a sentence of
        # its own loads them.
        language = user_input.language or self.hass.config.language
        # The devices a status answer named. A satellite listens again after one,
        # because "turn them off" is the likely next sentence.
        about: list[State] = []
        if intent_response.response_type is ha_intent.IntentResponseType.QUERY_ANSWER:
            if (
                decision.intent_type == ha_intent.INTENT_GET_STATE
                and language_util.matches(language, {"en"})
                and (
                    short := _status_answer(
                        decision, intent_response, await self._status_lines(language)
                    )
                )
                is not None
            ):
                spoken, about = short
                intent_response.async_set_speech(spoken)
                acted = about
            else:
                acted = list(intent_response.matched_states)
            await _speak_the_answer(
                self.hass,
                intent_response,
                decision.intent_type,
                language,
                await self._lines(language),
            )
        elif intent_response.response_type is ha_intent.IntentResponseType.ACTION_DONE:
            spoken = None
            if decision.each:
                # The status lines carry "a and b", which the list of names needs.
                lines = await self._status_lines(language) | await self._lines(language)
                spoken = _many(decision, acted, snapshot, lines)
            elif not intent_response.speech:
                spoken = (
                    await _render_action_answer(
                        self.hass,
                        intent_response,
                        decision.intent_type,
                        decision.slots,
                        language,
                    )
                    or (await self._lines(language))["done"]
                )
            if spoken is not None:
                intent_response.async_set_speech(spoken)
        self._remember(chat_log, decision, text, acted)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=bool(about),
        )

    def _remember(
        self,
        chat_log: conversation.ChatLog,
        decision: Interpretation,
        text: str,
        devices: Sequence[State],
    ) -> None:
        """Keep what this turn was about, in place of the last one."""
        domains = decision.slots.get("domain", {}).get("value") or [
            state.domain for state in devices
        ]
        if decision.rooms:
            where: tuple[str, ...] = tuple(decision.rooms)
        elif "area" in decision.slots:
            where = (decision.slots["area"]["value"],)
        elif "floor" in decision.slots:
            where = (decision.slots["floor"]["value"],)
        else:
            where = ()
        # A brightness is remembered as one whatever the sentence began as: "turn on
        # the lamp at 50%" ran HassLightSet, and the last turn says so.
        action = decision.action
        if decision.intent_type == ACTIONS["set_brightness"]:
            action = "set_brightness"
        self._memory.remember(
            chat_log.conversation_id,
            Turn(
                text=text,
                action=action,
                intent_type=decision.intent_type or ACTIONS.get(decision.action, ""),
                domains=tuple(dict.fromkeys(domains)),
                where=where,
                everything=decision.targets_everything,
                devices=tuple(state.entity_id for state in devices),
                names=tuple(state.name for state in devices),
                shape=decision.shape,
                asked_state=decision.asked_state,
            ),
        )

    async def _run(
        self,
        user_input: conversation.ConversationInput,
        decision: Interpretation,
        text: str,
    ) -> tuple[ha_intent.IntentResponse, list[State]]:
        """Run the intent: once per room a state question names, once per
        remembered device, or once. Also the states it acted on.

        Each run goes through Home Assistant's own intent, so its exposure check
        still decides. A room with nothing of the kind is left out, as is a
        remembered device that no longer matches or fails, and only when every run
        misses does the error go up to the caller. A read skips only a miss.
        """
        assert decision.intent_type is not None
        if decision.each:
            # Each device brings its own domain, and the rest (a brightness) is shared.
            shared = {k: v for k, v in decision.slots.items() if k != "domain"}
            runs = [shared | own for own in decision.each]
        elif decision.rooms:
            runs = [decision.slots | {"area": {"value": room}} for room in decision.rooms]
        else:
            runs = [decision.slots]
        skipped: tuple[type[ha_intent.IntentError], ...] = (ha_intent.MatchFailedError,)
        if decision.intent_type not in (
            ha_intent.INTENT_GET_STATE,
            ha_intent.INTENT_GET_TEMPERATURE,
        ):
            skipped = (ha_intent.MatchFailedError, ha_intent.IntentHandleError)
        responses: list[ha_intent.IntentResponse] = []
        missed: ha_intent.IntentError | None = None
        for slots in runs:
            try:
                responses.append(
                    await ha_intent.async_handle(
                        self.hass,
                        DOMAIN,
                        decision.intent_type,
                        slots,
                        text,
                        user_input.context,
                        language=user_input.language,
                        assistant=conversation.DOMAIN,
                        device_id=user_input.device_id,
                        satellite_id=user_input.satellite_id,
                        conversation_agent_id=user_input.agent_id,
                    )
                )
            except ha_intent.IntentError as err:
                # Once a device has changed, the turn says what changed rather than
                # ending in an error that sounds as if nothing did.
                if responses:
                    _LOGGER.warning(
                        "intent %s failed on one target after another changed: %s",
                        decision.intent_type,
                        err,
                    )
                    continue
                if len(runs) == 1 or not isinstance(err, skipped):
                    raise
                missed = err
        if not responses:
            assert missed is not None
            raise missed
        acted: dict[str, State] = {}
        for response in responses:
            done = {
                target.id
                for target in response.success_results
                if target.type is ha_intent.IntentResponseTargetType.ENTITY
            }
            for state in response.matched_states:
                if not done or state.entity_id in done:
                    acted.setdefault(state.entity_id, state)
        first, *others = responses
        if others:
            matched = {s.entity_id: s for r in responses for s in r.matched_states}
            unmatched = {
                s.entity_id: s
                for r in responses
                for s in r.unmatched_states
                if s.entity_id not in matched
            }
            first.async_set_states(list(matched.values()), list(unmatched.values()))
        return first, list(acted.values())

    # --- the two ways out ---

    async def _fall_back(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        why: str,
        line: str = "not_understood",
    ) -> conversation.ConversationResult:
        """Hand the whole sentence to the configured agent, having done nothing.

        With no agent to hand it to, say why, so a spent budget or a rejected key
        is not heard as a sentence the model failed to understand.
        """
        agent = self._fallback_agent
        _LOGGER.debug("falling back to %s because %s", agent or "nobody", why)
        if agent is None:
            # An error, as the default agent answers one. A satellite and the Assist
            # dialog treat an action_done reply as a command that went through.
            code = (
                ha_intent.IntentResponseErrorCode.NO_INTENT_MATCH
                if line == "not_understood"
                else ha_intent.IntentResponseErrorCode.FAILED_TO_HANDLE
            )
            return await self._speak(user_input, line, error=code)
        # This agent's session id, as the pipeline passes it, so the fallback agent
        # joins this conversation. The id the caller sent is None or an expired one
        # when a client starts, and the fallback agent would then keep its own
        # memory under an id the client never hears.
        result = await conversation.async_converse(
            self.hass,
            user_input.text,
            chat_log.conversation_id,
            user_input.context,
            language=user_input.language,
            agent_id=agent,
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )
        return result

    async def _lines(self, language: str) -> dict[str, str]:
        """This agent's own lines, in the language the pipeline is speaking.

        The intent layer localises its own replies, so anything this agent says
        itself has to be localised here or a Dutch pipeline answers in English.
        The English text is the last resort, so a missing key is still a sentence.

        This integration translates 13 languages. Home Assistant words two of these
        lines in all 63 that home-assistant-intents carries, so a pipeline speaking
        one of the other 50 hears a sentence rather than English. Our own wording
        wins wherever we have it.
        """
        ours = await translation.async_get_translations(
            self.hass, language, "common", [DOMAIN]
        )
        # The cache loads English underneath every language, so a language this
        # integration has not translated comes back as its English text rather than
        # missing. Two identical dictionaries is what tells those apart.
        shipped = None
        if not language_util.matches(language, {"en"}) and ours == (
            await translation.async_get_translations(self.hass, "en", "common", [DOMAIN])
        ):
            shipped = await _shipped(self.hass, language)
        lines = {}
        for key, fallback in _FALLBACK.items():
            text = None
            if shipped is not None and (name := _SHIPPED_SENTENCE.get(key)):
                text = shipped.errors.get(name)
            lines[key] = text or ours.get(f"component.{DOMAIN}.common.{key}", fallback)
        return lines

    async def _status_lines(self, language: str) -> dict[str, str]:
        """The short answers' sentences, as strings.json words them."""
        ours = await translation.async_get_translations(
            self.hass, language, "common", [DOMAIN]
        )
        return {
            key: ours.get(f"component.{DOMAIN}.common.{key}", text)
            for key, text in status.LINES.items()
        }

    async def _speak(
        self,
        user_input: conversation.ConversationInput,
        key: str,
        error: ha_intent.IntentResponseErrorCode | None = None,
        **placeholders: str,
    ) -> conversation.ConversationResult:
        """Say one of our own lines, with its placeholders filled in."""
        language = user_input.language or self.hass.config.language
        text = (await self._lines(language))[key].format(**placeholders)
        response = ha_intent.IntentResponse(language=user_input.language)
        if error is None:
            response.async_set_speech(text)
        else:
            response.async_set_error(error, text)
        return conversation.ConversationResult(
            response=response, conversation_id=user_input.conversation_id
        )


def _reasoning(trace: Mapping[str, Any], response: JevResponse) -> str:
    """The trace as lines a person reads in the Assist dialog."""
    if "level_for" in trace:
        lines = [f"Jev: the level said in words, {trace['level_for'] or 'not read'}"]
    elif "answers_command" in trace:
        lines = [
            f'Jev: a reply to "{trace["answers_command"]}", '
            f"picked {trace['picked'] or 'neither'}"
        ]
    else:
        lines = [
            f"Jev: {trace['action'] or 'no action'}, {trace['reason']}, "
            f"confidence {trace['confidence']:.2f}",
            f"Slots: {json.dumps(trace['slots'], ensure_ascii=False)}",
        ]
        if trace.get("shape"):
            rooms = ", ".join(trace.get("rooms") or []) or "as above"
            lines.append(
                f"Status: {trace['shape']}, asked {trace.get('asked_state') or 'nothing'}"
                f", cue {trace.get('shape_cue') or 'none'}, rooms {rooms}"
            )
        if trace.get("follow_up") == "refers_back":
            names = trace.get("remembered_names") or trace.get("remembered") or []
            lines.append(f"Follow-up: refers back to {', '.join(names)}")
        elif trace.get("follow_up") == "same_again":
            lines.append(f'Follow-up: same again as "{trace.get("last_said", "")}"')
    for key, answer in response.answers.items():
        if isinstance(answer, ChoiceAnswer):
            ranked = sorted((answer.probabilities or {}).items(), key=lambda kv: -kv[1])[
                :3
            ]
            spread = ", ".join(f"{k} {p:.2f}" for k, p in ranked)
            lines.append(f"{key}: {answer.choice} {answer.confidence:.2f} ({spread})")
        elif isinstance(answer, NoulAnswer):
            lines.append(f"{key}: {answer.noul:.2f}")
        else:
            lines.append(f"{key}: {json.dumps(asdict(answer), ensure_ascii=False)}")
    footer = f"{response.model}, {trace['input_tokens']} input tokens, "
    footer += f"{trace['latency_ms']:.0f} ms"
    if "exposed_entities" in trace:
        footer += f", {trace['exposed_entities']} entities"
    lines.append(footer)
    return "\n".join(lines)


# A template that compares the state against an English word writes the state word
# itself, in its own language. Hand it a translated one and every branch falls
# through. Measured against home-assistant-intents 2026.8.28: 3 of the 47 templates
# do this, Polish, Russian and Thai.
_COMPARES_STATE = re.compile(r"""==\s*['"](?:on|off|open|closed|locked|unlocked)['"]""")

# Home Assistant words these two in all 63 languages the intents package carries,
# against the 13 this integration translates. Saying what stock Assist says is the
# same choice the state answer makes.
_SHIPPED_SENTENCE = {"not_understood": "no_intent", "intent_failed": "handle_error"}


@dataclass(frozen=True, slots=True)
class _Shipped:
    """One language's share of home-assistant-intents."""

    state_answer: str | None
    writes_the_state_word: bool
    temperature_answer: str | None
    errors: Mapping[str, str]
    # The sentences for each action intent, keyed by the response name the default
    # agent's own sentence data picks.
    action_answers: Mapping[str, Mapping[str, str]]


# Read once per language, keyed by the language that was asked for rather than the
# variant it matched, so "it" and "it-IT" cost one load each. None means the
# package carries nothing for it and this agent falls back to English.
_SHIPPED: dict[str, _Shipped | None] = {}


def _load_shipped(language: str) -> _Shipped | None:
    """Read one language out of home-assistant-intents, in the executor.

    The package ships the answer template for a state question and the error
    sentences, both written by the people who translate the rest of Assist. Polish
    inflects the adjective by the last letter of the device name, Russian writes the
    state word in Russian, and German turns 21.5 into 21,5 Grad. None of that could
    be got right from a string table of this integration's own, and all of it reaches
    languages this integration does not translate.
    """
    try:
        from home_assistant_intents import (
            get_intents,
            get_languages,
        )
    except ImportError:  # pragma: no cover - the conversation integration installs it
        return None
    matches = language_util.matches(language, set(get_languages()))
    if not matches:
        return None
    intents = get_intents(matches[0])
    if not intents:
        return None
    responses = intents.get("responses", {})
    answer = responses.get("intents", {}).get("HassGetState", {}).get("one")
    if not isinstance(answer, str):
        answer = None
    # German keys its one sentence current_temperature, every other language default.
    temperatures = (
        responses.get("intents", {}).get(ha_intent.INTENT_GET_TEMPERATURE) or {}
    )
    temperature = temperatures.get("default") or temperatures.get("current_temperature")
    return _Shipped(
        state_answer=answer,
        writes_the_state_word=bool(answer and _COMPARES_STATE.search(answer)),
        temperature_answer=temperature if isinstance(temperature, str) else None,
        errors={
            key: text
            for key, text in responses.get("errors", {}).items()
            if isinstance(text, str) and text.strip()
        },
        action_answers={
            intent_type: {
                key: text
                for key, text in (
                    responses.get("intents", {}).get(intent_type) or {}
                ).items()
                if isinstance(text, str) and text.strip()
            }
            for intent_type in ACTIONS.values()
        },
    )


async def _shipped(hass: HomeAssistant, language: str) -> _Shipped | None:
    """What home-assistant-intents carries for a language, loaded once."""
    if language not in _SHIPPED:
        _SHIPPED[language] = await hass.async_add_executor_job(_load_shipped, language)
    return _SHIPPED[language]


async def _state_word(hass: HomeAssistant, state: State, language: str) -> str | None:
    """Home Assistant's own word for this state, or None when it has none.

    The three keys `async_translate_state` reads, in its order: the entity's own
    translation key, then the device class, then the domain's default. That helper
    cannot be called here, because it reads `hass.config.language`, which is the
    language of the user interface and not the one this pipeline speaks.

    The device class layer is what makes a door answer "aperto" rather than
    "acceso", and a motion sensor "rilevato" rather than "on".

    Lower case, because the shipped words are interface labels and are capitalised
    for a badge. The finished sentence gets its first letter back below.
    """
    domain = state.domain
    entry = er.async_get(hass).async_get(state.entity_id)
    if entry is not None and entry.translation_key is not None:
        own = await translation.async_get_translations(
            hass, language, "entity", {entry.platform}
        )
        key = (
            f"component.{entry.platform}.entity.{domain}"
            f".{entry.translation_key}.state.{state.state}"
        )
        if word := own.get(key):
            return word.lower()
    words = await translation.async_get_translations(
        hass, language, "entity_component", {domain}
    )
    if (device_class := state.attributes.get(ATTR_DEVICE_CLASS)) is not None:
        key = f"component.{domain}.entity_component.{device_class}.state.{state.state}"
        if word := words.get(key):
            return word.lower()
    word = words.get(f"component.{domain}.entity_component._.state.{state.state}")
    return word.lower() if word else None


class _SpokenState(template.TemplateState):
    """A state whose `state_with_unit` reads in the language being spoken.

    The answer templates reach the state only through this property, so one
    substitution here turns "Luce Tavolo è off" into "Luce Tavolo è spento" without
    touching the sentence. A numeric state has no translation, so a sensor keeps
    its rounded value and its unit.
    """

    __slots__ = ("_word",)

    def __init__(self, hass: HomeAssistant, state: State, word: str | None) -> None:
        """Carry the translated word, or None to leave the state as it is."""
        super().__init__(hass, state)
        self._word = word

    @property
    def state_with_unit(self) -> str:
        """The state in the spoken language, or Home Assistant's own formatting."""
        if self._word is None:
            return super().state_with_unit
        return self._word


async def _render_state_answer(
    hass: HomeAssistant,
    matched: list[State],
    unmatched: list[State],
    language: str,
) -> str | None:
    """The sentence the default agent would have said, or None if it cannot."""
    shipped = await _shipped(hass, language)
    if shipped is None or shipped.state_answer is None:
        return None
    words: dict[str, str | None] = {}
    if not shipped.writes_the_state_word:
        for state in (*matched, *unmatched):
            words[state.entity_id] = await _state_word(hass, state, language)

    def spoken(state: State) -> _SpokenState:
        return _SpokenState(hass, state, words.get(state.entity_id))

    answer = template.Template(shipped.state_answer, hass)
    query = {
        "matched": [spoken(state) for state in matched],
        "unmatched": [spoken(state) for state in unmatched],
    }
    parts = []
    for state in matched:
        try:
            rendered = answer.async_render(
                {
                    "slots": {"name": state.name},
                    "state": spoken(state),
                    "query": query,
                },
                parse_result=False,
            )
        except TemplateError as err:
            _LOGGER.debug("the %s state answer did not render: %s", language, err)
            return None
        # The templates are written over several lines and indented. The default
        # agent collapses that the same way before speaking it.
        sentence = " ".join(str(rendered).split())
        if not sentence:
            continue
        # Brazilian Portuguese answers with the state alone and Russian answers
        # "Выключено", both without the name, because the default agent reaches this
        # template only after the user named one device. Several answers in a row
        # need the name back or they say nothing about which device is which.
        if len(matched) > 1 and state.name.casefold() not in sentence.casefold():
            sentence = f"{state.name}: {sentence}"
        # Every template capitalises the device name, so a sentence that starts with
        # the state word instead, as the Brazilian Portuguese one does, would start
        # in lower case. Only the first character moves: `str.capitalize` would lower
        # the rest of the sentence.
        parts.append(sentence[0].upper() + sentence[1:])
    return ", ".join(parts) if parts else None


class _Reading(template.TemplateState):
    """A state whose `state` is its number, where it is one."""

    __slots__ = ("_reading",)

    def __init__(self, hass: HomeAssistant, state: State, reading: float | None) -> None:
        """Carry the number, or None to leave the state as it is."""
        super().__init__(hass, state)
        self._reading = reading

    # State has a plain state attribute. TemplateStateBase makes it a property with
    # the same ignore, so this one follows it.
    @property
    def state(self) -> Any:  # type: ignore[override]
        """The number, or the state string when it is not one."""
        return super().state if self._reading is None else self._reading


async def _render_temperature_answer(
    hass: HomeAssistant, state: State, language: str
) -> str | None:
    """The temperature as the default agent would say it, or None if it cannot.

    The template reads current_temperature from a climate device and the state
    from a sensor, and writes the unit and the decimal sign of the language. A
    sensor's state is a string, and the Hungarian template writes only a number: the
    default agent answered a 24.8 sensor with "Nem várt eredmény: 24.8". So a
    sensor's state goes in as a number, as a climate device gives it.
    """
    shipped = await _shipped(hass, language)
    if shipped is None or shipped.temperature_answer is None:
        return None
    try:
        reading: float | None = float(state.state)
    except ValueError:
        reading = None
    try:
        rendered = template.Template(shipped.temperature_answer, hass).async_render(
            {"slots": {}, "state": _Reading(hass, state, reading)},
            parse_result=False,
        )
    except TemplateError as err:
        _LOGGER.debug("the %s temperature answer did not render: %s", language, err)
        return None
    return " ".join(str(rendered).split()) or None


# One kind of device across a room or the whole house. The names differ between
# languages: English writes light_all and Polish lights_all.
_AREA_RESPONSES = {"light": ("lights_area",), "fan": ("fans_area",)}
_FLOOR_RESPONSES = {"light": ("lights_floor",)}
_ALL_RESPONSES = {"light": ("light_all", "lights_all"), "fan": ("fan_all",)}
# Domains whose own sentence fits a room as well as one device. English's default
# names the device from a table that has no cover, so a room of curtains came back
# as "Turned on None"; its cover sentence is "Opening".
_DOMAIN_RESPONSES = ("cover", "valve", "lock")

_SLOT_REFERENCE = re.compile(r"slots\.(\w+)")


def _response_keys(
    intent_type: str, slots: Mapping[str, Any], domain: str | None
) -> tuple[str, ...]:
    """The response names to try, closest first, for what this command did.

    The default agent reads the name from the sentence it matched. This agent has
    no sentence, so it reads the same thing off the slots it sent.
    """
    if intent_type == "HassLightSet":
        return ("brightness",)
    kinds = slots.get("domain", {}).get("value") or []
    kind = kinds[0] if len(kinds) == 1 else None
    closest: tuple[str, ...]
    if "area" in slots:
        closest = _AREA_RESPONSES.get(kind or "", ())
    elif "floor" in slots:
        closest = _FLOOR_RESPONSES.get(kind or "", ())
    elif slots.get("name", {}).get("value") == "all":
        closest = _ALL_RESPONSES.get(kind or "", ())
    elif domain is not None:
        # A scene is activated rather than turned on, and German says "Licht
        # eingeschaltet" for one light, where English has no sentence of its own.
        closest = (domain,)
    else:
        closest = ()
    if kind in _DOMAIN_RESPONSES and kind not in closest:
        closest = (*closest, kind)
    return (*closest, "default")


async def _render_action_answer(
    hass: HomeAssistant,
    response: ha_intent.IntentResponse,
    intent_type: str | None,
    slots: Mapping[str, Any],
    language: str,
) -> str | None:
    """The sentence the default agent says after the same action, or None.

    `async_handle` does the action and says nothing, and the Assist dialog shows no
    reply at all for an empty one. The default agent's words come from the same
    package as the state answers, written by the people who translate Assist.
    None when the package has nothing that fits, and the agent says "Done." instead.
    """
    shipped = await _shipped(hass, language)
    if shipped is None or intent_type is None:
        return None
    answers = shipped.action_answers.get(intent_type, {})
    states = [*response.matched_states, *response.unmatched_states]
    first = states[0] if states else None
    # The name slot is the device's own name, as the model picked it. "all" is not a
    # name to say back.
    speech_slots = {
        key: value["value"]
        for key, value in slots.items()
        if key in ("name", "area", "floor")
        and isinstance(value.get("value"), str)
        and value["value"] != "all"
    } | response.speech_slots
    for key in _response_keys(
        intent_type, slots, first.domain if first is not None else None
    ):
        text = answers.get(key)
        if text is None:
            continue
        # German says "{{ slots.name }} eingeschaltet". Rendered for a room, with no
        # name to put there, that is a sentence that starts with a blank.
        if any(name not in speech_slots for name in _SLOT_REFERENCE.findall(text)):
            continue
        try:
            rendered = template.Template(text, hass).async_render(
                {
                    "slots": speech_slots,
                    "state": template.TemplateState(hass, first) if first else None,
                    "query": {
                        "matched": [
                            template.TemplateState(hass, state)
                            for state in response.matched_states
                        ],
                        "unmatched": [
                            template.TemplateState(hass, state)
                            for state in response.unmatched_states
                        ],
                    },
                },
                parse_result=False,
            )
        except TemplateError as err:
            _LOGGER.debug(
                "the %s answer for %s did not render: %s", key, intent_type, err
            )
            continue
        sentence = " ".join(str(rendered).split())
        # A default that names the device from a table of domains renders a domain
        # missing from it as None. "Turned on None" is worse than "Done."
        if sentence and sentence.split()[-1] != "None":
            return sentence
    return None


# What a device is called in a short answer, by domain. A cover goes by its device
# class instead, so curtains are curtains and blinds are blinds.
_NOUNS = {
    "light": "light",
    "switch": "switch",
    "input_boolean": "switch",
    "fan": "fan",
    "cover": "cover",
    "valve": "valve",
    "lock": "lock",
}


def _noun(domain: str, states: Sequence[State]) -> str | None:
    """What these devices of one domain are called in a sentence, or None."""
    noun = _NOUNS.get(domain)
    if noun == "cover":
        classes = {state.attributes.get(ATTR_DEVICE_CLASS) for state in states}
        if len(classes) == 1 and isinstance(kind := classes.pop(), str):
            noun = kind.replace("_", " ")
    return noun


def _many(
    decision: Interpretation,
    acted: Sequence[State],
    snapshot: HomeSnapshot,
    lines: Mapping[str, str],
) -> str | None:
    """The agent's own sentence for one action run on each remembered device.

    lines holds the agent's lines and the status lines, which join the names.
    None for an action it has no sentence for. The reply is then the one Home
    Assistant's intent gave, and not "Done.", which only a reply with no sentence
    of its own falls back to.
    """
    if not acted:
        return None
    covers = all(state.domain == "cover" for state in acted)
    if decision.intent_type == "HassLightSet":
        if "brightness" not in decision.slots:
            return None
        key = "many_brightness"
    elif decision.action == "turn_on":
        key = "many_opening" if covers else "many_turned_on"
    elif decision.action == "turn_off":
        key = "many_closing" if covers else "many_turned_off"
    elif decision.action == "toggle":
        key = "many_toggled"
    else:
        return None
    rooms = {
        described.area if (described := snapshot.by_id(state.entity_id)) else None
        for state in acted
    }
    # One room in scope goes unsaid, as in a status answer: "shelf and study table".
    area_names = [room] if len(rooms) == 1 and (room := rooms.pop()) else []
    names = [
        status.short_name(
            state.name, area_names, _noun(state.domain, [state]) or state.domain
        )
        for state in acted
    ]
    level = decision.slots.get("brightness", {}).get("value", "")
    return lines[key].format(names=status.spoken_list(names, lines), level=level)


def _status_answer(
    decision: Interpretation,
    response: ha_intent.IntentResponse,
    lines: Mapping[str, str],
) -> tuple[str, list[State]] | None:
    """The short answer to a state question and the devices it is about, or None
    to keep the full list.

    None for a question about one named device, for "what is the status", for a
    state Jev was not sure was asked, and for devices whose state has no yes or no
    in it. Each of those keeps the sentence every state question had before.
    """
    if (
        decision.shape in (None, status.Shape.ONE, status.Shape.LIST)
        or decision.asked_state is None
    ):
        return None
    # A device named on its own is the one-device answer, word for word.
    if "name" in decision.slots:
        return None
    shape = status.Shape(decision.shape)
    asked = status.Bucket(decision.asked_state)
    states = [*response.matched_states, *response.unmatched_states]
    if not status.fits(asked, states):
        return None
    # Nothing in scope is said as that, not as a no: "I don't see any fans".
    domains = {state.domain for state in states} or set(
        decision.slots.get("domain", {}).get("value", [])
    )
    if len(domains) != 1 or (noun := _noun(domains.pop(), states)) is None:
        return None
    if decision.rooms:
        rooms = decision.rooms
    elif "area" in decision.slots:
        rooms = [decision.slots["area"]["value"]]
    else:
        rooms = []
    return (
        status.answer(shape, asked, states, noun=noun, area_names=rooms, lines=lines),
        status.about(shape, asked, states),
    )


async def _speak_the_answer(
    hass: HomeAssistant,
    response: ha_intent.IntentResponse,
    intent_type: str,
    language: str,
    say: dict[str, str],
) -> None:
    """Say what a state question found, in the language the pipeline is speaking.

    `HassGetState` fills in the matched states and stops. The spoken sentence is
    normally written by the default agent's response templates, which run only for
    sentences the default agent itself matched, so routing the intent here leaves a
    correct answer nobody hears. This renders the same template the default agent
    would have used. An English sentence is the last resort, for a language the
    intents package does not carry.
    """
    if response.response_type is not ha_intent.IntentResponseType.QUERY_ANSWER:
        return
    if response.speech:
        return
    matched = response.matched_states
    if not matched:
        response.async_set_speech(say["query_not_found"])
        return
    if intent_type == ha_intent.INTENT_GET_TEMPERATURE:
        spoken = await _render_temperature_answer(hass, matched[0], language)
        if spoken is None:
            temperature = matched[0].attributes.get("current_temperature")
            spoken = f"{matched[0].state if temperature is None else temperature} degrees"
        response.async_set_speech(spoken)
        return
    spoken = await _render_state_answer(
        hass, list(matched), list(response.unmatched_states), language
    )
    if spoken is None:
        spoken = ", ".join(f"{state.name} is {state.state}" for state in matched) + "."
    response.async_set_speech(spoken)
