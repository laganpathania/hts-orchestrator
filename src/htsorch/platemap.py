"""Seeded, stratified randomization of the assay plate, with an edge policy.

Edge policy (spec: assay.edge.policy) decides what the 76 perimeter wells hold:
  assay     all wells are equal; tests and controls are spread over edge and
            interior in proportion to how many of each exist
  controls  the perimeter holds controls only (neg/pos alternating); controls
            beyond 76 go inside, all test wells are inside
  pbs_fill  the perimeter and any unused well are filled with PBS: no cells,
            no compound. Only interior wells carry conditions

The seed is part of the spec, so a map is reproducible."""
from __future__ import annotations

import itertools
import random

from .spec import (
    PLATE_COLS, PLATE_ROWS, POS_WELL, VEHICLE_WELL, derive, is_edge, prep_well, well_name,
)


def _items(spec: dict, d: dict) -> dict:
    ids = spec["compounds"]["ids"]
    points = spec["dilution"]["points"]
    reps = spec["dilution"]["replicates"]
    tests = [
        {"type": "test", "compound_id": cid, "conc_index": j, "replicate": r + 1,
         "final_uM": d["final_uM"][j], "source_well": prep_well(i, j, points),
         "dispense_nl": d["nl_by_point"][j]}
        for i, cid in enumerate(ids) for j in range(points) for r in range(reps)
    ]
    neg = [{"type": "neg", "compound_id": "DMSO", "conc_index": "", "replicate": k + 1,
            "final_uM": 0.0, "source_well": VEHICLE_WELL, "dispense_nl": d["vehicle_nl"]} for k in range(d["n_neg"])]
    pos = [{"type": "pos", "compound_id": "KILL", "conc_index": "", "replicate": k + 1,
            "final_uM": d["final_uM"][0], "source_well": POS_WELL, "dispense_nl": d["pos_nl"]} for k in range(d["n_pos"])]
    return {"test": tests, "neg": neg, "pos": pos}


PBS_ITEM = {"type": "pbs", "compound_id": "PBS", "conc_index": "", "replicate": "",
            "final_uM": 0.0, "source_well": "", "dispense_nl": 0}


def build_plate_map(spec: dict, d: dict | None = None) -> list[dict]:
    d = d or derive(spec)
    n_wells = PLATE_ROWS * PLATE_COLS
    policy = d["edge_policy"]
    if policy not in ("assay", "controls", "pbs_fill"):
        raise ValueError(f"unknown edge policy '{policy}'")
    if d["n_assigned"] > d["n_usable_wells"]:
        raise ValueError(f"{d['n_assigned']} conditions do not fit on the {d['n_usable_wells']} wells "
                         f"available with edge policy '{policy}'")

    items = _items(spec, d)
    rng = random.Random(spec["experiment"]["random_seed"])
    all_wells = [(r, c) for r in range(PLATE_ROWS) for c in range(PLATE_COLS)]
    edge = [w for w in all_wells if is_edge(*w)]
    inner = [w for w in all_wells if not is_edge(*w)]
    rng.shuffle(edge)
    rng.shuffle(inner)

    placed: list[tuple[tuple[int, int], dict]] = []

    if policy == "assay":
        edge_frac = len(edge) / n_wells
        n_edge = {"neg": round(len(items["neg"]) * edge_frac), "pos": round(len(items["pos"]) * edge_frac)}
        n_edge["test"] = min(len(items["test"]), len(edge) - n_edge["neg"] - n_edge["pos"])
        pools = {}
        for kind in ("test", "neg", "pos"):
            pools[kind] = list(items[kind])
            rng.shuffle(pools[kind])
        for kind in ("neg", "pos", "test"):
            for k, item in enumerate(pools[kind]):
                if k < n_edge[kind] and edge:
                    pos = edge.pop()
                elif inner:
                    pos = inner.pop()
                else:
                    pos = edge.pop()
                placed.append((pos, item))

    elif policy == "controls":
        neg, pos_ = list(items["neg"]), list(items["pos"])
        rng.shuffle(neg)
        rng.shuffle(pos_)
        mixed = [x for pair in itertools.zip_longest(neg, pos_) for x in pair if x is not None]
        on_edge, rest = mixed[:len(edge)], mixed[len(edge):]
        for item in on_edge:
            placed.append((edge.pop(), item))
        inner_items = rest + items["test"]
        rng.shuffle(inner_items)
        for item in inner_items:
            placed.append((inner.pop(), item))

    else:  # pbs_fill
        everything = items["test"] + items["neg"] + items["pos"]
        rng.shuffle(everything)
        for item in everything:
            placed.append((inner.pop(), item))
        for pos in edge + inner:          # perimeter and unused interior wells
            placed.append((pos, dict(PBS_ITEM)))

    wells = []
    for (r, c), item in sorted(placed, key=lambda x: x[0]):
        wells.append({"well": well_name(r, c), "row": r, "col": c, "edge": is_edge(r, c), **item})
    return wells
