"""Workflow engine: runs the screen step by step on the device layer, under plate custody.

What it does that a pre-computed run record cannot:
  * the timing windows are enforced while the run happens. Before each seeding step the
    engine predicts where the window will land; if it cannot be met, a person decides
    (continue with a deviation, or abort and quarantine) before more cells are committed;
  * every fault is handled by the recovery policy table: retried, fixed by a person and
    resumed from where the step stopped, or the run pauses and waits for a decision;
  * state is saved after every step. The 72 h incubation is a saved state with a wake-up
    time, not a sleeping process: resume() can run in another process, days later;
  * a run that survived faults still ends in a complete, checkable record.

Statuses: new -> running -> waiting (incubation) / paused (needs a person) -> complete,
or aborted (plates quarantined). Everything runs on the simulated clock."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import SimClock
from .custody import Custody, CustodyError, transfer_plate
from .devices import apply_inventory, build_devices, idot_needs
from .faults import DeviceFault, FaultInjector
from .platemap import build_plate_map
from .policy import SimOperator, resolve_policy
from .chain import link_hash, verify_chain
from .spec import derive, load_spec
from .timing import dispense_progress, time_at

DEFAULT_START = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
CENTRIFUGE_MIN = 3.0     # placeholder
READ_MIN = 10.0          # placeholder
STATE_SCHEMA = "htsorch.engine.state/1"
PREP, ASSAY = "PREP-0001", "ASSAY-0001"
SHORT = {"flex_deck": "flex", "idot_deck": "idot"}


class Paused(Exception):
    """The run needs a person."""


class Waiting(Exception):
    """The run is waiting for a time (the incubation)."""


class Aborted(Exception):
    pass


class EngineError(RuntimeError):
    pass


def _iso(t) -> str:
    return t.isoformat() if isinstance(t, datetime) else t


class Orchestrator:
    def __init__(self, spec: dict, *, operator_id: str = "SIMULATED-OPERATOR", run_id: str | None = None,
                 start: datetime | None = None, faults: list | None = None, operator: SimOperator | None = None,
                 policy: dict | None = None, delays: dict | None = None, protocols: dict | None = None,
                 state_path=None, meta: dict | None = None, inventory: dict | None = None):
        self.spec = spec
        self.d = derive(spec)
        self.pm = build_plate_map(spec, self.d)
        self.progress = dispense_progress(spec, self.d, self.pm)
        self.operator_id = operator_id
        self.run_id = run_id or str(uuid.uuid4())
        self.origin = start or DEFAULT_START
        self.clock = SimClock(self.origin)
        self.injector = FaultInjector(faults or [])
        self.devs = build_devices(spec, self.d, self.clock, self.injector)
        self.inventory_set = apply_inventory(self.devs, self.d, inventory)     # starting stock, when staged
        self.cust = Custody(self.clock)
        self.cust.register(PREP, spec["prep_plate"]["barcode"], "prep")
        self.cust.register(ASSAY, spec["assay"]["plate"]["barcode"], "assay")
        self.operator = operator or SimOperator(name=operator_id)
        self.policy_overrides = policy or {}
        self.policy = resolve_policy(self.policy_overrides)
        self.delays = delays or {}
        self.protocols = protocols or {}
        self.state_path = Path(state_path) if state_path else None
        self.meta = dict(meta or {})    # free-form facts the caller wants kept with the run (for example the reader seed)
        if self.inventory_set:
            self.meta["inventory"] = self.inventory_set
        # mutable run state (everything below is saved)
        self.status = "new"
        self.i = 0
        self.st: dict = {}
        self.seg: dict = {}
        self.marks: dict = {}
        self.alerts: dict = {}
        self.attempts: dict = {}
        self.interventions: list = []
        self.pending: dict | None = None
        self.events: list = []
        self.states_at_read: dict | None = None
        self.incubation: dict | None = None
        self.steps = self._workflow()
        self.index = {s["id"]: k for k, s in enumerate(self.steps)}

    # ================================================================================== workflow
    def _workflow(self) -> list:
        s, d, tm = self.spec, self.d, self.spec["timing"]
        est, res = d["est"], d["reservoir_need_ul"]
        tips = {k: v["needed"] for k, v in d["tips"].items()}
        rg = s["assay"]["reagent"]
        steps: list = []

        def add(kind, id=None, **kw):
            steps.append({"kind": kind, "id": id or f"{kind}_{len(steps) + 1}", **kw})

        def move(plate, dst):
            add("move", plate=plate, dst=dst, event=f"arm_to_{SHORT.get(dst, dst)}", minutes=tm["arm_transfer_min"])

        def flex(id, protocol, minutes, tipgroup, tipn, reservoir, plate, advance):
            add("flex", id, protocol=protocol, minutes=minutes, tips={tipgroup: tipn},
                draws={reservoir: res[reservoir]}, plate=plate, advance=advance)

        add("note", "spec_validated", minutes=0.5)
        add("human", "approval", minutes=2.0)
        if d["edge_policy"] == "pbs_fill":
            move(ASSAY, "flex_deck")
            flex("flex_edge_fill", "flex_0_edge_fill.py", est["edge_fill_min"], "edge_fill", tips["edge_fill"],
                 "pbs", ASSAY, [(ASSAY, "edge_filled")])
        move(PREP, "flex_deck")
        transfers = d["n_compounds"] * s["dilution"]["points"] * 2 + 4
        flex("flex_dilution_series", "flex_1_dilution.py", transfers * tm["flex_cycle_s"] / 60.0,
             "dilution", tips["dilution"], "dmso", PREP, [(PREP, "filled")])
        move(PREP, "idot_deck")
        move(ASSAY, "idot_deck")
        add("idot", "idot_dispense", minutes=tm["idot_dispense_min"])
        move(ASSAY, "flex_deck")
        if self.delays.get("compound_wait_min"):
            add("delay", "injected_delay", minutes=float(self.delays["compound_wait_min"]))
        flex("flex_media", "flex_2_seeding.py", est["media_min"], "seeding", 1, "media", ASSAY, [(ASSAY, "media_added")])
        flex("flex_cells", "flex_2_seeding.py", est["cells_min"], "seeding", 1, "cells", ASSAY, [(ASSAY, "seeded")])
        move(ASSAY, "centrifuge")
        add("centrifuge", "centrifuge", minutes=CENTRIFUGE_MIN)
        move(ASSAY, "incubator")
        add("store", "incubator_store")
        add("incubate", "incubate")
        add("wake", "wake_and_verify")
        add("retrieve", "incubator_retrieve")
        move(ASSAY, "flex_deck")
        flex("flex_reagent", "flex_3_readout.py", est["reagent_min"], "readout", tips["readout"], "reagent",
             ASSAY, [(ASSAY, "reagent_added")])
        add("shake", "shake", minutes=rg["shake_min"])
        add("lysis", "lysis_incubation", minutes=rg["incubate_min"])
        move(ASSAY, "reader")
        add("read", "read_luminescence", minutes=READ_MIN)
        add("finish", "finish")
        return steps

    # ================================================================================== running
    def run(self) -> str:
        if self.status != "new":
            raise EngineError(f"run() starts a new run; this one is {self.status}. Use resume().")
        return self._loop()

    def resume(self, *, advance_to: datetime | None = None, advance_min: float | None = None,
               until_wake: bool = False, wake_delay_h: float = 0.0, response: dict | None = None) -> str:
        """Continue a saved run. Time may pass first (the simulation's way of letting 72 h go by);
        a paused run also needs a response: {'actor': name, 'decision': 'retry'|'abort'|'continue', 'note': ''}."""
        if self.status in ("complete", "aborted"):
            raise EngineError(f"run is {self.status}; nothing to resume")
        target = advance_to
        if advance_min is not None:
            target = self.clock.now() + timedelta(minutes=advance_min)
        self._pass_time(target)
        if self.status == "paused":
            if response is None:
                self.save()
                return self.status
            try:
                self._apply_response(response)
            except Aborted:
                self.status = "aborted"
                self.save()
                return self.status
        status = self._loop()
        if status == "waiting" and until_wake:        # the incubation: let simulated time run to the wake-up
            self._pass_time(datetime.fromisoformat(self.marks["wake_at"]) + timedelta(hours=wake_delay_h))
            status = self._loop()
        return status

    def _pass_time(self, target) -> None:
        if target and target > self.clock.now():
            t0 = self.clock.now()
            self.clock.advance_to(target)
            self._event("time_passed", "simulation", t0, self.clock.now(), actor="simulation",
                        note=f"while {self.status}")

    def _loop(self) -> str:
        self.status = "running"
        try:
            while self.i < len(self.steps):
                step = self.steps[self.i]
                getattr(self, "_h_" + step["kind"])(step)
                self.i += 1
                self.st = {}
                self.save()
            self.status = "complete"
        except Paused:
            self.status = "paused"
        except Waiting:
            self.status = "waiting"
        except Aborted:
            self.status = "aborted"
        self.save()
        return self.status

    # ================================================================================== events
    def _event(self, step, device, start, end, *, actor="software", human=False, state="idle", **detail) -> dict:
        start_s, end_s = _iso(start), _iso(end)
        minutes = (datetime.fromisoformat(end_s) - datetime.fromisoformat(start_s)).total_seconds() / 60.0
        e = {"step": step, "actor": actor, "device": device, "human_action": human, "start_utc": start_s,
             "end_utc": end_s, "duration_min": round(minutes, 2), "device_state_after": state, **detail}
        prev = self.events[-1]["event_hash"] if self.events else "0" * 64
        e["event_hash"] = link_hash(prev, e)
        self.events.append(e)
        return e

    def _dev_event(self, step_name, res, **extra):
        det = {k: v for k, v in res.detail.items() if k not in ("plate",)}
        proto = det.get("protocol")
        if proto and proto.split(" ")[0] in self.protocols:
            det["protocol_sha256"] = self.protocols[proto.split(" ")[0]]
        return self._event(step_name, res.device, res.start_utc, res.end_utc, plate=res.detail.get("plate"),
                           state=self.devs[res.device].state, **det, **extra)

    def _now(self) -> datetime:
        return self.clock.now()

    def _min(self, t) -> float:
        t = datetime.fromisoformat(t) if isinstance(t, str) else t
        return (t - self.origin).total_seconds() / 60.0

    # ================================================================================== steps
    def _h_note(self, step):
        t0 = self._now()
        self.clock.advance(step["minutes"])
        self._event(step["id"], "orchestrator", t0, self._now(), spec_sha256=self.spec["_meta"]["sha256"])

    def _h_human(self, step):
        t0 = self._now()
        self.clock.advance(step["minutes"])
        self._event(step["id"], "orchestrator", t0, self._now(), actor=self.operator_id, human=True)

    def _h_delay(self, step):
        t0 = self._now()
        self.clock.advance(step["minutes"])
        self._event(step["id"], "arm", t0, self._now(), actor="simulation", note="injected arm/handoff delay")

    def _h_move(self, step):
        plate, dst = step["plate"], step["dst"]

        def call():
            if self.cust.where(plate) == dst:          # a person found it at the destination
                return None
            return transfer_plate(self.cust, self.devs["arm"], plate, dst, step["minutes"])
        res = self._attempt(step, call)
        if res is not None:
            self._dev_event(step["event"], res)

    def _h_flex(self, step):
        st = self.st
        if "remaining" not in st:
            st.update(remaining={"minutes": step["minutes"], "tips": dict(step["tips"]), "draws": dict(step["draws"])},
                      progress=0.0, segments=[], total_min=step["minutes"], resumed=False)
            if step["id"] == "flex_media":
                self._schedule_cell_prep(step)

        def call():
            self._check_windows(step)
            r = st["remaining"]
            return self.devs["flex"].execute("run_protocol", protocol=step["protocol"], minutes=r["minutes"],
                                             tips=r["tips"], draws=r["draws"], plate=step["plate"])
        res = self._attempt(step, call)
        st["segments"].append({"start_utc": res.start_utc, "work_min": res.minutes, "p_from": st["progress"], "p_to": 1.0})
        self.seg[step["id"]] = st["segments"]
        self._dev_event(step["id"], res, resumed=st["resumed"])
        for plate, stage in step["advance"]:
            self.cust.advance(plate, stage)

    def _h_idot(self, step):
        st = self.st
        if "remaining" not in st:
            st["remaining"] = {"needs_nl": idot_needs(self.pm), "minutes": step["minutes"]}
            st["resumed"] = False

        def call():
            r = st["remaining"]
            return self.devs["idot"].execute("dispense", needs_nl=r["needs_nl"], minutes=r["minutes"], plate=ASSAY)
        res = self._attempt(step, call)
        self._dev_event(step["id"], res, resumed=st["resumed"], dispense_nl_max=self.d["compound_nl_max"])
        self.marks["idot_end"] = res.end_utc
        self.cust.advance(ASSAY, "compound_dosed")
        self.cust.advance(PREP, "consumed")

    def _simple(self, step, dev, command, **params):
        res = self._attempt(step, lambda: self.devs[dev].execute(command, **params))
        return res

    def _h_centrifuge(self, step):
        res = self._simple(step, "centrifuge", "spin", plate=ASSAY, minutes=step["minutes"])
        if res:
            self._dev_event("centrifuge", res, note="placeholder duration")
        self.cust.advance(ASSAY, "centrifuged")

    def _h_store(self, step):
        res = self._simple(step, "incubator", "store", plate=ASSAY)
        if res:
            self._dev_event(step["id"], res)
        self.cust.advance(ASSAY, "incubating")
        t = self._now()
        planned = self.spec["timing"]["windows"]["incubation_h"]
        self.marks["incubation_start"] = t.isoformat()
        self.marks["wake_at"] = (t + timedelta(hours=planned)).isoformat()
        self._event("wait_scheduled", "orchestrator", t, t, wake_at=self.marks["wake_at"],
                    note="state saved; the orchestrator exits during the wait")

    def _h_incubate(self, step):
        wake = datetime.fromisoformat(self.marks["wake_at"])
        if self._now() < wake:
            raise Waiting()
        self.marks["incubation_end"] = self._now().isoformat()

    def _h_wake(self, step):
        t0 = self._now()
        res = self._simple(step, "incubator", "check", plate=ASSAY)
        elapsed = (self._now() - datetime.fromisoformat(self.marks["incubation_start"])).total_seconds() / 3600.0
        self._event("wake_and_verify", "orchestrator", t0, self._now(),
                    checks=["plate in incubator", f"incubation elapsed {elapsed:.2f} h", "deck clear"],
                    incubator_check=res.detail if res else "acknowledged by operator")
        self.cust.advance(ASSAY, "incubated")

    def _h_retrieve(self, step):
        res = self._simple(step, "incubator", "retrieve", plate=ASSAY)
        if res:
            self._dev_event(step["id"], res)
        t_start = datetime.fromisoformat(self.marks["incubation_start"])
        win = self.spec["timing"]["windows"]
        actual_h = (datetime.fromisoformat(self.marks["incubation_end"]) - t_start).total_seconds() / 3600.0
        self.incubation = {"start_utc": t_start.isoformat(), "end_utc": self.marks["incubation_end"],
                           "planned_h": win["incubation_h"], "actual_h": round(actual_h, 3),
                           "tolerance_h": win["incubation_tolerance_h"],
                           "within_tolerance": abs(actual_h - win["incubation_h"]) <= win["incubation_tolerance_h"]}
        self._event("incubate", "incubator", t_start, self.marks["incubation_end"],
                    note="orchestrator persisted its state and exited during the wait")

    def _h_shake(self, step):
        res = self._simple(step, "shaker", "shake", plate=ASSAY, minutes=step["minutes"])
        self._dev_event("shake", res)

    def _h_lysis(self, step):
        t0 = self._now()
        self.clock.advance(step["minutes"])
        self._event("lysis_incubation", "shaker", t0, self._now(), plate=ASSAY)
        self.cust.advance(ASSAY, "lysed")

    def _h_read(self, step):
        res = self._simple(step, "reader", "read", plate=ASSAY, barcode=self.cust.plates[ASSAY].barcode,
                           expected_barcode=self.spec["assay"]["plate"]["barcode"], mode="luminescence",
                           minutes=step["minutes"])
        self._dev_event("read_luminescence", res, export="reader_export.csv")
        self.marks["read_end"] = self._now().isoformat()
        self.states_at_read = self._instrument_states()
        self.cust.advance(ASSAY, "read")

    def _h_finish(self, step):
        self.cust.advance(ASSAY, "complete")
        self.cust.advance(PREP, "complete")
        t = self._now()
        self._event("run_complete", "orchestrator", t, t, custody_ok=self.cust.verify() == [])

    # ================================================================================== faults
    def _attempt(self, step, call):
        """Run call() until it succeeds. Faults go through the recovery policy; this returns
        None only when a person has acknowledged a fault that the run is allowed to pass."""
        while True:
            if self.st.pop("skip_command", False):
                return None
            t0 = self._now()
            try:
                return call()
            except DeviceFault as f:
                if self._on_fault(step, f, t0) == "skip":
                    return None

    def _on_fault(self, step, f: DeviceFault, t0) -> str:
        self._absorb_partial(step, f, t0)
        self._event("fault", f.device, t0, self._now(), actor="software", state="error", code=f.code,
                    command=f.command, message=f.message, kind=f.kind, plate_risk=f.plate_risk,
                    injected=f.injected, at_step=step["id"], detail=f.detail)
        rule = self.policy[f.code]
        action = rule["action"]
        key = f"{step['id']}:{f.code}"
        if action == "retry":
            n = self.attempts.get(key, 0)
            if n < rule["max_retries"]:
                self.attempts[key] = n + 1
                rec = self.devs[f.device].recover("system", f"automatic retry {n + 1} of {rule['max_retries']}")
                self._event("recovery", f.device, self._now(), self._now(), cleared=rec["cleared"], by="system",
                            note=rec["note"])
                self.st["resumed"] = True
                return "retry"
            action = rule.get("then", "stop")
            reason = f"{f.code} still failing after {rule['max_retries']} retries"
        else:
            reason = f"{f.code} needs a person"
        n_fixes = self.attempts.get(key + ":fix", 0)
        limit = rule.get("max_fixes", 3)
        if n_fixes >= limit:
            self._pause(step, f, rule, f"{f.code} came back after {n_fixes} fixes; a person must look at the instrument")
        if self.operator.answers(f.code):
            t = self._now()
            self.clock.advance(self.operator.response_min)
            self._event("operator_response", "operator", t, self._now(), actor=self.operator.name, human=True,
                        code=f.code, note="called to the instrument")
            if action == "operator_fix":
                return self._operator_fix(step, f, rule, self.operator.name)
            # 'stop': the operator decides
            self._event("operator_decision", "operator", self._now(), self._now(), actor=self.operator.name,
                        human=True, decision=self.operator.stop_decision, about=f.code)
            if self.operator.stop_decision == "abort":
                self._abort(f"operator {self.operator.name} aborted after {f.code}")
            self._operator_fix(step, f, {"action": "stop"}, self.operator.name)
            return "retry"
        self._pause(step, f, rule, reason if action != "operator_fix" else "no operator available")
        raise AssertionError("unreachable")  # _pause raises

    def _operator_fix(self, step, f: DeviceFault, rule, actor: str, note: str = "") -> str:
        fix = rule.get("fix")
        t0 = self._now()
        fixkey = f"{step['id']}:{f.code}:fix"
        self.attempts[fixkey] = self.attempts.get(fixkey, 0) + 1
        detail = getattr(self, "_fix_" + fix)(f, actor, step) if fix else {}
        dev = self.devs[f.device]
        if dev.state == "error":
            dev.recover(actor, note or f"{f.code}: {fix or 'checked by operator'}")
        self._event("operator_action", f.device, t0, self._now(), actor=actor, human=True, code=f.code, fix=fix, **detail)
        self.interventions.append({"code": f.code, "device": f.device, "step": step["id"],
                                   "step_name": step.get("event", step["id"]), "actor": actor, "fix": fix,
                                   "at": self._now().isoformat(), **detail})
        self.st["resumed"] = True
        if rule.get("resume", True) is False:
            return "skip"
        return "retry"

    # ---- fixes: what the person does -----------------------------------------------------
    def _fix_load_tips(self, f, actor, step):
        groups = [s["group"] for s in f.detail.get("short_tips", [])]
        for g in groups:
            self.devs["flex"].execute("load_tips", group=g, actor=actor)
        return {"loaded_tip_groups": groups}

    def _fix_refill_reservoir(self, f, actor, step):
        done = {}
        for s in f.detail.get("short_liquid", []):
            add = round(s["needed_ul"] - s["available_ul"], 3)
            self.devs["flex"].execute("refill", reservoir=s["reservoir"], volume_ul=add, actor=actor)
            done[s["reservoir"]] = add
        return {"refilled_ul": done}

    def _fix_reload_source(self, f, actor, step):
        done = {}
        for well, ul in f.detail.get("short_ul", {}).items():
            self.devs["idot"].execute("load_source", well=well, volume_ul=ul, actor=actor)
            done[well] = ul
        return {"reloaded_ul": done}

    def _fix_locate_plate(self, f, actor, step):
        plate = step["plate"]
        lk = self.cust.plates[plate].last_known
        where = lk["to"] if self.operator.plate_found_at == "destination" else lk["from"]
        self.cust.human_place(plate, where, actor, f"found after {f.code}")
        return {"plate": plate, "found_at": where}

    def _fix_load_balance(self, f, actor, step):
        self.devs["centrifuge"].execute("load_balance", actor=actor)
        return {}

    def _fix_acknowledge_excursion(self, f, actor, step):
        self.cust.flag(ASSAY, "E401", f"incubator excursion acknowledged by {actor}: {f.detail}", actor)
        return {"excursion": f.detail}

    # ---- partial progress ----------------------------------------------------------------
    def _absorb_partial(self, step, f: DeviceFault, t0):
        st = self.st
        if step["kind"] == "flex" and "remaining" in st:
            frac = f.detail.get("completed_fraction", 0.0)
            r = st["remaining"]
            if frac > 0:
                new_p = st["progress"] + (1.0 - st["progress"]) * frac
                st["segments"].append({"start_utc": t0.isoformat(), "work_min": f.detail.get("work_minutes", 0.0),
                                       "p_from": st["progress"], "p_to": new_p})
                st["progress"] = new_p
                r["minutes"] = r["minutes"] * (1.0 - frac)
            if "short_tips" in f.detail:         # a real shortage: ask only for what is still missing
                r["tips"] = {s["group"]: s["needed"] - s["available"] for s in f.detail["short_tips"]}
                r["draws"] = {s["reservoir"]: round(s["needed_ul"] - s["available_ul"], 3)
                              for s in f.detail["short_liquid"]}
        elif step["kind"] == "idot" and "remaining" in st and "short_ul" in f.detail:
            r = st["remaining"]
            r["needs_nl"] = {w: round(ul * 1000.0, 3) for w, ul in f.detail["short_ul"].items()}
            r["minutes"] = r["minutes"] * (1.0 - f.detail.get("completed_fraction", 0.0))

    # ---- pause / abort -------------------------------------------------------------------
    def _pause(self, step, f: DeviceFault, rule, reason: str):
        self.pending = {"kind": "fault", "step": step["id"], "fault": f.as_dict(), "rule": rule, "reason": reason}
        t = self._now()
        self._event("paused", "orchestrator", t, t, reason=reason, code=f.code, hint=f.hint)
        raise Paused()

    def _apply_response(self, resp: dict):
        p, actor = self.pending, resp["actor"]
        t = self._now()
        decision = resp.get("decision", "retry" if p["kind"] == "fault" else "continue")
        self._event("operator_decision", "operator", t, t, actor=actor, human=True, decision=decision,
                    about=p.get("code") or p["fault"]["code"], note=resp.get("note", ""))
        if p["kind"] == "window":
            self.alerts[p["code"]]["decision"] = decision
            self.alerts[p["code"]]["by"] = actor
            self.pending = None
            if decision == "abort":
                self._abort(f"{p['code']} window breach: operator chose to abort")
            return
        f = p["fault"]
        step = self.steps[self.index[p["step"]]]
        self.pending = None
        if decision == "abort":
            self._abort(f"operator {actor} aborted after {f['code']}")
        rule = p["rule"]
        fault = DeviceFault(f["code"], f["device"], f["command"], f["message"], f["detail"])
        if rule["action"] == "operator_fix":
            out = self._operator_fix(step, fault, rule, actor, resp.get("note", ""))
        else:
            dev = self.devs[f["device"]]
            if dev.state == "error":
                dev.recover(actor, resp.get("note", "cleared by operator"))
            self.interventions.append({"code": f["code"], "device": f["device"], "step": step["id"],
                                       "step_name": step.get("event", step["id"]), "actor": actor,
                                       "fix": None, "at": self._now().isoformat()})
            out = "retry"
        if out == "skip":
            self.st["skip_command"] = True

    def _abort(self, reason: str):
        t = self._now()
        for pid, p in self.cust.plates.items():
            if p.status == "active" and p.location != "arm_gripper":
                self.cust.quarantine(pid, "system", reason)
        self._event("aborted", "orchestrator", t, t, reason=reason)
        raise Aborted()

    # ================================================================================== windows
    def _rem(self, sid: str) -> float:
        k = self.index[sid]
        if k < self.i:
            return 0.0
        if k == self.i and "remaining" in self.st:
            return self.st["remaining"]["minutes"]
        return self.steps[k]["minutes"]

    def _schedule_cell_prep(self, step):
        """The operator prepares the cell suspension so that it is ready cell_prep_to_start_min
        before the planned start of cell dispensing. A delay after this point ages the cells."""
        lead = self.spec["timing"]["cell_prep_to_start_min"]
        now = self._now()
        planned = now + timedelta(minutes=self._rem("flex_media"))
        prep = max(now, planned - timedelta(minutes=lead))
        self.marks["cell_prep"] = prep.isoformat()
        self._event("cell_suspension_prepared", "operator", now, now, actor=self.operator_id, human=True,
                    ready_utc=prep.isoformat(), note="scheduled so that cells are ready shortly before dispensing")

    def _check_windows(self, step):
        win = self.spec["timing"]["windows"]
        now, sid = self._now(), step["id"]
        if sid in ("flex_media", "flex_cells") and "idot_end" in self.marks:
            cells_end = now + timedelta(minutes=self._rem("flex_media") + self._rem("flex_cells"))
            self._window("D040", self._min(cells_end) - self._min(self.marks["idot_end"]),
                         win["compound_to_cells_max_min"], "Compound plate wait (iDOT end to cells in the last well)")
            if "cell_prep" in self.marks:
                self._window("D041", self._min(cells_end) - self._min(self.marks["cell_prep"]),
                             win["cell_suspension_max_age_min"], "Cell suspension age at the last well")
        if sid == "flex_reagent":
            cyc = self.d["cycles"]["reagent"]
            st = self.st
            first_p = 1.0 / cyc
            if st["segments"]:
                t_first = self._min(st["segments"][0]["start_utc"]) + first_p * st["total_min"]
            else:
                t_first = self._min(now) + first_p * st["remaining"]["minutes"]
            self._window("D042", self._min(now) + st["remaining"]["minutes"] - t_first,
                         win["max_reagent_lag_min"], "First-to-last-well reagent lag")

    def _window(self, code, predicted, limit, label):
        if predicted <= limit + 1e-9 or code in self.alerts:
            return
        t = self._now()
        self.alerts[code] = {"predicted_min": round(predicted, 1), "limit_min": limit, "at": t.isoformat(),
                             "decision": None, "by": None}
        self._event("window_alert", "orchestrator", t, t, code=code, predicted_min=round(predicted, 1), limit_min=limit,
                    message=f"{label}: predicted {predicted:.1f} min, window {limit} min")
        if not self.operator.answers("window"):
            self.pending = {"kind": "window", "code": code, "reason": f"{code} window cannot be met; decision needed"}
            self._event("paused", "orchestrator", t, t, reason=self.pending["reason"], code=code)
            raise Paused()
        self.clock.advance(self.operator.response_min)
        decision = self.operator.window_breach
        self.alerts[code].update(decision=decision, by=self.operator.name)
        self._event("operator_decision", "operator", t, self._now(), actor=self.operator.name, human=True,
                    decision=decision, about=code)
        if decision == "abort":
            self._abort(f"{code} window breach: operator chose to abort")

    # ================================================================================== output
    def _instrument_states(self) -> dict:
        out = {k: {"state": v.state, "simulated": True} for k, v in self.devs.items() if k != "flex"}
        fl = self.devs["flex"]
        out["flex"] = {"state": fl.state, "simulated": True,
                       "tips_used": {g: t["capacity"] - t["remaining"] for g, t in fl.tips.items()},
                       "tips_loaded": {g: t["capacity"] for g, t in fl.tips.items()}}
        return out

    def _well_timing(self) -> dict:
        cells_seg = [{"start_min": self._min(s["start_utc"]), "work_min": s["work_min"], "p_from": s["p_from"], "p_to": s["p_to"]}
                     for s in self.seg["flex_cells"]]
        reag_seg = [{"start_min": self._min(s["start_utc"]), "work_min": s["work_min"], "p_from": s["p_from"], "p_to": s["p_to"]}
                    for s in self.seg["flex_reagent"]]
        idot_end, prep = self._min(self.marks["idot_end"]), self._min(self.marks["cell_prep"])
        reag_t = {w: time_at(reag_seg, p["reagent"]) for w, p in self.progress.items()}
        first = min(reag_t.values())
        out = {}
        for w, p in self.progress.items():
            if p["cells"] is None:
                out[w] = {"cell_age_min": None, "compound_to_cells_min": None, "reagent_offset_min": reag_t[w] - first}
            else:
                t = time_at(cells_seg, p["cells"])
                out[w] = {"cell_age_min": t - prep, "compound_to_cells_min": t - idot_end,
                          "reagent_offset_min": reag_t[w] - first}
        return out

    def run_record(self) -> dict:
        """The run as the data package expects it. Only for a completed run."""
        if self.status != "complete":
            raise EngineError(f"run is {self.status}; a record exists only for a complete run")
        win = self.spec["timing"]["windows"]
        wt = self._well_timing()
        waits = [v["compound_to_cells_min"] for v in wt.values() if v["compound_to_cells_min"] is not None]
        ages = [v["cell_age_min"] for v in wt.values() if v["cell_age_min"] is not None]
        lags = [v["reagent_offset_min"] for v in wt.values()]
        summary = {"compound_to_cells_max_min": max(waits), "cell_age_max_min": max(ages), "reagent_lag_min": max(lags),
                   "windows": {"compound_to_cells_max_min": win["compound_to_cells_max_min"],
                               "cell_suspension_max_age_min": win["cell_suspension_max_age_min"],
                               "max_reagent_lag_min": win["max_reagent_lag_min"]}}
        deviations = []
        for code, label, val, lim in (
                ("D040", "Compound plate wait (iDOT end to cells)", summary["compound_to_cells_max_min"], win["compound_to_cells_max_min"]),
                ("D041", "Cell suspension age at dispense", summary["cell_age_max_min"], win["cell_suspension_max_age_min"]),
                ("D042", "First-to-last-well reagent lag", summary["reagent_lag_min"], win["max_reagent_lag_min"])):
            if val > lim:
                deviations.append({"code": code, "what": f"{label} was {val:.1f} min, window {lim} min"})
        inc = self.incubation
        if not inc["within_tolerance"]:
            deviations.append({"code": "D043", "what": f"Incubation was {inc['actual_h']:g} h, planned "
                                                       f"{inc['planned_h']} h +/- {inc['tolerance_h']} h"})
        if self.interventions:
            what = "; ".join(f"{i['code']} on {i['device']} during {i['step_name']} (by {i['actor']})" for i in self.interventions)
            deviations.append({"code": "D050", "what": f"A person intervened during the run: {what}", "affected_wells": "all"})
        if any(f["code"] == "C001" for f in self.cust.plates[ASSAY].flags):
            note = next(f["note"] for f in self.cust.plates[ASSAY].flags if f["code"] == "C001")
            deviations.append({"code": "D051", "what": f"Assay plate custody was uncertain for a time: {note}",
                               "affected_wells": "all"})
        for i in self.interventions:
            if i["fix"] == "acknowledge_excursion":
                deviations.append({"code": "D052", "what": f"Incubator excursion during incubation: {i['excursion']}",
                                   "affected_wells": "all"})
        head = self.events[-1]["event_hash"]
        return {"run_id": self.run_id, "operator_id": self.operator_id, "simulated": True, "clock": "simulated, UTC",
                "start_utc": self.events[0]["start_utc"], "end_utc": self.events[-1]["end_utc"],
                "events": self.events, "event_chain_head": head, "incubation": inc, "window_summary": summary,
                "deviations": deviations, "instrument_states_at_read": self.states_at_read, "well_timing": wt,
                "custody_log": self.cust.history, "plate_flags": {k: v.flags for k, v in self.cust.plates.items()},
                "alerts": self.alerts, "interventions": self.interventions,
                "fault_events": [e for e in self.events if e["step"] == "fault"]}

    # ================================================================================== persistence
    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"schema": STATE_SCHEMA, "spec_path": self.spec["_meta"]["path"], "spec_sha256": self.spec["_meta"]["sha256"],
               "run_id": self.run_id, "operator_id": self.operator_id, "origin": self.origin.isoformat(),
               "status": self.status, "i": self.i, "st": self.st, "seg": self.seg, "marks": self.marks,
               "alerts": self.alerts, "attempts": self.attempts, "interventions": self.interventions,
               "pending": self.pending, "events": self.events, "states_at_read": self.states_at_read,
               "incubation": self.incubation, "clock": self.clock.to_dict(), "custody": self.cust.to_dict(),
               "devices": {k: v.to_dict() for k, v in self.devs.items()}, "injector": self.injector.to_dict(),
               "operator": self.operator.to_dict(), "policy_overrides": self.policy_overrides,
               "delays": self.delays, "protocols": self.protocols, "meta": self.meta}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @classmethod
    def load(cls, state_path, spec_path=None) -> "Orchestrator":
        doc = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if doc.get("schema") != STATE_SCHEMA:
            raise ValueError(f"unknown state file schema {doc.get('schema')}")
        spec = load_spec(spec_path or doc["spec_path"])
        if spec["_meta"]["sha256"] != doc["spec_sha256"]:
            raise ValueError("the spec changed since this run started; refusing to continue with a different spec")
        o = cls(spec, operator_id=doc["operator_id"], run_id=doc["run_id"], start=datetime.fromisoformat(doc["origin"]),
                faults=[], operator=SimOperator.from_dict(doc["operator"]), policy=doc["policy_overrides"],
                delays=doc["delays"], protocols=doc["protocols"], state_path=state_path, meta=doc.get("meta"))
        o.clock = SimClock.from_dict(doc["clock"])
        o.injector = FaultInjector.from_dict(doc["injector"])
        o.devs = build_devices(spec, o.d, o.clock, o.injector)
        for k, dev in o.devs.items():
            dev.load_dict(doc["devices"][k])
        o.cust = Custody.from_dict(doc["custody"], o.clock)
        for name in ("status", "i", "st", "seg", "marks", "alerts", "attempts", "interventions", "pending", "events",
                     "states_at_read", "incubation"):
            setattr(o, name, doc[name])
        problems = []
        if not verify_chain(o.events):
            problems.append("event log hash chain broken")
        problems += o.cust.verify()
        if problems:
            raise ValueError("state file failed its integrity check: " + "; ".join(problems))
        return o
