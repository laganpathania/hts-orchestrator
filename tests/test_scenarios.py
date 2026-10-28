import json
from pathlib import Path

import pytest
import yaml

from htsorch import runner
from htsorch.cli import main
from htsorch.devices import apply_inventory
from htsorch.engine import Orchestrator
from htsorch.scenario import parse_scenario
from htsorch.spec import load_spec
from plant_helpers import make_plant

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "specs" / "ctg3d_screen.yaml"
SHIPPED = sorted((ROOT / "scenarios").glob("*.yaml"))


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    work = tmp_path_factory.mktemp("scenario_ctx")
    return runner.build_context(MAIN, work), work


@pytest.fixture(scope="module")
def shipped(ctx):
    c, work = ctx
    return {f.stem: runner.run_scenario(f, c, work / "runs") for f in SHIPPED}


def write(tmp_path, name, doc):
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


# ---- the shipped scenarios ----------------------------------------------------------------------------

def test_there_are_scenarios_for_every_kind_of_trouble():
    names = {f.stem for f in SHIPPED}
    assert len(names) >= 15
    assert {"low_tips", "short_reagent", "short_cells_pause", "idot_source_short", "lost_plate", "nobody_there",
            "late_handoff_abort", "stops_at_incubation"} <= names


@pytest.mark.parametrize("name", [f.stem for f in SHIPPED])
def test_each_shipped_scenario_behaves_as_its_expectation_says(shipped, name):
    r = shipped[name]
    assert r.passed, r.line()


def test_every_shipped_scenario_states_what_it_expects():
    for f in SHIPPED:
        sc = yaml.safe_load(f.read_text())
        assert sc.get("expect", {}).get("status"), f"{f.name} has no expected status"
        assert sc.get("description"), f"{f.name} has no description"


def test_the_staged_shortages_are_real_inventory_shortages_not_injected_faults(shipped):
    for name, code in (("low_tips", "E101"), ("short_reagent", "E102"), ("short_cells_pause", "E102"),
                       ("idot_source_short", "E202")):
        sc = yaml.safe_load((ROOT / "scenarios" / f"{name}.yaml").read_text())
        assert "faults" not in sc and "inventory" in sc, name
        assert shipped[name].facts["faults"] == [code]


# ---- the runner must FAIL when an expectation is wrong ---------------------------------------------------

def expect_fail(ctx, tmp_path, base, change, message):
    c, work = ctx
    doc = yaml.safe_load((ROOT / "scenarios" / f"{base}.yaml").read_text())
    change(doc)
    r = runner.run_scenario(write(tmp_path, f"bad_{base}", doc), c, work / "bad")
    assert not r.passed, f"the runner accepted a wrong expectation: {message}"
    assert any(message in m for m in r.failures), r.failures
    return r


def test_wrong_status_is_caught(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "lost_plate", lambda d: d["expect"].update(status="aborted"), "status: expected aborted, got complete")


def test_wrong_deviation_set_is_caught(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "lost_plate", lambda d: d["expect"].update(deviations=["D050"]), "deviations: expected ['D050']")
    expect_fail(ctx, tmp_path, "transient_faults", lambda d: d["expect"].update(deviations=["D050"]), "deviations")


def test_wrong_fault_order_or_count_is_caught(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "transient_faults", lambda d: d["expect"].update(faults=["E601", "E301", "E201", "E601"]), "faults:")
    expect_fail(ctx, tmp_path, "stuck_arm", lambda d: d["expect"].update(faults=["E301"]), "faults:")


def test_wrong_flags_interventions_and_alerts_are_caught(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "lost_plate", lambda d: d["expect"]["plate_flags"].update({"ASSAY-0001": ["C001"]}), "plate_flags")
    expect_fail(ctx, tmp_path, "lost_plate", lambda d: d["expect"].update(interventions=3), "interventions")
    expect_fail(ctx, tmp_path, "late_handoff_abort", lambda d: d["expect"]["alerts"].update({"D040": "continue"}), "alerts[D040]")


def test_events_and_resumed_steps_expectations_are_checked(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "low_tips", lambda d: d["expect"].update(events_absent=["operator_action"]), "events_absent")
    expect_fail(ctx, tmp_path, "low_tips", lambda d: d["expect"].update(events_include=["window_alert"]), "events_include")
    expect_fail(ctx, tmp_path, "low_tips", lambda d: d["expect"].update(resumed_steps=["flex_reagent"]), "resumed_steps")


def test_a_record_cannot_be_expected_from_a_run_that_has_none(ctx, tmp_path):
    expect_fail(ctx, tmp_path, "nobody_there", lambda d: d["expect"].update(deviations=["D050"]), "no run record")
    expect_fail(ctx, tmp_path, "nobody_there", lambda d: d["expect"].update(paused_reason_contains="banana"), "paused reason")
    expect_fail(ctx, tmp_path, "wrong_plate_abort", lambda d: d["expect"].update(package=True), "expected a data package")


def test_a_scenario_without_expectations_or_with_a_bad_file_fails(ctx, tmp_path):
    c, work = ctx
    r = runner.run_scenario(write(tmp_path, "noexpect", {"description": "x"}), c, work / "bad")
    assert not r.passed and "proves nothing" in r.failures[0]
    (tmp_path / "broken.yaml").write_text("description: a: b\n")
    r = runner.run_scenario(tmp_path / "broken.yaml", c, work / "bad")
    assert not r.passed and "invalid" in r.failures[0]


def test_staging_mistakes_are_reported_not_ignored(ctx, tmp_path):
    c, work = ctx
    for inv, msg in (({"tips": {"nope": 3}}, "no tip group"), ({"reservoirs": {"nope": {"fraction": 0.5}}}, "no reservoir"),
                     ({"idot_sources": {"ZZ99": {"usable_ul": 1}}}, "no source well")):
        r = runner.run_scenario(write(tmp_path, "badinv", {"inventory": inv, "expect": {"status": "complete"}}), c, work / "bad")
        assert not r.passed and msg in r.failures[0]
    r = runner.run_scenario(write(tmp_path, "latethen", {"expect": {"status": "complete"},
                                                         "then": [{"until_wake": True}, {"advance_min": 5}]}), c, work / "bad")
    assert not r.passed and "then[2] could not be applied" in r.failures[0]


# ---- invariants: the runner must notice a broken engine ---------------------------------------------------

def test_the_runner_notices_double_dosing(ctx, tmp_path, monkeypatch):
    """If resuming an iDOT dispense re-sent every well, the runner has to say so even though the scenario's own checks pass."""
    c, work = ctx
    monkeypatch.setattr(Orchestrator, "_absorb_partial", lambda self, step, f, t0: None)
    r = runner.run_scenario(ROOT / "scenarios" / "idot_source_short.yaml", c, work / "mut")
    assert not r.passed


def test_the_runner_notices_custody_damage(ctx, monkeypatch):
    from htsorch.custody import Custody
    c, work = ctx
    monkeypatch.setattr(Custody, "verify", lambda self: ["two plates in one place"])
    r = runner.run_scenario(ROOT / "scenarios" / "transient_faults.yaml", c, work / "mut2")
    assert not r.passed and any("invariant: two plates in one place" in m for m in r.failures)


def test_an_unanswered_fault_and_a_tampered_state_file_are_flagged(ctx, tmp_path):
    c, work = ctx
    sc = parse_scenario({"expect": {"status": "waiting"}})
    o = runner.build_orchestrator(c["spec"], sc, operator_id="X", start=None, protocols=c["protocols"],
                                  state_path=tmp_path / "state.json", meta={"seed": 7})
    o.run()
    assert runner._check_invariants(o, "waiting", c, tmp_path, None, {"status": "waiting", "package": False}) == []
    state = json.loads((tmp_path / "state.json").read_text())
    state["status"] = "complete"
    (tmp_path / "state.json").write_text(json.dumps(state))
    bad = runner._check_invariants(o, "waiting", c, tmp_path, None, {"status": "waiting", "package": False})
    assert any("reload" in m for m in bad)
    (tmp_path / "state.json").write_text(json.dumps({**state, "status": "waiting"}))
    o._event("fault", "arm", o.clock.now(), o.clock.now(), code="E301")           # a fault nobody answered
    bad = runner._check_invariants(o, "waiting", c, tmp_path, None, {"status": "waiting", "package": False})
    assert any("never answered" in m for m in bad)


# ---- scenario schema ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"expect": {}},                                                 # no status
    {"expect": {"status": "done"}},
    {"expect": {"status": "complete", "colour": "red"}},
    {"expect": {"status": "complete", "faults": "E301"}},
    {"expect": {"status": "complete", "interventions": -1}},
    {"expect": {"status": "complete", "plate_flags": {"A": "C001"}}},
    {"then": [{"respond": {"actor": "system"}}]},
    {"then": [{"respond": {"decision": "retry"}}]},
    {"then": [{"wait": 5}]},
    {"then": [{}]},
    {"inventory": {"tips": {"dilution": -1}}},
    {"inventory": {"reservoirs": {"media": {"fraction": 0.5, "volume_ul": 5}}}},
    {"inventory": {"idot_sources": {"A1": {"free_ul": 1}}}},
])
def test_invalid_scenario_files_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_scenario(bad)


def test_inventory_is_applied_exactly_and_validated_against_the_devices():
    p = make_plant()
    done = apply_inventory(p.devs, p.d, {"tips": {"seeding": 1}, "reservoirs": {"media": {"fraction": 0.25}, "cells": {"volume_ul": 4000}},
                                         "idot_sources": {"A1": {"usable_ul": 0.3}}})
    f, i = p.devs["flex"], p.devs["idot"]
    assert f.tips["seeding"]["remaining"] == 1 and f.reservoirs["cells"]["volume_ul"] == 4000
    assert f.reservoirs["media"]["volume_ul"] == pytest.approx(f.reservoirs["media"]["dead_ul"] + 0.25 * p.d["reservoir_need_ul"]["media"])
    assert i.sources["A1"] == pytest.approx(i.dead_ul + 0.3) and len(done) == 4
    with pytest.raises(ValueError, match="more than the"):
        apply_inventory(p.devs, p.d, {"tips": {"seeding": 10_000}})
    with pytest.raises(ValueError, match="capacity"):
        apply_inventory(p.devs, p.d, {"reservoirs": {"cells": {"volume_ul": 1e9}}})


# ---- the command line ---------------------------------------------------------------------------------------------------------

def test_cli_scenarios_exits_0_when_all_behave_and_1_when_one_does_not(tmp_path, capsys):
    good = tmp_path / "good"
    good.mkdir()
    (good / "ok.yaml").write_text((ROOT / "scenarios" / "stops_at_incubation.yaml").read_text())
    assert main(["scenarios", str(good), "--spec", str(MAIN)]) == 0
    assert "1 of 1 scenarios behave as expected" in capsys.readouterr().out
    doc = yaml.safe_load((ROOT / "scenarios" / "stops_at_incubation.yaml").read_text())
    doc["expect"]["status"] = "complete"
    (good / "wrong.yaml").write_text(yaml.safe_dump(doc))
    assert main(["scenarios", str(good), "--spec", str(MAIN)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  wrong" in out and "FAILED: wrong" in out


def test_run_command_accepts_a_staged_shortage_scenario(tmp_path):
    rc = main(["run", str(MAIN), "--out", str(tmp_path), "--no-simulate", "--animl", "off",
               "--scenario", str(ROOT / "scenarios" / "low_tips.yaml")])
    assert rc == 0
    pkg = next(tmp_path.glob("package-*"))
    doc = json.loads((pkg / "datapackage.json").read_text())
    assert any(f["code"] == "D050" for f in doc["flags"])
    state = json.loads(next(tmp_path.glob("*/state.json")).read_text())
    assert "tips 'dilution': 10 of 96 left" in state["meta"]["inventory"]
