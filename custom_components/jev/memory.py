"""What the home remembers of the last turn, so a follow-up has something to follow.

"Turn them off" means nothing alone. The conversation agent keeps the last turn of
each conversation here, and puts a short description of it in the request state, so
Jev can read "them" as the lights just asked about.

One turn per conversation, kept in memory only. A restart forgets it, which is
right: a follow-up arrives within minutes or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.helpers.chat_session import CONVERSATION_TIMEOUT
from homeassistant.util import dt as dt_util

# Not measured. A house-wide turn can name every light, and the names are billed
# as input tokens on the next request, so the list is cut to a size that still
# lets "those" mean something.
MAX_NAMES = 20


@dataclass(frozen=True, slots=True)
class Turn:
    """What one turn was about."""

    text: str
    action: str
    intent_type: str
    domains: tuple[str, ...]
    # Room or floor names in scope. Empty for a device or the whole house.
    where: tuple[str, ...]
    everything: bool
    devices: tuple[str, ...]
    names: tuple[str, ...]
    shape: str | None = None
    asked_state: str | None = None
    at: datetime = field(default_factory=dt_util.utcnow)

    def describe(self) -> dict[str, Any]:
        """The last_turn entry of the request state, without empty fields."""
        described: dict[str, Any] = {"said": self.text, "action": self.action}
        if self.names:
            described["about"] = list(self.names[:MAX_NAMES])
            if len(self.names) > MAX_NAMES:
                described["and_more"] = len(self.names) - MAX_NAMES
        if self.where:
            described["where"] = list(self.where)
        return described


class Memory:
    """The last turn of each conversation, for a window after it was said."""

    def __init__(self, window: timedelta = CONVERSATION_TIMEOUT) -> None:
        self._window = window
        self._turns: dict[str, Turn] = {}

    def get(self, conversation_id: str | None) -> Turn | None:
        """The turn to follow, or None when there is none or it has expired."""
        if conversation_id is None:
            return None
        turn = self._turns.get(conversation_id)
        if turn is None:
            return None
        if dt_util.utcnow() - turn.at > self._window:
            del self._turns[conversation_id]
            return None
        return turn

    def remember(self, conversation_id: str | None, turn: Turn) -> None:
        """Keep the turn in place of the last one."""
        if conversation_id is not None:
            self._turns[conversation_id] = turn
