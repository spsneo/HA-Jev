"""Follow-ups end to end, through conversation.async_converse.

"Which lights are on?" then "turn them off": the second sentence acts on what the
first answer was about, in the same conversation, within five minutes. Asserts what
the user hears, which services ran on which devices, and what Jev was asked.
"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import intent as ha_intent
from homeassistant.setup import async_setup_component
from jevclient import ChoiceAnswer, NoulAnswer
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jev.const import CONF_FALLBACK_AGENT

from .conftest import build_response

AGENT = "conversation.jev"
NOT_UNDERSTOOD = "Sorry, I did not understand that."


def c(choice, confidence=0.95, probabilities=None):
    return ChoiceAnswer(
        choice=choice, probabilities=probabilities or {}, confidence=confidence
    )


def asked(**overrides):
    """A confident status question about the Kids Room lights."""
    base = {
        "action": c("get_state"),
        "compound": NoulAnswer(noul=0.02),
        "free_text": NoulAnswer(noul=0.01),
        "later": NoulAnswer(noul=0.03),
        "part": NoulAnswer(noul=0.04),
        "except": NoulAnswer(noul=0.03),
        "plural": NoulAnswer(noul=0.03),
        "bright": NoulAnswer(noul=0.01),
        "target_type": c("area", 0.9),
        "entity": c("none_of_these", 0.9),
        "area": c("Kids Room"),
        "domain": c("light"),
        "shape": c("which"),
        "asked_state": c("on"),
        "refers_back": NoulAnswer(noul=0.05),
        "same_again": NoulAnswer(noul=0.05),
    }
    base.update(overrides)
    return build_response(**base)


def act(action):
    return action if isinstance(action, ChoiceAnswer) else c(action)


def named(entity_id, action="turn_on", **overrides):
    """A confident command for one named device."""
    fields = {
        "action": act(action),
        "target_type": c("entity", 0.9),
        "entity": c(entity_id, 1.0),
        "area": c("none_of_these", 0.9),
        "domain": c("none_of_these", 0.9),
        "shape": c("list", 0.5),
        "asked_state": c("none", 0.9),
    }
    fields.update(overrides)
    return asked(**fields)


def follow(action="turn_off", **overrides):
    """A sentence with no target of its own: "turn them off"."""
    fields = {
        "action": act(action),
        "target_type": c("none_of_these", 0.9),
        "entity": c("none_of_these", 0.9),
        "area": c("none_of_these", 0.9),
        "domain": c("none_of_these", 0.9),
        "shape": c("list", 0.5),
        "asked_state": c("none", 0.9),
        "refers_back": NoulAnswer(noul=0.9),
    }
    fields.update(overrides)
    return asked(**fields)


@pytest.fixture
async def home(hass, mock_client, config_entry):
    for component in ("homeassistant", "conversation", "light", "cover"):
        assert await async_setup_component(hass, component, {})
    areas = ar.async_get(hass)
    rooms = {
        n: areas.async_get_or_create(n)
        for n in ("Kids Room", "Living Room", "Study Room", "Kitchen", "Office")
    }
    curtain = {"device_class": "curtain", "supported_features": 15}
    rows = (
        ("light.kids_shelf", "Kids Room Shelf Light", "Kids Room", "on", {}, True),
        (
            "light.kids_study_table",
            "Kids Room Study Table Light",
            "Kids Room",
            "on",
            {},
            True,
        ),
        ("light.kids_ceiling", "Kids Room Ceiling Light", "Kids Room", "off", {}, True),
        (
            "light.kids_night",
            "Kids Room Night Lamp",
            "Kids Room",
            "unavailable",
            {},
            True,
        ),
        # In the room but not exposed: never named, never acted on.
        ("light.kids_hidden", "Kids Room Hidden Light", "Kids Room", "on", {}, False),
        ("light.living_lamp", "Living Room Lamp", "Living Room", "on", {}, True),
        ("light.kitchen", "Kitchen Light", "Kitchen", "off", {}, True),
        ("light.office", "Office Light", "Office", "on", {}, True),
        (
            "cover.study_main",
            "Study Room Main Curtain",
            "Study Room",
            "open",
            {**curtain, "current_position": 40},
            True,
        ),
        (
            "cover.study_sheer",
            "Study Room Sheer Curtain",
            "Study Room",
            "closed",
            {**curtain, "current_position": 0},
            True,
        ),
    )
    for entity_id, name, room, state, attrs, exposed in rows:
        add(hass, entity_id, name, rooms[room].id, state, attrs, exposed)

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    async def asked_only(state, questions):
        full = mock_client.ask.return_value
        mock_client.questions_seen = questions
        mock_client.state_seen = state
        return replace(
            full, answers={k: v for k, v in full.answers.items() if k in questions}
        )

    mock_client.ask.side_effect = asked_only
    return config_entry


def add(hass, entity_id, name, area_id, state, attrs=None, exposed=True):
    domain, object_id = entity_id.split(".")
    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        domain, "demo", object_id, suggested_object_id=object_id
    )
    reg.async_update_entity(entry.entity_id, name=name, area_id=area_id)
    hass.states.async_set(entity_id, state, {"friendly_name": name, **(attrs or {})})
    async_expose_entity(hass, conversation.DOMAIN, entity_id, exposed)


@pytest.fixture
def calls(hass, home):
    """Every light and cover service call, as (service, entity id, data)."""
    seen = []

    def record(call):
        ids = call.data.get("entity_id", [])
        for entity_id in [ids] if isinstance(ids, str) else ids:
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            seen.append((f"{call.domain}.{call.service}", entity_id, data))

    for domain, service in (
        ("light", "turn_on"),
        ("light", "turn_off"),
        ("cover", "open_cover"),
        ("cover", "close_cover"),
    ):
        hass.services.async_register(domain, service, record)
    return seen


async def turn(hass, text, conversation_id=None):
    return await conversation.async_converse(
        hass, text, conversation_id, Context(), language="en", agent_id=AGENT
    )


def spoken(result):
    return result.response.speech["plain"]["speech"]


def touched(calls, service=None):
    return {e for s, e, _ in calls if service is None or s == service}


def jev_calls(mock_client):
    return [a for a in mock_client.ask.await_args_list if "probe" not in a.args[1]]


# --- the table of the design ---------------------------------------------------


async def test_turn_them_off_after_which_lights_are_on(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")
    assert spoken(first).startswith("Shelf and study table are on.")

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", first.conversation_id)

    assert touched(calls, "light.turn_off") == {
        "light.kids_shelf",
        "light.kids_study_table",
    }
    assert touched(calls) == touched(calls, "light.turn_off")
    assert spoken(second) == "Turned off shelf and study table."


async def test_turn_it_off_after_turn_it_on(hass, home, calls, mock_client):
    mock_client.ask.return_value = named("light.kitchen")
    first = await turn(hass, "turn on the kitchen light")
    assert touched(calls, "light.turn_on") == {"light.kitchen"}
    calls.clear()

    mock_client.ask.return_value = follow()
    await turn(hass, "turn it off", first.conversation_id)
    assert touched(calls) == {"light.kitchen"}
    assert touched(calls, "light.turn_off") == {"light.kitchen"}


async def test_set_it_to_40_after_turn_it_on(hass, home, calls, mock_client):
    mock_client.ask.return_value = named("light.kitchen")
    first = await turn(hass, "turn on the kitchen light")
    calls.clear()

    mock_client.ask.return_value = follow("set_brightness")
    await turn(hass, "set it to 40%", first.conversation_id)
    assert calls == [("light.turn_on", "light.kitchen", {"brightness_pct": 40})]


@pytest.mark.parametrize(
    ("text", "again"),
    [
        ("and the living room?", 0.9),
        # A transcript with no "the", and a follow-up answer under the floor that a
        # sentence with no cue needs: still the same short question.
        ("and in living room", 0.7),
    ],
)
async def test_and_the_living_room(hass, home, calls, mock_client, text, again):
    mock_client.ask.return_value = asked(shape=c("any"))
    first = await turn(hass, "are any lights on in the kids room")
    assert spoken(first).startswith("Yes, two: shelf and study table.")

    mock_client.ask.return_value = asked(
        action=c("turn_on", 0.4),
        area=c("Living Room"),
        domain=c("none_of_these", 0.9),
        shape=c("list", 0.5),
        asked_state=c("none", 0.9),
        same_again=NoulAnswer(noul=again),
    )
    second = await turn(hass, text, first.conversation_id)
    assert spoken(second) == "Yes, one: lamp."
    assert calls == []


@pytest.mark.parametrize(
    ("first_text", "first_answer"),
    [
        (
            "turn off the lights in the kitchen",
            asked(action=c("turn_off"), area=c("Kitchen"), shape=c("list", 0.5)),
        ),
        ("set the shelf to 40%", named("light.kids_shelf", "set_brightness")),
        ("turn on the kitchen light at 50%", named("light.kitchen")),
    ],
)
async def test_and_the_office_after_a_command_changes_nothing(
    hass, home, calls, mock_client, first_text, first_answer
):
    # The kitchen case proves the guard. The brightness cases also fall back for want
    # of a level, and pin that a level is not carried over from the record.
    # A command is not repeated for another room. The sentence goes on as it did
    # before memory, and here it is not sure of an action, so nothing changes.
    mock_client.ask.return_value = first_answer
    first = await turn(hass, first_text)
    assert calls
    calls.clear()

    mock_client.ask.return_value = asked(
        action=c("turn_on", 0.4),
        area=c("Office"),
        domain=c("none_of_these", 0.9),
        shape=c("list", 0.5),
        asked_state=c("none", 0.9),
        same_again=NoulAnswer(noul=0.9),
    )
    second = await turn(hass, "and the office", first.conversation_id)
    assert calls == []
    assert spoken(second) == NOT_UNDERSTOOD


async def test_open_it_after_which_curtains_are_closed(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked(
        target_type=c("everything"),
        area=c("none_of_these", 0.9),
        domain=c("cover"),
        asked_state=c("closed"),
    )
    first = await turn(hass, "which curtains are closed")
    assert "sheer" in spoken(first).casefold()

    mock_client.ask.return_value = follow("turn_on")
    await turn(hass, "open it", first.conversation_id)
    assert touched(calls) == {"cover.study_sheer"}
    assert touched(calls, "cover.open_cover") == {"cover.study_sheer"}


async def test_an_unrelated_question_does_not_use_memory(hass, home, calls, mock_client):
    mock_client.ask.return_value = named("light.kitchen")
    first = await turn(hass, "turn on the kitchen light")
    calls.clear()

    mock_client.ask.return_value = named(
        "light.office",
        action="get_state",
        shape=c("one"),
        refers_back=NoulAnswer(noul=0.1),
        same_again=NoulAnswer(noul=0.1),
    )
    second = await turn(hass, "is the office light on", first.conversation_id)
    # The questions were asked, because there is a record, and both said no.
    assert "refers_back" in mock_client.questions_seen
    assert spoken(second) == "Office light is on"
    assert calls == []


# --- when there is no memory to use -------------------------------------------


async def test_after_five_minutes_there_is_no_memory(
    hass, home, calls, mock_client, freezer
):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", first.conversation_id)
    assert "refers_back" not in mock_client.questions_seen
    assert "last_turn" not in mock_client.state_seen
    assert spoken(second) == NOT_UNDERSTOOD
    assert calls == []


async def test_another_conversation_has_no_memory(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", "another-satellite")
    assert "refers_back" not in mock_client.questions_seen
    assert spoken(second) == NOT_UNDERSTOOD
    assert calls == []


async def test_no_conversation_id_neither_crashes_nor_remembers(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room", None)
    # The reply carries the session's id, so a client can continue with it.
    assert first.conversation_id is not None

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", None)
    assert second.conversation_id not in (None, first.conversation_id)
    assert spoken(second) == NOT_UNDERSTOOD
    assert calls == []


async def test_a_record_puts_the_last_turn_in_the_request(hass, home, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")
    assert "refers_back" not in mock_client.questions_seen
    assert "last_turn" not in mock_client.state_seen

    mock_client.ask.return_value = follow()
    await turn(hass, "turn them off", first.conversation_id)
    assert {"refers_back", "same_again"} <= set(mock_client.questions_seen)
    assert mock_client.state_seen["last_turn"] == {
        "said": "which lights are on in the kids room",
        "action": "get_state",
        "about": ["Kids Room Shelf Light", "Kids Room Study Table Light"],
        "where": ["Kids Room"],
    }


# --- safety --------------------------------------------------------------------


async def test_a_device_unexposed_in_between_is_not_acted_on(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")
    async_expose_entity(hass, conversation.DOMAIN, "light.kids_study_table", False)

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf"}
    # The record is filtered before anything runs: one device left is one named
    # device, and it gets Home Assistant's own sentence.
    assert spoken(second) == "Turned off the light"


async def test_both_read_behaves_as_today(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(same_again=NoulAnswer(noul=0.9))
    second = await turn(hass, "turn them off", first.conversation_id)
    assert spoken(second) == NOT_UNDERSTOOD
    assert calls == []


async def test_the_whole_house_gate_still_asks_before_turn_them_on(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked(
        target_type=c("everything"), area=c("none_of_these", 0.9)
    )
    first = await turn(hass, "which lights are on")
    assert "living room lamp" in spoken(first).casefold()

    mock_client.ask.return_value = follow("turn_on")
    second = await turn(hass, "turn them on", first.conversation_id)
    assert spoken(second).startswith("That would affect the whole house.")
    assert calls == []


async def test_a_follow_up_is_one_jev_call(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")
    before = len(jev_calls(mock_client))

    mock_client.ask.return_value = follow()
    await turn(hass, "turn them off", first.conversation_id)
    assert len(jev_calls(mock_client)) == before + 1
    assert touched(calls) == {"light.kids_shelf", "light.kids_study_table"}


# --- which turns write a record ------------------------------------------------


async def test_a_fallback_turn_keeps_the_record(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(
        "none_of_these", refers_back=NoulAnswer(noul=0.05)
    )
    missed = await turn(hass, "play some music", first.conversation_id)
    assert spoken(missed) == NOT_UNDERSTOOD

    mock_client.ask.return_value = follow()
    await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf", "light.kids_study_table"}


async def test_a_fallback_between_keeps_the_earlier_record(
    hass, home, calls, mock_client
):
    """A misheard sentence after an action does not wipe what "it" was."""
    mock_client.ask.return_value = named("light.kitchen")
    first = await turn(hass, "turn on the kitchen light")
    calls.clear()

    mock_client.ask.return_value = follow(compound=NoulAnswer(noul=0.95))
    missed = await turn(hass, "turn it off and the other", first.conversation_id)
    assert spoken(missed) == NOT_UNDERSTOOD
    assert calls == []

    mock_client.ask.return_value = follow()
    await turn(hass, "turn it off", first.conversation_id)
    assert touched(calls, "light.turn_off") == {"light.kitchen"}


async def test_a_which_device_turn_keeps_the_record(hass, home, calls, mock_client):
    areas = ar.async_get(hass)
    add(
        hass,
        "light.kitchen_lamp",
        "Lamp",
        areas.async_get_area_by_name("Kitchen").id,
        "off",
    )
    add(
        hass,
        "light.office_lamp",
        "Lamp",
        areas.async_get_area_by_name("Office").id,
        "off",
    )

    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = named("light.kitchen_lamp")
    which = await turn(hass, "turn on the lamp", first.conversation_id)
    assert spoken(which).startswith("Do you mean")
    assert calls == []

    # The reply is read as a new command, since it picks neither lamp.
    mock_client.ask.return_value = follow()
    await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf", "light.kids_study_table"}


async def test_already_on_then_turn_it_off(hass, home, calls, mock_client):
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})
    mock_client.ask.return_value = named(
        "light.kitchen",
        action=c("turn_on", 0.3, {"turn_on": 0.45, "get_state": 0.4}),
    )
    first = await turn(hass, "turn on the kitchen light")
    assert spoken(first) == "Kitchen Light is already on."
    assert calls == []

    mock_client.ask.return_value = follow()
    await turn(hass, "turn it off", first.conversation_id)
    assert touched(calls, "light.turn_off") == {"light.kitchen"}


# --- what the reads and the replies say ----------------------------------------


async def test_are_they_still_on_reads_the_remembered_devices(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(
        "get_state", shape=c("all", 0.9), asked_state=c("on", 0.9)
    )
    second = await turn(hass, "are they still on", first.conversation_id)
    # The ceiling light in the same room is off, so a read of the room says no.
    assert spoken(second) == "Yes, both lights are on."


async def test_them_after_a_two_room_list_acts_in_both_rooms(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked(area=c("Kids Room", 0.9))
    first = await turn(hass, "which lights are on in the kids room or the living room")
    assert "living room lamp" in spoken(first).casefold()

    mock_client.ask.return_value = follow()
    second = await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls, "light.turn_off") == {
        "light.kids_shelf",
        "light.kids_study_table",
        "light.living_lamp",
    }
    assert spoken(second) == (
        "Turned off kids room shelf light, kids room study table light and "
        "living room lamp."
    )


async def test_several_curtains_get_the_agents_own_sentence(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = asked(
        area=c("Study Room"), domain=c("cover"), shape=c("list", 0.5)
    )
    first = await turn(hass, "what are the study room curtains doing")

    mock_client.ask.return_value = follow("turn_off")
    second = await turn(hass, "close them", first.conversation_id)
    assert touched(calls, "cover.close_cover") == {
        "cover.study_main",
        "cover.study_sheer",
    }
    assert spoken(second) == "Closing main and sheer."


async def test_set_them_to_40_names_each(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow("set_brightness")
    second = await turn(hass, "set them to 40%", first.conversation_id)
    assert sorted(calls) == [
        ("light.turn_on", "light.kids_shelf", {"brightness_pct": 40}),
        ("light.turn_on", "light.kids_study_table", {"brightness_pct": 40}),
    ]
    assert spoken(second) == "Set shelf and study table to 40%."


# --- the microphone ------------------------------------------------------------


async def test_a_status_answer_keeps_the_microphone_open(hass, home, mock_client):
    mock_client.ask.return_value = asked()
    result = await turn(hass, "which lights are on in the kids room")
    assert result.continue_conversation is True


async def test_an_action_does_not(hass, home, calls, mock_client):
    mock_client.ask.return_value = named("light.kitchen")
    result = await turn(hass, "turn on the kitchen light")
    assert touched(calls) == {"light.kitchen"}
    assert result.continue_conversation is False


async def test_a_full_list_does_not(hass, home, mock_client):
    mock_client.ask.return_value = asked(shape=c("list"), asked_state=c("none", 0.8))
    result = await turn(hass, "tell me the status of every light in the kids room")
    assert "ceiling" in spoken(result).casefold()
    assert result.continue_conversation is False


async def test_a_status_answer_naming_nothing_does_not(hass, home, mock_client):
    hass.states.async_set(
        "cover.study_sheer",
        "open",
        {
            "friendly_name": "Study Room Sheer Curtain",
            "device_class": "curtain",
            "supported_features": 15,
            "current_position": 100,
        },
    )
    mock_client.ask.return_value = asked(
        area=c("Study Room"), domain=c("cover"), asked_state=c("closed")
    )
    result = await turn(hass, "which curtains are closed in the study room")
    assert spoken(result) == "None of the curtains are closed."
    assert result.continue_conversation is False


# --- a device that fails ---------------------------------------------------------


def failing(*names):
    """async_handle, with a handle error for the devices named."""
    real = ha_intent.async_handle

    async def handle(hass, platform, intent_type, slots, *args, **kwargs):
        if slots.get("name", {}).get("value") in names:
            raise ha_intent.IntentHandleError(f"{names} cannot do that")
        return await real(hass, platform, intent_type, slots, *args, **kwargs)

    return patch(
        "custom_components.jev.conversation.ha_intent.async_handle", side_effect=handle
    )


async def test_a_device_that_fails_is_left_out(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow()
    with failing("Kids Room Study Table Light"):
        second = await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf"}
    assert spoken(second) == "Turned off shelf."


async def test_every_device_failing_says_it_did_not_work(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow()
    with failing("Kids Room Shelf Light", "Kids Room Study Table Light"):
        second = await turn(hass, "turn them off", first.conversation_id)
    assert calls == []
    assert spoken(second) == "Sorry, that did not work."
    assert second.continue_conversation is False


@pytest.mark.parametrize(
    ("action", "said"),
    [
        ("turn_on", "Turned on shelf and study table."),
        ("toggle", "Toggled shelf and study table."),
    ],
)
async def test_each_action_has_its_own_sentence(
    hass, home, calls, mock_client, action, said
):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(action)
    second = await turn(hass, "do that to them", first.conversation_id)
    assert spoken(second) == said


def test_an_action_with_no_sentence_of_its_own_is_left_to_home_assistant():
    from custom_components.jev.conversation import _FALLBACK, _many
    from custom_components.jev.interpret import Interpretation
    from custom_components.jev.snapshot import HomeSnapshot

    shelf = State("light.kids_shelf", "on", {"friendly_name": "Shelf"})
    read = Interpretation("HassGetState", {}, "get_state", 0.9, "ok", fallback=False)
    no_level = replace(read, intent_type="HassLightSet", action="set_brightness")
    assert _many(read, [shelf], HomeSnapshot(entities=[]), _FALLBACK) is None
    assert _many(no_level, [shelf], HomeSnapshot(entities=[]), _FALLBACK) is None
    assert _many(no_level, [], HomeSnapshot(entities=[]), _FALLBACK) is None


def test_the_reasoning_says_what_a_repeat_repeats():
    from custom_components.jev.conversation import _reasoning

    trace = {
        "action": "get_state",
        "reason": "ok",
        "confidence": 1.0,
        "slots": {},
        "follow_up": "same_again",
        "last_said": "are any lights on in the kids room",
        "input_tokens": 321,
        "latency_ms": 274.0,
    }
    shown = _reasoning(trace, build_response())
    assert 'Follow-up: same again as "are any lights on in the kids room"' in shown


async def test_an_unexpected_error_after_a_device_changed_is_left_out(
    hass, home, calls, mock_client
):
    """Once a device has changed, the turn never ends in an error."""
    real = ha_intent.async_handle

    async def handle(hass, platform, intent_type, slots, *args, **kwargs):
        if slots.get("name", {}).get("value") == "Kids Room Study Table Light":
            raise ha_intent.IntentUnexpectedError("the integration broke")
        return await real(hass, platform, intent_type, slots, *args, **kwargs)

    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow()
    with patch(
        "custom_components.jev.conversation.ha_intent.async_handle", side_effect=handle
    ):
        second = await turn(hass, "turn them off", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf"}
    assert spoken(second) == "Turned off shelf."
    calls.clear()

    # The record holds what changed, and only that.
    mock_client.ask.return_value = follow("turn_on")
    await turn(hass, "turn them back on", first.conversation_id)
    assert touched(calls) == {"light.kids_shelf"}


async def test_a_sure_floor_elsewhere_acts_on_that_floor(hass, home, calls, mock_client):
    floors = fr.async_get(hass)
    areas = ar.async_get(hass)
    for room, floor in (("Kids Room", "Upstairs"), ("Office", "Downstairs")):
        floor_id = (
            floors.async_get_floor_by_name(floor) or floors.async_create(floor)
        ).floor_id
        areas.async_update(areas.async_get_area_by_name(room).id, floor_id=floor_id)

    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(
        target_type=c("area", 0.95),
        floor=c("Downstairs"),
        domain=c("light"),
        refers_back=NoulAnswer(noul=0.85),
    )
    await turn(hass, "turn off the lights downstairs", first.conversation_id)
    assert touched(calls) == {"light.office"}


async def test_the_fallback_agent_keeps_its_own_conversation(
    hass, home, calls, mock_client
):
    """A client that starts with no id continues the fallback agent's session too."""
    hass.config_entries.async_update_entry(
        home, options={CONF_FALLBACK_AGENT: "conversation.home_assistant"}
    )
    await hass.async_block_till_done()
    handed: list[tuple[str | None, str | None]] = []
    real = conversation.async_converse

    async def converse(hass, text, conversation_id, *args, **kwargs):
        result = await real(hass, text, conversation_id, *args, **kwargs)
        if kwargs.get("agent_id") != AGENT:
            handed.append((conversation_id, result.conversation_id))
        return result

    mock_client.ask.return_value = follow("none_of_these", refers_back=NoulAnswer(0.05))
    with patch(
        "custom_components.jev.conversation.conversation.async_converse",
        side_effect=converse,
    ):
        first = await turn(hass, "play some music", None)
        second = await turn(hass, "play some music", first.conversation_id)

    assert first.conversation_id is not None
    assert second.conversation_id == first.conversation_id
    # Each handover joins this agent's session, so the fallback agent's own memory
    # lives under the id the client continues with.
    assert handed == [
        (first.conversation_id, first.conversation_id),
        (first.conversation_id, first.conversation_id),
    ]


async def test_a_sure_question_is_never_made_the_last_action(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = named("light.kitchen", action="turn_off")
    first = await turn(hass, "turn off the kitchen light")
    calls.clear()

    mock_client.ask.return_value = named(
        "light.living_lamp",
        action=c("get_state", 0.97),
        shape=c("one"),
        asked_state=c("on"),
        same_again=NoulAnswer(noul=0.85),
    )
    second = await turn(hass, "is the living room lamp on", first.conversation_id)
    assert calls == []
    assert hass.states.get("light.living_lamp").state == "on"
    assert spoken(second) == "Living room lamp is on"


async def test_turn_off_the_others_turns_off_nothing(hass, home, calls, mock_client):
    mock_client.ask.return_value = asked()
    first = await turn(hass, "which lights are on in the kids room")

    mock_client.ask.return_value = follow(refers_back=NoulAnswer(noul=0.95))
    second = await turn(hass, "turn off the others", first.conversation_id)
    assert calls == []
    assert spoken(second) == NOT_UNDERSTOOD


async def test_them_keeps_the_remembered_devices_inside_the_cap(
    hass, home, calls, mock_client
):
    # Four entities fit. "turn them off" names nothing, so ranked on its own words
    # the cap would keep the curtains and the other kids room lights and leave out
    # the two lights it means.
    with patch("custom_components.jev.conversation.MAX_CONVERSATION_ENTITIES", 4):
        mock_client.ask.return_value = asked()
        first = await turn(hass, "which lights are on in the kids room")
        assert spoken(first).startswith("Shelf and study table are on.")

        mock_client.ask.return_value = follow()
        second = await turn(hass, "turn them off", first.conversation_id)
        offered = {e["entity_id"] for e in mock_client.state_seen["entities"]}

    assert {"light.kids_shelf", "light.kids_study_table"} <= offered
    assert len(offered) == 4
    assert touched(calls) == {"light.kids_shelf", "light.kids_study_table"}
    assert spoken(second) == "Turned off shelf and study table."


def test_the_list_of_names_is_joined_in_the_pipelines_language():
    from custom_components.jev.conversation import _FALLBACK, _many
    from custom_components.jev.interpret import Interpretation
    from custom_components.jev.snapshot import HomeSnapshot

    acted = [
        State("light.kids_shelf", "off", {"friendly_name": "Shelf"}),
        State("light.kids_study_table", "off", {"friendly_name": "Study table"}),
    ]
    off = Interpretation("HassTurnOff", {}, "turn_off", 0.9, "ok", fallback=False)
    lines = {**_FALLBACK, "status_and": "{first} und {last}"}
    assert (
        _many(off, acted, HomeSnapshot(entities=[]), lines)
        == "Turned off shelf und study table."
    )


async def test_a_split_question_is_never_made_the_last_action(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = named("light.kitchen", action="turn_off")
    first = await turn(hass, "turn off the kitchen light")
    calls.clear()

    mock_client.ask.return_value = named(
        "light.living_lamp",
        action=c(
            "get_state",
            0.55,
            {"get_state": 0.55, "get_temperature": 0.42, "turn_off": 0.03},
        ),
        shape=c("one"),
        asked_state=c("on"),
        same_again=NoulAnswer(noul=0.85),
    )
    await turn(hass, "is the living room lamp on", first.conversation_id)
    assert calls == []
    assert hass.states.get("light.living_lamp").state == "on"


async def test_an_uncued_sentence_read_as_nothing_is_never_made_the_last_action(
    hass, home, calls, mock_client
):
    mock_client.ask.return_value = named("light.kitchen", action="turn_off")
    first = await turn(hass, "turn off the kitchen light")
    calls.clear()

    mock_client.ask.return_value = named(
        "light.living_lamp",
        action=c(
            "none_of_these",
            0.45,
            {"none_of_these": 0.45, "get_state": 0.4, "turn_off": 0.15},
        ),
        shape=c("one"),
        asked_state=c("on"),
        same_again=NoulAnswer(noul=0.85),
    )
    await turn(hass, "is the living room lamp on", first.conversation_id)
    assert calls == []
    assert hass.states.get("light.living_lamp").state == "on"
