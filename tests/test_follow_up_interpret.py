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
