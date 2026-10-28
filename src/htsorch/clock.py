"""Simulated clock shared by every device and by the custody log.

Nothing in the platform reads the wall clock while a run is simulated. A 72 h
incubation is one call to advance(), and the clock can be saved and restored."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SimClock:
    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, minutes: float) -> datetime:
        if minutes < 0:
            raise ValueError(f"time cannot run backwards ({minutes} min)")
        self._now += timedelta(minutes=minutes)
        return self._now

    def advance_to(self, when: datetime) -> datetime:
        if when < self._now:
            raise ValueError(f"time cannot run backwards ({when.isoformat()} is before {self._now.isoformat()})")
        self._now = when
        return self._now

    def to_dict(self) -> dict:
        return {"now_utc": self._now.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "SimClock":
        return cls(datetime.fromisoformat(d["now_utc"]))
