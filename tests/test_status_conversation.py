"""Status answers end to end, through conversation.async_converse.

Drop into tests/ of the fork. Uses the fork's own conftest (mock_client,
config_entry, build_response). Asserts what the user hears and what Jev was asked,
never which helper ran.

New Jev questions this assumes (added to build_questions, same single call):
  "shape"        Choice: one | any | all | which | how_many | list
  "asked_state"  Choice: on | off | open | closed | locked | unlocked | none
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent as ha_intent
from homeassistant.setup import async_setup_component
from jevclient import ChoiceAnswer, NoulAnswer

from custom_components.jev.const import CONF_FALLBACK_AGENT

from .conftest import build_response

AGENT = "conversation.jev"
FALLBACK = "conversation.home_assistant"  # stands in for the Sonnet agent
NONE_CHOICE = "none_of_these"


def c(choice, confidence=0.95):
    return ChoiceAnswer(choice=choice, probabilities={}, confidence=confidence)


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
        "shape": c("any"),
        "asked_state": c("on"),
    }
    base.update(overrides)
    return base


@pytest.fixture
async def home(hass, mock_client, config_entry):
    for component in ("homeassistant", "conversation", "light", "cover", "fan"):
        assert await async_setup_component(hass, component, {})
    areas = ar.async_get(hass)
    reg = er.async_get(hass)
    rooms = {
        n: areas.async_get_or_create(n)
        for n in ("Kids Room", "Dining Room", "Living Room", "Study Room")
    }
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
        # In the room but not exposed: must never be counted or named.
        ("light.kids_hidden", "Kids Room Hidden Light", "Kids Room", "on", {}, False),
        ("light.dining_pendant", "Dining Room Pendant", "Dining Room", "off", {}, True),
        ("light.living_lamp", "Living Room Lamp", "Living Room", "on", {}, True),
        (
            "cover.study_main",
            "Study Room Main Curtain",
            "Study Room",
            "open",
            {"device_class": "curtain", "current_position": 40, "supported_features": 15},
            True,
        ),
        (
            "cover.study_sheer",
            "Study Room Sheer Curtain",
            "Study Room",
            "closed",
            {"device_class": "curtain", "current_position": 0, "supported_features": 15},
            True,
        ),
        ("fan.kids", "Kids Room Fan", "Kids Room", "on", {}, True),
        ("fan.study", "Study Room Fan", "Study Room", "on", {}, True),
        ("fan.dining", "Dining Room Fan", "Dining Room", "off", {}, True),
    )
    for entity_id, name, room, state, attrs, exposed in rows:
        domain, object_id = entity_id.split(".")
        entry = reg.async_get_or_create(
            domain, "demo", object_id, suggested_object_id=object_id
        )
        reg.async_update_entity(entry.entity_id, name=name, area_id=rooms[room].id)
        hass.states.async_set(entity_id, state, {"friendly_name": name, **attrs})
        async_expose_entity(hass, conversation.DOMAIN, entity_id, exposed)

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    async def asked_only(state, questions):
        full = mock_client.ask.return_value
        mock_client.questions_seen = questions
        return replace(
            full, answers={k: v for k, v in full.answers.items() if k in questions}
        )

    mock_client.ask.side_effect = asked_only
    for service in ("open_cover", "close_cover"):
        hass.services.async_register("cover", service, lambda call: None)
    return config_entry


async def say(hass, text):
    result = await conversation.async_converse(
        hass, text, None, Context(), language="en", agent_id=AGENT
    )
    return result.response.speech["plain"]["speech"]


# --- the reported symptoms -----------------------------------------------------


async def test_any_lights_on_in_a_room_answers_yes_and_names_only_those_on(
    hass, home, mock_client
):
    mock_client.ask.return_value = build_response(**asked())
    spoken = await say(hass, "are any lights on in the kids room")
    assert spoken.startswith("Yes, two: ")
    assert "shelf" in spoken and "study table" in spoken
    assert "ceiling" not in spoken.casefold()
    # Unreachable is mentioned, never folded into the yes or the no.
    assert "night lamp" in spoken.casefold()


async def test_an_unexposed_light_is_never_counted(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked())
    spoken = await say(hass, "are any lights on in the kids room")
    assert "hidden" not in spoken.casefold()
    assert "three" not in spoken.casefold()


async def test_two_rooms_joined_by_or_are_both_answered(hass, home, mock_client):
    """Jev's area question has one answer. The second room comes from the text."""
    mock_client.ask.return_value = build_response(**asked(area=c("Dining Room", 0.7)))
    spoken = await say(hass, "are any lights on in the dining or living room")
    assert spoken.startswith("Yes")
    assert "living room lamp" in spoken.casefold()


async def test_a_room_name_inside_a_device_name_does_not_widen_the_scope(
    hass, home, mock_client
):
    """'study table' must not pull in the Study Room."""
    mock_client.ask.return_value = build_response(**asked())
    spoken = await say(hass, "is the study table light on in the kids room")
    assert "curtain" not in spoken.casefold()


@pytest.mark.parametrize(
    ("action", "verb"), [("turn_on", "Opening"), ("turn_off", "Closing")]
)
async def test_curtains_in_a_room_never_say_none(hass, home, mock_client, action, verb):
    mock_client.ask.return_value = build_response(
        **asked(action=c(action), domain=c("cover"), area=c("Study Room"))
    )
    spoken = await say(hass, f"{action.replace('_', ' ')} the study room curtains")
    assert "None" not in spoken
    assert spoken.startswith(verb)


# --- the requested examples ----------------------------------------------------


async def test_which_curtains_are_closed(hass, home, mock_client):
    """House-wide. Today the whole-house gate refuses this: it is meant for actions."""
    mock_client.ask.return_value = build_response(
        **asked(
            area=c("none_of_these", 0.9),
            target_type=c("everything"),
            domain=c("cover"),
            shape=c("which"),
            asked_state=c("closed"),
        )
    )
    spoken = await say(hass, "which curtains are closed")
    assert "sheer" in spoken.casefold()
    assert "main" not in spoken.casefold()


async def test_are_all_curtains_closed_names_the_partly_open_one(hass, home, mock_client):
    mock_client.ask.return_value = build_response(
        **asked(
            area=c("Study Room"),
            domain=c("cover"),
            shape=c("all"),
            asked_state=c("closed"),
        )
    )
    spoken = await say(hass, "are all the study room curtains closed")
    assert spoken.startswith("No.")
    assert "main" in spoken.casefold() and "40" in spoken


async def test_how_many_fans_are_running(hass, home, mock_client):
    mock_client.ask.return_value = build_response(
        **asked(
            area=c("none_of_these", 0.9),
            target_type=c("everything"),
            domain=c("fan"),
            shape=c("how_many"),
            asked_state=c("on"),
        )
    )
    assert await say(hass, "how many fans are running") == "Two fans are on."


async def test_asking_for_detail_keeps_the_full_list(hass, home, mock_client):
    mock_client.ask.return_value = build_response(
        **asked(shape=c("list"), asked_state=c("none", 0.8))
    )
    spoken = await say(hass, "tell me the status of every light in the kids room")
    for word in ("shelf", "study table", "ceiling"):
        assert word in spoken.casefold()


async def test_lexical_cue_overrides_an_unsure_shape(hass, home, mock_client):
    """'how many' in the text beats a 0.5 'any' from the model."""
    mock_client.ask.return_value = build_response(
        **asked(
            area=c("none_of_these", 0.9),
            target_type=c("everything"),
            domain=c("fan"),
            shape=c("any", 0.5),
            asked_state=c("on"),
        )
    )
    assert await say(hass, "how many fans are running") == "Two fans are on."


# --- what must not change ------------------------------------------------------


async def test_a_status_question_is_still_one_jev_call(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked())
    await say(hass, "are any lights on in the kids room")
    # Setup's probe is a call too; count only the conversation's own.
    asks = [a for a in mock_client.ask.await_args_list if "probe" not in a.args[1]]
    assert len(asks) == 1
    assert {"shape", "asked_state"} <= set(mock_client.questions_seen)


async def test_usage_is_recorded_for_a_status_question(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked())
    await say(hass, "are any lights on in the kids room")
    await hass.async_block_till_done()
    calls = hass.states.get("sensor.jev_calls_today")
    assert calls is not None and int(calls.state) >= 1


async def test_an_unclear_status_question_still_goes_to_the_fallback_agent(
    hass, home, mock_client
):
    hass.config_entries.async_update_entry(home, options={CONF_FALLBACK_AGENT: FALLBACK})
    await hass.async_block_till_done()
    mock_client.ask.return_value = build_response(
        **asked(action=c("get_state", 0.3), shape=c("any", 0.4), asked_state=c("on", 0.4))
    )
    with patch(
        "custom_components.jev.conversation.conversation.async_converse",
        wraps=conversation.async_converse,
    ) as handed_over:
        await say(hass, "anything weird going on with the lights")
    handovers = [
        x for x in handed_over.await_args_list if x.kwargs.get("agent_id") != AGENT
    ]
    assert len(handovers) == 1
    assert handovers[0].kwargs["agent_id"] == FALLBACK


async def test_one_named_device_question_is_unchanged(hass, home, mock_client):
    """The shipped 'X is on' sentence stays for a one-device question.

    Today's exact output, capitalisation included (the template's | capitalize
    lowers the rest of the name). Pinned so the new path leaves it alone.
    """
    mock_client.ask.return_value = build_response(
        **asked(
            target_type=c("entity"), entity=c("light.kids_shelf", 1.0), shape=c("one")
        )
    )
    assert (
        await say(hass, "is the kids room shelf light on")
        == "Kids room shelf light is on"
    )


# --- where the short answer stands aside ---------------------------------------


async def test_another_language_keeps_the_full_list(hass, home, mock_client):
    """The number words and plurals are English, so Dutch hears every light."""
    mock_client.ask.return_value = build_response(**asked())
    result = await conversation.async_converse(
        hass,
        "zijn er lampen aan in de kinderkamer",
        None,
        Context(),
        language="nl",
        agent_id=AGENT,
    )
    spoken = result.response.speech["plain"]["speech"]
    assert not spoken.startswith("Yes")
    assert "ceiling" in spoken.casefold()


async def test_a_state_that_does_not_fit_the_devices_keeps_the_full_list(
    hass, home, mock_client
):
    """'On' is not a thing a curtain is, so Jev's asked state was a misread."""
    mock_client.ask.return_value = build_response(
        **asked(area=c("Study Room"), domain=c("cover"), asked_state=c("on"))
    )
    spoken = await say(hass, "are any study room curtains on")
    assert "main curtain" in spoken.casefold()
    assert "sheer curtain" in spoken.casefold()


async def test_two_kinds_of_device_keep_the_full_list(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked(domain=c(NONE_CHOICE, 0.9)))
    spoken = await say(hass, "is anything on in the kids room")
    assert "fan" in spoken.casefold() and "shelf" in spoken.casefold()


async def test_a_second_room_with_nothing_of_the_kind_is_left_out(
    hass, home, mock_client
):
    mock_client.ask.return_value = build_response(
        **asked(area=c("Dining Room", 0.7), domain=c("fan"))
    )
    spoken = await say(hass, "are any fans on in the dining or living room")
    assert spoken == "No, all fans are off."


async def test_two_rooms_with_nothing_of_the_kind_say_so(hass, home, mock_client):
    """No curtains at all is not "no curtains are open"."""
    mock_client.ask.return_value = build_response(
        **asked(area=c("Dining Room", 0.7), domain=c("cover"), asked_state=c("open"))
    )
    spoken = await say(hass, "are any curtains open in the dining or living room")
    assert spoken == "I don't see any covers."


def refusing(*rooms):
    """async_handle, with a match failure for the rooms given."""
    real = ha_intent.async_handle

    async def handle(hass, platform, intent_type, slots, *args, **kwargs):
        if slots.get("area", {}).get("value") in rooms:
            raise ha_intent.MatchFailedError(
                result=ha_intent.MatchTargetsResult(
                    False, ha_intent.MatchFailedReason.AREA
                ),
                constraints=ha_intent.MatchTargetsConstraints(),
            )
        return await real(hass, platform, intent_type, slots, *args, **kwargs)

    return patch(
        "custom_components.jev.conversation.ha_intent.async_handle", side_effect=handle
    )


async def test_a_room_the_intent_refuses_is_left_out(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked(area=c("Dining Room", 0.7)))
    with refusing("Dining Room"):
        spoken = await say(hass, "are any lights on in the dining or living room")
    assert spoken == "Yes, one: living room lamp."


async def test_every_room_refused_falls_back(hass, home, mock_client):
    mock_client.ask.return_value = build_response(**asked(area=c("Dining Room", 0.7)))
    with refusing("Dining Room", "Living Room"):
        result = await conversation.async_converse(
            hass,
            "are any lights on in the dining or living room",
            None,
            Context(),
            language="en",
            agent_id=AGENT,
        )
    assert result.response.response_type is ha_intent.IntentResponseType.ERROR


async def test_one_named_device_keeps_its_answer_whatever_the_shape(
    hass, home, mock_client
):
    """A shape answer of "any" for one named light is a misread, not a group."""
    mock_client.ask.return_value = build_response(
        **asked(target_type=c("entity"), entity=c("light.kids_shelf", 1.0))
    )
    assert await say(hass, "is the kids room shelf light on") == (
        "Kids room shelf light is on"
    )
