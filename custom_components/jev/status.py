"""Short answers to status questions, worked out in code after the one Jev call.

`HassGetState` hands back every device in scope. Jev says what shape of answer was
asked for, any, all, which or how many, and which state the question is about. The
rest is counting, and counting is exact and free in code where a model only
judges. So "are any lights on in the kids room" is "Yes, two: shelf and study
table." rather than one sentence per light.

Three rules shape the answers.

A device that cannot be reached is neither yes nor no. It is named on its own, so
"no lights are on" never quietly means "none that I could read".

A curtain part of the way open is open and not closed, and the answer says how far.
Home Assistant reports a curtain at 40% as "open".

Nothing in scope is not a no. "I don't see any lights in the Kids Room" tells the
user the question missed, where "No" would tell them something false.

Only English is said this way. The number words and plurals are English, so every
other language keeps the full list.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_ENTITY_ID
from homeassistant.core import State


class Shape(StrEnum):
    """What kind of answer the question asks for."""

    ONE = "one"
    ANY = "any"
    ALL = "all"
    WHICH = "which"
    HOW_MANY = "how_many"
    LIST = "list"


class Bucket(StrEnum):
    """What a state means, read for its domain."""

    ON = "on"
    OFF = "off"
    OPEN = "open"
    PARTLY_OPEN = "partly_open"
    CLOSED = "closed"
    MOVING = "moving"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    JAMMED = "jammed"
    UNKNOWN = "unknown"
    # A state with no yes or no in it, such as a media player's "playing".
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Reading:
    """One device's state, as a bucket and, for a cover or valve, a position."""

    bucket: Bucket
    position: int | None = None


# The states a question can ask about, and the buckets that count as each. A
# curtain part of the way open counts as open.
ASKABLE: Mapping[Bucket, frozenset[Bucket]] = {
    Bucket.ON: frozenset({Bucket.ON}),
    Bucket.OFF: frozenset({Bucket.OFF}),
    Bucket.OPEN: frozenset({Bucket.OPEN, Bucket.PARTLY_OPEN}),
    Bucket.CLOSED: frozenset({Bucket.CLOSED}),
    Bucket.LOCKED: frozenset({Bucket.LOCKED}),
    Bucket.UNLOCKED: frozenset({Bucket.UNLOCKED}),
}

# Binary sensors whose on means open, and the one whose on means unlocked.
_OPENINGS = ("door", "garage_door", "opening", "window")
_POSITIONED = ("cover", "valve")
# Home Assistant's answer template shows four names and then counts the rest
# ("a, b, c and 6 more"). The same cap, so a long answer reads the same either way.
MAX_NAMES = 4

# One word per count, for the counts a room holds. Past twelve the digits read as
# well as a word.
_COUNTS = (
    "no",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)

# The sentences, with their placeholders. conversation.py passes the strings.json
# wording over these; a test keeps the two the same.
LINES: Mapping[str, str] = {
    "status_none_seen": "I don't see any {nouns}.",
    "status_none_seen_in": "I don't see any {nouns} in the {area}.",
    "status_unreachable": "I can't reach the {names}.",
    "status_any_yes": "Yes, {count}: {names}.",
    "status_all_other": "No, all {nouns} are {state}.",
    "status_none_are": "No {nouns} are {state}.",
    "status_none_are_unreachable": (
        "No {nouns} are {state}, but I can't reach the {names}."
    ),
    "status_all_one": "Yes, the {noun} is {state}.",
    "status_all_both": "Yes, both {nouns} are {state}.",
    "status_all_yes": "Yes, all {count} {nouns} are {state}.",
    "status_all_no": "No. {exceptions}.",
    "status_all_unreachable": (
        "Every {noun} I can reach is {state}, but I can't reach the {names}."
    ),
    "status_which_none": "None of the {nouns} are {state}.",
    "status_is": "{name} is {state}",
    "status_are": "{names} are {state}",
    "status_one_is": "{count} {noun} is {state}.",
    "status_many_are": "{count} {nouns} are {state}.",
    "status_and": "{first} and {last}",
    "status_and_more": "{names} and {count} more",
    "status_partly_open": "{position}% open",
    "status_with_position": "{name} ({position}% open)",
}


def _position(state: State) -> int | None:
    raw = state.attributes.get("current_position")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return round(raw)


def classify(state: State) -> Reading:
    """What this state means for its domain."""
    domain = state.domain
    value = state.state
    position = _position(state) if domain in _POSITIONED else None
    if value in ("unknown", "unavailable"):
        return Reading(Bucket.UNKNOWN)
    if domain in _POSITIONED:
        if value in ("opening", "closing"):
            return Reading(Bucket.MOVING, position)
        if value == "closed":
            return Reading(Bucket.CLOSED, position)
        if value == "open":
            if position is not None and 0 < position < 100:
                return Reading(Bucket.PARTLY_OPEN, position)
            return Reading(Bucket.OPEN, position)
        return Reading(Bucket.OTHER, position)
    if domain == "lock":
        known = {
            "locked": Bucket.LOCKED,
            "unlocked": Bucket.UNLOCKED,
            "open": Bucket.UNLOCKED,
            "jammed": Bucket.JAMMED,
        }
        if value in known:
            return Reading(known[value])
        if value in ("locking", "unlocking", "opening"):
            return Reading(Bucket.MOVING)
        return Reading(Bucket.OTHER)
    if domain == "binary_sensor":
        device_class = state.attributes.get(ATTR_DEVICE_CLASS)
        if device_class in _OPENINGS:
            on, off = Bucket.OPEN, Bucket.CLOSED
        elif device_class == "lock":
            on, off = Bucket.UNLOCKED, Bucket.LOCKED
        else:
            on, off = Bucket.ON, Bucket.OFF
        return Reading({"on": on, "off": off}.get(value, Bucket.OTHER))
    return Reading({"on": Bucket.ON, "off": Bucket.OFF}.get(value, Bucket.OTHER))


def without_groups(states: Sequence[State]) -> list[State]:
    """Drop a group whose members are here too, so nothing is counted twice."""
    present = {state.entity_id for state in states}

    def is_covered_group(state: State) -> bool:
        members = state.attributes.get(ATTR_ENTITY_ID)
        return isinstance(members, list | tuple) and any(m in present for m in members)

    return [state for state in states if not is_covered_group(state)]


def short_name(name: str, area_names: Sequence[str], noun: str) -> str:
    """The name as it is said inside an answer, in lower case.

    With one room in scope the room and the kind of device go unsaid: "Kids Room
    Shelf Light" is "shelf". With several, the room tells the devices apart, so the
    name stays whole. A name that is nothing but the room and the noun stays whole
    too, or there would be nothing left to say.
    """
    full = " ".join(name.split()).lower()
    if len(area_names) != 1:
        return full
    short = full
    room = area_names[0].lower()
    if short.startswith(room + " "):
        short = short[len(room) + 1 :]
    for ending in (_plural(noun), noun):
        if short.endswith(" " + ending):
            short = short[: -len(ending) - 1]
            break
        if short == ending:
            short = ""
            break
    return short.strip() or full


def _plural(noun: str) -> str:
    return noun + ("es" if noun.endswith(("s", "sh", "ch", "x")) else "s")


def _count(number: int) -> str:
    return _COUNTS[number] if number < len(_COUNTS) else str(number)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


@dataclass(frozen=True, slots=True)
class _Device:
    name: str
    reading: Reading
    state: str


def answer(
    shape: Shape,
    asked: Bucket | None,
    states: Sequence[State],
    *,
    noun: str,
    area_names: Sequence[str],
    lines: Mapping[str, str] | None = None,
) -> str:
    """One sentence that answers the question.

    asked is the state the question is about, None for "what is the status".
    area_names are the rooms in scope, empty for the whole house.
    """
    say = {**LINES, **(lines or {})}
    nouns = _plural(noun)

    def join(names: Sequence[str]) -> str:
        if len(names) > MAX_NAMES:
            shown = ", ".join(names[: MAX_NAMES - 1])
            return say["status_and_more"].format(
                names=shown, count=len(names) - MAX_NAMES + 1
            )
        if len(names) == 1:
            return names[0]
        return say["status_and"].format(first=", ".join(names[:-1]), last=names[-1])

    def word(device: _Device) -> str:
        reading = device.reading
        if reading.bucket is Bucket.PARTLY_OPEN:
            return say["status_partly_open"].format(position=reading.position)
        if reading.bucket in (Bucket.MOVING, Bucket.OTHER):
            return device.state
        return str(reading.bucket)

    def named(device: _Device) -> str:
        # "Yes, one: study main (40% open)", because open is not all the way.
        if device.reading.bucket is Bucket.PARTLY_OPEN:
            return say["status_with_position"].format(
                name=device.name, position=device.reading.position
            )
        return device.name

    devices = [
        _Device(short_name(state.name, area_names, noun), classify(state), state.state)
        for state in without_groups(states)
    ]
    if not devices:
        if len(area_names) == 1:
            return say["status_none_seen_in"].format(nouns=nouns, area=area_names[0])
        return say["status_none_seen"].format(nouns=nouns)

    if asked not in ASKABLE or shape in (Shape.ONE, Shape.LIST):
        listed = [say["status_is"].format(name=d.name, state=word(d)) for d in devices]
        return _capital(", ".join(listed)) + "."

    assert asked is not None
    wanted = ASKABLE[asked]
    state = str(asked)
    unknown = [d.name for d in devices if d.reading.bucket is Bucket.UNKNOWN]
    known = [d for d in devices if d.reading.bucket is not Bucket.UNKNOWN]
    yes = [d for d in known if d.reading.bucket in wanted]
    no = [d for d in known if d.reading.bucket not in wanted]
    unreachable = say["status_unreachable"].format(names=join(unknown)) if unknown else ""

    def then_unreachable(sentence: str) -> str:
        return _capital(f"{sentence} {unreachable}".strip())

    if not known:
        return _capital(unreachable)

    if shape is Shape.ANY:
        if yes:
            return then_unreachable(
                say["status_any_yes"].format(
                    count=_count(len(yes)), names=join([named(d) for d in yes])
                )
            )
        if unknown:
            return _capital(
                say["status_none_are_unreachable"].format(
                    nouns=nouns, state=state, names=join(unknown)
                )
            )
        if len({d.reading.bucket for d in no}) == 1 and no[0].reading.bucket not in (
            Bucket.MOVING,
            Bucket.OTHER,
        ):
            return say["status_all_other"].format(nouns=nouns, state=word(no[0]))
        return say["status_none_are"].format(nouns=nouns, state=state)

    if shape is Shape.ALL:
        if no:
            by_word: dict[str, list[str]] = {}
            for device in no:
                by_word.setdefault(word(device), []).append(device.name)
            if len(no) > MAX_NAMES:
                exceptions = say["status_are"].format(
                    names=join([d.name for d in no]), state=f"not {state}"
                )
            else:
                exceptions = join(
                    [
                        say["status_is" if len(names) == 1 else "status_are"].format(
                            name=names[0], names=join(names), state=spoken
                        )
                        for spoken, names in by_word.items()
                    ]
                )
            return then_unreachable(
                say["status_all_no"].format(exceptions=_capital(exceptions))
            )
        if unknown:
            return say["status_all_unreachable"].format(
                noun=noun, state=state, names=join(unknown)
            )
        if len(yes) == 1:
            return say["status_all_one"].format(noun=noun, state=state)
        if len(yes) == 2:
            return say["status_all_both"].format(nouns=nouns, state=state)
        return say["status_all_yes"].format(
            count=_count(len(yes)), nouns=nouns, state=state
        )

    if shape is Shape.WHICH:
        if not yes:
            return then_unreachable(
                say["status_which_none"].format(nouns=nouns, state=state)
            )
        names = [named(d) for d in yes]
        key = "status_is" if len(names) == 1 else "status_are"
        return then_unreachable(
            say[key].format(name=names[0], names=join(names), state=state) + "."
        )

    # HOW_MANY
    if not yes:
        return then_unreachable(say["status_none_are"].format(nouns=nouns, state=state))
    key = "status_one_is" if len(yes) == 1 else "status_many_are"
    return then_unreachable(
        say[key].format(count=_count(len(yes)), noun=noun, nouns=nouns, state=state)
    )
