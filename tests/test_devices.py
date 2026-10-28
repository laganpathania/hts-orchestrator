import json

import pytest

from htsorch.clock import SimClock
from htsorch.devices import Incubator, build_devices, idot_needs
from htsorch.faults import FAULTS, DeviceFault, FaultInjector, RecoveryNotPermitted
from plant_helpers import ASSAY, T0, make_plant


# ---- clock ------------------------------------------------------------------------------

def test_clock_advances_and_never_runs_backwards():
    c = SimClock(T0)
    c.advance(90)
    assert (c.now() - T0).total_seconds() == 5400
    with pytest.raises(ValueError):
        c.advance(-1)
    with pytest.raises(ValueError):
        c.advance_to(T0)
    assert SimClock.from_dict(json.loads(json.dumps(c.to_dict()))).now() == c.now()


# ---- injector -----------------------------------------------------------------------------

def test_injector_rejects_wrong_device_and_unknown_code():
    with pytest.raises(ValueError, match="belongs to 'arm'"):
        FaultInjector([{"device": "flex", "command": "run_protocol", "code": "E301"}])
    with pytest.raises(ValueError, match="unknown fault code"):
        FaultInjector([{"device": "arm", "command": "move", "code": "E999"}])
    with pytest.raises(ValueError):
        FaultInjector([{"device": "arm", "command": "move", "code": "E301", "at": 0}])


def test_injected_fault_fires_on_the_nth_call_then_a_retry_succeeds():
    p = make_plant([{"device": "arm", "command": "move", "code": "E301", "at": 2}])
    arm = p.devs["arm"]
    arm.execute("move", plate="X", src="a", dst="b", minutes=2)          # call 1 fine
    with pytest.raises(DeviceFault) as ei:
        arm.execute("move", plate="X", src="b", dst="c", minutes=2)      # call 2 fails
    assert ei.value.code == "E301" and ei.value.injected and arm.state == "error"
    arm.recover("system")
    arm.execute("move", plate="X", src="b", dst="c", minutes=2)          # call 3 fine
    assert p.inj.fired[0]["call"] == 2 and p.inj.pending() == []


def test_times_makes_a_fault_persist_across_retries():
    p = make_plant([{"device": "reader", "command": "read", "code": "E601", "at": 1, "times": 3}])
    r = p.devs["reader"]
    for _ in range(3):
        with pytest.raises(DeviceFault):
            r.execute("read", plate="X", barcode="B", expected_barcode="B", mode="luminescence", minutes=10)
        r.recover("system")
    assert r.execute("read", plate="X", barcode="B", expected_barcode="B", mode="luminescence", minutes=10)


def test_unfired_rule_is_reported_as_pending():
    p = make_plant([{"device": "arm", "command": "move", "code": "E301", "at": 5}])
    assert len(p.inj.pending()) == 1


# ---- error state and recovery ------------------------------------------------------------------

def test_device_in_error_refuses_commands_until_recovered():
    p = make_plant([{"device": "arm", "command": "move", "code": "E301"}])
    arm = p.devs["arm"]
    with pytest.raises(DeviceFault):
        arm.execute("move", plate="X", src="a", dst="b", minutes=2)
    with pytest.raises(DeviceFault) as ei:
        arm.execute("move", plate="X", src="a", dst="b", minutes=2)
    assert ei.value.code == "E001" and ei.value.detail["blocked_by"] == "E301"
    assert arm.fault_count == 1                                          # E001 is a refusal, not a new fault


def test_system_may_clear_transient_faults_but_not_operator_faults():
    p = make_plant([{"device": "arm", "command": "move", "code": "E302"}])
    arm = p.devs["arm"]
    with pytest.raises(DeviceFault) as ei:
        arm.execute("move", plate="X", src="a", dst="b", minutes=2)
    assert ei.value.kind == "operator"
    with pytest.raises(RecoveryNotPermitted):
        arm.recover("system")
    rec = arm.recover("A.Operator", "checked the gripper")
    assert rec["cleared"] == "E302" and arm.state == "idle" and arm.recoveries[-1]["actor"] == "A.Operator"


def test_a_fault_costs_simulated_time():
    p = make_plant([{"device": "idot", "command": "dispense", "code": "E201"}])
    t = p.clock.now()
    with pytest.raises(DeviceFault) as ei:
        p.devs["idot"].execute("dispense", needs_nl={"A1": 250.0}, minutes=20)
    assert (p.clock.now() - t).total_seconds() / 60 == FAULTS["E201"].minutes_lost == 2.0
    assert ei.value.at == p.clock.now().isoformat()


def test_unknown_command_and_wrong_saved_state_are_rejected():
    p = make_plant()
    with pytest.raises(ValueError, match="unknown command"):
        p.devs["arm"].execute("teleport")
    with pytest.raises(ValueError, match="saved state is for"):
        p.devs["arm"].load_dict(p.devs["reader"].to_dict())


# ---- Flex inventory ----------------------------------------------------------------------------------

def test_flex_runs_out_of_tips_partway_and_recovers_after_a_reload():
    p = make_plant()
    flex = p.devs["flex"]
    cap = flex.tips["seeding"]["capacity"]
    flex.execute("run_protocol", protocol="seed", minutes=1, tips={"seeding": cap - 1})
    with pytest.raises(DeviceFault) as ei:
        flex.execute("run_protocol", protocol="seed", minutes=1, tips={"seeding": 3}, plate=ASSAY)
    f = ei.value
    assert f.code == "E101" and f.plate_risk == "partial_step"
    assert f.detail["short_tips"][0]["available"] == 1 and f.detail["completed_fraction"] == pytest.approx(1 / 3, abs=1e-3)
    assert f.detail["work_minutes"] == pytest.approx(1 / 3, abs=1e-3)
    assert flex.tips["seeding"]["remaining"] == 0                        # it used what was there
    with pytest.raises(RecoveryNotPermitted):
        flex.recover("system")
    flex.execute("load_tips", group="seeding", actor="A.Operator")       # maintenance works while in error
    flex.recover("A.Operator", "new rack")
    flex.execute("run_protocol", protocol="seed", minutes=1, tips={"seeding": 3})
    assert flex.tips["seeding"]["remaining"] == cap - 3


def test_flex_liquid_stops_at_the_dead_volume():
    p = make_plant()
    flex = p.devs["flex"]
    need = p.d["reservoir_need_ul"]["media"]
    flex.execute("run_protocol", protocol="seed", minutes=1, draws={"media": need})      # exactly what was planned
    assert flex.reservoirs["media"]["volume_ul"] == pytest.approx(flex.reservoirs["media"]["dead_ul"])
    with pytest.raises(DeviceFault) as ei:
        flex.execute("run_protocol", protocol="seed2", minutes=1, draws={"media": 100.0})
    assert ei.value.code == "E102" and ei.value.detail["short_liquid"][0]["available_ul"] == 0
    flex.execute("refill", reservoir="media", volume_ul=500.0, actor="A.Operator")
    flex.recover("A.Operator")
    flex.execute("run_protocol", protocol="seed2", minutes=1, draws={"media": 100.0})


def test_short_reservoir_load_fails_the_run_and_reports_how_much():
    full = make_plant().devs["flex"].reservoirs["cells"]["volume_ul"]
    p = make_plant(reservoir_load_ul={"cells": full - 2000.0})
    with pytest.raises(DeviceFault) as ei:
        p.devs["flex"].execute("run_protocol", protocol="seed", minutes=1,
                               draws={"cells": p.d["reservoir_need_ul"]["cells"]})
    s = ei.value.detail["short_liquid"][0]
    assert ei.value.code == "E102" and s["needed_ul"] - s["available_ul"] == pytest.approx(2000.0, abs=1)


def test_refill_beyond_capacity_is_refused():
    p = make_plant()
    with pytest.raises(ValueError, match="exceeds"):
        p.devs["flex"].execute("refill", reservoir="media", volume_ul=1e6, actor="A.Operator")


# ---- iDOT -------------------------------------------------------------------------------------------------

def test_idot_has_enough_source_volume_for_the_plate_map():
    p = make_plant()
    needs = idot_needs(p.pm)
    res = p.devs["idot"].execute("dispense", needs_nl=needs, minutes=20, plate=ASSAY)
    expected = sum(w["dispense_nl"] for w in p.pm if w["type"] != "pbs")
    assert res.detail["dispensed_nl"] == pytest.approx(expected)
    assert all(v >= p.d["idot_dead_ul"] - 1e-9 for v in p.devs["idot"].sources.values())


def test_idot_dispenses_what_it_can_then_reports_the_short_wells():
    p = make_plant()
    idot = p.devs["idot"]
    well = next(iter(idot.sources))
    idot.sources[well] = idot.dead_ul + 0.4              # 0.4 uL usable = 400 nL
    with pytest.raises(DeviceFault) as ei:
        idot.execute("dispense", needs_nl={well: 750.0, "A12": 250.0}, minutes=20, plate=ASSAY)
    f = ei.value
    assert f.code == "E202" and f.detail["short_ul"][well] == pytest.approx(0.35, abs=1e-3)
    assert f.detail["dispensed_nl"] == pytest.approx(650.0) and idot.sources[well] == pytest.approx(idot.dead_ul)
    idot.execute("load_source", well=well, volume_ul=5.0, actor="A.Operator")
    idot.recover("A.Operator")
    idot.execute("dispense", needs_nl={well: 350.0}, minutes=5)


# ---- the other devices ---------------------------------------------------------------------------------------

def test_incubation_time_survives_a_restart():
    p = make_plant()
    inc = p.devs["incubator"]
    inc.execute("store", plate=ASSAY)
    saved = json.loads(json.dumps(inc.to_dict()))
    saved_clock = json.loads(json.dumps(p.clock.to_dict()))
    # "process restarts": new clock, new device, state loaded from disk, time has passed
    clock2 = SimClock.from_dict(saved_clock)
    clock2.advance(72 * 60 + 5)
    inc2 = Incubator(clock2)
    inc2.load_dict(saved)
    res = inc2.execute("check", plate=ASSAY)
    assert res.detail["elapsed_h"] == pytest.approx(72.0 + 5 / 60, abs=0.01)


def test_incubator_excursion_is_an_operator_fault_with_environment_risk():
    p = make_plant([{"device": "incubator", "command": "check", "code": "E401",
                     "detail": {"temp_c": 41.5, "minutes": 20}}])
    p.devs["incubator"].execute("store", plate=ASSAY)
    with pytest.raises(DeviceFault) as ei:
        p.devs["incubator"].execute("check", plate=ASSAY)
    assert ei.value.plate_risk == "environment" and ei.value.detail == {"temp_c": 41.5, "minutes": 20}


def test_incubator_full_and_unknown_plate():
    p = make_plant()
    inc = p.devs["incubator"]
    for i in range(inc.slots):
        inc.execute("store", plate=f"P{i}")
    with pytest.raises(DeviceFault) as ei:
        inc.execute("store", plate="P9")
    assert ei.value.code == "E402"
    assert inc.state == "error"
    inc.recover("system")                                  # transient: the system may clear it
    with pytest.raises(ValueError):
        inc.execute("check", plate="NOPE")


def test_centrifuge_refuses_without_a_balance_plate_and_reader_refuses_the_wrong_plate():
    p = make_plant()
    cf = p.devs["centrifuge"]
    cf.balance_plate_loaded = False
    with pytest.raises(DeviceFault) as ei:
        cf.execute("spin", plate=ASSAY, minutes=3)
    assert ei.value.code == "E501"
    cf.execute("load_balance", actor="A.Operator")
    cf.recover("A.Operator")
    assert cf.execute("spin", plate=ASSAY, minutes=3)
    with pytest.raises(DeviceFault) as ei:
        p.devs["reader"].execute("read", plate=ASSAY, barcode="OTHER", expected_barcode="ASSAY-0001",
                                 mode="luminescence", minutes=10)
    assert ei.value.code == "E602"
    p.devs["reader"].recover("A.Operator", "right plate loaded")
    with pytest.raises(ValueError, match="does not support"):
        p.devs["reader"].execute("read", plate=ASSAY, barcode="A", expected_barcode="A",
                                 mode="absorbance", minutes=10)


def test_every_device_state_round_trips_through_json():
    p = make_plant()
    p.devs["flex"].execute("run_protocol", protocol="x", minutes=1, tips={"seeding": 2}, draws={"media": 500.0})
    p.devs["incubator"].execute("store", plate=ASSAY)
    saved = json.loads(json.dumps({k: v.to_dict() for k, v in p.devs.items()}))
    fresh = build_devices(p.spec, p.d, SimClock(T0), None)
    for k, dev in fresh.items():
        dev.load_dict(saved[k])
    assert {k: v.status() for k, v in fresh.items()} == {k: v.status() for k, v in p.devs.items()}


def test_a_partial_step_costs_the_work_done_before_the_fault():
    p = make_plant()
    flex = p.devs["flex"]
    flex.tips["seeding"]["remaining"] = 1
    t = p.clock.now()
    with pytest.raises(DeviceFault) as ei:
        flex.execute("run_protocol", protocol="seed", minutes=12.0, tips={"seeding": 2}, plate=ASSAY)
    assert ei.value.detail["completed_fraction"] == 0.5
    assert (p.clock.now() - t).total_seconds() / 60 == pytest.approx(6.0)       # half the step ran


def test_tip_and_liquid_shortages_are_both_reported_so_the_step_can_resume_once():
    p = make_plant()
    flex = p.devs["flex"]
    flex.tips["seeding"]["remaining"] = 0
    flex.reservoirs["media"]["volume_ul"] = flex.reservoirs["media"]["dead_ul"] + 100.0
    with pytest.raises(DeviceFault) as ei:
        flex.execute("run_protocol", protocol="seed", minutes=10, tips={"seeding": 1}, draws={"media": 400.0})
    d = ei.value.detail
    assert ei.value.code == "E101" and d["short_tips"][0]["needed"] == 1
    assert d["short_liquid"][0]["needed_ul"] == 400.0 and d["short_liquid"][0]["available_ul"] == 100.0
    assert d["completed_fraction"] == 0.0
