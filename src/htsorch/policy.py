"""Recovery policy: what the workflow engine does for each fault, as a table.

action
  retry         the system clears the device and tries again, up to max_retries times;
                after that the run pauses for a person ('then': stop)
  operator_fix  a person must act (reload tips, refill, find the plate ...). The engine
                performs the named fix under the operator's name, clears the device and
                resumes the step from where it stopped
  stop          the run pauses and waits for a person to decide: retry or abort

resume (operator_fix only): True = repeat the failed command for what is left;
False = the person's acknowledgement is enough and the run carries on past it.

Faults never silently disappear: each one is an event in the log, and anything a person
had to do becomes deviation D050 in the data package."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

DEFAULT_POLICY = {
    "E001": {"action": "stop"},
    "E101": {"action": "operator_fix", "fix": "load_tips", "resume": True},
    "E102": {"action": "operator_fix", "fix": "refill_reservoir", "resume": True},
    "E103": {"action": "retry", "max_retries": 2, "then": "stop"},
    "E201": {"action": "retry", "max_retries": 2, "then": "stop"},
    "E202": {"action": "operator_fix", "fix": "reload_source", "resume": True},
    "E301": {"action": "retry", "max_retries": 3, "then": "stop"},
    "E302": {"action": "operator_fix", "fix": "locate_plate", "resume": True},
    "E401": {"action": "operator_fix", "fix": "acknowledge_excursion", "resume": False},
    "E402": {"action": "retry", "max_retries": 2, "then": "stop"},
    "E501": {"action": "operator_fix", "fix": "load_balance", "resume": True},
    "E601": {"action": "retry", "max_retries": 3, "then": "stop"},
    "E602": {"action": "stop"},
    "E701": {"action": "retry", "max_retries": 2, "then": "stop"},
}

ACTIONS = ("retry", "operator_fix", "stop")
FIXES = ("load_tips", "refill_reservoir", "reload_source", "locate_plate", "load_balance",
         "acknowledge_excursion")


def resolve_policy(overrides: dict | None = None) -> dict:
    """The default table with per-code changes merged in; unknown codes or actions are rejected."""
    from .faults import FAULTS
    table = copy.deepcopy(DEFAULT_POLICY)
    for code, change in (overrides or {}).items():
        if code not in FAULTS:
            raise ValueError(f"policy names unknown fault code {code}")
        table[code] = {**table.get(code, {}), **change}
    for code in FAULTS:
        if code not in table:
            raise ValueError(f"no recovery policy for {code}")
        row = table[code]
        if row.get("action") not in ACTIONS:
            raise ValueError(f"{code}: unknown action {row.get('action')!r}")
        if row["action"] == "operator_fix" and row.get("fix") not in FIXES:
            raise ValueError(f"{code}: unknown fix {row.get('fix')!r}")
    return table


@dataclass
class SimOperator:
    """Stands in for the person at the bench.

    available        False = nobody answers: the run pauses at the first thing needing a person
    unavailable_codes  only these faults find nobody
    response_min     minutes from the call to the person's action (eats timing windows)
    window_breach    'continue' (flagged) or 'abort' when a timing window cannot be met
    plate_found_at   where a lost plate turns up: 'source' or 'destination'
    stop_decision    'retry' or 'abort' when a run is paused for a decision"""
    name: str = "SIMULATED-OPERATOR"
    available: bool = True
    unavailable_codes: list = field(default_factory=list)
    response_min: float = 10.0
    window_breach: str = "continue"
    plate_found_at: str = "source"
    stop_decision: str = "retry"

    def __post_init__(self):
        for attr, ok in (("window_breach", ("continue", "abort")), ("plate_found_at", ("source", "destination")),
                         ("stop_decision", ("retry", "abort"))):
            if getattr(self, attr) not in ok:
                raise ValueError(f"operator.{attr} must be one of {ok}")

    def answers(self, code: str) -> bool:
        return self.available and code not in self.unavailable_codes

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "SimOperator":
        return cls(**d)
