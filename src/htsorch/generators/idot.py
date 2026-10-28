"""iDOT worklist generator.

IMPORTANT: this is OUR file-based representation of a dispense list. It has not
been checked against the current vendor software import format. Before real use,
map these columns to the format the iDOT software actually accepts."""
from __future__ import annotations

import csv
import io

COLUMNS = ["sequence", "source_plate", "source_well", "target_plate", "target_well", "volume_nl",
           "condition", "compound_id"]


def generate_worklist(spec: dict, plate_map: list) -> str:
    src_plate = spec["prep_plate"]["barcode"]
    tgt_plate = spec["assay"]["plate"]["barcode"]
    # Group by source well so the dispenser changes source as rarely as possible.
    rows = sorted((w for w in plate_map if w["type"] != "pbs"), key=lambda w: (w["source_well"][0], int(w["source_well"][1:]), w["row"], w["col"]))
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(COLUMNS)
    for n, w in enumerate(rows, start=1):
        cond = w["type"] if w["type"] != "test" else f"conc{w['conc_index'] + 1}"
        wr.writerow([n, src_plate, w["source_well"], tgt_plate, w["well"], w["dispense_nl"], cond, w["compound_id"]])
    return buf.getvalue()


def plate_map_csv(plate_map: list) -> str:
    cols = ["well", "type", "compound_id", "conc_index", "final_uM", "replicate", "source_well", "edge"]
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(cols)
    for w in plate_map:
        wr.writerow([w[c] for c in cols])
    return buf.getvalue()
