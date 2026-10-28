"""The "Identify" step: manual baseline, automation-fit score and ROI, computed from the experiment spec.

All timings, costs and probabilities come from identify/assumptions.yaml and carry a basis
(source / spec / placeholder). The model reports its result and the plates per week at which the investment pays back,
because that figure is far less sensitive to any single placeholder than the absolute savings.
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml

from .platemap import build_plate_map
from .spec import derive, load_spec
from .validator import has_errors, validate


def load_assumptions(path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def v(a: dict, *keys):
    """Value of an assumption, e.g. v(a, 'manual', 'sec_per_dilution_transfer')."""
    node = a
    for k in keys:
        node = node[k]
    return node["value"]


def manual_baseline(spec: dict, d: dict, a: dict, compound: str | None = None) -> dict:
    """Hands-on minutes per assay plate (with its prep plate) when people do everything the system would do.

    compound="idot" (reference: people with an iDOT): the iDOT dispenses the 30 nL compound doses in every variant,
    so the operator only loads and starts it (the 10-15 min run is unattended). compound="hand" (default, the comparison chosen for the case): everything by hand
    with a repeater pipette, one aspiration per source well and one dispense per target well; it cannot reach 30 nL and
    only shows how far a person would be from the window.
    Seeding and the reagent step use an 8-channel pipette with multi-dispensing, exactly as the Flex protocols do.
    """
    m = lambda k: v(a, "manual", k)
    compound = compound or m("compound_dosing")
    points = spec["dilution"]["points"]
    dilution_transfers = d["n_compounds"] * points + 2                      # series plus positive control master = source wells
    tm = spec["timing"]
    asp_s, disp_s = m("sec_per_multichannel_aspirate"), m("sec_per_multichannel_dispense")
    liquids = {}
    for name in ("media", "cells", "reagent"):
        aspirations = d["cycles"][name]
        dispenses = aspirations * d["dispenses_per_aspirate"][name]
        liquids[name] = {"aspirations": aspirations, "dispenses": dispenses,
                         "min": (aspirations * asp_s + dispenses * disp_s) / 60}
    mixes = d["cycles"]["cells"] // spec["flex"]["mix_cell_every_cycles"]
    liquids["cells"]["min"] += mixes * m("sec_per_cell_mix") / 60
    refills = 3 * m("reservoir_refills_per_liquid") * m("min_per_reservoir_refill")
    additions_min = sum(x["min"] for x in liquids.values()) + refills
    multi_ops = sum(x["aspirations"] + x["dispenses"] for x in liquids.values())

    if compound == "hand":
        aspirates = dilution_transfers
        dispenses = d["n_assigned"] if m("randomized_layout") else math.ceil(d["n_assigned"] / 8)
        compound_min = (aspirates * m("sec_per_compound_aspirate") + dispenses * m("sec_per_compound_dispense")) / 60
        compound_ops, compound_step = aspirates + dispenses, "compound transfer by hand: 102 aspirations, 384 dispenses"
    else:
        compound_min, compound_ops = m("min_idot_loading_and_start"), 0
        compound_step = f"iDOT dosing: load and start ({tm['idot_dispense_min']:g} min run is unattended)"
    steps = {
        "setup and calculation": m("min_setup_and_calculation"),
        "dilution series": dilution_transfers * m("sec_per_dilution_transfer") / 60,
        compound_step: compound_min,
        "media, cells and reagent additions (8-channel, multi-dispense)": additions_min,
        "carrying plates between instruments": m("handoffs_per_assay_plate") * m("min_per_plate_handoff"),
        "cleanup and documentation": m("min_cleanup_and_documentation"),
        "export and analysis": m("min_reader_export_and_analysis"),
    }
    transfers = dilution_transfers + compound_ops + multi_ops
    window = tm["windows"]["compound_to_cells_max_min"]
    # compound end -> cells in the last well, by hand: carry the plate, add media, then cells (as the validator defines it)
    to_cells = tm["arm_transfer_min"] + liquids["media"]["min"] + liquids["cells"]["min"] if compound == "idot" else compound_min
    p = m("error_probability_per_manual_transfer")
    return {"compound_mode": compound, "steps_min": {k: round(x, 1) for k, x in steps.items()},
            "hands_on_h": round(sum(steps.values()) / 60, 2), "transfer_operations": transfers,
            "compound_step_min": round(compound_min, 1), "compound_to_cells_min": round(to_cells, 1),
            "compound_to_cells_window_min": window, "window_ratio": round(to_cells / window, 2),
            "reagent_lag_min": round(liquids["reagent"]["min"], 1), "reagent_lag_window_min": tm["windows"]["max_reagent_lag_min"],
            "seeding_min": {k: round(x["min"], 1) for k, x in liquids.items()},
            "p_plate_has_a_transfer_error": round(1 - (1 - p) ** transfers, 3)}


def automated_hands_on(a: dict) -> dict:
    g = lambda k: v(a, "automated", k)
    steps = {"setup and loading": g("min_setup_and_loading"), "monitoring and interventions": g("min_monitoring_and_interventions_per_run"),
             "day-3 loading": g("min_day3_loading"), "review of the data package": g("min_review_of_package")}
    return {"steps_min": steps, "hands_on_h": round(sum(steps.values()) / 60, 2)}


def _band(value, bands):
    for floor, score in bands:
        if value >= floor:
            return score
    return 1


def fit_score(spec: dict, d: dict, pm, base: dict, a: dict, econ: dict) -> dict:
    crit = a["fit_criteria"]
    errors = has_errors(validate(spec, d, pm))
    runs = v(a, "economics", "runs_per_week") * v(a, "economics", "weeks_per_year")
    failed_share = econ["manual_failed_plates_per_year"] / runs if runs else 0
    # the tightest window by hand: compound-to-cells or the first-to-last-well reagent lag
    timing_ratio = max(base["window_ratio"], base["reagent_lag_min"] / base["reagent_lag_window_min"])
    scores = {
        "repetition_and_volume": _band(base["transfer_operations"], [(1000, 5), (500, 4), (200, 3), (50, 2)]),
        "hands_on_burden": _band(base["hands_on_h"], [(6, 5), (4, 4), (2, 3), (1, 2)]),
        "timing_criticality": 5 if timing_ratio > 1 else 3 if timing_ratio > 0.5 else 1,
        "process_definition": 2 if errors else 5,
        "error_and_deviation_impact": _band(failed_share, [(0.15, 5), (0.08, 4), (0.04, 3), (0.01, 2)]),
    }
    for k, c in crit.items():
        if "score" in c:
            scores[k] = c["score"]
    total_w = sum(c["weight"] for c in crit.values())
    pct = round(sum(crit[k]["weight"] * s for k, s in scores.items()) / (5 * total_w) * 100, 1)
    t = a["fit_thresholds"]
    verdict = "strong" if pct >= t["strong"] else "conditional" if pct >= t["conditional"] else "weak"
    return {"scores": scores, "weights": {k: c["weight"] for k, c in crit.items()}, "percent": pct, "verdict": verdict}


def economics(base: dict, auto: dict, a: dict, runs_per_week: float | None = None, scale: dict | None = None) -> dict:
    """Costs, savings, payback and NPV. `scale` multiplies named capex lines (for the low/high cases)."""
    e = lambda k: v(a, "economics", k)
    fx = v(a, "fx_usd_to_eur")
    rpw = e("runs_per_week") if runs_per_week is None else runs_per_week
    scale = scale or {}
    capex = {k: c["value"] * fx * scale.get(k, scale.get("all", 1)) for k, c in a["economics"]["capex_usd"].items()}
    effort_h = sum(c["value"] for c in a["economics"]["one_off_effort_hours"].values())
    effort_cost = effort_h * e("labour_cost_per_hour") * scale.get("effort", 1)
    one_off = sum(capex.values()) + effort_cost
    runs = rpw * e("weeks_per_year")
    saved_h = (base["hands_on_h"] - auto["hands_on_h"]) * runs
    labour_saving = saved_h * e("labour_cost_per_hour")
    manual_failed = runs * base["p_plate_has_a_transfer_error"]
    auto_failed = runs * v(a, "automated", "residual_error_probability_per_run")
    failure_saving = (manual_failed - auto_failed) * e("cost_of_a_failed_plate")
    service = sum(capex.values()) * e("service_share_of_capex_per_year")
    net = labour_saving + failure_saving - service
    n, r = int(e("years")), e("discount_rate")
    npv = -one_off + sum(net / (1 + r) ** t for t in range(1, n + 1))
    return {"runs_per_year": runs, "capex_eur": {k: round(x) for k, x in capex.items()}, "capex_total_eur": round(sum(capex.values())),
            "one_off_effort_h": effort_h, "one_off_effort_eur": round(effort_cost), "one_off_total_eur": round(one_off),
            "hands_on_hours_saved_per_year": round(saved_h), "labour_saving_eur": round(labour_saving),
            "manual_failed_plates_per_year": round(manual_failed, 1), "auto_failed_plates_per_year": round(auto_failed, 1),
            "failure_saving_eur": round(failure_saving), "service_eur": round(service), "net_annual_eur": round(net),
            "payback_years": round(one_off / net, 2) if net > 0 else None, f"npv_{n}y_eur": round(npv)}


def breakeven_runs_per_week(base, auto, a, max_years: float = 3.0, scale=None) -> float | None:
    """Runs per week at which the one-off cost is paid back in `max_years` (bisection; None if never within 200/week)."""
    lo, hi = 0.0, 200.0
    ok = lambda rpw: (lambda ec: ec["payback_years"] is not None and ec["payback_years"] <= max_years)(economics(base, auto, a, rpw, scale))
    if not ok(hi):
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return round(hi, 1)


def run(spec_path, assumptions_path) -> dict:
    spec = load_spec(spec_path)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    a = load_assumptions(assumptions_path)
    base, auto = manual_baseline(spec, d, a), automated_hands_on(a)
    econ = economics(base, auto, a)
    fit = fit_score(spec, d, pm, base, a, econ)
    cases = {"low cost (capex x0.7, effort x0.7)": {"all": 0.7, "effort": 0.7}, "base": {}, "high cost (capex x1.5, effort x1.5)": {"all": 1.5, "effort": 1.5}}
    scen = {name: economics(base, auto, a, scale=sc) for name, sc in cases.items()}
    v_no_arm = a["variants"]["flex_and_idot_no_arm"]
    carry_h = base["steps_min"]["carrying plates between instruments"] / 60
    auto_no_arm = dict(auto, hands_on_h=round(auto["hands_on_h"] + carry_h, 2))
    sc_no_arm = {**{k: 0 for k in v_no_arm["capex_not_needed"]}, "effort": v_no_arm["effort_scale"]["value"]}
    variants = {"full system (Flex, iDOT, arm)": {"hands_on_h": auto["hands_on_h"], "economics": econ,
                                                  "breakeven_runs_per_week_3y": breakeven_runs_per_week(base, auto, a)},
                "Flex and iDOT, no arm": {"hands_on_h": auto_no_arm["hands_on_h"],
                                          "economics": economics(base, auto_no_arm, a, scale=sc_no_arm),
                                          "breakeven_runs_per_week_3y": breakeven_runs_per_week(base, auto_no_arm, a, scale=sc_no_arm),
                                          "by_runs_per_week": {str(r): economics(base, auto_no_arm, a, runs_per_week=r, scale=sc_no_arm) for r in (1, 2, 4, 8, 16)}}}
    sens = {str(r): economics(base, auto, a, runs_per_week=r) for r in (1, 2, 4, 8, 16)}
    refs = {m: manual_baseline(spec, d, a, compound=m) for m in ("hand", "idot")}
    return {"baseline": base, "reference_by_hand": refs["hand"], "reference_idot_assisted": refs["idot"], "automated": auto, "economics": econ, "fit": fit, "cost_cases": scen, "by_runs_per_week": sens, "variants": variants,
            "breakeven_runs_per_week_3y": breakeven_runs_per_week(base, auto, a),
            "breakeven_runs_per_week_3y_high_cost": breakeven_runs_per_week(base, auto, a, scale=cases["high cost (capex x1.5, effort x1.5)"]),
            "placeholders": sorted(_placeholders(a))}


def _placeholders(node, path=""):
    if isinstance(node, dict):
        if node.get("basis") == "placeholder":
            yield path.strip(".")
        for k, x in node.items():
            yield from _placeholders(x, f"{path}.{k}")
