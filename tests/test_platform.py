import copy
import csv
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from htsorch.generators import idot, opentrons
from htsorch.platemap import build_plate_map
from htsorch.spec import derive, load_spec
from htsorch.validator import has_errors, validate

SPEC = Path(__file__).resolve().parents[1] / "specs" / "ctg3d_screen.yaml"


@pytest.fixture()
def spec():
    return load_spec(SPEC)


def codes(findings, level):
    return {f.code for f in findings if f.level == level}


# ---- derived numbers -------------------------------------------------------

def test_derived_numbers(spec):
    d = derive(spec)
    assert d["assay_volume_ul"] == 40
    assert d["total_well_volume_ul"] == 80
    assert d["dmso_percent"] == pytest.approx(0.075) and d["dmso_percent"] < 0.1
    assert d["top_stock_uM"] == pytest.approx(10 * 40000 / 30)
    assert d["nl_by_point"] == [30] * 5 and d["compound_nl_min"] >= 20
    assert d["final_uM"][0] == pytest.approx(10)
    assert d["final_uM"][-1] == pytest.approx(10 / 4 ** 4)
    assert d["top_master_ul"] == pytest.approx(25 * d["top_stock_uM"] / 20000)
    assert d["n_test_wells"] == 300 and d["n_assigned"] == 384


def test_valid_spec_has_no_errors_and_one_dmso_warning(spec):
    f = validate(spec)
    assert not has_errors(f)
    assert "V001" in codes(f, "WARN")


# ---- failure cases the validator must catch ---------------------------------

def test_dmso_above_limit_blocks(spec):
    spec["assay"]["compound_dispense"]["top_nl"] = 50      # 0.125%, not below the 0.1% limit
    assert "V001" in codes(validate(spec), "ERROR")


def test_dmso_exactly_at_the_limit_blocks(spec):
    spec["assay"]["compound_dispense"]["top_nl"] = 40      # 0.1%: the limit is "below", so this blocks
    assert "V001" in codes(validate(spec), "ERROR")


def test_dmso_at_or_below_warn_level_is_clean(spec):
    spec["assay"]["limits"]["dmso_warn_percent"] = 0.08   # 30 nL is 0.075%
    f = validate(spec)
    assert "V001" not in codes(f, "WARN") and "V001" not in codes(f, "ERROR")


def test_well_volume_over_plate_limit_blocks(spec):
    spec["assay"]["media_volume_ul"] = 25
    spec["assay"]["cell_volume_ul"] = 25
    assert "V002" in codes(validate(spec), "ERROR")


def test_reagent_reservoir_too_small_blocks(spec):
    spec["flex"]["reservoirs"]["reagent"] = {"load_name": "nest_12_reservoir_15ml", "capacity_ul": 15000, "dead_volume_ul": 1500}
    assert "V020" in codes(validate(spec), "ERROR")


def test_not_enough_tips_blocks(spec):
    spec["flex"]["tip_racks_loaded"]["dilution"] = 0
    assert "V021" in codes(validate(spec), "ERROR")


def test_idot_dead_volume_blocks(spec):
    spec["prep_plate"]["idot_source_dead_volume_ul"] = 20
    assert "V030" in codes(validate(spec), "ERROR")


def test_master_stock_shortage_blocks(spec):
    spec["compounds"]["master_available_ul"] = 3
    assert "V060" in codes(validate(spec), "ERROR")


def test_too_many_conditions_blocks(spec):
    spec["controls"]["negative"]["count"] = 60
    assert "V010" in codes(validate(spec), "ERROR")


def test_pipette_range_blocks(spec):
    spec["flex"]["pipettes"]["flex_1channel_50"]["min_ul"] = 20
    assert "V011" in codes(validate(spec), "ERROR")   # the 16.7 uL master transfer is below range


def test_timing_window_blocks_when_cells_age_out(spec):
    spec["timing"]["windows"]["cell_suspension_max_age_min"] = 10
    assert "V041" in codes(validate(spec), "ERROR")


def test_every_finding_says_what_did_and_who(spec):
    for f in validate(spec):
        assert f.what and f.system_action and f.who


# ---- plate map ---------------------------------------------------------------

def test_plate_map_counts_and_unique_wells(spec):
    pm = build_plate_map(spec)
    assert len(pm) == 384 and len({w["well"] for w in pm}) == 384
    kinds = [w["type"] for w in pm]
    assert (kinds.count("test"), kinds.count("neg"), kinds.count("pos")) == (300, 42, 42)


def test_every_condition_has_three_replicates(spec):
    pm = build_plate_map(spec)
    seen = {}
    for w in pm:
        if w["type"] == "test":
            seen.setdefault((w["compound_id"], w["conc_index"]), set()).add(w["replicate"])
    assert len(seen) == 100 and all(v == {1, 2, 3} for v in seen.values())


def test_plate_map_is_deterministic_and_seed_dependent(spec):
    a = [w["well"] + w["type"] + str(w["compound_id"]) for w in build_plate_map(spec)]
    b = [w["well"] + w["type"] + str(w["compound_id"]) for w in build_plate_map(copy.deepcopy(spec))]
    assert a == b
    spec["experiment"]["random_seed"] += 1
    c = [w["well"] + w["type"] + str(w["compound_id"]) for w in build_plate_map(spec)]
    assert a != c


def test_controls_are_stratified_between_edge_and_interior(spec):
    pm = build_plate_map(spec)
    for kind in ("neg", "pos"):
        edge = sum(1 for w in pm if w["type"] == kind and w["edge"])
        assert edge == round(42 * 76 / 384)


# ---- generators --------------------------------------------------------------

def test_idot_worklist_is_complete_and_constant_volume_for_the_shipped_spec(spec):
    pm = build_plate_map(spec)
    rows = list(csv.DictReader(io.StringIO(idot.generate_worklist(spec, pm))))
    assert len(rows) == 384
    assert {r["volume_nl"] for r in rows} == {"30"}
    assert len({r["target_well"] for r in rows}) == 384
    # DMSO delivered to every well is identical (solvent matched)
    assert all(float(r["volume_nl"]) == spec["assay"]["compound_dispense"]["top_nl"] for r in rows)


def test_worklist_sources_match_plate_map(spec):
    pm = {w["well"]: w for w in build_plate_map(spec)}
    for r in csv.DictReader(io.StringIO(idot.generate_worklist(spec, list(pm.values())))):
        assert r["source_well"] == pm[r["target_well"]]["source_well"]


@pytest.mark.parametrize("gen", [opentrons.generate_dilution, opentrons.generate_seeding, opentrons.generate_readout])
def test_generated_protocols_compile(spec, gen):
    compile(gen(spec), "generated.py", "exec")


@pytest.mark.skipif(shutil.which("opentrons_simulate") is None, reason="opentrons not installed")
@pytest.mark.parametrize("gen", [opentrons.generate_dilution, opentrons.generate_seeding, opentrons.generate_readout])
def test_generated_protocols_pass_opentrons_simulate(spec, gen, tmp_path):
    p = tmp_path / "proto.py"
    p.write_text(gen(spec))
    r = subprocess.run(["opentrons_simulate", str(p)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-500:]


@pytest.mark.skipif(shutil.which("opentrons_simulate") is None, reason="opentrons not installed")
def test_seeding_simulation_dispenses_every_well_once_per_liquid(spec, tmp_path):
    p = tmp_path / "seed.py"
    p.write_text(opentrons.generate_seeding(spec))
    out = subprocess.run(["opentrons_simulate", str(p)], capture_output=True, text=True, timeout=300).stdout
    disp = [l for l in out.splitlines() if l.startswith("Dispensing 20.0 uL into")]
    assert len(disp) == 2 * 48   # media + cells, 8-channel: 2 passes x 24 columns each


# ---- CellTiter-Glo 3D shake / incubation ---------------------------------------

def test_default_spec_passes_lysis_check(spec):
    f = validate(spec)
    assert "V044" not in codes(f, "ERROR")
    assert any(x.code == "V044" and x.level == "INFO" for x in f)


@pytest.mark.parametrize("shake,incubate,expected", [(2, 30, 1), (5, 10, 1), (2, 10, 2)])
def test_short_shake_or_incubation_blocks(spec, shake, incubate, expected):
    spec["assay"]["reagent"].update(shake_min=shake, incubate_min=incubate)
    errs = [f for f in validate(spec) if f.level == "ERROR" and f.code == "V044"]
    assert len(errs) == expected


@pytest.mark.parametrize("key", ["shake_min", "incubate_min", "shake_min_required", "incubate_min_required"])
def test_missing_lysis_parameter_blocks(spec, key):
    del spec["assay"]["reagent"][key]
    assert "V044" in codes(validate(spec), "ERROR")


def test_readout_protocol_states_shake_and_incubation(spec):
    code = opentrons.generate_readout(spec)
    assert "shake 5 min" in code and "incubate 30 min" in code


# ---- edge policy ---------------------------------------------------------------

def with_policy(spec, policy, reps=None):
    spec["assay"]["edge"]["policy"] = policy
    if reps:
        spec["dilution"]["replicates"] = reps
    return spec


def test_unknown_edge_policy_blocks(spec):
    assert "V013" in codes(validate(with_policy(spec, "everywhere")), "ERROR")


def test_controls_policy_puts_only_controls_on_perimeter(spec):
    pm = build_plate_map(with_policy(spec, "controls"))
    edge = [w for w in pm if w["edge"]]
    assert len(edge) == 76 and all(w["type"] in ("neg", "pos") for w in edge)
    assert abs(sum(w["type"] == "neg" for w in edge) - 38) <= 1
    assert all(not w["edge"] for w in pm if w["type"] == "test")
    assert len(pm) == 384 and not has_errors(validate(spec))


def test_controls_policy_warns_when_fewer_controls_than_perimeter(spec):
    spec = with_policy(spec, "controls")
    spec["controls"]["negative"]["count"] = 20
    spec["controls"]["positive"]["count"] = 20
    spec["dilution"]["replicates"] = 3
    f = validate(spec)
    assert any(x.code == "V013" and x.level == "WARN" for x in f)


def test_pbs_fill_blocks_when_conditions_do_not_fit(spec):
    f = validate(with_policy(spec, "pbs_fill"))
    assert "V010" in codes(f, "ERROR")


def test_pbs_fill_map_and_worklist(spec):
    spec = with_policy(spec, "pbs_fill", reps=2)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    pbs = [w for w in pm if w["type"] == "pbs"]
    assert len(pm) == 384 and len(pbs) == d["n_pbs"] == 100
    assert all(w["type"] == "pbs" for w in pm if w["edge"])
    assert all(w["dispense_nl"] == 0 for w in pbs)
    rows = list(csv.DictReader(io.StringIO(idot.generate_worklist(spec, pm))))
    assert len(rows) == 284
    assert {r["target_well"] for r in rows}.isdisjoint({w["well"] for w in pbs})


def test_pbs_fill_one_channel_seeding_blows_cell_hold_window(spec):
    f = validate(with_policy(spec, "pbs_fill", reps=2))
    assert {"V040", "V041"} <= codes(f, "ERROR")


def test_pbs_fill_requires_pbs_reservoir(spec):
    spec = with_policy(spec, "pbs_fill", reps=2)
    del spec["flex"]["reservoirs"]["pbs"]
    assert "V020" in codes(validate(spec), "ERROR")


def test_pbs_volume_mismatch_warns(spec):
    spec = with_policy(spec, "pbs_fill", reps=2)
    spec["assay"]["edge"]["pbs_volume_ul"] = 30
    assert "V014" in codes(validate(spec), "WARN")


@pytest.mark.skipif(shutil.which("opentrons_simulate") is None, reason="opentrons not installed")
def test_pbs_fill_protocols_simulate_and_skip_pbs_wells(spec, tmp_path):
    spec = with_policy(spec, "pbs_fill", reps=2)
    spec["timing"]["windows"]["cell_suspension_max_age_min"] = 60   # relaxed only for this test
    spec["timing"]["windows"]["compound_to_cells_max_min"] = 120    # (1-channel seeding is slow)
    assert not has_errors(validate(spec))
    d = derive(spec)
    pm = build_plate_map(spec, d)
    pbs = {w["well"] for w in pm if w["type"] == "pbs"}

    def dispensed_wells(code, volume):
        f = tmp_path / "p.py"
        f.write_text(code)
        r = subprocess.run(["opentrons_simulate", str(f)], capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-500:]
        import re
        return [m.group(1) for m in re.finditer(rf"Dispensing {volume} uL into (\w+) of Assay plate", r.stdout)]

    seeded = dispensed_wells(opentrons.generate_seeding(spec, d, pm), "20.0")
    assert len(seeded) == 2 * 284 and pbs.isdisjoint(seeded)
    filled = dispensed_wells(opentrons.generate_edge_fill(spec, d, pm), "40.0")
    assert set(filled) == pbs and len(filled) == 100


# ---- variable iDOT volume with a floor ---------------------------------------

def test_stock_factor_below_final_factor_makes_the_volume_vary(spec):
    spec["dilution"]["stock_factor"] = 3.5
    spec["assay"]["compound_dispense"]["top_nl"] = 39
    d = derive(spec)
    assert d["nl_by_point"] == pytest.approx([39, 34.1, 29.9, 26.1, 22.9], abs=0.05)
    assert d["dmso_percent"] == pytest.approx(0.0975) and d["dmso_percent"] < 0.1
    assert not has_errors(validate(spec))
    pm = build_plate_map(spec, d)
    by_conc = {(w["compound_id"], w["conc_index"]): w["dispense_nl"] for w in pm if w["type"] == "test"}
    assert {v for (c, j), v in by_conc.items() if j == 4} == {d["nl_by_point"][4]}
    assert {w["dispense_nl"] for w in pm if w["type"] == "neg"} == {d["vehicle_nl"]} == {39}
    rows = list(csv.DictReader(io.StringIO(idot.generate_worklist(spec, pm))))
    assert len({r["volume_nl"] for r in rows}) == 5 and min(float(r["volume_nl"]) for r in rows) >= 20


def test_idot_volume_below_the_minimum_blocks(spec):
    spec["dilution"]["stock_factor"] = 3
    spec["assay"]["compound_dispense"]["top_nl"] = 39
    f = validate(spec)
    assert "V003" in codes(f, "ERROR")
    assert any("below the 20 nL minimum" in x.what for x in f if x.code == "V003")


def test_the_shipped_example_specs_behave_as_their_headers_say():
    from htsorch.spec import load_spec
    ok = validate(load_spec("specs/example_variable_volume.yaml"))
    assert not has_errors(ok) and "V003" not in codes(ok, "ERROR")
    bad = validate(load_spec("specs/example_fails_idot_min_volume.yaml"))
    assert "V003" in codes(bad, "ERROR")
