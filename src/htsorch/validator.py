"""Pre-flight validation. Every finding says what happened, what the system did
and who has to act, so the same messages can be shown to a scientist or logged.

Limits flagged VERIFY in the spec are placeholders, not vendor facts."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .platemap import build_plate_map
from .spec import PLATE_COLS, PLATE_ROWS, derive


@dataclass
class Finding:
    level: str          # ERROR | WARN | INFO
    code: str
    what: str
    system_action: str
    who: str

    def line(self) -> str:
        return f"[{self.level}] {self.code}: {self.what} -> {self.system_action} ({self.who})"


def _err(code, what, who="Scientist: change the spec") -> Finding:
    return Finding("ERROR", code, what, "Build blocked, no files generated", who)


def _warn(code, what, who="Scientist: confirm before running") -> Finding:
    return Finding("WARN", code, what, "Build continues, warning recorded in run record", who)


def _info(code, what) -> Finding:
    return Finding("INFO", code, what, "None", "-")


def validate(spec: dict, d: dict | None = None, plate_map: list | None = None) -> list[Finding]:
    d = d or derive(spec)
    a = spec["assay"]
    fx = spec["flex"]
    win = spec["timing"]["windows"]
    out: list[Finding] = []

    # --- DMSO -------------------------------------------------------------
    lim = a["limits"]
    pct = d["dmso_percent"]
    msg = f"Final DMSO is {pct:.3f}% (up to {d['compound_nl_max']:g} nL into {d['assay_volume_ul']:g} uL)"
    if pct >= lim["dmso_max_percent"]:
        out.append(_err("V001", f"{msg}, not below the {lim['dmso_max_percent']}% limit. "
                                f"Lower the compound volume or raise the assay volume"))
    elif pct > lim["dmso_warn_percent"]:
        out.append(_warn("V001", f"{msg}, above the {lim['dmso_warn_percent']}% warning level "
                                 f"(limit {lim['dmso_max_percent']}%). Run a solvent tolerance check for this cell line"))
    else:
        out.append(_info("V001", f"{msg}, within limits"))

    # --- iDOT dispense volumes (they vary with the stock series) ------------
    floor = d["compound_nl_floor"]
    vols = ", ".join(f"{v:g}" for v in d["nl_by_point"])
    if floor is not None and d["compound_nl_min"] < floor:
        low = [j + 1 for j, v in enumerate(d["nl_by_point"]) if v < floor]
        out.append(_err("V003", f"iDOT volume per well by concentration point is {vols} nL; point(s) {low} fall below the "
                                f"{floor:g} nL minimum. Raise the top volume, or make the stock series steeper "
                                f"(dilution.stock_factor closer to or above dilution.factor)"))
    else:
        note = "" if floor is None else f" (minimum {floor:g} nL)"
        kind = "constant" if len(set(d["nl_by_point"])) == 1 else "varies by concentration"
        out.append(_info("V003", f"iDOT volume per well by concentration point: {vols} nL, {kind}{note}; "
                                 f"solvent wells get {d['vehicle_nl']:g} nL"))

    # --- well volume -----------------------------------------------------
    cap = a["plate"]["max_working_volume_ul"]
    if d["total_well_volume_ul"] > cap:
        out.append(_err("V002", f"Assay {d['assay_volume_ul']:g} uL + reagent {d['reagent_volume_ul']:g} uL = "
                                f"{d['total_well_volume_ul']:g} uL exceeds the plate working volume of {cap} uL. "
                                f"Reduce the assay volume or the reagent ratio"))
    else:
        out.append(_info("V002", f"Final well volume {d['total_well_volume_ul']:g} uL within the {cap} uL plate limit"))

    # --- edge policy and plate coverage ----------------------------------
    policy = d["edge_policy"]
    if policy not in ("assay", "controls", "pbs_fill"):
        out.append(_err("V013", f"Unknown edge policy '{policy}'. Use assay, controls or pbs_fill"))
        return out
    ctrl = d["n_neg"] + d["n_pos"]
    out.append(_info("V013", f"Edge policy '{policy}': {d['n_edge_wells']} perimeter wells, "
                             f"{d['n_usable_wells']} of {d['n_plate_wells']} wells available for conditions"))
    if policy == "controls" and ctrl < d["n_edge_wells"]:
        out.append(_warn("V013", f"Only {ctrl} controls for {d['n_edge_wells']} perimeter wells: "
                                 f"{d['n_edge_wells'] - ctrl} perimeter wells get media and cells but no compound or solvent"))
    if d["n_assigned"] > d["n_usable_wells"]:
        out.append(_err("V010", f"{d['n_assigned']} conditions do not fit on the {d['n_usable_wells']} wells "
                                f"available with edge policy '{policy}'. Reduce replicates or controls, "
                                f"use a second plate, or change the edge policy"))
    elif policy == "pbs_fill":
        out.append(_info("V010", f"{d['n_assigned']} conditions in {d['n_usable_wells']} interior wells; "
                                 f"{d['n_pbs']} wells filled with PBS ({d['pbs_volume_ul']:g} uL)"))
        if abs(d["pbs_volume_ul"] - d["assay_volume_ul"]) > 1e-9:
            out.append(_warn("V014", f"PBS fill {d['pbs_volume_ul']:g} uL differs from the assay volume "
                                     f"{d['assay_volume_ul']:g} uL; reagent ratio in PBS wells will not be 1:1"))
        if d["pbs_volume_ul"] + d["reagent_volume_ul"] > cap:
            out.append(_err("V014", f"PBS {d['pbs_volume_ul']:g} uL + reagent {d['reagent_volume_ul']:g} uL "
                                    f"exceeds the plate working volume of {cap} uL"))
    elif d["n_assigned"] < d["n_usable_wells"]:
        out.append(_warn("V010", f"{d['n_usable_wells'] - d['n_assigned']} wells have no assigned condition"))
    else:
        out.append(_info("V010", f"{d['n_assigned']} conditions fill all {d['n_usable_wells']} usable wells"))

    series_cols = math.ceil(d["n_compounds"] / PLATE_ROWS) * spec["dilution"]["points"]
    if series_cols > 11:
        out.append(_err("V012", f"Dilution series need {series_cols} prep-plate columns; only 11 are free "
                                f"(columns 12+ hold the control wells)"))

    # --- Flex pipette ranges --------------------------------------------
    p1 = fx["pipettes"]["flex_1channel_50"]
    p8 = fx["pipettes"]["flex_8channel_50"]
    checks = [
        ("master to top well", d["top_master_ul"], p1), ("serial transfer", d["step_ul"], p1),
        ("media dispense", a["media_volume_ul"], p8), ("cell dispense", a["cell_volume_ul"], p8),
        ("reagent dispense", d["reagent_volume_ul"], p8),
    ]
    if policy == "pbs_fill":
        checks.append(("PBS fill dispense", d["pbs_volume_ul"], p1))
    for name, vol, pip in checks:
        if not (pip["min_ul"] <= vol <= pip["max_ul"]):
            out.append(_err("V011", f"{name} of {vol:g} uL is outside the pipette range "
                                    f"{pip['min_ul']}-{pip['max_ul']} uL"))

    # --- stock and source volumes ---------------------------------------
    comp = spec["compounds"]
    if d["top_master_ul"] + 0 > comp["master_available_ul"]:
        out.append(_err("V060", f"Master stock needs {d['top_master_ul']:g} uL per compound, "
                                f"only {comp['master_available_ul']} uL available"))
    pos = spec["controls"]["positive"]
    if d["pos_master_ul"] > pos["master_available_ul"]:
        out.append(_err("V060", f"Positive control master needs {d['pos_master_ul']:g} uL, "
                                f"only {pos['master_available_ul']} uL available"))

    for label, need, have in (
        ("compound source well", d["need_test_src_ul"], d["remaining_ul"]),
        ("solvent (DMSO) source well", d["need_neg_src_ul"], spec["prep_plate"]["well_volume_ul"]),
        ("positive control source well", d["need_pos_src_ul"], spec["prep_plate"]["well_volume_ul"]),
    ):
        required = need + d["idot_dead_ul"]
        if required > have:
            out.append(_err("V030", f"{label}: iDOT needs {need:g} uL + {d['idot_dead_ul']:g} uL dead volume = "
                                    f"{required:g} uL but only {have:g} uL remain. Increase the prep-plate fill volume"))
        else:
            out.append(_info("V030", f"{label}: {required:g} uL needed, {have:g} uL available"))

    # --- reservoirs ------------------------------------------------------
    for name in d["reservoir_need_ul"]:
        if name not in fx["reservoirs"]:
            out.append(_err("V020", f"Edge policy '{policy}' needs a '{name}' reservoir but none is defined in flex.reservoirs"))
    for name, res in fx["reservoirs"].items():
        if name not in d["reservoir_need_ul"]:
            continue
        need = d["reservoir_need_ul"][name] + res["dead_volume_ul"]
        if need > res["capacity_ul"]:
            out.append(_err("V020", f"{name} reservoir ({res['load_name']}) needs {need / 1000:.1f} mL "
                                    f"(incl. {res['dead_volume_ul'] / 1000:.1f} mL dead volume) but holds "
                                    f"{res['capacity_ul'] / 1000:.0f} mL. Use a larger reservoir or split the run"))
        else:
            out.append(_info("V020", f"{name} reservoir: load {need / 1000:.1f} mL of {res['capacity_ul'] / 1000:.0f} mL"))

    # --- tips ------------------------------------------------------------
    for proto, t in d["tips"].items():
        if t["needed"] > t["capacity"]:
            out.append(_err("V021", f"{proto} protocol needs {t['needed']} {t['unit']} but only "
                                    f"{t['capacity']} are loaded. Load another tip rack", "Operator: reload tips"))
        else:
            out.append(_info("V021", f"{proto}: {t['needed']} of {t['capacity']} {t['unit']} used"))

    # --- timing windows --------------------------------------------------
    est = d["est"]
    for code, label, value, limit in (
        ("V040", "Compound plate wait (iDOT end to cells in the last well)", est["compound_to_cells_min"], win["compound_to_cells_max_min"]),
        ("V041", "Cell suspension age at last dispense", est["cell_age_at_last_dispense_min"], win["cell_suspension_max_age_min"]),
        ("V042", "First-to-last-well reagent lag", est["reagent_lag_min"], win["max_reagent_lag_min"]),
    ):
        text = f"{label}: estimated {value:.1f} min, window {limit} min"
        if value > limit:
            out.append(_err(code, text + ". Window cannot be met, speed up the step or relax the window"))
        elif value > 0.8 * limit:
            out.append(_warn(code, text + " (above 80% of window)"))
        else:
            out.append(_info(code, text))
    # --- CellTiter-Glo 3D: spheroids need longer lysis than 2D cultures -----
    out.extend(check_reagent_protocol(a["reagent"]))
    if policy == "pbs_fill":
        out.append(_info("V045", f"PBS pre-fill of {d['n_pbs']} wells estimated {est['edge_fill_min']:.1f} min "
                                 f"(1-channel, runs before the iDOT step, outside the timing windows)"))
        out.append(_info("V045", f"Seeding runs well by well with the 1-channel pipette ({d['n_assigned']} wells) "
                                 f"because an 8-channel cannot skip single perimeter wells"))
    out.append(_info("V043", f"Incubation {win['incubation_h']} h +/- {win['incubation_tolerance_h']} h "
                             f"enforced at readout by the orchestrator (late wake-up beyond tolerance becomes deviation D043)"))

    # --- plate map integrity --------------------------------------------
    if not any(f.level == "ERROR" and f.code in ("V010", "V012") for f in out):
        pm = plate_map if plate_map is not None else build_plate_map(spec, d)
        out.extend(check_plate_map(pm, d))
    return out


def check_plate_map(pm: list, d: dict) -> list[Finding]:
    out: list[Finding] = []
    wells = [w["well"] for w in pm]
    if len(set(wells)) != len(wells):
        return [_err("V050", "Plate map assigns a well twice")]
    counts = {k: sum(1 for w in pm if w["type"] == k) for k in ("test", "neg", "pos")}
    expected = {"test": d["n_test_wells"], "neg": d["n_neg"], "pos": d["n_pos"]}
    if counts != expected:
        return [_err("V050", f"Plate map counts {counts} differ from expected {expected}")]

    half_r, half_c = PLATE_ROWS // 2, PLATE_COLS // 2
    for kind in ("neg", "pos"):
        quad = [0, 0, 0, 0]
        for w in pm:
            if w["type"] == kind:
                quad[(w["row"] >= half_r) * 2 + (w["col"] >= half_c)] += 1
        if min(quad) < 3:
            out.append(_warn("V051", f"{kind} controls per quadrant {quad}: a quadrant has fewer than 3, "
                                     f"drift detection will be weak. Try another random seed"))
        else:
            out.append(_info("V051", f"{kind} controls per quadrant {quad}"))
    edge_share = {k: sum(1 for w in pm if w["type"] == k and w["edge"]) for k in counts}

    policy = d["edge_policy"]
    if policy == "controls" and edge_share["test"]:
        out.append(_err("V052", f"Edge policy 'controls' but {edge_share['test']} test wells sit on the perimeter"))
    if policy == "pbs_fill":
        pbs = [w for w in pm if w["type"] == "pbs"]
        if any(w["edge"] and w["type"] != "pbs" for w in pm):
            out.append(_err("V052", "Edge policy 'pbs_fill' but a perimeter well holds a condition"))
        if len(pbs) != d["n_pbs"]:
            out.append(_err("V052", f"Expected {d['n_pbs']} PBS wells, plate map has {len(pbs)}"))
        if any(w["dispense_nl"] for w in pbs):
            out.append(_err("V052", "A PBS well is scheduled for a compound dispense"))
    if not any(f.code == "V052" for f in out):
        out.append(_info("V050", f"Plate map OK: {counts}, edge wells {edge_share}"))
    return out


def check_reagent_protocol(reagent: dict) -> list[Finding]:
    """Explicit shake and incubation checks. Missing values are errors, not defaults,
    so a 2D-style protocol cannot slip through unnoticed."""
    out: list[Finding] = []
    need = ("shake_min", "incubate_min", "shake_min_required", "incubate_min_required")
    missing = [k for k in need if reagent.get(k) is None]
    if missing:
        return [_err("V044", f"{reagent.get('name', 'Reagent')}: spec is missing {', '.join(missing)}. "
                             f"Shake and incubation times must be stated explicitly for spheroids")]
    name = reagent.get("name", "Reagent")
    for label, key, req in (("Shake", "shake_min", "shake_min_required"),
                            ("Incubation", "incubate_min", "incubate_min_required")):
        if reagent[key] < reagent[req]:
            out.append(_err("V044", f"{name}: {label.lower()} {reagent[key]:g} min is below the "
                                    f"{reagent[req]:g} min needed for complete lysis of spheroids "
                                    f"(2D protocols are shorter). Increase it or change the assay"))
    if not out:
        out.append(_info("V044", f"{name}: shake {reagent['shake_min']:g} min (>= {reagent['shake_min_required']:g}), "
                                 f"incubate {reagent['incubate_min']:g} min (>= {reagent['incubate_min_required']:g})"))
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "ERROR" for f in findings)


def to_dicts(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]
