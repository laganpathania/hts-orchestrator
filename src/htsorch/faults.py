"""Fault catalogue, the exception devices raise, and the fault injector.

Every fault has a code, a kind and a plate risk, so a recovery policy (next milestone)
can be written as a table instead of scattered if-statements.

  kind        transient  the system may clear it and retry on its own
              operator   a person must act before the device can run again
  plate_risk  none          the plate was not touched
              partial_step  the step was only partly done to the plate
              uncertain     the plate's location is no longer known
              environment   the plate sat outside its required conditions

Injection rules are deterministic: a rule fires on the n-th call of device.command
(counting failed calls too), for `times` calls in a row. A retry after one failure
therefore succeeds, unless the rule says times: 2 or more."""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("transient", "operator")
PLATE_RISKS = ("none", "partial_step", "uncertain", "environment")


@dataclass(frozen=True)
class FaultDef:
    code: str
    device: str            # device name this fault belongs to, or "*" for any device
    title: str
    kind: str
    plate_risk: str
    minutes_lost: float    # time the fault costs before it is detected (eats timing windows)
    hint: str


_DEFS = [
    FaultDef("E001", "*", "Command refused: device is in an error state", "operator", "none", 0.0,
             "Clear the earlier fault first"),
    FaultDef("E101", "flex", "Tips exhausted", "operator", "partial_step", 0.0,
             "Reload the tip rack, then resume from the last completed cycle"),
    FaultDef("E102", "flex", "Reservoir below dead volume", "operator", "partial_step", 0.0,
             "Refill the reservoir, then resume from the last completed cycle"),
    FaultDef("E103", "flex", "Protocol aborted by the instrument", "transient", "partial_step", 3.0,
             "Check deck state, then rerun from the last completed cycle"),
    FaultDef("E201", "idot", "Dispense verification failed", "transient", "partial_step", 2.0,
             "Retry the dispense for the wells not yet confirmed"),
    FaultDef("E202", "idot", "Source well volume insufficient", "operator", "partial_step", 0.0,
             "Refill or replace the source well, then dispense the remaining wells"),
    FaultDef("E301", "arm", "Handoff failed before pick-up", "transient", "none", 1.0,
             "The plate is still at its source; retry the move"),
    FaultDef("E302", "arm", "Plate lost or dropped in transit", "operator", "uncertain", 1.0,
             "Locate the plate by hand and confirm where it is"),
    FaultDef("E401", "incubator", "Temperature excursion", "operator", "environment", 0.0,
             "Check the incubator and the plates; flag the affected plates"),
    FaultDef("E402", "incubator", "Door or slot fault", "transient", "none", 1.0,
             "Retry; if it persists, call the operator"),
    FaultDef("E501", "centrifuge", "Rotor imbalance or no balance plate", "operator", "none", 1.0,
             "Load a balance plate"),
    FaultDef("E601", "reader", "Reader not ready", "transient", "none", 2.0,
             "Wait and retry"),
    FaultDef("E602", "reader", "Plate barcode does not match the expected plate", "operator", "none", 0.0,
             "Check which plate is in the reader"),
    FaultDef("E701", "shaker", "Shaker stalled", "transient", "partial_step", 1.0,
             "Restart the shaking step"),
]
FAULTS: dict[str, FaultDef] = {f.code: f for f in _DEFS}


class DeviceFault(Exception):
    """A device could not complete a command."""

    def __init__(self, code: str, device: str, command: str, message: str | None = None,
                 detail: dict | None = None, injected: bool = False, at: str | None = None):
        if code not in FAULTS:
            raise ValueError(f"unknown fault code {code}")
        d = FAULTS[code]
        self.code, self.device, self.command = code, device, command
        self.message = message or d.title
        self.detail = dict(detail or {})
        self.injected = injected
        self.at = at
        super().__init__(f"{code} {device}.{command}: {self.message}")

    @property
    def kind(self) -> str:
        return FAULTS[self.code].kind

    @property
    def plate_risk(self) -> str:
        return FAULTS[self.code].plate_risk

    @property
    def minutes_lost(self) -> float:
        return FAULTS[self.code].minutes_lost

    @property
    def hint(self) -> str:
        return FAULTS[self.code].hint

    def as_dict(self) -> dict:
        return {"code": self.code, "device": self.device, "command": self.command, "message": self.message,
                "kind": self.kind, "plate_risk": self.plate_risk, "minutes_lost": self.minutes_lost,
                "hint": self.hint, "injected": self.injected, "at": self.at, "detail": self.detail}


class RecoveryNotPermitted(Exception):
    """The system tried to clear a fault that needs a person."""


class FaultInjector:
    def __init__(self, rules: list[dict] | None = None):
        self.rules: list[dict] = []
        self.calls: dict[str, int] = {}
        self.fired: list[dict] = []
        for r in rules or []:
            self.add(**r)

    def add(self, device: str, command: str, code: str, at: int = 1, times: int = 1,
            message: str | None = None, detail: dict | None = None) -> None:
        if code not in FAULTS:
            raise ValueError(f"unknown fault code {code}")
        owner = FAULTS[code].device
        if owner not in ("*", device):
            raise ValueError(f"{code} belongs to '{owner}', not '{device}'")
        if at < 1 or times < 1:
            raise ValueError("'at' and 'times' must be 1 or more")
        self.rules.append({"device": device, "command": command, "code": code, "at": at, "times": times,
                           "message": message, "detail": dict(detail or {})})

    def check(self, device: str, command: str, when) -> None:
        key = f"{device}.{command}"
        n = self.calls[key] = self.calls.get(key, 0) + 1
        for r in self.rules:
            if r["device"] == device and r["command"] == command and r["at"] <= n < r["at"] + r["times"]:
                self.fired.append({"device": device, "command": command, "code": r["code"], "call": n,
                                   "at": when.isoformat()})
                raise DeviceFault(r["code"], device, command, r["message"], r["detail"], injected=True)

    def pending(self) -> list[dict]:
        """Rules that have not fired yet (a scenario that never triggers is usually a mistake)."""
        out = []
        for r in self.rules:
            key = f"{r['device']}.{r['command']}"
            if self.calls.get(key, 0) < r["at"]:
                out.append(r)
        return out

    def to_dict(self) -> dict:
        return {"rules": self.rules, "calls": self.calls, "fired": self.fired}

    @classmethod
    def from_dict(cls, d: dict) -> "FaultInjector":
        inj = cls(d["rules"])
        inj.calls, inj.fired = dict(d["calls"]), list(d["fired"])
        return inj
