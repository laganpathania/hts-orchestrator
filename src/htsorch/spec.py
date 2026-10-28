"""Spec loading and derived calculations. All numbers the validator and the
generators use come from derive(), so there is one place to check the maths."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import yaml

ROWS = "ABCDEFGHIJKLMNOP"
PLATE_ROWS = 16
PLATE_COLS = 24

VEHICLE_WELL = "A12"   # prep-plate well holding pure DMSO (negative control source)
POS_WELL = "B12"       # prep-plate well holding the positive-control top stock


def load_spec(path) -> dict:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    spec = yaml.safe_load(raw)
    spec["_meta"] = {"path": str(p), "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    return spec


def well_name(row: int, col: int) -> str:
    return f"{ROWS[row]}{col + 1}"


def is_edge(row: int, col: int) -> bool:
    return row in (0, PLATE_ROWS - 1) or col in (0, PLATE_COLS - 1)


def prep_well(comp_index: int, point_index: int, points: int) -> str:
    """Compound series run along a row of the 384-well prep plate."""
    row = comp_index % PLATE_ROWS
    col = (comp_index // PLATE_ROWS) * points + point_index
    return well_name(row, col)


def max_dispenses_per_aspirate(volume_ul: float, pipette_max_ul: float, overage_ul: float) -> int:
    return max(1, int(math.floor((pipette_max_ul - overage_ul) / volume_ul)))


def derive(spec: dict) -> dict:
    a = spec["assay"]
    dil = spec["dilution"]
    comps = spec["compounds"]
    ctr = spec["controls"]
    pp = spec["prep_plate"]
    fx = spec["flex"]
    tm = spec["timing"]

    media = a["media_volume_ul"]
    cells = a["cell_volume_ul"]
    cd = a.get("compound_dispense") or {}
    top_nl = cd.get("top_nl", a.get("compound_volume_nl"))     # legacy specs give one fixed volume
    min_nl = cd.get("min_nl")
    assay_ul = media + cells
    reagent_ul = assay_ul * a["reagent"]["ratio_to_assay_volume"]
    total_ul = assay_ul + reagent_ul
    assay_nl = assay_ul * 1000.0

    points, factor, reps = dil["points"], dil["factor"], dil["replicates"]
    stock_factor = dil.get("stock_factor", factor)
    top_stock_uM = dil["top_final_uM"] * assay_nl / top_nl
    stock_uM = [top_stock_uM / stock_factor ** i for i in range(points)]
    final_uM = [dil["top_final_uM"] / factor ** i for i in range(points)]
    # iDOT volume per concentration: final x assay volume / stock, rounded to 0.1 nL (iDOT resolution)
    nl_by_point = [_nl(f * assay_nl / s) for f, s in zip(final_uM, stock_uM)]
    max_nl = max(nl_by_point)
    vehicle_nl = max_nl            # vehicle wells get the largest dose volume, so they carry the most DMSO
    pos_nl = nl_by_point[0]        # the kill control is dosed from the top stock
    dmso_pct = max_nl / assay_nl * 100.0

    n_comp = len(comps["ids"])
    n_tests = n_comp * points * reps
    n_neg = ctr["negative"]["count"]
    n_pos = ctr["positive"]["count"]
    n_wells = a["plate"]["format"]

    well_vol = pp["well_volume_ul"]
    step_ul = well_vol / stock_factor                      # volume carried to next point
    top_master_ul = well_vol * top_stock_uM / (comps["master_conc_mM"] * 1000.0)
    pos_master_ul = well_vol * top_stock_uM / (ctr["positive"]["master_conc_mM"] * 1000.0)
    remaining_ul = well_vol - step_ul                # in every non-last series well
    dead = pp["idot_source_dead_volume_ul"]

    need_test_src = reps * max_nl / 1000.0          # worst case over the series
    need_neg_src = n_neg * vehicle_nl / 1000.0
    need_pos_src = n_pos * pos_nl / 1000.0

    # Edge policy: what the 76 perimeter wells are used for.
    edge_cfg = a.get("edge") or {}
    policy = edge_cfg.get("policy", "assay")
    n_edge = 2 * PLATE_COLS + 2 * (PLATE_ROWS - 2)
    n_inner = PLATE_ROWS * PLATE_COLS - n_edge
    n_assigned = n_tests + n_neg + n_pos
    n_ctrl = n_neg + n_pos
    if policy == "pbs_fill":
        usable = n_inner
    elif policy == "controls":
        usable = n_inner + min(n_edge, n_ctrl)
    else:
        usable = n_wells
    n_pbs = max(0, n_wells - n_assigned) if policy == "pbs_fill" else 0
    pbs_ul = edge_cfg.get("pbs_volume_ul", assay_ul)
    # An 8-channel cannot skip single wells (rows A and P share a pass with other rows),
    # so pbs_fill seeds well by well with the 1-channel pipette.
    well_by_well = policy == "pbs_fill"

    passes, cols = 2, PLATE_COLS
    p1_max = fx["pipettes"]["flex_1channel_50"]["max_ul"]
    p8_max = fx["pipettes"]["flex_8channel_50"]["max_ul"]
    over = fx["overage_ul"]
    seed_max = p1_max if well_by_well else p8_max
    n_media = max_dispenses_per_aspirate(media, seed_max, over)
    n_cells = max_dispenses_per_aspirate(cells, seed_max, over)
    n_reagent = max_dispenses_per_aspirate(reagent_ul, p8_max, over)
    n_pbs_asp = max_dispenses_per_aspirate(pbs_ul, p1_max, over)

    def cycles8(n):
        return passes * math.ceil(cols / n)

    def cycles1(n_wells_, n):
        return math.ceil(n_wells_ / n)

    def asp8(n, vol):
        # all 8 channels draw from the same trough well
        return cycles8(n) * 8 * (min(n, cols) * vol + over)

    def asp1(n_wells_, n, vol):
        return cycles1(n_wells_, n) * over + n_wells_ * vol

    if well_by_well:
        cyc = {"media": cycles1(n_assigned, n_media), "cells": cycles1(n_assigned, n_cells),
               "reagent": cycles8(n_reagent)}
        seed_need = {"media": asp1(n_assigned, n_media, media), "cells": asp1(n_assigned, n_cells, cells)}
    else:
        cyc = {"media": cycles8(n_media), "cells": cycles8(n_cells), "reagent": cycles8(n_reagent)}
        seed_need = {"media": asp8(n_media, media), "cells": asp8(n_cells, cells)}
    res_need = {
        **seed_need,
        "reagent": asp8(n_reagent, reagent_ul),
        "dmso": _dmso_total(n_comp, points, well_vol, top_master_ul, remaining_ul, pos_master_ul),
    }
    if policy == "pbs_fill":
        res_need["pbs"] = asp1(n_pbs, n_pbs_asp, pbs_ul)

    seed_unit = ("1-ch tips", 96) if well_by_well else ("8-ch tip columns", 12)
    tips = {
        "dilution": {"needed": 1 + n_comp + 1, "capacity": 96 * fx["tip_racks_loaded"]["dilution"], "unit": "1-ch tips"},
        "seeding": {"needed": 2, "capacity": seed_unit[1] * fx["tip_racks_loaded"]["seeding"], "unit": seed_unit[0]},
        "readout": {"needed": 1, "capacity": 12 * fx["tip_racks_loaded"]["readout"], "unit": "8-ch tip columns"},
    }
    if policy == "pbs_fill":
        tips["edge_fill"] = {"needed": 1, "capacity": 96 * fx["tip_racks_loaded"].get("edge_fill", 0), "unit": "1-ch tips"}

    cyc_s = tm["flex_cycle_s"]
    est = {
        "media_min": cyc["media"] * cyc_s / 60.0,
        "cells_min": cyc["cells"] * cyc_s / 60.0,
        "reagent_min": cyc["reagent"] * cyc_s / 60.0,
        "edge_fill_min": (cycles1(n_pbs, n_pbs_asp) * cyc_s / 60.0) if policy == "pbs_fill" else 0.0,
    }
    # Windows apply per well, so the worst case is the last well served.
    est["compound_to_cells_min"] = tm["arm_transfer_min"] + est["media_min"] + est["cells_min"]
    est["cell_age_at_last_dispense_min"] = tm["cell_prep_to_start_min"] + est["cells_min"]
    est["reagent_lag_min"] = (cyc["reagent"] - 1) * cyc_s / 60.0     # first well to last well

    return {
        "assay_volume_ul": assay_ul,
        "reagent_volume_ul": reagent_ul,
        "total_well_volume_ul": total_ul,
        "dmso_percent": dmso_pct,
        "nl_by_point": nl_by_point,
        "compound_nl_min": min(nl_by_point),
        "compound_nl_max": max_nl,
        "compound_nl_floor": min_nl,
        "vehicle_nl": vehicle_nl,
        "pos_nl": pos_nl,
        "stock_factor": stock_factor,
        "top_stock_uM": top_stock_uM,
        "stock_uM": stock_uM,
        "final_uM": final_uM,
        "n_compounds": n_comp,
        "n_test_wells": n_tests,
        "n_neg": n_neg,
        "n_pos": n_pos,
        "n_assigned": n_assigned,
        "n_plate_wells": n_wells,
        "edge_policy": policy,
        "n_edge_wells": n_edge,
        "n_usable_wells": usable,
        "n_pbs": n_pbs,
        "pbs_volume_ul": pbs_ul,
        "well_by_well": well_by_well,
        "step_ul": step_ul,
        "top_master_ul": top_master_ul,
        "pos_master_ul": pos_master_ul,
        "remaining_ul": remaining_ul,
        "idot_dead_ul": dead,
        "need_test_src_ul": need_test_src,
        "need_neg_src_ul": need_neg_src,
        "need_pos_src_ul": need_pos_src,
        "dispenses_per_aspirate": {"media": n_media, "cells": n_cells, "reagent": n_reagent, "pbs": n_pbs_asp},
        "cycles": cyc,
        "reservoir_need_ul": res_need,
        "tips": tips,
        "est": est,
    }


def _nl(x: float) -> float:
    r = round(x, 1)
    return int(r) if r == int(r) else r


def _dmso_total(n_comp, points, well_vol, top_master_ul, remaining_ul, pos_master_ul) -> float:
    per_series = (well_vol - top_master_ul) + (points - 1) * remaining_ul
    return n_comp * per_series + (well_vol - pos_master_ul) + well_vol  # + pos top + vehicle well
