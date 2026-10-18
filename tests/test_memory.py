"""The memory of the last turn: kept per conversation, gone after the window."""

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.jev.memory import Memory, Turn


def make_turn(**changes):
    fields = {
        "text": "which lights are on",
        "action": "get_state",
        "intent_type": "HassGetState",
        "domains": ("light",),
        "where": ("Kids Room",),
        "everything": False,
        "devices": ("light.shelf", "light.study_table"),
        "names": ("Shelf", "Study table"),
    }
    fields.update(changes)
    return Turn(**fields)


def test_a_turn_is_kept_for_its_conversation():
    memory = Memory()
    turn = make_turn()
    memory.remember("abc", turn)
    assert memory.get("abc") is turn


def test_another_conversation_sees_nothing():
    memory = Memory()
    memory.remember("abc", make_turn())
    assert memory.get("other") is None


def test_a_turn_expires_after_the_window():
    memory = Memory()
    old = make_turn(at=dt_util.utcnow() - timedelta(minutes=6))
    memory.remember("abc", old)
    assert memory.get("abc") is None
    assert memory.get("abc") is None


def test_a_new_turn_replaces_the_old():
    memory = Memory()
    memory.remember("abc", make_turn(text="first"))
    newer = make_turn(text="second")
    memory.remember("abc", newer)
    assert memory.get("abc") is newer


def test_no_conversation_id_remembers_nothing():
    memory = Memory()
    memory.remember(None, make_turn())
    assert memory.get(None) is None


def test_describe_names_the_devices():
    assert make_turn().describe() == {
        "said": "which lights are on",
        "action": "get_state",
        "about": ["Shelf", "Study table"],
        "where": ["Kids Room"],
    }


def test_describe_omits_empty_fields():
    turn = make_turn(where=(), devices=(), names=())
    assert turn.describe() == {"said": "which lights are on", "action": "get_state"}


def test_describe_caps_a_long_list():
    names = tuple(f"Light {n}" for n in range(25))
    described = make_turn(names=names).describe()
    assert described["about"] == list(names[:20])
    assert described["and_more"] == 5
