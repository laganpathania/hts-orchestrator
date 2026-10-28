import copy
from pathlib import Path

import pytest

from htsorch import identify
from htsorch.cli import main
from htsorch.platemap import build_plate_map
from htsorch.spec import derive, load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC, ASSUME = ROOT / "specs" / "ctg3d_screen.yaml", ROOT / "identify" / "assumptions.yaml"


@pytest.fixture()
def a():
    return identify.load_assumptions(ASSUME)


@pytest.fixture(scope="module")
def result():
    return identify.run(SPEC, ASSUME)


def test_every_assumption_states_its_basis_and_sources_have_urls(a):
    def walk(n, path=""):
        if isinstance(n, dict) and "value" in n:
            assert n.get("basis") in ("source", "spec", "placeholder"), path
            if n["basis"] == "source":
                assert n.get("url", "").startswith("https://"), path
        elif isinstance(n, dict):
            for k, x in n.items():
                walk(x, f"{path}.{k}")
    walk({k: a[k] for k in ("manual", "automated", "economics", "fx_usd_to_eur")})
    assert sum(c["weight"] for c in a["fit_criteria"].values()) == 100


def test_baseline_follows_the_spec_not_a_fixed_number(a):
    spec = load_spec(SPEC)
    d = derive(spec)
    assert identify.manual_baseline(spec, d, a)["compound_mode"] == "hand"       # the comparison chosen for the case
    b1 = identify.manual_baseline(spec, d, a, compound="idot")
    assert b1["compound_mode"] == "idot"
    # 102 source wells + seeding: media 24 asp + 48 disp, cells 24 + 48, reagent 48 + 48
    assert b1["transfer_operations"] == 102 + (24 + 48) + (24 + 48) + (48 + 48)
    assert b1["compound_step_min"] == 10                                   # operator loads and starts; the run itself is unattended
    assert b1["compound_to_cells_min"] == pytest.approx(spec["timing"]["arm_transfer_min"] + b1["seeding_min"]["media"] + b1["seeding_min"]["cells"], abs=0.1)
    assert b1["window_ratio"] == pytest.approx(b1["compound_to_cells_min"] / 60, abs=0.01)
    h = identify.manual_baseline(spec, d, a, compound="hand")
    assert h["compound_step_min"] == pytest.approx((102 * 20 + 384 * 10) / 60)       # 102 aspirations, 384 dispenses
    assert h["window_ratio"] == pytest.approx(h["compound_step_min"] / 60, abs=0.01) and h["window_ratio"] > 1
    assert h["hands_on_h"] > b1["hands_on_h"]
    a2 = copy.deepcopy(a)
    a2["manual"]["randomized_layout"]["value"] = False
    assert identify.manual_baseline(spec, d, a2, compound="hand")["compound_step_min"] < h["compound_step_min"] / 2
    spec2 = copy.deepcopy(spec)
    spec2["controls"]["negative"]["count"] = 21
    spec2["controls"]["positive"]["count"] = 21
    assert identify.manual_baseline(spec2, derive(spec2), a, compound="hand")["transfer_operations"] == h["transfer_operations"] - 42


def test_seeding_uses_multi_dispense_and_the_iDOT_time_comes_from_the_spec(a):
    spec = load_spec(SPEC)
    d = derive(spec)
    assert d["dispenses_per_aspirate"]["cells"] == 2
    base = identify.manual_baseline(spec, d, a)
    d1 = copy.deepcopy(d)
    d1["cycles"] = {"media": 48, "cells": 48, "reagent": 48}               # one dispense per aspiration instead of two
    d1["dispenses_per_aspirate"] = {"media": 1, "cells": 1, "reagent": 1, "pbs": 1}
    single = identify.manual_baseline(spec, d1, a)
    assert single["seeding_min"]["media"] > base["seeding_min"]["media"] and single["seeding_min"]["cells"] > base["seeding_min"]["cells"]
    assert "15 min run is unattended" in " ".join(identify.manual_baseline(spec, d, a, compound="idot")["steps_min"])
    spec3 = copy.deepcopy(spec)
    spec3["timing"]["idot_dispense_min"] = 10
    assert "10 min run is unattended" in " ".join(identify.manual_baseline(spec3, derive(spec3), a, compound="idot")["steps_min"])
    assert spec["timing"]["idot_dispense_min"] == 15


def test_automation_beats_manual_hands_on_time(result):
    assert result["automated"]["hands_on_h"] < result["baseline"]["hands_on_h"] / 2


def test_fit_score_is_a_weighted_percentage_and_responds_to_inputs(a):
    spec = load_spec(SPEC)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    base, auto = identify.manual_baseline(spec, d, a), identify.automated_hands_on(a)
    econ = identify.economics(base, auto, a)
    f = identify.fit_score(spec, d, pm, base, a, econ)
    expect = sum(f["weights"][k] * s for k, s in f["scores"].items()) / 5
    assert f["percent"] == pytest.approx(expect, abs=0.1) and f["verdict"] == "strong"
    low = dict(base, window_ratio=0.2, reagent_lag_min=1, hands_on_h=0.5, transfer_operations=20)
    f2 = identify.fit_score(spec, d, pm, low, a, dict(econ, manual_failed_plates_per_year=0))
    assert f2["percent"] < f["percent"] and f2["scores"]["timing_criticality"] == 1


def test_economics_arithmetic_and_monotonic_behaviour(a, result):
    e = result["economics"]
    assert e["one_off_total_eur"] == e["capex_total_eur"] + e["one_off_effort_eur"]
    assert e["net_annual_eur"] == pytest.approx(e["labour_saving_eur"] + e["failure_saving_eur"] - e["service_eur"], abs=2)
    assert e["payback_years"] == pytest.approx(e["one_off_total_eur"] / e["net_annual_eur"], abs=0.01)
    pb = [result["by_runs_per_week"][k]["payback_years"] for k in ("2", "4", "8", "16")]
    assert pb == sorted(pb, reverse=True)
    low, high = result["cost_cases"]["low cost (capex x0.7, effort x0.7)"], result["cost_cases"]["high cost (capex x1.5, effort x1.5)"]
    assert low["payback_years"] < e["payback_years"] < high["payback_years"]
    assert e["capex_eur"]["flex_base"] == round(24950 * 0.92)


def test_breakeven_is_consistent_with_the_payback_it_claims(a, result):
    spec = load_spec(SPEC)
    base, auto = identify.manual_baseline(spec, derive(spec), a), identify.automated_hands_on(a)
    r = result["breakeven_runs_per_week_3y"]
    assert identify.economics(base, auto, a, runs_per_week=r + 0.2)["payback_years"] <= 3.0
    assert identify.economics(base, auto, a, runs_per_week=r - 0.5)["payback_years"] > 3.0
    assert result["breakeven_runs_per_week_3y_high_cost"] > r


def test_nothing_is_saved_when_automation_is_not_faster(a):
    base = {"hands_on_h": 1.0, "p_plate_has_a_transfer_error": 0.0}
    auto = {"hands_on_h": 2.0}
    e = identify.economics(base, auto, a)
    assert e["payback_years"] is None and e["net_annual_eur"] < 0
    assert identify.breakeven_runs_per_week(base, auto, a) is None


def test_placeholders_are_listed_so_nobody_mistakes_them_for_facts(result):
    assert len(result["placeholders"]) >= 25
    assert "economics.capex_usd.idot" in result["placeholders"]
    assert "economics.capex_usd.flex_base" not in result["placeholders"]


def test_cli_identify_writes_the_report(tmp_path, capsys):
    assert main(["identify", "--spec", str(SPEC), "--assumptions", str(ASSUME), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "identify.json").exists() and "automation fit" in capsys.readouterr().out


def test_the_no_arm_variant_costs_less_and_needs_fewer_plates_to_pay_back(result):
    full = result["variants"]["full system (Flex, iDOT, arm)"]
    no_arm = result["variants"]["Flex and iDOT, no arm"]
    assert no_arm["economics"]["capex_total_eur"] == full["economics"]["capex_total_eur"] - round(0.92 * (80000 + 20000))
    assert no_arm["hands_on_h"] > full["hands_on_h"]                      # a person carries the plates
    assert no_arm["breakeven_runs_per_week_3y"] < full["breakeven_runs_per_week_3y"]
    assert full["economics"]["payback_years"] == result["economics"]["payback_years"]
