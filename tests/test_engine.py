import csv
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from htsorch import datapackage
from htsorch.cli import main
from htsorch.devices import idot_needs
from htsorch.engine import ASSAY, PREP, EngineError, Orchestrator
from htsorch.policy import SimOperator
from htsorch.scenario import parse_scenario
from htsorch.spec import load_spec

SPECS = Path(__file__).resolve().parents[1] / "specs"
MAIN = SPECS / "ctg3d_screen.yaml"


def make(tmp_path, faults=None, operator=None, policy=None, delays=None, spec_path=MAIN):
    spec = load_spec(spec_path)
    op = SimOperator(name="T.Operator", **(operator or {}))
    return Orchestrator(spec, operator_id="T.Operator", faults=faults, operator=op, policy=policy, delays=delays,
                        state_path=tmp_path / "state.json")


def finish(o, wake_delay_h=0.0):
    st = o.run()
    return o.resume(until_wake=True, wake_delay_h=wake_delay_h) if st == "waiting" else st


def steps(o, name):
    return [e for e in o.events if e["step"] == name]


def arm_call_number(o, dst):
    moves = [s for s in o.steps if s["kind"] == "move"]
    return next(i for i, s in enumerate(moves, 1) if s["dst"] == dst)


# ---- the nominal run ----------------------------------------------------------------------------------

def test_nominal_run_waits_at_the_incubation_then_completes(tmp_path):
    o = make(tmp_path)
    assert o.run() == "waiting" and o.status == "waiting"
    assert o.cust.plates[ASSAY].stage == "incubating" and o.cust.where(ASSAY) == "incubator"
    assert o.resume(until_wake=True) == "complete"
    assert o.cust.plates[ASSAY].status == o.cust.plates[PREP].status == "complete"
    assert o.cust.verify() == [] and not o.interventions and not o.alerts
    assert not [e for e in o.events if e["step"] in ("fault", "paused", "window_alert")]


def test_measured_windows_equal_the_validators_predictions(tmp_path):
    """One timing model: what the validator predicts is what the engine measures."""
    o = make(tmp_path)
    finish(o)
    rec, est = o.run_record(), o.d["est"]
    ws = rec["window_summary"]
    assert ws["compound_to_cells_max_min"] == pytest.approx(est["compound_to_cells_min"])
    assert ws["cell_age_max_min"] == pytest.approx(est["cell_age_at_last_dispense_min"])
    assert ws["reagent_lag_min"] == pytest.approx(est["reagent_lag_min"])
    assert rec["deviations"] == []


def test_the_run_can_be_continued_by_a_new_process_from_the_state_file(tmp_path):
    o = make(tmp_path)
    o.run()
    again = Orchestrator.load(tmp_path / "state.json")
    assert again.status == "waiting" and again.run_id == o.run_id
    assert again.resume(until_wake=True) == "complete"
    assert again.incubation["within_tolerance"] and again.incubation["actual_h"] == pytest.approx(72.0)


def test_waiting_run_does_not_proceed_before_the_wake_time(tmp_path):
    o = make(tmp_path)
    o.run()
    assert o.resume(advance_min=60 * 70) == "waiting"                 # 70 h in
    assert o.resume(advance_min=60 * 2 + 1) == "complete"             # past 72 h


def test_late_wake_within_tolerance_is_fine_and_beyond_it_is_a_deviation(tmp_path):
    ok = make(tmp_path / "a")
    assert finish(ok, wake_delay_h=1.5) == "complete"
    assert ok.incubation["within_tolerance"] and not ok.run_record()["deviations"]
    late = make(tmp_path / "b")
    finish(late, wake_delay_h=3.0)
    assert late.incubation["actual_h"] == pytest.approx(75.0) and not late.incubation["within_tolerance"]
    assert [d["code"] for d in late.run_record()["deviations"]] == ["D043"]


def test_state_file_tampering_and_spec_changes_are_refused(tmp_path):
    o = make(tmp_path)
    o.run()
    path = tmp_path / "state.json"
    doc = json.loads(path.read_text())
    doc["events"][3]["duration_min"] = 1.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="integrity check"):
        Orchestrator.load(bad)
    spec = yaml.safe_load(MAIN.read_text())
    spec["timing"]["windows"]["incubation_h"] = 48
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(spec))
    with pytest.raises(ValueError, match="spec changed"):
        Orchestrator.load(path, changed)


def test_run_cannot_be_started_twice_and_incomplete_runs_have_no_record(tmp_path):
    o = make(tmp_path)
    o.run()
    with pytest.raises(EngineError):
        o.run()
    with pytest.raises(EngineError, match="complete run"):
        o.run_record()


# ---- transient faults: the system handles them -----------------------------------------------------------------

def test_transient_faults_are_retried_without_a_person(tmp_path):
    o = make(tmp_path, faults=[
        {"device": "arm", "command": "move", "code": "E301", "at": 3},
        {"device": "idot", "command": "dispense", "code": "E201"},
        {"device": "reader", "command": "read", "code": "E601", "times": 2},
        {"device": "flex", "command": "run_protocol", "code": "E103", "at": 4},
        {"device": "shaker", "command": "shake", "code": "E701"},
    ])
    assert finish(o) == "complete"
    assert len(steps(o, "fault")) == 6 and len(steps(o, "recovery")) == 6
    assert not o.interventions and o.run_record()["deviations"] == []
    assert o.cust.verify() == [] and not o.cust.plates[ASSAY].flags
    assert o.devs["idot"].dispensed_nl == pytest.approx(sum(idot_needs(o.pm).values()))   # nobody dosed twice


def test_a_stalled_shaker_restarts_the_full_shake_never_a_shortened_one(tmp_path):
    o = make(tmp_path, faults=[{"device": "shaker", "command": "shake", "code": "E701"}])
    assert finish(o) == "complete"
    need = o.spec["assay"]["reagent"]["shake_min"]
    assert [e["duration_min"] for e in steps(o, "shake")] == [need]          # one successful shake, full length
    assert steps(o, "fault")[0]["code"] == "E701" and need >= o.spec["assay"]["reagent"]["shake_min_required"]


def test_retries_are_limited_and_then_a_person_decides(tmp_path):
    fault = [{"device": "arm", "command": "move", "code": "E301", "at": 3, "times": 4}]
    o = make(tmp_path / "a", faults=fault)                                  # operator available: decides to retry
    assert finish(o) == "complete" and len(o.interventions) == 1
    assert [d["code"] for d in o.run_record()["deviations"]] == ["D050"]

    p = make(tmp_path / "b", faults=fault, operator={"available": False})   # nobody there: the run waits
    assert p.run() == "paused"
    assert "still failing after 3 retries" in p.pending["reason"]
    assert p.cust.where(ASSAY) == "plate_hotel" or p.cust.where(PREP) in ("plate_hotel", "flex_deck")
    assert p.resume(response={"actor": "A.Operator", "decision": "retry"}, until_wake=True) == "complete"
    assert p.interventions[0]["actor"] == "A.Operator"

    q = make(tmp_path / "c", faults=fault, operator={"stop_decision": "abort"})
    assert finish(q) == "aborted"
    assert all(pl.status == "quarantined" for pl in q.cust.plates.values())


def test_policy_table_can_be_changed_per_fault(tmp_path):
    fault = [{"device": "arm", "command": "move", "code": "E301", "at": 3, "times": 2}]
    o = make(tmp_path, faults=fault, policy={"E301": {"max_retries": 1}}, operator={"available": False})
    assert o.run() == "paused"                                              # two failures, one retry allowed


# ---- operator faults: stock, source volume, lost plate ---------------------------------------------------------------

def test_running_out_of_tips_mid_protocol_is_fixed_by_a_person_and_resumed(tmp_path):
    o = make(tmp_path)
    o.devs["flex"].tips["dilution"]["remaining"] = 10                      # protocol needs 22
    assert finish(o) == "complete"
    f = steps(o, "fault")[0]
    assert f["code"] == "E101" and f["detail"]["completed_fraction"] == pytest.approx(10 / 22, abs=1e-3)
    assert steps(o, "operator_action")[0]["loaded_tip_groups"] == ["dilution"]
    dil = steps(o, "flex_dilution_series")
    assert len(dil) == 1 and dil[0]["resumed"] is True                       # the rest of the step ran after the reload
    assert o.devs["flex"].tips["dilution"]["remaining"] == o.devs["flex"].tips["dilution"]["capacity"] - 12
    assert [d["code"] for d in o.run_record()["deviations"]] == ["D050"]


def test_a_short_reagent_load_delays_only_the_wells_served_after_the_refill(tmp_path):
    o = make(tmp_path)
    res = o.devs["flex"].reservoirs["reagent"]
    res["volume_ul"] = res["dead_ul"] + 0.5 * o.d["reservoir_need_ul"]["reagent"]
    assert finish(o) == "complete"
    rec = o.run_record()
    late = {w for w, v in rec["well_timing"].items() if v["reagent_offset_min"] > 15}
    expected = {w for w, p in o.progress.items() if p["reagent"] > 0.5}
    assert late == expected and 0 < len(late) < 384
    assert "D042" in {d["code"] for d in rec["deviations"]}
    # exact per-well times, worked out independently from the logged segments: the step ran
    # for half its length, stopped, and the rest ran after the refill
    t = lambda iso: o._min(iso)
    s1 = t(steps(o, "fault")[0]["start_utc"])
    ev = steps(o, "flex_reagent")[0]
    s2, total = t(ev["start_utc"]), o.d["est"]["reagent_min"]
    assert ev["duration_min"] == pytest.approx(total / 2) and s2 > s1 + total / 2 + 10      # a real gap
    first = s1 + total * min(p["reagent"] for p in o.progress.values())
    for w, p in o.progress.items():
        when = s1 + total * p["reagent"] if p["reagent"] <= 0.5 else s2 + (p["reagent"] - 0.5) * total
        assert rec["well_timing"][w]["reagent_offset_min"] == pytest.approx(when - first, abs=1e-6), w
    assert o.alerts["D042"]["decision"] == "continue"                        # raised before the data was affected further
    assert steps(o, "operator_action")[0]["refilled_ul"]["reagent"] > 0


def test_a_paused_run_ages_the_cells_and_the_flags_follow_the_late_wells(tmp_path):
    o = make(tmp_path, operator={"unavailable_codes": ["E102"], "response_min": 0})
    res = o.devs["flex"].reservoirs["cells"]
    res["volume_ul"] = res["dead_ul"] + 0.5 * o.d["reservoir_need_ul"]["cells"]
    assert o.run() == "paused" and o.pending["fault"]["code"] == "E102"
    assert o.resume(advance_min=50) == "paused"                              # time passes, nobody has come
    assert o.resume(response={"actor": "A.Operator", "decision": "retry"}, until_wake=True) == "complete"
    rec = o.run_record()
    late = {w for w, p in o.progress.items() if p["cells"] is not None and p["cells"] > 0.5}
    aged = {w for w, v in rec["well_timing"].items() if (v["cell_age_min"] or 0) > 45}
    assert aged == late
    assert {"D040", "D041", "D050"} <= {d["code"] for d in rec["deviations"]}
    assert steps(o, "time_passed")[0]["duration_min"] == 50.0


def test_idot_source_shortage_dispenses_the_rest_without_dosing_anything_twice(tmp_path):
    o = make(tmp_path)
    idot = o.devs["idot"]
    well = next(w for w in idot.sources if idot_needs(o.pm).get(w, 0) >= 750)
    short = idot_needs(o.pm)[well] / 1000.0 - 0.3
    idot.sources[well] = idot.dead_ul + 0.3
    assert finish(o) == "complete"
    f = steps(o, "fault")[0]
    assert f["code"] == "E202" and f["detail"]["short_ul"][well] == pytest.approx(short, abs=1e-3)
    assert idot.dispensed_nl == pytest.approx(sum(idot_needs(o.pm).values()))
    assert steps(o, "operator_action")[0]["reloaded_ul"][well] == pytest.approx(short, abs=1e-3)


@pytest.mark.parametrize("found_at", ["source", "destination"])
def test_a_lost_plate_is_found_by_a_person_and_flagged_for_good(tmp_path, found_at):
    n = arm_call_number(make(tmp_path / "probe"), "centrifuge")
    o = make(tmp_path / found_at, faults=[{"device": "arm", "command": "move", "code": "E302", "at": n}],
             operator={"plate_found_at": found_at})
    assert finish(o) == "complete"
    flags = [f["code"] for f in o.cust.plates[ASSAY].flags]
    assert flags == ["C001", "C002"] and o.cust.verify() == []
    assert steps(o, "operator_action")[0]["found_at"] == ("flex_deck" if found_at == "source" else "centrifuge")
    path = [e["destination"] for e in o.cust.timeline(ASSAY) if e["op"] == "move_complete"]
    assert path[-1] == "reader"
    if found_at == "source":
        assert "centrifuge" in path                                           # the move was repeated and completed
    else:
        assert "centrifuge" not in path                                       # a person put it there; no second move
        assert any(e["op"] == "human_place" and e["destination"] == "centrifuge" for e in o.cust.timeline(ASSAY))
    assert {"D050", "D051"} <= {d["code"] for d in o.run_record()["deviations"]}


def test_incubator_excursion_is_acknowledged_flagged_and_the_run_continues(tmp_path):
    o = make(tmp_path, faults=[{"device": "incubator", "command": "check", "code": "E401",
                                "detail": {"temp_c": 41.5, "minutes": 20}}])
    assert finish(o) == "complete"
    assert [f["code"] for f in o.cust.plates[ASSAY].flags] == ["E401"]
    assert {"D050", "D052"} <= {d["code"] for d in o.run_record()["deviations"]}
    assert len(steps(o, "fault")) == 1                                       # acknowledged, not re-checked in a loop


def test_wrong_plate_in_the_reader_stops_for_a_decision(tmp_path):
    fault = [{"device": "reader", "command": "read", "code": "E602"}]
    ok = make(tmp_path / "retry", faults=fault)
    assert finish(ok) == "complete" and ok.interventions[0]["code"] == "E602"
    ab = make(tmp_path / "abort", faults=fault, operator={"stop_decision": "abort"})
    assert finish(ab) == "aborted" and ab.cust.plates[ASSAY].stage == "lysed"


def test_a_fix_that_does_not_hold_cannot_loop_forever(tmp_path):
    """The injected fault carries no information the fix can act on, so every fix 'works' and the fault returns."""
    o = make(tmp_path, faults=[{"device": "idot", "command": "dispense", "code": "E202", "times": 10}])
    assert o.run() == "paused"
    assert len(o.interventions) == 3 and "came back after 3 fixes" in o.pending["reason"]


# ---- timing windows enforced while the run happens ------------------------------------------------------------------

def test_a_window_that_cannot_be_met_is_caught_before_cells_are_committed(tmp_path):
    o = make(tmp_path, delays={"compound_wait_min": 50}, operator={"window_breach": "abort"})
    assert o.run() == "aborted"
    a = o.alerts["D040"]
    assert a["predicted_min"] > 60 and a["decision"] == "abort"
    assert not steps(o, "flex_media")                                        # no cells were ever dispensed
    assert all(p.status == "quarantined" for p in o.cust.plates.values())
    with pytest.raises(EngineError):
        o.resume()


def test_continuing_past_a_predicted_breach_is_recorded_as_a_decision_and_a_deviation(tmp_path):
    o = make(tmp_path, delays={"compound_wait_min": 50}, operator={"response_min": 10})
    assert finish(o) == "complete"
    assert o.alerts["D040"]["decision"] == "continue" and o.alerts["D040"]["by"] == "T.Operator"
    rec = o.run_record()
    assert "D040" in {d["code"] for d in rec["deviations"]}
    assert rec["window_summary"]["compound_to_cells_max_min"] == pytest.approx(16.4 + 50 + 10)   # the decision took 10 min


def test_with_nobody_there_a_predicted_breach_pauses_the_run_for_a_decision(tmp_path):
    o = make(tmp_path, delays={"compound_wait_min": 50}, operator={"available": False})
    assert o.run() == "paused" and o.pending["kind"] == "window"
    assert o.resume(response={"actor": "A.Operator", "decision": "abort"}) == "aborted"
    p = make(tmp_path / "b", delays={"compound_wait_min": 50}, operator={"available": False})
    p.run()
    assert p.resume(response={"actor": "A.Operator", "decision": "continue"}, until_wake=True) == "complete"
    assert p.alerts["D040"]["by"] == "A.Operator"


# ---- the record the engine produces feeds the data package ------------------------------------------------------------------

def run_cli(tmp_path, scenario=None, *extra):
    argv = ["run", str(MAIN), "--out", str(tmp_path), "--no-simulate", "--operator", "T.Operator", *extra]
    if scenario is not None:
        f = tmp_path / "scenario.yaml"
        f.write_text(yaml.safe_dump(scenario))
        argv += ["--scenario", str(f)]
    return main(argv)


def test_a_faulted_run_still_produces_a_verifiable_package_with_audit_trail(tmp_path):
    n = arm_call_number(make(tmp_path / "p"), "centrifuge")
    rc = run_cli(tmp_path, {"faults": [{"device": "arm", "command": "move", "code": "E302", "at": n}]})
    assert rc == 0
    pkg = sorted(tmp_path.glob("package-*"))[-1]
    doc = json.loads((pkg / "datapackage.json").read_text())
    assert datapackage.verify_package(pkg) == []
    codes = {f["code"] for f in doc["flags"] if f["phase"] == "runtime"}
    assert {"D050", "D051"} <= codes
    rows = list(csv.DictReader(open(pkg / "wells.csv")))
    assert all("D051" in r["flags"] for r in rows)
    audit = json.loads((pkg / "audit" / "custody_log.json").read_text())
    assert [f["code"] for f in audit["plates"][ASSAY]] == ["C001", "C002"]
    assert any(e["step"] == "fault" and e["code"] == "E302" for e in doc["timeline"]["events"])
    assert doc["operations"]["interventions"][0]["code"] == "E302"
    (pkg / "audit" / "custody_log.json").write_text((pkg / "audit" / "custody_log.json").read_text().replace("C001", "X001"))
    assert any("custody_log" in p for p in datapackage.verify_package(pkg))


def test_cli_pauses_without_a_package_and_resume_finishes_it(tmp_path, capsys):
    n = arm_call_number(make(tmp_path / "p"), "centrifuge")
    rc = run_cli(tmp_path, {"faults": [{"device": "arm", "command": "move", "code": "E302", "at": n}],
                            "operator": {"available": False}})
    assert rc == 2 and not list(tmp_path.glob("package-*"))
    state = next(tmp_path.glob("*/state.json"))
    assert json.loads(state.read_text())["status"] == "paused"
    assert "PAUSED" in capsys.readouterr().out
    assert main(["resume", str(state)]) == 2                                  # no response: still paused
    rc = main(["resume", str(state), "--operator", "A.Operator", "--decision", "retry", "--until-wake",
               "--out", str(tmp_path)])
    assert rc == 0 and list(tmp_path.glob("package-*"))
    assert datapackage.verify_package(next(tmp_path.glob("package-*"))) == []


def test_cli_stops_at_the_wait_and_a_later_resume_completes(tmp_path):
    assert run_cli(tmp_path, None, "--stop-at-wait") == 2
    state = next(tmp_path.glob("*/state.json"))
    assert json.loads(state.read_text())["status"] == "waiting"
    assert main(["resume", str(state), "--until-wake", "--out", str(tmp_path)]) == 0
    assert datapackage.verify_package(next(tmp_path.glob("package-*"))) == []


def test_cli_aborted_run_returns_3_and_writes_no_package(tmp_path):
    rc = run_cli(tmp_path, {"delays": {"compound_wait_min": 50}, "operator": {"window_breach": "abort"}})
    assert rc == 3 and not list(tmp_path.glob("package-*"))


# ---- scenario files ---------------------------------------------------------------------------------------------------------------

def test_scenario_files_are_validated():
    assert parse_scenario({"faults": [{"device": "arm", "command": "move", "code": "E301"}]})["faults"]
    for bad in ({"fauts": []}, {"faults": [{"device": "flex", "command": "run_protocol", "code": "E301"}]},
                {"policy": {"E999": {}}}, {"operator": {"window_breach": "maybe"}}, {"delays": {"coffee_min": 5}},
                {"wake_delay_h": -1}):
        with pytest.raises(ValueError):
            parse_scenario(bad)
