"""Device layer: one interface for every instrument, simulated implementations behind it.

The workflow engine (next milestone) only ever calls  device.execute(command, **params).
Replacing a simulated device with a real driver (vendor API, OPC-UA client) means writing
another class with the same interface; the workflow does not change.

What these classes model, and what they do not:
  - state (idle / error), command history counters, resource inventories (tips, liquid,
    iDOT source volumes, incubation timers) and typed faults with a time cost;
  - NOT the physics or the vendor protocols. Durations are passed in by the caller.

A device that raised a fault stays in 'error' and refuses commands (E001) until recover()
is called. Transient faults can be cleared by the system; operator faults need a person.
Maintenance commands (reload tips, refill) are allowed while in error: that is what a
person does before clearing the fault."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import SimClock
from .faults import DeviceFault, FaultInjector, RecoveryNotPermitted

EPS = 1e-6


@dataclass
class CommandResult:
    device: str
    command: str
    start_utc: str
    end_utc: str
    minutes: float
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"device": self.device, "command": self.command, "start_utc": self.start_utc,
                "end_utc": self.end_utc, "minutes": round(self.minutes, 3), "detail": self.detail}


class Device:
    kind = "device"
    commands: tuple = ()
    maintenance: tuple = ()          # allowed while the device is in error

    def __init__(self, name: str, clock: SimClock, injector: FaultInjector | None = None):
        self.name, self.clock, self.injector = name, clock, injector
        self.state = "idle"
        self.last_fault: dict | None = None
        self.command_count = 0
        self.fault_count = 0
        self.recoveries: list[dict] = []

    # ---- the one call the workflow engine uses ------------------------------------------
    def execute(self, command: str, **params) -> CommandResult:
        if command not in self.commands + self.maintenance:
            raise ValueError(f"{self.name}: unknown command '{command}' (known: {', '.join(self.commands + self.maintenance)})")
        is_maint = command in self.maintenance
        if self.state == "error" and not is_maint:
            raise DeviceFault("E001", self.name, command, f"{self.name} is in error state after {self.last_fault['code']}",
                              {"blocked_by": self.last_fault["code"]}, at=self.clock.now().isoformat())
        start = self.clock.now()
        try:
            if not is_maint and self.injector:
                self.injector.check(self.name, command, start)
            minutes, detail = getattr(self, "_" + command)(**params)
        except DeviceFault as f:
            # work done before the fault (partial protocol) plus the time the fault itself cost
            self.clock.advance(f.minutes_lost + f.detail.get("work_minutes", 0.0))
            f.at = self.clock.now().isoformat()
            self.state = "error"
            self.fault_count += 1
            self.last_fault = f.as_dict()
            raise
        self.clock.advance(minutes)
        self.command_count += 1
        return CommandResult(self.name, command, start.isoformat(), self.clock.now().isoformat(), minutes, detail)

    def recover(self, actor: str, note: str = "") -> dict:
        """Clear the device's error state. The system may do this only for transient faults."""
        if self.state != "error":
            return {"device": self.name, "cleared": None, "actor": actor, "note": "device was not in error"}
        if self.last_fault["kind"] == "operator" and actor == "system":
            raise RecoveryNotPermitted(f"{self.name}: {self.last_fault['code']} needs an operator "
                                       f"({self.last_fault['hint']})")
        rec = {"device": self.name, "cleared": self.last_fault["code"], "actor": actor, "note": note,
               "at": self.clock.now().isoformat()}
        self.recoveries.append(rec)
        self.state, self.last_fault = "idle", None
        return rec

    def status(self) -> dict:
        return {"device": self.name, "kind": self.kind, "state": self.state, "simulated": True,
                "commands_run": self.command_count, "faults": self.fault_count,
                "last_fault": self.last_fault["code"] if self.last_fault else None, **self._status()}

    # ---- persistence ------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "state": self.state, "last_fault": self.last_fault,
                "command_count": self.command_count, "fault_count": self.fault_count,
                "recoveries": self.recoveries, "extra": self._extra()}

    def load_dict(self, d: dict) -> None:
        if d["name"] != self.name or d["kind"] != self.kind:
            raise ValueError(f"saved state is for {d['kind']} '{d['name']}', not {self.kind} '{self.name}'")
        self.state, self.last_fault = d["state"], d["last_fault"]
        self.command_count, self.fault_count = d["command_count"], d["fault_count"]
        self.recoveries = list(d["recoveries"])
        self._load_extra(d["extra"])

    # ---- hooks for subclasses ---------------------------------------------------------------
    def _status(self) -> dict:
        return {}

    def _extra(self) -> dict:
        return {}

    def _load_extra(self, e: dict) -> None:
        pass

    def _fault(self, code: str, command: str, message: str | None = None, **detail) -> DeviceFault:
        return DeviceFault(code, self.name, command, message, detail)


# ---- Opentrons Flex ------------------------------------------------------------------------

class Flex(Device):
    """Tip and liquid inventory. A protocol that needs more than is loaded consumes what is
    there, then fails: the plate has had part of the step."""
    kind = "flex"
    commands = ("run_protocol",)
    maintenance = ("load_tips", "refill")

    def __init__(self, clock, injector=None, *, tips: dict, reservoirs: dict, name: str = "flex"):
        super().__init__(name, clock, injector)
        self.tips = {g: dict(v) for g, v in tips.items()}               # remaining, capacity, unit
        self.reservoirs = {r: dict(v) for r, v in reservoirs.items()}   # volume_ul, dead_ul, capacity_ul

    def _run_protocol(self, protocol: str, minutes: float, tips: dict | None = None,
                      draws: dict | None = None, plate: str | None = None):
        tips, draws = tips or {}, draws or {}
        for g in tips:
            if g not in self.tips:
                raise ValueError(f"unknown tip group '{g}'")
        for r in draws:
            if r not in self.reservoirs:
                raise ValueError(f"unknown reservoir '{r}'")
        short_tips = []
        for g, n in tips.items():
            use = min(n, self.tips[g]["remaining"])
            self.tips[g]["remaining"] -= use
            if use < n:
                short_tips.append({"group": g, "needed": n, "available": use, "unit": self.tips[g]["unit"]})
        short_liq = []
        for r, ul in draws.items():
            res = self.reservoirs[r]
            usable = max(0.0, res["volume_ul"] - res["dead_ul"])
            take = min(ul, usable)
            res["volume_ul"] -= take
            if ul - take > EPS:
                short_liq.append({"reservoir": r, "needed_ul": round(ul, 3), "available_ul": round(take, 3)})
        if short_tips or short_liq:
            ratios = [x["available"] / x["needed"] for x in short_tips] + \
                     [x["available_ul"] / x["needed_ul"] for x in short_liq if x["needed_ul"]]
            done = round(min(ratios), 3)         # the step stopped when the first resource ran out
            detail = dict(protocol=protocol, plate=plate, short_tips=short_tips, short_liquid=short_liq,
                          completed_fraction=done, work_minutes=round(minutes * done, 3))
            if short_tips:
                s = short_tips[0]
                raise self._fault("E101", "run_protocol",
                                  f"{protocol}: needs {s['needed']} {s['unit']} of '{s['group']}', {s['available']} left",
                                  **detail)
            s = short_liq[0]
            raise self._fault("E102", "run_protocol",
                              f"{protocol}: '{s['reservoir']}' needs {s['needed_ul']:g} uL, {s['available_ul']:g} uL above dead volume",
                              **detail)
        return minutes, {"protocol": protocol, "plate": plate, "tips": dict(tips),
                         "draws_ul": {k: round(v, 3) for k, v in draws.items()}}

    def _load_tips(self, group: str, actor: str, minutes: float = 5.0):
        if group not in self.tips:
            raise ValueError(f"unknown tip group '{group}'")
        self.tips[group]["remaining"] = self.tips[group]["capacity"]
        return minutes, {"group": group, "remaining": self.tips[group]["remaining"], "actor": actor}

    def _refill(self, reservoir: str, volume_ul: float, actor: str, minutes: float = 5.0):
        res = self.reservoirs.get(reservoir)
        if res is None:
            raise ValueError(f"unknown reservoir '{reservoir}'")
        if res["volume_ul"] + volume_ul > res["capacity_ul"] + EPS:
            raise ValueError(f"{reservoir}: {res['volume_ul'] + volume_ul:g} uL exceeds the {res['capacity_ul']:g} uL capacity")
        res["volume_ul"] += volume_ul
        return minutes, {"reservoir": reservoir, "volume_ul": round(res["volume_ul"], 3), "actor": actor}

    def _status(self):
        return {"tips": {g: {"remaining": v["remaining"], "capacity": v["capacity"]} for g, v in self.tips.items()},
                "reservoirs_ul": {r: round(v["volume_ul"], 3) for r, v in self.reservoirs.items()}}

    def _extra(self):
        return {"tips": self.tips, "reservoirs": self.reservoirs}

    def _load_extra(self, e):
        self.tips, self.reservoirs = e["tips"], e["reservoirs"]


# ---- iDOT ---------------------------------------------------------------------------------

class IDot(Device):
    """Tracks the volume left in each source well. Dispensing more than is above the dead
    volume dispenses what there is, then fails (E202)."""
    kind = "idot"
    commands = ("dispense",)
    maintenance = ("load_source",)

    def __init__(self, clock, injector=None, *, sources: dict, dead_ul: float, name: str = "idot"):
        super().__init__(name, clock, injector)
        self.sources = dict(sources)      # well -> uL
        self.dead_ul = dead_ul
        self.dispensed_nl = 0.0

    def _dispense(self, needs_nl: dict, minutes: float, plate: str | None = None):
        short, done = {}, 0.0
        for well, nl in needs_nl.items():
            have = max(0.0, self.sources.get(well, 0.0) - self.dead_ul)
            take_nl = min(nl, have * 1000.0)
            self.sources[well] = self.sources.get(well, 0.0) - take_nl / 1000.0
            done += take_nl
            if nl - take_nl > EPS:
                short[well] = round((nl - take_nl) / 1000.0, 4)
        self.dispensed_nl += done
        if short:
            requested = sum(needs_nl.values())
            frac = round(done / requested, 3) if requested else 0.0
            raise self._fault("E202", "dispense", f"{len(short)} source well(s) short: {', '.join(sorted(short))}",
                              plate=plate, short_ul=short, dispensed_nl=round(done, 1),
                              requested_nl=round(requested, 1), completed_fraction=frac,
                              work_minutes=round(minutes * frac, 3))
        return minutes, {"plate": plate, "wells": len(needs_nl), "dispensed_nl": round(done, 1)}

    def _load_source(self, well: str, volume_ul: float, actor: str, minutes: float = 3.0):
        self.sources[well] = self.sources.get(well, 0.0) + volume_ul
        return minutes, {"well": well, "volume_ul": round(self.sources[well], 4), "actor": actor}

    def _status(self):
        return {"dispensed_nl": round(self.dispensed_nl, 1), "source_wells": len(self.sources)}

    def _extra(self):
        return {"sources": self.sources, "dead_ul": self.dead_ul, "dispensed_nl": self.dispensed_nl}

    def _load_extra(self, e):
        self.sources, self.dead_ul, self.dispensed_nl = e["sources"], e["dead_ul"], e["dispensed_nl"]


# ---- the other devices --------------------------------------------------------------------

class Arm(Device):
    """The arm moves one plate per command. Whether the move is legal is decided by the
    custody layer before the arm is asked; see custody.transfer_plate."""
    kind = "arm"
    commands = ("move",)

    def _move(self, plate: str, src: str, dst: str, minutes: float):
        if src == dst:
            raise ValueError("arm: source and destination are the same")
        return minutes, {"plate": plate, "from": src, "to": dst}


class Incubator(Device):
    """Keeps the start time of every plate inside it, so the incubation time can be read
    back after a restart. Temperature is recorded, not simulated."""
    kind = "incubator"
    commands = ("store", "check", "retrieve")

    def __init__(self, clock, injector=None, *, setpoint_c: float = 37.0, slots: int = 4, name: str = "incubator"):
        super().__init__(name, clock, injector)
        self.setpoint_c, self.slots = setpoint_c, slots
        self.timers: dict[str, str] = {}     # plate -> ISO start time

    def elapsed_h(self, plate: str) -> float:
        return (self.clock.now() - datetime.fromisoformat(self.timers[plate])).total_seconds() / 3600.0

    def _store(self, plate: str, minutes: float = 0.5):
        if plate in self.timers:
            raise ValueError(f"{plate} is already in the incubator")
        if len(self.timers) >= self.slots:
            raise self._fault("E402", "store", "no free slot", plate=plate, slots=self.slots)
        self.timers[plate] = (self.clock.now()).isoformat()
        return minutes, {"plate": plate, "setpoint_c": self.setpoint_c, "started_utc": self.timers[plate]}

    def _check(self, plate: str, minutes: float = 0.0):
        if plate not in self.timers:
            raise ValueError(f"{plate} is not in the incubator")
        return minutes, {"plate": plate, "temp_c": self.setpoint_c, "elapsed_h": round(self.elapsed_h(plate), 4)}

    def _retrieve(self, plate: str, minutes: float = 0.5):
        if plate not in self.timers:
            raise ValueError(f"{plate} is not in the incubator")
        elapsed = self.elapsed_h(plate)
        started = self.timers.pop(plate)
        return minutes, {"plate": plate, "started_utc": started, "elapsed_h": round(elapsed, 4)}

    def _status(self):
        return {"setpoint_c": self.setpoint_c, "plates": sorted(self.timers)}

    def _extra(self):
        return {"setpoint_c": self.setpoint_c, "slots": self.slots, "timers": self.timers}

    def _load_extra(self, e):
        self.setpoint_c, self.slots, self.timers = e["setpoint_c"], e["slots"], e["timers"]


class Centrifuge(Device):
    kind = "centrifuge"
    commands = ("spin",)
    maintenance = ("load_balance",)

    def __init__(self, clock, injector=None, *, name: str = "centrifuge"):
        super().__init__(name, clock, injector)
        self.balance_plate_loaded = True

    def _spin(self, plate: str, minutes: float):
        if not self.balance_plate_loaded:
            raise self._fault("E501", "spin", "no balance plate in the rotor", plate=plate)
        return minutes, {"plate": plate}

    def _load_balance(self, actor: str, minutes: float = 2.0):
        self.balance_plate_loaded = True
        return minutes, {"actor": actor}

    def _status(self):
        return {"balance_plate_loaded": self.balance_plate_loaded}

    def _extra(self):
        return {"balance_plate_loaded": self.balance_plate_loaded}

    def _load_extra(self, e):
        self.balance_plate_loaded = e["balance_plate_loaded"]


class Shaker(Device):
    kind = "shaker"
    commands = ("shake",)

    def _shake(self, plate: str, minutes: float):
        return minutes, {"plate": plate}


class Reader(Device):
    """Reads the barcode before the measurement and refuses the wrong plate."""
    kind = "reader"
    commands = ("read",)
    MODES = ("luminescence",)

    def _read(self, plate: str, barcode: str, expected_barcode: str, mode: str, minutes: float):
        if mode not in self.MODES:
            raise ValueError(f"reader does not support mode '{mode}'")
        if barcode != expected_barcode:
            raise self._fault("E602", "read", f"plate barcode '{barcode}', expected '{expected_barcode}'",
                              plate=plate, barcode=barcode, expected=expected_barcode)
        return minutes, {"plate": plate, "barcode": barcode, "mode": mode}


# ---- building the set of devices from a spec --------------------------------------------

def idot_sources(spec: dict, d: dict) -> dict:
    """Volume in each prep-plate well that the iDOT will draw from (same assumptions as V030)."""
    from .spec import POS_WELL, VEHICLE_WELL, prep_well
    pp = spec["prep_plate"]
    pts = spec["dilution"]["points"]
    src = {prep_well(i, j, pts): d["remaining_ul"]
           for i in range(d["n_compounds"]) for j in range(pts)}
    src[VEHICLE_WELL] = pp["well_volume_ul"]
    src[POS_WELL] = pp["well_volume_ul"]
    return src


def idot_needs(plate_map: list) -> dict:
    """nL to dispense from each source well, from the plate map (PBS wells get none)."""
    needs: dict[str, float] = {}
    for w in plate_map:
        if w["type"] != "pbs":
            needs[w["source_well"]] = needs.get(w["source_well"], 0.0) + w["dispense_nl"]
    return needs


def build_devices(spec: dict, d: dict, clock: SimClock, injector: FaultInjector | None = None, *,
                  reservoir_load_ul: dict | None = None) -> dict:
    """Simulated devices stocked exactly as the validator assumed: a full tip rack per
    protocol, each reservoir loaded with what the run needs plus its dead volume.
    reservoir_load_ul overrides a loading (for example a short fill)."""
    fx = spec["flex"]
    tips = {g: {"remaining": t["capacity"], "capacity": t["capacity"], "unit": t["unit"]} for g, t in d["tips"].items()}
    res = {}
    for name, need in d["reservoir_need_ul"].items():
        r = fx["reservoirs"][name]
        load = (reservoir_load_ul or {}).get(name, need + r["dead_volume_ul"])
        res[name] = {"volume_ul": load, "dead_ul": r["dead_volume_ul"], "capacity_ul": r["capacity_ul"]}
    inc = Incubator(clock, injector)
    return {
        "flex": Flex(clock, injector, tips=tips, reservoirs=res),
        "idot": IDot(clock, injector, sources=idot_sources(spec, d), dead_ul=d["idot_dead_ul"]),
        "arm": Arm("arm", clock, injector),
        "incubator": inc,
        "centrifuge": Centrifuge(clock, injector),
        "shaker": Shaker("shaker", clock, injector),
        "reader": Reader("reader", clock, injector),
    }


INVENTORY_KEYS = {"tips", "reservoirs", "idot_sources"}


def check_inventory(inv: dict | None) -> None:
    """Shape check only (no spec needed). Names are checked against the devices in apply_inventory."""
    inv = inv or {}
    if not isinstance(inv, dict):
        raise ValueError("inventory must be a mapping")
    unknown = set(inv) - INVENTORY_KEYS
    if unknown:
        raise ValueError(f"unknown inventory key(s): {', '.join(sorted(unknown))} (known: {', '.join(sorted(INVENTORY_KEYS))})")
    for g, n in (inv.get("tips") or {}).items():
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ValueError(f"inventory.tips.{g}: tips left must be a whole number, 0 or more")
    for name, how in (inv.get("reservoirs") or {}).items():
        if not isinstance(how, dict) or set(how) not in ({"fraction"}, {"volume_ul"}):
            raise ValueError(f"inventory.reservoirs.{name}: give either 'fraction' (of what the run needs) or 'volume_ul'")
        v = next(iter(how.values()))
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"inventory.reservoirs.{name}: value must be a number, 0 or more")
    for well, how in (inv.get("idot_sources") or {}).items():
        if not isinstance(how, dict) or set(how) != {"usable_ul"} or not isinstance(how["usable_ul"], (int, float)) or how["usable_ul"] < 0:
            raise ValueError(f"inventory.idot_sources.{well}: give 'usable_ul' (volume above the dead volume), 0 or more")


def apply_inventory(devs: dict, d: dict, inv: dict | None) -> list[str]:
    """Stage the starting stock: tips left, reservoir loads, iDOT source volumes. Returns a plain-language
    list of what was set, for the record. Unknown names are an error, never silently ignored."""
    check_inventory(inv)
    done: list[str] = []
    flex, idot = devs["flex"], devs["idot"]
    for g, n in ((inv or {}).get("tips") or {}).items():
        if g not in flex.tips:
            raise ValueError(f"inventory: no tip group '{g}' (known: {', '.join(sorted(flex.tips))})")
        if n > flex.tips[g]["capacity"]:
            raise ValueError(f"inventory.tips.{g}: {n} is more than the {flex.tips[g]['capacity']} loaded")
        flex.tips[g]["remaining"] = n
        done.append(f"tips '{g}': {n} of {flex.tips[g]['capacity']} left")
    for name, how in ((inv or {}).get("reservoirs") or {}).items():
        if name not in flex.reservoirs:
            raise ValueError(f"inventory: no reservoir '{name}' (known: {', '.join(sorted(flex.reservoirs))})")
        res = flex.reservoirs[name]
        if "fraction" in how:
            vol = res["dead_ul"] + how["fraction"] * d["reservoir_need_ul"][name]
        else:
            vol = float(how["volume_ul"])
        if vol > res["capacity_ul"] + EPS:
            raise ValueError(f"inventory.reservoirs.{name}: {vol:g} uL exceeds the {res['capacity_ul']:g} uL capacity")
        res["volume_ul"] = vol
        done.append(f"reservoir '{name}': {vol:g} uL loaded (dead volume {res['dead_ul']:g} uL)")
    for well, how in ((inv or {}).get("idot_sources") or {}).items():
        if well not in idot.sources:
            raise ValueError(f"inventory: iDOT has no source well '{well}'")
        idot.sources[well] = idot.dead_ul + how["usable_ul"]
        done.append(f"iDOT source {well}: {how['usable_ul']:g} uL usable")
    return done
