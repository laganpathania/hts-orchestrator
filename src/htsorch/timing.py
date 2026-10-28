"""Per-well timing, shared by the validator estimates and the workflow engine.

A dispensing step works through the plate in cycles (8-channel: two passes of columns;
1-channel: well by well). dispense_progress() says how far through the step each well is
served, as a fraction of the step (0..1]. A step that ran at some real times, perhaps with
a pause in the middle, turns that fraction into a clock time with time_at().

Because the validator and the engine both use this, a predicted window and a measured
window cannot disagree about what is being measured."""
from __future__ import annotations

import math

from .spec import PLATE_COLS


def dispense_progress(spec: dict, d: dict, plate_map: list) -> dict:
    """well -> {'cells': p, 'reagent': p}; PBS wells have no cells entry."""
    n_cells = d["dispenses_per_aspirate"]["cells"]
    n_reag = d["dispenses_per_aspirate"]["reagent"]
    seeded = sorted((w for w in plate_map if w["type"] != "pbs"), key=lambda w: (w["row"], w["col"]))
    order = {w["well"]: i for i, w in enumerate(seeded)}
    out = {}
    for w in plate_map:
        r, c = w["row"], w["col"]
        reag_cycle = (r % 2) * math.ceil(PLATE_COLS / n_reag) + c // n_reag
        entry = {"cells": None, "reagent": (reag_cycle + 1) / d["cycles"]["reagent"]}
        if w["type"] != "pbs":
            if d["well_by_well"]:
                cell_cycle = order[w["well"]] // n_cells
            else:
                cell_cycle = (r % 2) * math.ceil(PLATE_COLS / n_cells) + c // n_cells
            entry["cells"] = (cell_cycle + 1) / d["cycles"]["cells"]
        out[w["well"]] = entry
    return out


def time_at(segments: list, p: float) -> float:
    """Minutes after the step's first segment began at which progress p was reached.

    segments: [{'start_min': float, 'work_min': float, 'p_from': float, 'p_to': float}, ...]
    start_min is measured from a common origin chosen by the caller."""
    for s in segments:
        if s["p_from"] < p <= s["p_to"] + 1e-9:
            span = s["p_to"] - s["p_from"]
            return s["start_min"] + (p - s["p_from"]) / span * s["work_min"]
    last = segments[-1]
    return last["start_min"] + last["work_min"]
