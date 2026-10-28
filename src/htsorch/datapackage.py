"""FAIR-style data package written at the end of Day 3.

One folder with open formats (JSON, CSV), a persistent run identifier, hashes of every
input and output, a data dictionary with units, and the pre-flight and run-time flags that
could change how a result should be read. Flags travel with the data on purpose: a
reviewer looking at a strange well can see whether it is biology or process.

Standard note: this is our own JSON layout (schema 0.1.0). It is not AnIML or any
other published standard, and makes no claim of conformance to one."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, animl
from .readout import parse_reader_export
from .chain import verify_chain
from .validator import to_dicts

SCHEMA = {"name": "hts-orchestrator.datapackage", "version": "0.1.0"}

NOTES = {
    "V001": "DMSO is above the warning level. A solvent effect on viability is possible; read potency against the DMSO solvent wells.",
    "V013": "Some perimeter wells hold no control, so edge behaviour is less well characterised.",
    "V010": "Some wells carry no condition; they are not part of the analysis.",
    "V014": "PBS blank volume differs from the assay volume, so blank luminescence is not a like-for-like background.",
    "V040": "Compound spots sat dry before cells were added. Evaporation or water uptake of DMSO spots can shift apparent potency in all wells.",
    "V041": "Cells waited in suspension. Viability and density can drift in dispense order; see cell_age_min per well.",
    "V042": "Reagent was added over a spread of time, so wells dosed last had less lysis incubation; see reagent_offset_min per well.",
    "V051": "Few controls in one plate quadrant, so spatial drift is harder to detect there.",
    "D040": "Compound-to-cells wait exceeded its window in the affected wells. Potency may be shifted by evaporation or water uptake; treat hits there with caution.",
    "D041": "Cell suspension was older than its hold window in the affected wells. Cell number or viability may differ from the rest of the plate.",
    "D042": "Reagent lag exceeded its window for the affected wells; signal may be biased by shorter lysis incubation.",
    "D043": "Incubation was outside tolerance for the whole plate. Spheroid age differs from plan; compare with other plates before pooling.",
    "D050": "A person intervened during the run (see the timeline for what and when). Check the timing flags for wells affected by the delay.",
    "D051": "The assay plate was out of tracked custody for a time. It was found and confirmed by a person; handling conditions during that time are unknown.",
    "D052": "The incubator reported a temperature excursion while the plate was inside. Spheroid growth may differ from other plates; compare before pooling.",
    "QC001": "Plate failed the Z' criterion. Hits from this plate are not reliable without a repeat.",
}

COLUMNS = [
    ("well", "Well name, row letter + column number", ""),
    ("plate_row", "Zero-based row index", ""),
    ("plate_col", "Zero-based column index", ""),
    ("type", "test, neg (negative control, DMSO solvent), pos (positive control, kill) or pbs (blank)", ""),
    ("compound_id", "Compound identifier; DMSO, KILL or PBS for non-test wells", ""),
    ("concentration_uM", "Final concentration in the well; 0 for solvent controls and PBS", "umol/L"),
    ("replicate", "Replicate number within the condition", ""),
    ("source_well", "iDOT prep-plate source well", ""),
    ("edge", "True for perimeter wells", ""),
    ("raw_rlu", "Luminescence as exported by the reader (synthetic in this demo)", "RLU"),
    ("normalized_pct", "(raw - mean kill) / (mean solvent control - mean kill) x 100; blank for PBS wells", "%"),
    ("cell_age_min", "Cell suspension age when this well was seeded (simulated clock)", "min"),
    ("compound_to_cells_min", "Time from end of iDOT dispense to cells in this well (simulated clock)", "min"),
    ("reagent_offset_min", "Delay of reagent addition relative to the first well (simulated clock)", "min"),
    ("flags", "Semicolon-separated flag codes that apply to this well; see flags in datapackage.json", ""),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flags(findings, run, analysis, plate_map, window) -> list[dict]:
    flags = []
    for f in findings:
        if f.level == "WARN":
            flags.append({"code": f.code, "level": "WARN", "phase": "preflight", "message": f.what,
                          "system_action": f.system_action, "responsible": f.who,
                          "interpretation": NOTES.get(f.code, ""), "affected_wells": "all"})
    wt = run["well_timing"]
    checks = {
        "D040": [w for w, v in wt.items() if (v["compound_to_cells_min"] or 0) > window["compound_to_cells_max_min"]],
        "D041": [w for w, v in wt.items() if (v["cell_age_min"] or 0) > window["cell_suspension_max_age_min"]],
        "D042": [w for w, v in wt.items() if v["reagent_offset_min"] > window["max_reagent_lag_min"]],
        "D043": [w["well"] for w in plate_map] if any(x["code"] == "D043" for x in run["deviations"]) else [],
    }
    for dev in run["deviations"]:
        wells = checks.get(dev["code"], [])
        affected = dev["affected_wells"] if "affected_wells" in dev else (
            "all" if len(wells) == len(plate_map) else sorted(wells))
        flags.append({"code": dev["code"], "level": "DEVIATION", "phase": "runtime", "message": dev["what"],
                      "system_action": "Run continued, deviation recorded", "responsible": "Scientist: review before using affected wells",
                      "interpretation": NOTES.get(dev["code"], ""), "affected_wells": affected})
    if not analysis["qc"]["z_prime_pass"]:
        flags.append({"code": "QC001", "level": "QC_FAIL", "phase": "analysis",
                      "message": f"Z' = {analysis['qc']['z_prime']:.2f}, below 0.5",
                      "system_action": "Results written with flag", "responsible": "Scientist: repeat or justify",
                      "interpretation": NOTES["QC001"], "affected_wells": "all"})
    return flags


def _well_rows(plate_map, readings, analysis, run, flags):
    per_well = {}
    for fl in flags:
        if fl["affected_wells"] == "all":
            for w in plate_map:
                per_well.setdefault(w["well"], []).append(fl["code"])
        else:
            for w in fl["affected_wells"]:
                per_well.setdefault(w, []).append(fl["code"])
    rows = []
    for w in plate_map:
        t = run["well_timing"][w["well"]]
        rows.append({
            "well": w["well"], "plate_row": w["row"], "plate_col": w["col"], "type": w["type"],
            "compound_id": w["compound_id"], "concentration_uM": 0 if w["type"] in ("neg", "pbs") else f"{w['final_uM']:.6g}",
            "replicate": w["replicate"], "source_well": w["source_well"], "edge": w["edge"],
            "raw_rlu": f"{readings[w['well']]:.1f}",
            "normalized_pct": "" if w["type"] == "pbs" else f"{analysis['normalized_pct'][w['well']]:.2f}",
            "cell_age_min": "" if t["cell_age_min"] is None else f"{t['cell_age_min']:.2f}",
            "compound_to_cells_min": "" if t["compound_to_cells_min"] is None else f"{t['compound_to_cells_min']:.2f}",
            "reagent_offset_min": f"{t['reagent_offset_min']:.2f}",
            "flags": ";".join(sorted(set(per_well.get(w["well"], [])))),
        })
    return rows


def _csv_text(rows, fields) -> str:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    wr.writeheader()
    wr.writerows(rows)
    return buf.getvalue()


def write_package(out_dir, **kw) -> Path:
    """Write the package; a failed export (for example AnIML validation) leaves no half-written folder."""
    pkg = Path(out_dir) / f"package-{kw['spec']['experiment']['id']}-{kw['run']['run_id'][:8]}"
    try:
        return _write_package(out_dir, **kw)
    except BaseException:
        shutil.rmtree(pkg, ignore_errors=True)
        raise


def _write_package(out_dir, *, spec, d, plate_map, findings, manifest, build_dir, run, truth, readings,
                  export_text, analysis, license_text="UNSPECIFIED - set before sharing",
                  animl_mode="auto", animl_schema_dir=None) -> Path:
    # Ingest the export the way a real run would: parse it, and refuse a wrong-plate file.
    parsed = parse_reader_export(export_text, spec["assay"]["plate"]["barcode"])
    assert parsed.keys() == readings.keys()
    readings = parsed     # everything downstream uses what was ingested, never the simulator's own floats

    pkg = Path(out_dir) / f"package-{spec['experiment']['id']}-{run['run_id'][:8]}"
    if pkg.exists():
        shutil.rmtree(pkg)
    (pkg / "inputs").mkdir(parents=True)
    (pkg / "analysis").mkdir()

    files: list[dict] = []

    def put(rel, data, role, media):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_bytes(raw)
        files.append({"path": rel, "role": role, "media_type": media, "bytes": len(raw), "sha256": _sha(raw)})

    spec_text = Path(spec["_meta"]["path"]).read_bytes()
    put("inputs/spec.yaml", spec_text, "input", "application/yaml")
    protocols = {}
    for name in sorted(manifest["files"]):
        data = (Path(build_dir) / name).read_bytes()
        if name.endswith(".py"):
            protocols[name] = _sha(data)
        put(f"inputs/{name}", data, "input", "text/x-python" if name.endswith(".py") else "text/csv")
    put("reader_export.csv", export_text, "raw_data", "text/csv")
    if run.get("custody_log") is not None:
        put("audit/custody_log.json", json.dumps({"note": "Plate custody log, hash-chained. Plate flags travel with the data.",
                                                  "plates": run["plate_flags"], "log": run["custody_log"]}, indent=1),
            "audit", "application/json")

    flags = _flags(findings, run, analysis, plate_map, run["window_summary"]["windows"])
    rows = _well_rows(plate_map, readings, analysis, run, flags)
    put("wells.csv", _csv_text(rows, [c[0] for c in COLUMNS]), "derived_data", "text/csv")
    fit_cols = ["compound_id", "status", "ic50_uM", "hill", "bottom_pct", "top_pct", "r2", "n_points",
                "top_conc_uM", "pct_at_top_conc", "note"]
    put("analysis/curve_fits.csv",
        _csv_text([{k: ("" if r.get(k) is None else r.get(k)) for k in fit_cols} for r in analysis["fits"]], fit_cols),
        "derived_data", "text/csv")
    put("simulation_truth.json", json.dumps({
        "WARNING": "Synthetic ground truth used to generate the reader data. Not part of a real data package.",
        "ic50_truth_uM": truth}, indent=1), "simulation_only", "application/json")

    a = spec["assay"]
    doc = {
        "schema": SCHEMA,
        "identifiers": {
            "run_id": run["run_id"], "experiment_id": spec["experiment"]["id"],
            "assay_plate_barcode": a["plate"]["barcode"], "prep_plate_barcode": spec["prep_plate"]["barcode"],
            "package_created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "provenance": {
            "operator_id": run["operator_id"], "simulated": True,
            "simulation_statement": "All times, device states and reader values are simulated. No hardware was used.",
            "software": {"htsorch": __version__, "python": platform.python_version(),
                         **_versions(["opentrons", "numpy", "scipy", "yaml"])},
            "spec_sha256": spec["_meta"]["sha256"], "generated_file_sha256": manifest["files"],
            "protocol_sha256": protocols,
            "opentrons_simulate": manifest.get("opentrons_simulate", {}),
        },
        "experiment": {
            "description": spec["experiment"]["description"].strip(), "random_seed": spec["experiment"]["random_seed"],
            "edge_policy": d["edge_policy"],
            "volumes": {"media_uL": a["media_volume_ul"], "cells_uL": a["cell_volume_ul"],
                        "compound_nL": d["compound_nl_max"], "compound_nL_by_point": d["nl_by_point"], "reagent_uL": d["reagent_volume_ul"]},
            "dmso_percent": round(d["dmso_percent"], 4),
            "reagent": {"name": a["reagent"]["name"], "shake_min": a["reagent"]["shake_min"],
                        "incubate_min": a["reagent"]["incubate_min"]},
            "dilution": {"final_uM": d["final_uM"], "factor": spec["dilution"]["factor"], "unit": "umol/L"},
            "readout": {"mode": "luminescence", "unit": "RLU"},
        },
        "timeline": {"clock": run["clock"], "start_utc": run["start_utc"], "end_utc": run["end_utc"],
                     "incubation": run["incubation"], "window_summary": run["window_summary"],
                     "events": run["events"], "event_chain_head": run["event_chain_head"]},
        "instrument_states_at_read": run["instrument_states_at_read"],
        "operations": {"window_alerts": run.get("alerts", {}), "interventions": run.get("interventions", [])},
        "preflight_validation": to_dicts(findings),
        "flags": flags,
        "analysis": {"qc": analysis["qc"], "curve_fits_file": "analysis/curve_fits.csv",
                     "method": "4-parameter logistic, least squares, bounds on hill and plateaus; "
                               "IC50 withheld when effect at top concentration is below 30%"},
        "data_dictionary": [{"column": c, "description": desc, "unit": unit} for c, desc, unit in COLUMNS],
        "access": {"formats": ["JSON", "CSV", "YAML", "Python"], "license": license_text, "doi": None,
                   "note": "No persistent public identifier is minted; run_id is a local UUID."},
        "limitations": manifest.get("boundaries", []) + [
            "Reader values are synthetic and generated from known IC50s.",
            "Timing figures are estimates on a simulated clock.",
            "datapackage.json is a project-specific JSON layout, not a published standard; "
            "see 'animl' for the schema-validated AnIML export when it was produced.",
        ],
    }
    animl_files, doc["animl"] = animl.export(doc, rows, analysis, run, readings, plate_map,
                                              mode=animl_mode, schema_dir=animl_schema_dir)
    for rel, data in animl_files.items():
        put(rel, data, "animl", "application/xml")
    # file list is written last so that datapackage.json lists every other file with its hash
    doc["files"] = sorted(files, key=lambda f: f["path"])
    (pkg / "datapackage.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    return pkg


def _versions(mods):
    import importlib
    out = {}
    for m in mods:
        try:
            out[m] = getattr(importlib.import_module(m), "__version__", "unknown")
        except ImportError:
            out[m] = "not installed"
    return out


def verify_package(pkg) -> list[str]:
    """Return a list of problems; empty means every listed file hash and the event chain check out."""
    pkg = Path(pkg)
    doc = json.loads((pkg / "datapackage.json").read_text(encoding="utf-8"))
    problems = []
    for f in doc["files"]:
        p = pkg / f["path"]
        if not p.exists():
            problems.append(f"missing {f['path']}")
        elif _sha(p.read_bytes()) != f["sha256"]:
            problems.append(f"hash mismatch {f['path']}")
    if not verify_chain(doc["timeline"]["events"]):
        problems.append("event hash chain broken")
    cl = pkg / "audit" / "custody_log.json"
    if cl.exists() and not verify_chain(json.loads(cl.read_text(encoding="utf-8"))["log"]):
        problems.append("custody log hash chain broken")
    for name, h in doc["provenance"]["protocol_sha256"].items():
        p = pkg / "inputs" / name
        if not p.exists() or _sha(p.read_bytes()) != h:
            problems.append(f"protocol hash mismatch {name}")
    return problems
