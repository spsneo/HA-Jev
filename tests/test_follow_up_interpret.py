"""Reading a follow-up: "turn them off" and "and the living room?".

Calls build_questions and interpret directly, with mocked Jev answers and a
hand-built snapshot, so no Home Assistant and no request is needed.
"""

from jevclient import ChoiceAnswer, NoulAnswer

from custom_components.jev.interpret import (
    FOLLOW_UP_FLOOR,
    build_questions,
    interpret,
    refers_back_cue,
    same_again_cue,
)
from custom_components.jev.memory import Turn
from custom_components.jev.snapshot import ExposedEntity, HomeSnapshot

from .conftest import build_response

MIN = 0.6


def c(choice, confidence=0.95, probabilities=None):
    return ChoiceAnswer(
        choice=choice, probabilities=probabilities or {}, confidence=confidence
    )


def light(entity_id, name, area, state="on"):
    return ExposedEntity(
        entity_id=entity_id,
        name=name,
        domain="light",
        area=area,
        state=state,
        area_id=area.lower().replace(" ", "_"),
    )


SNAPSHOT = HomeSnapshot(
    entities=[
        light("light.shelf", "Shelf", "Kids Room"),
        light("light.study_table", "Study table", "Kids Room"),
        light("light.ceiling", "Ceiling", "Kids Room", "off"),
        light("light.lamp", "Lamp", "Living Room", "off"),
        ExposedEntity("fan.desk", "Desk fan", "fan", "Kids Room", "off", "kids_room"),
    ],
    areas=["Kids Room", "Living Room"],
)


def turn(devices=("light.shelf", "light.study_table"), **kw):
    fields = {
        "text": "which lights are on in the kids room",
        "action": "get_state",
        "intent_type": "HassGetState",
        "domains": ("light",),
        "where": ("Kids Room",),
        "everything": False,
        "devices": devices,
        "names": tuple(d.split(".")[1] for d in devices),
        "shape": "which",
        "asked_state": "on",
    }
    fields.update(kw)
    return Turn(**fields)


def answers(**overrides):
    """A sentence that names no target of its own."""
    base = {
        "action": c("turn_off"),
        "compound": NoulAnswer(noul=0.02),
        "free_text": NoulAnswer(noul=0.01),
        "later": NoulAnswer(noul=0.03),
        "part": NoulAnswer(noul=0.04),
        "except": NoulAnswer(noul=0.03),
        "plural": NoulAnswer(noul=0.03),
        "bright": NoulAnswer(noul=0.01),
        "target_type": c("none_of_these", 0.9),
        "entity": c("none_of_these", 0.9),
        "area": c("none_of_these", 0.9),
        "domain": c("none_of_these", 0.9),
        "shape": c("list", 0.5),
        "asked_state": c("none", 0.9),
        "refers_back": NoulAnswer(noul=0.05),
        "same_again": NoulAnswer(noul=0.05),
    }
    base.update(overrides)
    return build_response(**base)


def test_no_record_asks_nothing_new():
    questions = build_questions("turn them off", SNAPSHOT, 50)
    assert "refers_back" not in questions
    assert "same_again" not in questions


def test_a_record_adds_the_two_questions():
    questions = build_questions("turn them off", SNAPSHOT, 50, follow_up=True)
    assert "refers_back" in questions
    assert "same_again" in questions


def test_them_targets_the_remembered_devices():
    decision = interpret(
        answers(action=c("turn_off"), refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.intent_type == "HassTurnOff"
    assert decision.follow_up == "refers_back"
    assert [s["name"]["value"] for s in decision.each] == ["Shelf", "Study table"]
    assert decision.remembered == ["light.shelf", "light.study_table"]
    assert decision.slots["domain"]["value"] == ["light"]
    assert decision.targets_everything is False


def test_them_over_two_kinds_bounds_each_to_its_own():
    decision = interpret(
        answers(action=c("turn_off"), refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf", "fan.desk"), domains=("light", "fan")),
    )
    assert decision.slots["domain"]["value"] == ["light", "fan"]
    assert [s["domain"]["value"] for s in decision.each] == [["light"], ["fan"]]


def test_it_with_one_device_is_a_named_device():
    decision = interpret(
        answers(action=c("set_brightness"), refers_back=NoulAnswer(noul=0.9)),
        "set it to 40%",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf",)),
    )
    assert decision.intent_type == "HassLightSet"
    assert decision.slots["name"]["value"] == "Shelf"
    assert decision.slots["preferred_area_id"]["value"] == "kids_room"
    assert decision.slots["brightness"]["value"] == 40
    assert decision.each == []
    assert decision.remembered == ["light.shelf"]


def test_it_when_it_is_already_so_says_so():
    # "turn it on" with the remembered light already on reads as a low-confidence
    # turn_on, as any command that is already done does.
    decision = interpret(
        answers(
            action=c("turn_on", 0.3, {"turn_on": 0.45, "get_state": 0.4}),
            refers_back=NoulAnswer(noul=0.9),
        ),
        "turn it on",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf",)),
    )
    assert decision.already_satisfied == ("Shelf", "on")
    assert decision.already_entity == "light.shelf"
    assert decision.follow_up == "refers_back"
    assert decision.remembered == ["light.shelf"]


def test_them_with_an_unsure_action_is_refused():
    decision = interpret(
        answers(action=c("turn_off", 0.4), refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.should_fall_back
    assert decision.follow_up is None


def test_and_the_living_room_repeats_the_last_question():
    decision = interpret(
        answers(
            action=c("turn_on", 0.4),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "and the living room?",
        SNAPSHOT,
        MIN,
        last=turn(shape="any", asked_state="on"),
    )
    assert decision.intent_type == "HassGetState"
    assert decision.follow_up == "same_again"
    assert decision.slots["area"]["value"] == "Living Room"
    assert decision.slots["domain"]["value"] == ["light"]
    assert decision.shape == "any"
    assert decision.asked_state == "on"


def test_same_again_with_no_sure_target_goes_on_as_today():
    decision = interpret(
        answers(action=c("turn_on", 0.4), same_again=NoulAnswer(noul=0.9)),
        "and the same again",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.should_fall_back
    assert decision.follow_up is None


def test_a_cue_lifts_an_answer_under_the_floor():
    assert MIN < 0.7 < FOLLOW_UP_FLOOR
    lifted = interpret(
        answers(refers_back=NoulAnswer(noul=0.7)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert lifted.follow_up == "refers_back"
    not_lifted = interpret(
        answers(refers_back=NoulAnswer(noul=0.7)),
        "turn the shelf off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert not_lifted.follow_up is None


def test_a_cue_alone_never_acts():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.1)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up is None
    assert decision.should_fall_back


def test_both_read_is_neither():
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.9),
            same_again=NoulAnswer(noul=0.9),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
        ),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up is None
    assert decision.remembered == []


def test_no_record_reads_no_follow_up():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.95)),
        "turn them off",
        SNAPSHOT,
        MIN,
    )
    assert decision.follow_up is None
    assert decision.should_fall_back


def test_a_remembered_device_no_longer_exposed_is_dropped():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf", "light.gone", "light.study_table")),
    )
    assert [s["name"]["value"] for s in decision.each] == ["Shelf", "Study table"]
    assert decision.remembered == ["light.shelf", "light.study_table"]


def test_nothing_left_of_the_record_goes_on_as_today():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.gone",)),
    )
    assert decision.follow_up is None
    assert decision.should_fall_back


def test_them_after_a_house_wide_turn_targets_everything():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.9)),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(everything=True, where=()),
    )
    assert decision.targets_everything is True


def test_already_done_names_its_entity():
    decision = interpret(
        answers(
            action=c("turn_on", 0.3, {"turn_on": 0.45, "get_state": 0.4}),
            entity=c("light.shelf", 0.95),
            target_type=c("entity", 0.9),
        ),
        "turn on the shelf",
        SNAPSHOT,
        MIN,
    )
    assert decision.already_satisfied == ("Shelf", "on")
    assert decision.already_entity == "light.shelf"


def test_the_cues():
    assert refers_back_cue("Turn them off")
    assert refers_back_cue("switch that one on")
    assert refers_back_cue("turn that off")
    assert not refers_back_cue("turn the item off")
    assert same_again_cue("And the living room?")
    assert same_again_cue("and in the kitchen")
    assert same_again_cue("what about the office")
    assert same_again_cue("same for the hall")
    assert not same_again_cue("turn off the hall and the kitchen")


def test_a_level_for_them_leaves_out_what_is_not_a_light():
    decision = interpret(
        answers(action=c("set_brightness"), refers_back=NoulAnswer(noul=0.9)),
        "set them to 40%",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf", "fan.desk"), domains=("light", "fan")),
    )
    assert decision.slots["domain"]["value"] == ["light"]
    assert [s["name"]["value"] for s in decision.each] == ["Shelf"]
    assert decision.remembered == ["light.shelf"]


def test_a_level_for_remembered_fans_goes_to_the_fallback():
    fans = HomeSnapshot(
        entities=[
            ExposedEntity("fan.one", "Fan one", "fan", None, "on"),
            ExposedEntity("fan.two", "Fan two", "fan", None, "on"),
        ],
    )
    decision = interpret(
        answers(action=c("set_brightness"), refers_back=NoulAnswer(noul=0.9)),
        "set them to 40%",
        fans,
        MIN,
        last=turn(devices=("fan.one", "fan.two"), domains=("fan",)),
    )
    assert decision.should_fall_back


def test_and_the_living_room_for_a_temperature_repeats_it():
    decision = interpret(
        answers(
            action=c("turn_on", 0.4),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "and the living room?",
        SNAPSHOT,
        MIN,
        last=turn(action="get_temperature", intent_type="HassClimateGetTemperature"),
    )
    assert decision.intent_type == "HassClimateGetTemperature"
    assert decision.slots == {"area": {"value": "Living Room"}}
    assert decision.follow_up == "same_again"


def test_a_sentence_naming_its_own_device_is_not_a_follow_up():
    # "turn off the ceiling light" with a stray refers_back answer: the sure entity
    # answer names a device the last turn was not about.
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.9),
            target_type=c("entity", 0.95),
            entity=c("light.ceiling", 0.95),
        ),
        "turn that ceiling light off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up is None
    assert decision.remembered == []
    assert decision.slots["name"]["value"] == "Ceiling"


def test_a_sure_entity_among_the_remembered_keeps_the_follow_up():
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.9),
            target_type=c("entity", 0.95),
            entity=c("light.shelf", 0.95),
        ),
        "turn them off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up == "refers_back"
    assert decision.remembered == ["light.shelf", "light.study_table"]


def test_a_stricter_floor_holds_for_an_uncued_follow_up():
    strict = 0.9
    uncued = interpret(
        answers(refers_back=NoulAnswer(noul=0.85)),
        "switch the lot off",
        SNAPSHOT,
        strict,
        last=turn(),
    )
    assert uncued.follow_up is None
    cued = interpret(
        answers(refers_back=NoulAnswer(noul=0.85)),
        "turn them off",
        SNAPSHOT,
        strict,
        last=turn(),
    )
    assert cued.follow_up == "refers_back"


def test_one_device_left_of_a_house_wide_turn_is_not_the_whole_house():
    decision = interpret(
        answers(refers_back=NoulAnswer(noul=0.9)),
        "turn it off",
        SNAPSHOT,
        MIN,
        last=turn(devices=("light.shelf", "light.gone"), everything=True, where=()),
    )
    assert decision.follow_up == "refers_back"
    assert decision.targets_everything is False


ON_FLOORS = HomeSnapshot(
    entities=SNAPSHOT.entities,
    areas=SNAPSHOT.areas,
    floors=["Downstairs", "Upstairs"],
    area_floors={"Kids Room": "Upstairs", "Living Room": "Downstairs"},
)


def test_a_sure_room_without_the_remembered_devices_is_not_a_follow_up():
    for text, back in (
        ("turn off both living room lights", 0.7),  # cued by "both"
        ("turn off the living room lights", 0.85),
    ):
        decision = interpret(
            answers(
                refers_back=NoulAnswer(noul=back),
                target_type=c("area", 0.95),
                area=c("Living Room", 0.95),
            ),
            text,
            SNAPSHOT,
            MIN,
            last=turn(),
        )
        assert decision.follow_up is None, text
        assert decision.each == [], text
        assert decision.slots["area"]["value"] == "Living Room", text


def test_a_sure_room_holding_the_remembered_devices_keeps_the_follow_up():
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.9),
            target_type=c("area", 0.95),
            area=c("Kids Room", 0.95),
        ),
        "turn them off in the kids room",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up == "refers_back"
    assert decision.remembered == ["light.shelf", "light.study_table"]


def test_a_sure_floor_without_the_remembered_devices_is_not_a_follow_up():
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.85),
            target_type=c("area", 0.95),
            floor=c("Downstairs", 0.95),
        ),
        "turn off the lights downstairs",
        ON_FLOORS,
        MIN,
        last=turn(),
    )
    assert decision.follow_up is None
    assert decision.slots["floor"]["value"] == "Downstairs"


def test_a_sure_floor_holding_the_remembered_devices_keeps_the_follow_up():
    decision = interpret(
        answers(
            refers_back=NoulAnswer(noul=0.9),
            target_type=c("area", 0.95),
            floor=c("Upstairs", 0.95),
        ),
        "turn them off upstairs",
        ON_FLOORS,
        MIN,
        last=turn(),
    )
    assert decision.follow_up == "refers_back"
    assert decision.remembered == ["light.shelf", "light.study_table"]


def test_a_sure_question_after_an_action_is_not_a_repeat():
    # "turn off the ceiling" then "is the lamp on": Jev is sure of a question, so a
    # same_again read of it must not make it the last turn's turn_off.
    last = turn(
        devices=("light.ceiling",),
        action="turn_off",
        intent_type="HassTurnOff",
        shape=None,
        asked_state=None,
        text="turn off the ceiling",
    )
    decision = interpret(
        answers(
            action=c("get_state", 0.97),
            target_type=c("entity", 0.9),
            entity=c("light.lamp", 1.0),
            shape=c("one"),
            asked_state=c("on"),
            same_again=NoulAnswer(noul=0.85),
        ),
        "is the lamp on",
        SNAPSHOT,
        MIN,
        last=last,
    )
    assert decision.intent_type == "HassGetState"
    assert decision.action == "get_state"
    assert decision.follow_up is None
    assert decision.slots["name"]["value"] == "Lamp"


def test_a_sure_kind_no_remembered_device_has_is_not_a_follow_up():
    # "turn off the fan" after a question about the lights is about a fan.
    decision = interpret(
        answers(
            action=c("turn_off"),
            domain=c("fan", 0.95),
            refers_back=NoulAnswer(noul=0.85),
        ),
        "turn off the fan",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up is None
    assert decision.remembered == []
    assert decision.each == []


def test_a_sure_kind_the_remembered_devices_have_keeps_the_follow_up():
    decision = interpret(
        answers(
            action=c("turn_off"),
            domain=c("light", 0.95),
            refers_back=NoulAnswer(noul=0.85),
        ),
        "turn those lights off",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up == "refers_back"
    assert decision.remembered == ["light.shelf", "light.study_table"]


def test_the_others_is_not_the_remembered_devices():
    # "the others" means the devices the last turn was not about, so acting on the
    # remembered set would act on the opposite of what was meant.
    for text in (
        "turn off the others",
        "turn the rest off",
        "switch off the remaining ones",
        "turn them all off except the shelf",
        "turn off the other one",
    ):
        decision = interpret(
            answers(action=c("turn_off"), refers_back=NoulAnswer(noul=0.95)),
            text,
            SNAPSHOT,
            MIN,
            last=turn(),
        )
        assert decision.follow_up is None, text
        assert decision.remembered == [], text
        assert decision.each == [], text


def brightness_turn(level):
    return turn(
        devices=("light.shelf",),
        action="set_brightness",
        intent_type="HassLightSet",
        shape=None,
        asked_state=None,
        text="set the shelf to 40%",
        level=level,
    )


def and_the_living_room():
    return answers(
        action=c("turn_on", 0.4),
        target_type=c("area", 0.9),
        area=c("Living Room", 0.9),
        same_again=NoulAnswer(noul=0.9),
    )


def test_and_the_living_room_sets_the_remembered_level():
    decision = interpret(
        and_the_living_room(),
        "and the living room",
        SNAPSHOT,
        MIN,
        last=brightness_turn(40),
    )
    assert decision.intent_type == "HassLightSet"
    assert decision.follow_up == "same_again"
    assert decision.slots["brightness"]["value"] == 40
    assert decision.slots["area"]["value"] == "Living Room"
    # No second request for a level the sentence never said.
    assert decision.needs_level is False


def test_a_level_said_again_wins_over_the_remembered_one():
    decision = interpret(
        and_the_living_room(),
        "and the living room at 70%",
        SNAPSHOT,
        MIN,
        last=brightness_turn(40),
    )
    assert decision.slots["brightness"]["value"] == 70


def test_a_brightness_turn_with_no_level_is_not_repeated():
    decision = interpret(
        and_the_living_room(),
        "and the living room",
        SNAPSHOT,
        MIN,
        last=brightness_turn(None),
    )
    assert decision.follow_up is None
    assert decision.needs_level is False
    assert decision.intent_type != "HassLightSet"


TURN_OFF_THE_CEILING = {
    "devices": ("light.ceiling",),
    "action": "turn_off",
    "intent_type": "HassTurnOff",
    "shape": None,
    "asked_state": None,
    "text": "turn off the ceiling",
}


def test_a_question_split_between_two_reads_is_not_a_repeat_of_a_command():
    # Neither question is sure alone, but together they are: it is a question.
    decision = interpret(
        answers(
            action=c(
                "get_state",
                0.55,
                {"get_state": 0.55, "get_temperature": 0.42, "turn_off": 0.03},
            ),
            target_type=c("entity", 0.9),
            entity=c("light.lamp", 1.0),
            shape=c("one"),
            asked_state=c("on"),
            same_again=NoulAnswer(noul=0.85),
        ),
        "is the lamp on",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up is None
    assert decision.intent_type == "HassGetState"
    assert decision.slots["name"]["value"] == "Lamp"


def test_an_unsure_uncued_question_is_not_a_repeat_of_a_command():
    decision = interpret(
        answers(
            action=c("get_state", 0.5, {"get_state": 0.5, "turn_on": 0.3}),
            target_type=c("entity", 0.9),
            entity=c("light.lamp", 1.0),
            shape=c("one"),
            asked_state=c("on"),
            same_again=NoulAnswer(noul=0.85),
        ),
        "is the lamp on",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up is None
    assert decision.intent_type != "HassTurnOff"
    assert decision.intent_type in ("HassGetState", None)


def test_a_cued_repeat_of_a_command_read_as_an_unsure_question_still_repeats():
    # "and the living room?" often leans to a question. With the cue and no sure
    # question, it repeats the last command.
    decision = interpret(
        answers(
            action=c("get_state", 0.4, {"get_state": 0.4, "turn_off": 0.35}),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "and the living room?",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up == "same_again"
    assert decision.intent_type == "HassTurnOff"
    assert decision.slots["area"]["value"] == "Living Room"


def test_a_cued_question_split_between_two_reads_is_not_a_repeat_of_a_command():
    # The words open like a repeat, but the two question reads together are sure.
    decision = interpret(
        answers(
            action=c(
                "get_state",
                0.55,
                {"get_state": 0.55, "get_temperature": 0.42, "turn_off": 0.03},
            ),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "and the living room, is anything on?",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up is None
    assert decision.intent_type != "HassTurnOff"


def test_a_question_after_a_question_still_repeats_without_a_cue():
    # Only a command is guarded: asking the last question again changes nothing.
    decision = interpret(
        answers(
            action=c("get_state", 0.9),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "how about the living room",
        SNAPSHOT,
        MIN,
        last=turn(),
    )
    assert decision.follow_up == "same_again"
    assert decision.intent_type == "HassGetState"
    assert decision.asked_state == "on"


def is_the_lamp_on(action, **overrides):
    fields = {
        "target_type": c("entity", 0.9),
        "entity": c("light.lamp", 1.0),
        "shape": c("one"),
        "asked_state": c("on"),
        "same_again": NoulAnswer(noul=0.85),
    }
    fields.update(overrides)
    response = answers(action=action, **fields)
    if action is None:
        response.answers.pop("action")
    return interpret(
        response, "is the lamp on", SNAPSHOT, MIN, last=turn(**TURN_OFF_THE_CEILING)
    )


def test_an_uncued_sentence_whose_top_read_is_not_the_last_action_is_not_a_repeat():
    for action in (
        c(
            "none_of_these",
            0.45,
            {"none_of_these": 0.45, "get_state": 0.4, "turn_off": 0.15},
        ),
        c("report", 0.5, {"report": 0.5, "get_state": 0.4, "turn_off": 0.1}),
        None,
    ):
        decision = is_the_lamp_on(action)
        assert decision.follow_up is None, action
        assert decision.intent_type != "HassTurnOff", action


def test_an_uncued_sentence_whose_top_read_is_the_last_action_still_repeats():
    decision = interpret(
        answers(
            action=c("turn_off", 0.5, {"turn_off": 0.5, "turn_on": 0.3}),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "the living room too",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up == "same_again"
    assert decision.intent_type == "HassTurnOff"
    assert decision.slots["area"]["value"] == "Living Room"


def test_a_cued_fragment_with_no_action_of_its_own_still_repeats():
    decision = interpret(
        answers(
            action=c("none_of_these", 0.6),
            target_type=c("area", 0.9),
            area=c("Living Room", 0.9),
            same_again=NoulAnswer(noul=0.9),
        ),
        "and the living room",
        SNAPSHOT,
        MIN,
        last=turn(**TURN_OFF_THE_CEILING),
    )
    assert decision.follow_up == "same_again"
    assert decision.intent_type == "HassTurnOff"
