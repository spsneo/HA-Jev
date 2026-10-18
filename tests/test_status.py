"""Status answers: the deterministic layer between HassGetState and speech.

Target module: custom_components/jev/status.py (does not exist yet).
These are pure-function tests: State objects in, one English sentence out.
No Jev call, no Home Assistant instance, so they run in milliseconds and spend nothing.

Planned API (see the implementation plan):
    Shape            ONE | ANY | ALL | WHICH | HOW_MANY | LIST
    classify(state)  -> Reading(bucket, position)        # domain-aware meaning
    answer(shape, asked, states, *, noun, area_names) -> str
"""

import pytest
from homeassistant.core import State

from custom_components.jev.status import (
    Bucket,
    Shape,
    answer,
    classify,
    fits,
    short_name,
)


def light(object_id, state, name):
    return State(f"light.{object_id}", state, {"friendly_name": name})


def curtain(object_id, state, name, position=None, **extra):
    attrs = {"friendly_name": name, "device_class": "curtain", **extra}
    if position is not None:
        attrs["current_position"] = position
    return State(f"cover.{object_id}", state, attrs)


def fan(object_id, state, name):
    return State(f"fan.{object_id}", state, {"friendly_name": name})


def window(object_id, state, name):
    return State(
        f"binary_sensor.{object_id}",
        state,
        {"friendly_name": name, "device_class": "window"},
    )


KIDS = ["Kids Room"]


# --- classify: what a state means, per domain ---------------------------------


@pytest.mark.parametrize(
    ("state", "bucket", "position"),
    [
        (light("a", "on", "A"), Bucket.ON, None),
        (light("a", "off", "A"), Bucket.OFF, None),
        (light("a", "unavailable", "A"), Bucket.UNKNOWN, None),
        (light("a", "unknown", "A"), Bucket.UNKNOWN, None),
        (curtain("c", "closed", "C", 0), Bucket.CLOSED, 0),
        (curtain("c", "open", "C", 100), Bucket.OPEN, 100),
        # HA reports a 40% curtain as "open". It is not fully open and not closed.
        (curtain("c", "open", "C", 40), Bucket.PARTLY_OPEN, 40),
        (curtain("c", "opening", "C", 60), Bucket.MOVING, 60),
        # No position support: trust the state word.
        (curtain("c", "open", "C"), Bucket.OPEN, None),
        # binary_sensor window: on means open.
        (window("w", "on", "W"), Bucket.OPEN, None),
        (window("w", "off", "W"), Bucket.CLOSED, None),
        (fan("f", "on", "F"), Bucket.ON, None),
    ],
)
def test_classify_reads_the_domain_meaning(state, bucket, position):
    reading = classify(state)
    assert reading.bucket is bucket
    assert reading.position == position


# --- the requested examples ----------------------------------------------------


def test_any_on_none_on():
    states = [
        light("k1", "off", "Kids Room Shelf Light"),
        light("k2", "off", "Kids Room Ceiling Light"),
    ]
    assert (
        answer(Shape.ANY, Bucket.ON, states, noun="light", area_names=KIDS)
        == "No, all lights are off."
    )


def test_any_on_names_only_the_ones_on():
    states = [
        light("k1", "on", "Kids Room Shelf Light"),
        light("k2", "on", "Kids Room Study Table Light"),
        light("k3", "off", "Kids Room Ceiling Light"),
    ]
    assert (
        answer(Shape.ANY, Bucket.ON, states, noun="light", area_names=KIDS)
        == "Yes, two: shelf and study table."
    )


def test_any_on_does_not_say_all_off_when_one_is_unreachable():
    states = [
        light("k1", "off", "Kids Room Shelf Light"),
        light("k2", "unavailable", "Kids Room Night Lamp"),
    ]
    spoken = answer(Shape.ANY, Bucket.ON, states, noun="light", area_names=KIDS)
    assert spoken.startswith("No lights are on")
    assert "all" not in spoken
    assert "night lamp" in spoken.casefold()
    assert "can't reach" in spoken


def test_which_closed_lists_only_closed_curtains():
    states = [
        curtain("a", "closed", "Dining Room Sheer Curtain", 0),
        curtain("b", "open", "Dining Room Main Curtain", 100),
        curtain("c", "closed", "Master Bedroom Main Curtain", 0),
    ]
    spoken = answer(
        Shape.WHICH,
        Bucket.CLOSED,
        states,
        noun="curtain",
        area_names=["Dining Room", "Master Bedroom"],
    )
    assert "Dining Room Main" not in spoken
    assert "dining room sheer" in spoken.casefold()
    assert "master bedroom main" in spoken.casefold()


def test_which_closed_none():
    states = [curtain("a", "open", "Study Main Curtain", 100)]
    assert (
        answer(Shape.WHICH, Bucket.CLOSED, states, noun="curtain", area_names=[])
        == "None of the curtains are closed."
    )


def test_all_closed_yes():
    states = [
        window("w1", "off", "Bedroom Window"),
        window("w2", "off", "Kitchen Window"),
    ]
    assert (
        answer(Shape.ALL, Bucket.CLOSED, states, noun="window", area_names=[])
        == "Yes, both windows are closed."
    )


def test_all_closed_names_the_exceptions():
    states = [window("w1", "off", "Bedroom Window"), window("w2", "on", "Kitchen Window")]
    spoken = answer(Shape.ALL, Bucket.CLOSED, states, noun="window", area_names=[])
    assert spoken.startswith("No.")
    assert "kitchen" in spoken.casefold()
    assert "bedroom" not in spoken.casefold()


def test_all_closed_with_unknown_is_not_yes():
    states = [
        window("w1", "off", "Bedroom Window"),
        window("w2", "unavailable", "Kitchen Window"),
    ]
    spoken = answer(Shape.ALL, Bucket.CLOSED, states, noun="window", area_names=[])
    assert not spoken.startswith("Yes")
    assert "can't reach" in spoken


def test_partly_open_curtain_is_not_closed_and_says_how_far():
    states = [
        curtain("a", "open", "Study Main Curtain", 40),
        curtain("b", "closed", "Study Sheer Curtain", 0),
    ]
    spoken = answer(
        Shape.ALL, Bucket.CLOSED, states, noun="curtain", area_names=["Study Room"]
    )
    assert spoken.startswith("No.")
    assert "40" in spoken and "open" in spoken


def test_partly_open_counts_as_open_for_any_open():
    states = [curtain("a", "open", "Study Main Curtain", 40)]
    assert answer(
        Shape.ANY, Bucket.OPEN, states, noun="curtain", area_names=[]
    ).startswith("Yes")


def test_how_many_fans():
    states = [
        fan("f1", "on", "Kids Fan"),
        fan("f2", "on", "Study Fan"),
        fan("f3", "off", "Guest Fan"),
    ]
    assert (
        answer(Shape.HOW_MANY, Bucket.ON, states, noun="fan", area_names=[])
        == "Two fans are on."
    )


def test_how_many_zero():
    states = [fan("f1", "off", "Kids Fan")]
    assert (
        answer(Shape.HOW_MANY, Bucket.ON, states, noun="fan", area_names=[])
        == "No fans are on."
    )


def test_list_keeps_the_detailed_answer():
    """Asked for detail, every device and its state is still said."""
    states = [
        light("k1", "on", "Kids Room Shelf Light"),
        light("k2", "off", "Kids Room Ceiling Light"),
    ]
    spoken = answer(Shape.LIST, None, states, noun="light", area_names=KIDS)
    assert "shelf" in spoken.casefold() and "ceiling" in spoken.casefold()
    assert "on" in spoken and "off" in spoken


def test_long_lists_are_capped():
    states = [light(f"l{i}", "on", f"Living Room Lamp {i}") for i in range(9)]
    spoken = answer(
        Shape.WHICH, Bucket.ON, states, noun="light", area_names=["Living Room"]
    )
    assert "and 6 more" in spoken


def test_a_group_and_its_members_are_counted_once():
    group = State(
        "cover.living_room_sheer_curtains",
        "open",
        {
            "friendly_name": "Living Room Sheer Curtains",
            "entity_id": ["cover.s1", "cover.s2"],
            "current_position": 100,
        },
    )
    states = [
        group,
        curtain("s1", "open", "Living Room Sheer Curtain 1", 100),
        curtain("s2", "open", "Living Room Sheer Curtain 2", 100),
    ]
    assert (
        answer(
            Shape.HOW_MANY,
            Bucket.OPEN,
            states,
            noun="curtain",
            area_names=["Living Room"],
        )
        == "Two curtains are open."
    )


def test_nothing_matched_is_not_a_negative_answer():
    """No lights at all in the scope is different from no lights on."""
    spoken = answer(Shape.ANY, Bucket.ON, [], noun="light", area_names=KIDS)
    assert spoken == "I don't see any lights in the Kids Room."


@pytest.mark.parametrize(
    ("name", "areas", "noun", "expected"),
    [
        ("Kids Room Shelf Light", KIDS, "light", "shelf"),
        ("Kids Room Study Table Light", KIDS, "light", "study table"),
        # A name that is only the room and the noun keeps the room.
        ("Kids Room Light", KIDS, "light", "kids room light"),
        # Another room's device keeps its room, so a two-room answer is clear.
        ("Living Room Lamp", ["Dining Room", "Living Room"], "light", "living room lamp"),
    ],
)
def test_short_names(name, areas, noun, expected):
    assert short_name(name, areas, noun) == expected


# --- the cases around the examples --------------------------------------------


def lock(object_id, state, name):
    return State(f"lock.{object_id}", state, {"friendly_name": name})


@pytest.mark.parametrize(
    ("state", "bucket"),
    [
        (lock("l", "locked", "L"), Bucket.LOCKED),
        (lock("l", "unlocked", "L"), Bucket.UNLOCKED),
        (lock("l", "open", "L"), Bucket.UNLOCKED),
        (lock("l", "jammed", "L"), Bucket.JAMMED),
        (lock("l", "locking", "L"), Bucket.MOVING),
        (lock("l", "weird", "L"), Bucket.OTHER),
        (
            State("binary_sensor.d", "on", {"device_class": "lock"}),
            Bucket.UNLOCKED,
        ),
        (State("binary_sensor.m", "on", {"device_class": "motion"}), Bucket.ON),
        (State("cover.c", "stopped", {}), Bucket.OTHER),
        (State("media_player.tv", "playing", {}), Bucket.OTHER),
    ],
)
def test_classify_other_domains(state, bucket):
    assert classify(state).bucket is bucket


def test_a_position_that_is_not_a_number_is_ignored():
    assert classify(curtain("c", "open", "C", position="half")).position is None


def test_any_with_mixed_states_says_none_are():
    """A moving curtain is not closed, so "all curtains are closed" would be wrong."""
    states = [curtain("a", "closed", "A Curtain", 0), curtain("b", "opening", "B", 30)]
    spoken = answer(Shape.ANY, Bucket.OPEN, states, noun="curtain", area_names=[])
    assert spoken == "No curtains are open."


def test_any_says_a_moving_curtain_is_moving():
    states = [curtain("a", "closing", "A Curtain", 30)]
    spoken = answer(Shape.ALL, Bucket.OPEN, states, noun="curtain", area_names=[])
    assert spoken == "No. A curtain is closing."


def test_only_unreachable_devices_is_said_as_that():
    states = [light("k1", "unavailable", "Kids Room Night Lamp")]
    spoken = answer(Shape.ANY, Bucket.ON, states, noun="light", area_names=KIDS)
    assert spoken == "I can't reach the night lamp."


def test_nothing_in_the_whole_house():
    assert answer(Shape.ANY, Bucket.ON, [], noun="fan", area_names=[]) == (
        "I don't see any fans."
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "Yes, the light is on."),
        (3, "Yes, all three lights are on."),
    ],
)
def test_all_yes_counts(count, expected):
    states = [light(f"l{i}", "on", f"Lamp {i}") for i in range(count)]
    assert answer(Shape.ALL, Bucket.ON, states, noun="light", area_names=[]) == expected


def test_all_with_many_exceptions_counts_them():
    states = [light(f"l{i}", "off", f"Lamp {i}") for i in range(6)]
    spoken = answer(Shape.ALL, Bucket.ON, states, noun="light", area_names=[])
    assert spoken == "No. Lamp 0, lamp 1, lamp 2 and 3 more are not on."


def test_how_many_one():
    states = [fan("f1", "on", "Kids Fan"), fan("f2", "off", "Study Fan")]
    assert answer(Shape.HOW_MANY, Bucket.ON, states, noun="fan", area_names=[]) == (
        "One fan is on."
    )


def test_which_one_and_unreachable():
    states = [light("a", "on", "Desk"), light("b", "unknown", "Hall")]
    spoken = answer(Shape.WHICH, Bucket.ON, states, noun="light", area_names=[])
    assert spoken == "Desk is on. I can't reach the hall."


def test_a_question_with_no_state_lists():
    states = [light("a", "on", "Desk")]
    assert answer(Shape.ANY, None, states, noun="light", area_names=[]) == "Desk is on."


def test_switches_are_plural_with_es():
    states = [State("switch.a", "off", {"friendly_name": "Kettle"})]
    spoken = answer(Shape.HOW_MANY, Bucket.ON, states, noun="switch", area_names=[])
    assert spoken == "No switches are on."


def test_the_wording_can_be_passed_in():
    states = [fan("f1", "on", "Kids Fan")]
    spoken = answer(
        Shape.HOW_MANY,
        Bucket.ON,
        states,
        noun="fan",
        area_names=[],
        lines={"status_one_is": "{count} {noun} runs."},
    )
    assert spoken == "One fan runs."


@pytest.mark.parametrize(
    ("asked", "states", "expected"),
    [
        (Bucket.ON, [light("a", "on", "A"), light("b", "unavailable", "B")], True),
        (Bucket.OPEN, [curtain("c", "opening", "C", 30)], True),
        (Bucket.LOCKED, [lock("l", "jammed", "L")], True),
        # On is not a thing a curtain is.
        (Bucket.ON, [curtain("c", "open", "C", 100)], False),
        (Bucket.ON, [State("media_player.tv", "playing", {})], False),
    ],
)
def test_the_asked_state_fits_the_devices(asked, states, expected):
    assert fits(asked, states) is expected
