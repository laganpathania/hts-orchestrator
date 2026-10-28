import json
import random

import pytest

from htsorch.clock import SimClock
from htsorch.custody import LOCATIONS, STAGES, Custody, CustodyError, transfer_plate
from htsorch.faults import DeviceFault
from plant_helpers import ASSAY, PREP, T0, make_plant


def fresh():
    c = Custody(SimClock(T0))
    c.register("A", "BC-A", "assay")
    c.register("P", "BC-P", "prep")
    return c


def refusal_codes(c):
    return [e["code"] for e in c.history if e["op"].startswith("refused:")]


# ---- registering and the log ---------------------------------------------------------------------

def test_duplicate_plate_or_barcode_is_refused():
    c = fresh()
    with pytest.raises(CustodyError) as ei:
        c.register("A", "BC-NEW", "assay")
    assert ei.value.code == "K011"
    with pytest.raises(CustodyError):
        c.register("B", "BC-A", "assay")


def test_refusals_are_logged_and_change_nothing():
    c = fresh()
    before = json.dumps({k: v for k, v in c.to_dict()["plates"].items()}, sort_keys=True)
    with pytest.raises(CustodyError):
        c.advance("A", "seeded")                      # skipping steps
    with pytest.raises(CustodyError):
        c.begin_move("NOPE", "flex_deck")
    assert refusal_codes(c) == ["K006", "K001"]
    assert json.dumps({k: v for k, v in c.to_dict()["plates"].items()}, sort_keys=True) == before
    assert c.verify() == []


def test_history_tampering_and_state_drift_are_detected():
    c = fresh()
    c.begin_move("A", "flex_deck"); c.complete_move("A")
    assert c.verify() == []
    c.history[1]["destination"] = "reader"
    assert "custody log hash chain broken" in c.verify()
    c2 = fresh()
    c2.plates["A"].stage = "complete"                 # state changed without a log entry
    assert any("does not match the last log entry" in p for p in c2.verify())


# ---- location rules ---------------------------------------------------------------------------------------

def test_station_capacity_is_enforced():
    c = fresh()
    c.register("B", "BC-B", "assay"); c.register("C", "BC-C", "assay")
    for pid in ("A", "B"):
        c.begin_move(pid, "centrifuge") if pid == "A" else None
    c.complete_move("A")
    with pytest.raises(CustodyError) as ei:
        c.begin_move("B", "centrifuge")               # capacity 1
    assert ei.value.code == "K003"
    for pid in ("B", "C"):                            # flex deck holds 2
        c.begin_move(pid, "flex_deck"); c.complete_move(pid)
    c.register("D", "BC-D", "assay")
    with pytest.raises(CustodyError) as ei:
        c.begin_move("D", "flex_deck")
    assert ei.value.code == "K003"


def test_gripper_holds_one_plate_and_a_plate_in_the_gripper_cannot_be_processed():
    c = fresh()
    c.begin_move("A", "flex_deck")
    with pytest.raises(CustodyError) as ei:
        c.begin_move("P", "flex_deck")
    assert ei.value.code == "K004"
    with pytest.raises(CustodyError) as ei:
        c.advance("A", "edge_filled")
    assert ei.value.code == "K010"
    with pytest.raises(CustodyError) as ei:
        c.quarantine("A", "system", "x")
    assert ei.value.code == "K010"
    c.complete_move("A")
    assert c.verify() == [] and c.where("A") == "flex_deck"


def test_a_prep_plate_cannot_enter_the_incubator_or_reader():
    c = fresh()
    for dst in ("incubator", "reader", "centrifuge"):
        with pytest.raises(CustodyError) as ei:
            c.begin_move("P", dst)
        assert ei.value.code == "K005"
    with pytest.raises(CustodyError) as ei:
        c.begin_move("A", "moon")
    assert ei.value.code == "K012"
    with pytest.raises(CustodyError) as ei:
        c.begin_move("A", "plate_hotel")
    assert ei.value.code == "K013"


# ---- stage rules -------------------------------------------------------------------------------------------

def test_a_step_must_happen_at_its_station_and_in_order():
    c = fresh()
    c.begin_move("A", "incubator"); c.complete_move("A")
    with pytest.raises(CustodyError) as ei:
        c.advance("A", "compound_dosed")              # right order, wrong station
    assert ei.value.code == "K007"
    c2 = fresh()
    c2.begin_move("A", "idot_deck"); c2.complete_move("A")
    c2.advance("A", "compound_dosed")
    with pytest.raises(CustodyError) as ei:
        c2.advance("A", "compound_dosed")             # cannot repeat a step
    assert ei.value.code == "K006"
    with pytest.raises(CustodyError) as ei:
        c2.advance("A", "incubating")                 # cannot skip seeding
    assert ei.value.code == "K006"


def test_stage_table_is_a_chain_from_new_to_complete():
    for role, table in STAGES.items():
        seen, stage = [], "new"
        while stage != "complete":
            stage = list(table[stage])[-1]            # the longest path takes the last option
            seen.append(stage)
            assert len(seen) < 20
        assert seen[-1] == "complete"
        assert {s for t in table.values() for s in t} <= set(table) | {"complete"}


# ---- failed handoffs ----------------------------------------------------------------------------------------

def test_handoff_failure_before_pickup_leaves_the_plate_at_its_source_and_a_retry_works():
    p = make_plant([{"device": "arm", "command": "move", "code": "E301"}])
    with pytest.raises(DeviceFault) as ei:
        transfer_plate(p.cust, p.devs["arm"], ASSAY, "idot_deck", 2)
    assert ei.value.code == "E301"
    assert p.cust.where(ASSAY) == "plate_hotel" and p.cust.gripper is None
    assert [e["op"] for e in p.cust.timeline(ASSAY)][-2:] == ["move_begin", "move_aborted"]
    p.devs["arm"].recover("system")
    transfer_plate(p.cust, p.devs["arm"], ASSAY, "idot_deck", 2)
    assert p.cust.where(ASSAY) == "idot_deck" and p.cust.verify() == []


def test_plate_lost_in_transit_blocks_everything_until_a_person_confirms_where_it_is():
    p = make_plant([{"device": "arm", "command": "move", "code": "E302"}])
    c = p.cust
    with pytest.raises(DeviceFault):
        transfer_plate(c, p.devs["arm"], ASSAY, "idot_deck", 2)
    assert c.where(ASSAY) == "unknown" and c.gripper is None
    assert c.plates[ASSAY].flags[0]["code"] == "C001"
    for attempt in (lambda: c.advance(ASSAY, "compound_dosed"), lambda: c.begin_move(ASSAY, "flex_deck")):
        with pytest.raises(CustodyError) as ei:
            attempt()
        assert ei.value.code == "K010"
    with pytest.raises(CustodyError) as ei:
        c.human_place(ASSAY, "plate_hotel", "system", "auto")
    assert ei.value.code == "K009"
    p.devs["arm"].recover("A.Operator", "found the plate under the stacker")
    c.human_place(ASSAY, "plate_hotel", "A.Operator", "found under the stacker")
    assert [f["code"] for f in c.plates[ASSAY].flags] == ["C001", "C002"]       # the flag stays with the plate
    transfer_plate(c, p.devs["arm"], ASSAY, "idot_deck", 2)
    assert c.where(ASSAY) == "idot_deck" and c.verify() == []


def test_arm_in_error_refuses_the_move_and_the_plate_does_not_move():
    p = make_plant([{"device": "arm", "command": "move", "code": "E302"}])
    with pytest.raises(DeviceFault):
        transfer_plate(p.cust, p.devs["arm"], PREP, "flex_deck", 2)
    p.devs["arm"].recover("A.Operator")
    p.cust.human_place(PREP, "plate_hotel", "A.Operator", "located")
    p.devs["arm"].state, p.devs["arm"].last_fault = "error", {"code": "E302", "kind": "operator", "hint": ""}
    with pytest.raises(DeviceFault) as ei:
        transfer_plate(p.cust, p.devs["arm"], PREP, "flex_deck", 2)
    assert ei.value.code == "E001" and p.cust.where(PREP) == "plate_hotel" and p.cust.gripper is None


# ---- quarantine ----------------------------------------------------------------------------------------------------

def test_quarantined_plates_are_frozen_until_a_person_releases_them():
    c = fresh()
    c.begin_move("A", "idot_deck"); c.complete_move("A")
    c.quarantine("A", "system", "iDOT dispense could not be confirmed")
    with pytest.raises(CustodyError) as ei:
        c.advance("A", "compound_dosed")
    assert ei.value.code == "K008"
    with pytest.raises(CustodyError):
        c.begin_move("A", "flex_deck")
    with pytest.raises(CustodyError) as ei:
        c.release("A", "system", "auto")
    assert ei.value.code == "K009"
    c.begin_move("A", "quarantine"); c.complete_move("A")        # moving it out of the way is allowed
    c.release("A", "A.Operator", "dispense confirmed by weighing")
    assert c.plates["A"].status == "active"
    assert [f["code"] for f in c.plates["A"].flags] == ["Q001", "Q002"]
    assert c.verify() == []


def test_only_quarantined_or_complete_plates_can_be_discarded_and_only_at_the_discard_station():
    c = fresh()
    with pytest.raises(CustodyError) as ei:
        c.discard("A", "A.Operator", "no reason")
    assert ei.value.code == "K008"
    c.quarantine("A", "A.Operator", "contaminated")
    with pytest.raises(CustodyError) as ei:
        c.discard("A", "A.Operator", "bin it")
    assert ei.value.code == "K007"
    c.begin_move("A", "discard"); c.complete_move("A")
    c.discard("A", "A.Operator", "contaminated")
    assert c.plates["A"].status == "discarded"
    with pytest.raises(CustodyError):
        c.begin_move("A", "plate_hotel")


# ---- persistence and the invariant ---------------------------------------------------------------------------------------

def test_custody_survives_a_json_round_trip():
    p = make_plant([{"device": "arm", "command": "move", "code": "E302", "at": 2}])
    c = p.cust
    transfer_plate(c, p.devs["arm"], PREP, "flex_deck", 2)
    with pytest.raises(DeviceFault):
        transfer_plate(c, p.devs["arm"], ASSAY, "idot_deck", 2)
    saved = json.loads(json.dumps(c.to_dict()))
    c2 = Custody.from_dict(saved, SimClock(T0))
    assert c2.verify() == [] and c2.where(ASSAY) == "unknown" and c2.where(PREP) == "flex_deck"
    assert c2.plates[ASSAY].flags == c.plates[ASSAY].flags


@pytest.mark.parametrize("seed", range(8))
def test_no_sequence_of_requests_can_break_the_invariants(seed):
    rnd = random.Random(seed)
    c = Custody(SimClock(T0))
    ids = ["A1", "A2", "A3", "P1"]
    for i in ids:
        c.register(i, f"BC-{i}", "prep" if i.startswith("P") else "assay")
    stages = sorted({s for t in STAGES["assay"].values() for s in t} | {"new"})
    ops = [lambda: c.begin_move(rnd.choice(ids), rnd.choice(list(LOCATIONS) + ["moon"])),
           lambda: c.complete_move(rnd.choice(ids)),
           lambda: c.abort_move(rnd.choice(ids)),
           lambda: c.mark_lost(rnd.choice(ids)),
           lambda: c.advance(rnd.choice(ids), rnd.choice(stages)),
           lambda: c.quarantine(rnd.choice(ids), rnd.choice(["system", "Op"]), "fuzz"),
           lambda: c.release(rnd.choice(ids), rnd.choice(["system", "Op"]), "fuzz"),
           lambda: c.human_place(rnd.choice(ids), rnd.choice(list(LOCATIONS)), rnd.choice(["system", "Op"]), "fuzz"),
           lambda: c.discard(rnd.choice(ids), "Op", "fuzz")]
    ok = refused = 0
    for _ in range(600):
        try:
            rnd.choice(ops)()
            ok += 1
        except CustodyError:
            refused += 1
        assert c.verify() == [], c.history[-1]
    assert ok > 20 and refused > 50                    # the run exercised both outcomes


def test_guided_random_walks_reach_deep_states_and_keep_the_invariants():
    """Mostly plausible requests (so plates get far), with a share of bad ones mixed in."""
    reached, refused = set(), 0
    for seed in range(6):
        rnd = random.Random(100 + seed)
        c = Custody(SimClock(T0))
        ids = ["A1", "A2", "A3", "P1"]
        for i in ids:
            c.register(i, f"BC-{i}", "prep" if i.startswith("P") else "assay")
        for _ in range(500):
            pid = rnd.choice(ids)
            p = c.plates[pid]
            try:
                if p.location == "arm_gripper":
                    rnd.choice([c.complete_move] * 8 + [c.abort_move, c.mark_lost])(pid)
                elif p.location == "unknown":
                    c.human_place(pid, rnd.choice(["plate_hotel", "flex_deck"]), "Op", "found")
                elif rnd.random() < 0.55 and STAGES[p.role].get(p.stage):
                    nxt, need = rnd.choice(list(STAGES[p.role][p.stage].items()))
                    if need and p.location != need and c.gripper is None:
                        c.begin_move(pid, need)               # go where the next step happens
                    else:
                        c.advance(pid, nxt, note="walk")
                elif rnd.random() < 0.08:
                    rnd.choice([lambda: c.quarantine(pid, "system", "walk"), lambda: c.release(pid, "Op", "walk")])()
                elif c.gripper is None:
                    c.begin_move(pid, rnd.choice(list(LOCATIONS)))
                reached.add((p.role, p.stage, p.status))
            except CustodyError:
                refused += 1
            assert c.verify() == [], (seed, c.history[-1])
    assay_stages = {r[1] for r in reached if r[0] == "assay"}
    assert {"seeded", "incubating", "read"} <= assay_stages, assay_stages
    assert {r[2] for r in reached} >= {"active", "quarantined", "complete"}
    assert refused > 0
