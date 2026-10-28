import json

import pytest

from htsorch.custody import Custody
from htsorch.clock import SimClock
from htsorch.devices import build_devices
from htsorch.faults import DeviceFault, RecoveryNotPermitted
from plant_helpers import ASSAY, PREP, T0, call, make_plant, move, run_nominal


def test_nominal_run_completes_and_leaves_a_consistent_record():
    p = make_plant()
    run_nominal(p)
    assert p.cust.plates[ASSAY].status == "complete" and p.cust.plates[PREP].status == "complete"
    assert p.cust.verify() == [] and not p.inj.fired
    assert not [e for e in p.log if "fault" in e]
    # the stock the validator promised was exactly enough: nothing left over to spare
    flex = p.devs["flex"]
    for name, r in flex.reservoirs.items():
        assert r["volume_ul"] == pytest.approx(r["dead_ul"], abs=1e-6), name
    assert flex.tips["seeding"]["remaining"] == flex.tips["seeding"]["capacity"] - 2
    # simulated time: 72 h incubation plus the handling around it
    total_h = (p.clock.now() - T0).total_seconds() / 3600
    assert 72.5 < total_h < 75.5
    assert p.devs["incubator"].timers == {}


def test_nominal_run_visits_the_stations_in_the_expected_order():
    p = make_plant()
    run_nominal(p)
    path = [e["destination"] for e in p.cust.timeline(ASSAY) if e["op"] == "move_complete"]
    assert path == ["idot_deck", "flex_deck", "centrifuge", "incubator", "flex_deck", "reader"]
    assert p.devs["arm"].command_count == 8             # 6 assay moves + 2 prep moves


def test_transient_faults_on_several_devices_are_retried_and_the_run_still_completes():
    p = make_plant([
        {"device": "arm", "command": "move", "code": "E301", "at": 3},
        {"device": "idot", "command": "dispense", "code": "E201"},
        {"device": "reader", "command": "read", "code": "E601", "times": 2},
        {"device": "flex", "command": "run_protocol", "code": "E103", "at": 4},
    ])
    t_clean = make_plant()
    run_nominal(t_clean)
    run_nominal(p)
    assert p.cust.verify() == [] and p.cust.plates[ASSAY].status == "complete"
    assert len(p.inj.fired) == 5 and p.inj.pending() == []
    lost_min = sum(f["fault"]["minutes_lost"] for f in p.log if "fault" in f)
    extra = ((p.clock.now() - t_clean.clock.now()).total_seconds()) / 60
    assert extra == pytest.approx(lost_min)             # faults cost exactly their stated time
    assert not p.cust.plates[ASSAY].flags               # transient retries leave no plate flag


def test_operator_fault_stops_the_run_and_leaves_a_state_a_person_can_resume_from():
    p = make_plant([{"device": "arm", "command": "move", "code": "E302", "at": 5}])
    with pytest.raises(DeviceFault) as ei:
        run_nominal(p)
    assert ei.value.code == "E302"
    # 5th arm move = assay plate to the centrifuge (after the 2 prep moves and 2 assay moves)
    assert p.cust.where(ASSAY) == "unknown" and p.cust.plates[ASSAY].stage == "seeded"
    with pytest.raises(RecoveryNotPermitted):
        p.devs["arm"].recover("system")
    p.devs["arm"].recover("A.Operator", "plate found on the deck")
    p.cust.human_place(ASSAY, "flex_deck", "A.Operator", "plate found on the deck")
    assert p.cust.verify() == []


def test_everything_needed_to_resume_after_a_restart_is_in_the_saved_state():
    p = make_plant()
    move(p, PREP, "flex_deck"); move(p, ASSAY, "idot_deck")
    call(p, "flex", "run_protocol", protocol="x", minutes=5, tips={"dilution": 5})
    saved = json.loads(json.dumps({"clock": p.clock.to_dict(), "custody": p.cust.to_dict(), "injector": p.inj.to_dict(),
                                   "devices": {k: v.to_dict() for k, v in p.devs.items()}}))
    clock = SimClock.from_dict(saved["clock"])
    cust = Custody.from_dict(saved["custody"], clock)
    devs = build_devices(p.spec, p.d, clock, None)
    for k, dev in devs.items():
        dev.load_dict(saved["devices"][k])
    assert cust.verify() == [] and cust.where(ASSAY) == "idot_deck"
    assert devs["flex"].tips["dilution"]["remaining"] == p.devs["flex"].tips["dilution"]["remaining"]
    assert clock.now() == p.clock.now()
