"""Scenario runner: runs each scenario on the simulated plant and checks what came out.

Two kinds of checks:
  * the scenario's own `expect` block (status, faults, deviations, flags ...), exact;
  * invariants every run must satisfy whatever the scenario says: custody and event logs verify,
    the saved state reloads to the same status, nothing is dosed twice, no plate is left in the
    gripper, every fault was answered, a complete run leaves every device clear and a verifiable
    data package.

A scenario with no `expect` fails: a scenario that checks nothing proves nothing."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import datapackage, readout
from .chain import verify_chain
from .devices import idot_needs
from .engine import ASSAY, PREP, EngineError, Orchestrator
from .platemap import build_plate_map
from .scenario import load_scenario
from .spec import derive, load_spec
from .validator import validate

ANSWERS = {"recovery", "operator_action", "paused", "aborted", "operator_decision"}


@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    status: str = ""
    failures: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def line(self) -> str:
        f = self.facts
        bits = [f"status {self.status}", f"faults {','.join(f.get('faults', [])) or '-'}",
                f"deviations {','.join(f.get('deviations') or []) or '-'}" if f.get("deviations") is not None else "no record"]
        head = f"{'PASS' if self.passed else 'FAIL'}  {self.name:<24} " + "  ".join(bits)
        return head + "".join(f"\n        - {m}" for m in self.failures)


def build_orchestrator(spec: dict, sc: dict, *, operator_id: str, start: datetime | None, protocols: dict,
                       state_path, meta: dict | None = None, extra_compound_wait_min: float = 0.0) -> Orchestrator:
    """One place that turns a parsed scenario into an Orchestrator (used by `run` and the runner)."""
    delays = dict(sc["delays"])
    if extra_compound_wait_min:
        delays["compound_wait_min"] = extra_compound_wait_min
    op = sc["operator"]
    op.name = operator_id
    return Orchestrator(spec, operator_id=operator_id, start=start, faults=sc["faults"], operator=op,
                        policy=sc["policy"], delays=delays, protocols=protocols, state_path=state_path,
                        meta={**(meta or {}), "scenario": sc["description"]}, inventory=sc["inventory"])


def build_context(spec_path, work: Path) -> dict:
    """Build the protocols once (needed for the package inputs) and derive everything else."""
    from .cli import cmd_build
    spec = load_spec(spec_path)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_build(str(spec_path), str(work / "build"), simulate=False)
    if rc:
        raise RuntimeError(f"{spec_path} does not build; fix the spec before running scenarios")
    d = derive(spec)
    pm = build_plate_map(spec, d)
    build_dir = work / "build" / spec["experiment"]["id"]
    manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    return {"spec": spec, "d": d, "pm": pm, "findings": validate(spec, d, pm), "manifest": manifest,
            "build_dir": build_dir, "protocols": {n: h for n, h in manifest["files"].items() if n.endswith(".py")},
            "spec_path": spec_path}


def _drive(sc: dict, o: Orchestrator) -> str:
    """Run, apply the scripted `then` actions, and let the incubation pass unless the scenario expects to be waiting."""
    status = o.run()
    for i, act in enumerate(sc["then"], 1):
        kw = {k: v for k, v in act.items() if k != "respond"}
        if "respond" in act:
            kw["response"] = act["respond"]
        try:
            status = o.resume(**kw)
        except EngineError as e:
            raise EngineError(f"then[{i}] could not be applied: {e}") from e
    if status == "waiting" and sc["expect"]["status"] != "waiting":
        status = o.resume(until_wake=True, wake_delay_h=sc["wake_delay_h"])
    return status


def run_scenario(path, ctx: dict, work: Path) -> ScenarioResult:
    path = Path(path)
    name = path.stem
    try:
        sc = load_scenario(path)
    except Exception as e:                               # an invalid scenario file is a failed scenario
        return ScenarioResult(name, "", False, "invalid", [f"scenario file is invalid: {e}"])
    if sc["expect"] is None:
        return ScenarioResult(name, sc["description"], False, "no expectation",
                              ["no 'expect' block: a scenario that checks nothing proves nothing"])
    res = ScenarioResult(name, sc["description"], True)
    run_dir = work / name
    try:
        o = build_orchestrator(ctx["spec"], sc, operator_id="SIMULATED-OPERATOR", start=None, protocols=ctx["protocols"],
                               state_path=run_dir / "state.json", meta={"seed": 7})
        status = _drive(sc, o)
    except Exception as e:                               # staging or driving failed: report, do not crash the batch
        res.passed, res.status = False, "error"
        res.failures.append(f"{type(e).__name__}: {e}")
        return res
    res.status = status
    rec = None
    if status == "complete":
        rec = o.run_record()
    facts = {"faults": [e["code"] for e in o.events if e["step"] == "fault"],
             "deviations": sorted({d["code"] for d in rec["deviations"]}) if rec else None,
             "interventions": len(o.interventions),
             "plate_flags": {pid: [f["code"] for f in p.flags] for pid, p in o.cust.plates.items()},
             "alerts": {c: a["decision"] for c, a in o.alerts.items()},
             "steps": {e["step"] for e in o.events},
             "resumed": {e["step"] for e in o.events if e.get("resumed")}}
    res.facts = facts
    fails = res.failures
    fails += _check_expect(sc["expect"], o, status, facts)
    fails += _check_invariants(o, status, ctx, run_dir, rec, sc["expect"])
    res.passed = not fails
    return res


def _check_expect(exp: dict, o: Orchestrator, status: str, facts: dict) -> list[str]:
    out = []

    def want(label, expected, actual):
        if expected != actual:
            out.append(f"{label}: expected {expected}, got {actual}")
    want("status", exp["status"], status)
    if "faults" in exp:
        want("faults", exp["faults"], facts["faults"])
    if "deviations" in exp:
        if facts["deviations"] is None:
            out.append(f"deviations: expected {sorted(exp['deviations'])}, but the run is {status} so there is no run record")
        else:
            want("deviations", sorted(exp["deviations"]), facts["deviations"])
    if "interventions" in exp:
        want("interventions", exp["interventions"], facts["interventions"])
    for pid, codes in (exp.get("plate_flags") or {}).items():
        want(f"plate_flags[{pid}]", codes, facts["plate_flags"].get(pid, "unknown plate"))
    for code, decision in (exp.get("alerts") or {}).items():
        want(f"alerts[{code}]", decision, facts["alerts"].get(code, "no alert"))
    if "paused_reason_contains" in exp:
        reason = (o.pending or {}).get("reason", "") if status == "paused" else f"(run is {status})"
        if exp["paused_reason_contains"] not in reason:
            out.append(f"paused reason: expected to contain {exp['paused_reason_contains']!r}, got {reason!r}")
    for s in exp.get("resumed_steps", []):
        if s not in facts["resumed"]:
            out.append(f"resumed_steps: {s} did not finish after a stop (resumed steps: {sorted(facts['resumed']) or 'none'})")
    for s in exp.get("events_include", []):
        if s not in facts["steps"]:
            out.append(f"events_include: no '{s}' event in the log")
    for s in exp.get("events_absent", []):
        if s in facts["steps"]:
            out.append(f"events_absent: found a '{s}' event in the log")
    return out


def _check_invariants(o: Orchestrator, status: str, ctx: dict, run_dir: Path, rec, exp: dict) -> list[str]:
    bad = [f"invariant: {p}" for p in o.cust.verify()]
    if not verify_chain(o.events):
        bad.append("invariant: event log hash chain broken")
    if o.cust.gripper is not None:
        bad.append(f"invariant: {o.cust.gripper} left in the gripper")
    plan = sum(idot_needs(o.pm).values())
    dosed = o.devs["idot"].dispensed_nl
    if dosed > plan + 1e-6 or (status == "complete" and abs(dosed - plan) > 1e-6):
        bad.append(f"invariant: iDOT dispensed {dosed:g} nL, plan is {plan:g} nL (dosed twice or not at all)")
    for i, e in enumerate(o.events):
        if e["step"] == "fault" and not any(x["step"] in ANSWERS for x in o.events[i + 1:]):
            bad.append(f"invariant: fault {e['code']} at {e['start_utc']} was never answered")
    try:
        again = Orchestrator.load(run_dir / "state.json")
        if again.status != status or again.events[-1]["event_hash"] != o.events[-1]["event_hash"]:
            bad.append("invariant: the saved state does not reload to the same status and log")
    except Exception as e:
        bad.append(f"invariant: the saved state does not reload: {e}")
    if status == "complete":
        uncleared = [k for k, v in o.devs.items() if v.state != "idle"]
        if uncleared:
            bad.append(f"invariant: devices still in error after a complete run: {uncleared}")
        if any(p.status != "complete" for p in o.cust.plates.values()):
            bad.append("invariant: a complete run left a plate not complete")
        if exp.get("package", True):
            bad += _check_package(o, ctx, run_dir, rec)
    elif exp.get("package") is True:
        bad.append(f"package: expected a data package, but the run is {status}")
    if status == "aborted" and any(p.status == "active" for p in o.cust.plates.values()):
        bad.append("invariant: an aborted run left a plate active instead of quarantined")
    return bad


def _check_package(o: Orchestrator, ctx: dict, run_dir: Path, rec) -> list[str]:
    spec, d, pm = ctx["spec"], ctx["d"], ctx["pm"]
    try:
        truth = readout.simulate_truth(spec, 7)
        readings = readout.simulate_reader(spec, pm, truth, 7)
        export = readout.write_reader_export(spec, readings)
        analysis = readout.analyze(pm, readout.parse_reader_export(export, spec["assay"]["plate"]["barcode"]))
        pkg = datapackage.write_package(run_dir, spec=spec, d=d, plate_map=pm, findings=ctx["findings"],
                                        manifest=ctx["manifest"], build_dir=ctx["build_dir"], run=rec, truth=truth,
                                        readings=readings, export_text=export, analysis=analysis, animl_mode="off")
        return [f"package: {p}" for p in datapackage.verify_package(pkg)]
    except Exception as e:
        return [f"package: could not be built: {type(e).__name__}: {e}"]


def find_scenarios(paths) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    return out


def run_all(paths, spec_path, work_dir=None) -> list[ScenarioResult]:
    files = find_scenarios(paths)
    if not files:
        raise ValueError("no scenario files found")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(work_dir) if work_dir else Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        ctx = build_context(spec_path, work)
        return [run_scenario(f, ctx, work / "runs") for f in files]
