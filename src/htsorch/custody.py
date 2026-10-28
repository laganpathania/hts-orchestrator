"""Plate custody: where every plate is, what has been done to it, and which moves are legal.

The rules are enforced, not advisory. An illegal request is refused with a code (K0xx),
the refusal is written to the log, and the state does not change.

  Location   a plate is in exactly one place: a station, the arm's gripper, or 'unknown'.
             Stations have a capacity; the gripper holds one plate.
  Stage      an assay plate moves through new -> ... -> complete in a fixed order, and each
             step must happen at the right station (cells are seeded on the Flex, never in
             the incubator).
  Status     active, quarantined, complete or discarded. Quarantined plates can only be
             moved to quarantine or discard; only a person can release them.
  Moves      two phases: begin_move (plate in the gripper) then complete_move, abort_move
             or mark_lost. A failed handoff therefore never leaves a plate 'half moved'.
  Uncertain  if a plate is lost in transit its location becomes 'unknown'. Nothing can move
             or process it until a person confirms where it is. The plate carries flag C001
             from then on, so the data from that well can be read with that in mind.

Every change is appended to a hash-chained log, so edits to the history are detectable
and the current state can be checked against it (verify)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .clock import SimClock
from .faults import DeviceFault
from .chain import link_hash, verify_chain

LOCATIONS = {"plate_hotel": 99, "flex_deck": 2, "idot_deck": 2, "centrifuge": 1, "incubator": 4,
             "reader": 1, "quarantine": 99, "discard": 99}
GRIPPER, UNKNOWN = "arm_gripper", "unknown"
ROLE_LOCATIONS = {
    "assay": set(LOCATIONS),
    "prep": {"plate_hotel", "flex_deck", "idot_deck", "quarantine", "discard"},
}
# stage -> {next stage: station where that step happens (None = any station)}
STAGES = {
    "assay": {
        "new": {"edge_filled": "flex_deck", "compound_dosed": "idot_deck"},
        "edge_filled": {"compound_dosed": "idot_deck"},
        "compound_dosed": {"media_added": "flex_deck"},
        "media_added": {"seeded": "flex_deck"},
        "seeded": {"centrifuged": "centrifuge"},
        "centrifuged": {"incubating": "incubator"},
        "incubating": {"incubated": "incubator"},
        "incubated": {"reagent_added": "flex_deck"},
        "reagent_added": {"lysed": "flex_deck"},
        "lysed": {"read": "reader"},
        "read": {"complete": None},
    },
    "prep": {"new": {"filled": "flex_deck"}, "filled": {"consumed": "idot_deck"}, "consumed": {"complete": None}},
}


class CustodyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass
class Plate:
    plate_id: str
    barcode: str
    role: str
    location: str
    stage: str = "new"
    status: str = "active"
    stage_since: str = ""
    flags: list = field(default_factory=list)
    in_transit: dict | None = None      # {"from", "to"} while in the gripper
    last_known: dict | None = None      # where it was when it was lost


class Custody:
    def __init__(self, clock: SimClock):
        self.clock = clock
        self.plates: dict[str, Plate] = {}
        self.history: list[dict] = []
        self.gripper: str | None = None

    # ---- log ------------------------------------------------------------------------------
    def _log(self, op: str, actor: str, plate: Plate | None, **detail) -> dict:
        e = {"seq": len(self.history) + 1, "at": self.clock.now().isoformat(), "actor": actor, "op": op,
             "plate": plate.plate_id if plate else None, **detail}
        if plate:
            e.update(location_after=plate.location, stage_after=plate.stage, status_after=plate.status)
        prev = self.history[-1]["event_hash"] if self.history else "0" * 64
        e["event_hash"] = link_hash(prev, e)
        self.history.append(e)
        return e

    def _refuse(self, op: str, actor: str, plate: Plate | None, code: str, message: str):
        self._log(f"refused:{op}", actor, plate, code=code, message=message)
        raise CustodyError(code, message)

    def _get(self, plate_id: str, op: str, actor: str) -> Plate:
        p = self.plates.get(plate_id)
        if p is None:
            self._refuse(op, actor, None, "K001", f"unknown plate '{plate_id}'")
        return p

    def _free(self, location: str) -> int:
        return LOCATIONS[location] - sum(1 for p in self.plates.values() if p.location == location)

    def _check_destination(self, p: Plate, dst: str, op: str, actor: str):
        if dst not in LOCATIONS:
            self._refuse(op, actor, p, "K012", f"'{dst}' is not a station")
        if dst not in ROLE_LOCATIONS[p.role]:
            self._refuse(op, actor, p, "K005", f"a {p.role} plate cannot go to '{dst}'")
        if self._free(dst) < 1:
            self._refuse(op, actor, p, "K003", f"'{dst}' is full ({LOCATIONS[dst]} plate(s))")

    @staticmethod
    def _human(actor: str) -> bool:
        return bool(actor) and actor != "system"

    # ---- registering and looking ---------------------------------------------------------------
    def register(self, plate_id: str, barcode: str, role: str, location: str = "plate_hotel", actor: str = "system") -> Plate:
        if role not in STAGES:
            raise CustodyError("K015", f"unknown plate role '{role}'")
        if plate_id in self.plates or any(p.barcode == barcode for p in self.plates.values()):
            self._refuse("register", actor, None, "K011", f"plate '{plate_id}' / barcode '{barcode}' already registered")
        p = Plate(plate_id, barcode, role, location, stage_since=self.clock.now().isoformat())
        if location not in LOCATIONS or location not in ROLE_LOCATIONS[role]:
            self._refuse("register", actor, None, "K005", f"cannot register a {role} plate at '{location}'")
        if self._free(location) < 1:
            self._refuse("register", actor, None, "K003", f"'{location}' is full")
        self.plates[plate_id] = p
        self._log("register", actor, p, barcode=barcode, role=role)
        return p

    def where(self, plate_id: str) -> str:
        return self.plates[plate_id].location

    def timeline(self, plate_id: str) -> list[dict]:
        return [e for e in self.history if e["plate"] == plate_id]

    # ---- moves (two phases) ----------------------------------------------------------------------
    def begin_move(self, plate_id: str, dst: str, actor: str = "system") -> Plate:
        p = self._get(plate_id, "begin_move", actor)
        if p.status == "discarded":
            self._refuse("begin_move", actor, p, "K008", f"{plate_id} is discarded")
        if p.status == "quarantined" and dst not in ("quarantine", "discard"):
            self._refuse("begin_move", actor, p, "K008", f"{plate_id} is quarantined; it may only go to quarantine or discard")
        if p.location in (GRIPPER, UNKNOWN):
            self._refuse("begin_move", actor, p, "K010",
                         f"{plate_id} is {'in the gripper' if p.location == GRIPPER else 'at an unknown location'}")
        if dst == p.location:
            self._refuse("begin_move", actor, p, "K013", f"{plate_id} is already at '{dst}'")
        if self.gripper is not None:
            self._refuse("begin_move", actor, p, "K004", f"gripper is holding {self.gripper}")
        self._check_destination(p, dst, "begin_move", actor)
        p.in_transit = {"from": p.location, "to": dst}
        p.location = GRIPPER
        self.gripper = plate_id
        self._log("move_begin", actor, p, source=p.in_transit["from"], destination=dst)
        return p

    def complete_move(self, plate_id: str, actor: str = "system") -> Plate:
        p = self._get(plate_id, "complete_move", actor)
        if not p.in_transit:
            self._refuse("complete_move", actor, p, "K014", f"{plate_id} is not in transit")
        dst = p.in_transit["to"]
        if self._free(dst) < 1:
            self._refuse("complete_move", actor, p, "K003", f"'{dst}' is full")
        src = p.in_transit["from"]
        p.location, p.in_transit, self.gripper = dst, None, None
        self._log("move_complete", actor, p, source=src, destination=dst)
        return p

    def abort_move(self, plate_id: str, actor: str = "system", reason: str = "") -> Plate:
        """The arm never picked the plate up: it is still at its source."""
        p = self._get(plate_id, "abort_move", actor)
        if not p.in_transit:
            self._refuse("abort_move", actor, p, "K014", f"{plate_id} is not in transit")
        src = p.in_transit["from"]
        p.location, p.in_transit, self.gripper = src, None, None
        self._log("move_aborted", actor, p, source=src, reason=reason)
        return p

    def mark_lost(self, plate_id: str, actor: str = "system", reason: str = "") -> Plate:
        """The plate left the source and never arrived: location unknown until a person finds it."""
        p = self._get(plate_id, "mark_lost", actor)
        if not p.in_transit:
            self._refuse("mark_lost", actor, p, "K014", f"{plate_id} is not in transit")
        p.last_known, p.in_transit = p.in_transit, None
        p.location, self.gripper = UNKNOWN, None
        self._add_flag(p, "C001", f"plate lost in transit {p.last_known['from']} -> {p.last_known['to']}: {reason}", actor)
        self._log("move_lost", actor, p, last_known=p.last_known, reason=reason)
        return p

    def human_place(self, plate_id: str, location: str, operator: str, reason: str) -> Plate:
        """A person puts the plate somewhere, or confirms where it was found."""
        p = self._get(plate_id, "human_place", operator)
        if not self._human(operator):
            self._refuse("human_place", operator, p, "K009", "this action needs a named person, not the system")
        if p.location == GRIPPER:
            self._refuse("human_place", operator, p, "K010", f"{plate_id} is in the gripper; finish or abort the move first")
        if p.status == "discarded":
            self._refuse("human_place", operator, p, "K008", f"{plate_id} is discarded")
        if p.status == "quarantined" and location not in ("quarantine", "discard"):
            self._refuse("human_place", operator, p, "K008", f"{plate_id} is quarantined")
        self._check_destination(p, location, "human_place", operator)
        was_unknown = p.location == UNKNOWN
        before = p.location
        p.location = location
        if was_unknown:
            self._add_flag(p, "C002", f"location confirmed by {operator}: {location} ({reason})", operator)
        self._log("human_place", operator, p, source=before, destination=location, reason=reason)
        return p

    # ---- stages and status -------------------------------------------------------------------------
    def advance(self, plate_id: str, stage: str, actor: str = "system", note: str = "") -> Plate:
        p = self._get(plate_id, "advance", actor)
        if p.status != "active":
            self._refuse("advance", actor, p, "K008", f"{plate_id} is {p.status}")
        if p.location in (GRIPPER, UNKNOWN):
            self._refuse("advance", actor, p, "K010",
                         f"{plate_id} is {'in the gripper' if p.location == GRIPPER else 'at an unknown location'}")
        allowed = STAGES[p.role].get(p.stage, {})
        if stage not in allowed:
            self._refuse("advance", actor, p, "K006",
                         f"{p.role} plate cannot go from '{p.stage}' to '{stage}' (allowed: {', '.join(allowed) or 'none'})")
        need = allowed[stage]
        if need and p.location != need:
            self._refuse("advance", actor, p, "K007", f"'{stage}' happens at {need}, but {plate_id} is at {p.location}")
        before = p.stage
        p.stage, p.stage_since = stage, self.clock.now().isoformat()
        if stage == "complete":
            p.status = "complete"
        self._log("advance", actor, p, stage_before=before, note=note)
        return p

    def quarantine(self, plate_id: str, actor: str, reason: str) -> Plate:
        p = self._get(plate_id, "quarantine", actor)
        if p.status != "active":
            self._refuse("quarantine", actor, p, "K008", f"{plate_id} is {p.status}")
        if p.location == GRIPPER:
            self._refuse("quarantine", actor, p, "K010", f"{plate_id} is in the gripper; finish or abort the move first")
        p.status = "quarantined"
        self._add_flag(p, "Q001", f"quarantined at stage '{p.stage}': {reason}", actor)
        self._log("quarantine", actor, p, reason=reason)
        return p

    def release(self, plate_id: str, operator: str, reason: str) -> Plate:
        p = self._get(plate_id, "release", operator)
        if not self._human(operator):
            self._refuse("release", operator, p, "K009", "only a named person can release a quarantined plate")
        if p.status != "quarantined":
            self._refuse("release", operator, p, "K008", f"{plate_id} is {p.status}, not quarantined")
        p.status = "active"
        self._add_flag(p, "Q002", f"released by {operator}: {reason}", operator)
        self._log("release", operator, p, reason=reason)
        return p

    def discard(self, plate_id: str, actor: str, reason: str) -> Plate:
        p = self._get(plate_id, "discard", actor)
        if p.status not in ("quarantined", "complete"):
            self._refuse("discard", actor, p, "K008", f"only quarantined or complete plates can be discarded ({plate_id} is {p.status})")
        if p.location != "discard":
            self._refuse("discard", actor, p, "K007", f"{plate_id} must be at 'discard' first (it is at {p.location})")
        p.status = "discarded"
        self._log("discard", actor, p, reason=reason)
        return p

    def flag(self, plate_id: str, code: str, note: str, actor: str = "system") -> Plate:
        p = self._get(plate_id, "flag", actor)
        self._add_flag(p, code, note, actor)
        self._log("flag", actor, p, code=code, note=note)
        return p

    def _add_flag(self, p: Plate, code: str, note: str, actor: str):
        p.flags.append({"code": code, "note": note, "at": self.clock.now().isoformat(), "actor": actor})

    # ---- integrity -----------------------------------------------------------------------------------
    def verify(self) -> list[str]:
        problems = []
        if not verify_chain(self.history):
            problems.append("custody log hash chain broken")
        for loc, cap in LOCATIONS.items():
            n = sum(1 for p in self.plates.values() if p.location == loc)
            if n > cap:
                problems.append(f"{loc} holds {n} plates, capacity {cap}")
        in_grip = [p.plate_id for p in self.plates.values() if p.location == GRIPPER]
        if len(in_grip) > 1:
            problems.append(f"gripper holds {len(in_grip)} plates: {in_grip}")
        if in_grip != ([self.gripper] if self.gripper else []):
            problems.append(f"gripper record {self.gripper} does not match plates in transit {in_grip}")
        last: dict[str, dict] = {}
        for e in self.history:
            if e["plate"]:
                last[e["plate"]] = e
        for pid, p in self.plates.items():
            if (p.location == GRIPPER) != bool(p.in_transit):
                problems.append(f"{pid}: in-transit record does not match location {p.location}")
            if p.location != GRIPPER and p.location != UNKNOWN and p.location not in LOCATIONS:
                problems.append(f"{pid}: location '{p.location}' is not a station")
            e = last.get(pid)
            if e is None or (e["location_after"], e["stage_after"], e["status_after"]) != (p.location, p.stage, p.status):
                problems.append(f"{pid}: current state does not match the last log entry")
        return problems

    # ---- persistence ---------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"plates": {k: asdict(v) for k, v in self.plates.items()}, "history": self.history, "gripper": self.gripper}

    @classmethod
    def from_dict(cls, d: dict, clock: SimClock) -> "Custody":
        c = cls(clock)
        c.plates = {k: Plate(**v) for k, v in d["plates"].items()}
        c.history, c.gripper = list(d["history"]), d["gripper"]
        return c


def transfer_plate(custody: Custody, arm, plate_id: str, dst: str, minutes: float, actor: str = "system"):
    """Move a plate with the arm and keep custody consistent whatever happens.

    Illegal moves are refused before the arm is asked. If the arm fails, the plate's
    record follows the fault's plate risk: 'uncertain' means the plate is lost, anything
    else means it never left its source. The fault is re-raised for the caller to handle."""
    src = custody.where(plate_id)
    custody.begin_move(plate_id, dst, actor)
    try:
        res = arm.execute("move", plate=plate_id, src=src, dst=dst, minutes=minutes)
    except DeviceFault as f:
        if f.plate_risk == "uncertain":
            custody.mark_lost(plate_id, actor, f"{f.code} {f.message}")
        else:
            custody.abort_move(plate_id, actor, f"{f.code} {f.message}")
        raise
    custody.complete_move(plate_id, actor)
    return res
