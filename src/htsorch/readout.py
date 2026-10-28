"""Simulated plate reader, export parser, plate QC and 4-parameter curve fits.

The reader values are synthetic: generated from known IC50s plus noise, so the
analysis can be checked against the truth. The truth is kept apart from the data."""
from __future__ import annotations

import csv
import io
import math

import numpy as np
from scipy.optimize import curve_fit

NEG_MEAN_RLU = 1.0e6      # arbitrary synthetic scale
KILL_FRACTION = 0.03
BACKGROUND_RLU = 800.0
CV = 0.08


def simulate_truth(spec: dict, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    ids = spec["compounds"]["ids"]
    ic50 = 10 ** rng.uniform(math.log10(0.05), math.log10(20), size=len(ids))
    inactive = rng.choice(len(ids), size=max(1, len(ids) // 5), replace=False)
    ic50[inactive] = 1e4
    hill = rng.uniform(0.8, 1.6, size=len(ids))
    return {cid: {"ic50_uM": float(ic50[i]), "hill": float(hill[i]), "active": bool(ic50[i] < 1e3)}
            for i, cid in enumerate(ids)}


def simulate_reader(spec: dict, plate_map: list, truth: dict, seed: int) -> dict:
    rng = np.random.default_rng(seed + 1)
    vals = {}
    for w in plate_map:
        if w["type"] == "pbs":
            base = BACKGROUND_RLU
        elif w["type"] == "neg":
            base = NEG_MEAN_RLU
        elif w["type"] == "pos":
            base = NEG_MEAN_RLU * KILL_FRACTION
        else:
            t = truth[w["compound_id"]]
            frac = KILL_FRACTION + (1 - KILL_FRACTION) / (1 + (w["final_uM"] / t["ic50_uM"]) ** t["hill"])
            base = NEG_MEAN_RLU * frac
        vals[w["well"]] = float(base * rng.lognormal(0.0, CV))
    return vals


def write_reader_export(spec: dict, values: dict) -> str:
    buf = io.StringIO()
    buf.write(f"# plate_barcode={spec['assay']['plate']['barcode']}\n")
    buf.write("# mode=luminescence\n# unit=RLU\n# instrument=SIMULATED-READER\n")
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(["well", "value"])
    for well in sorted(values, key=lambda x: (x[0], int(x[1:]))):
        wr.writerow([well, f"{values[well]:.1f}"])
    return buf.getvalue()


def parse_reader_export(text: str, expected_barcode: str) -> dict:
    meta, rows = {}, []
    for line in text.splitlines():
        if line.startswith("#"):
            k, _, v = line[1:].strip().partition("=")
            meta[k] = v
        elif line.strip():
            rows.append(line)
    if meta.get("plate_barcode") != expected_barcode:
        raise ValueError(f"reader export is for plate '{meta.get('plate_barcode')}', expected '{expected_barcode}'")
    if meta.get("mode") != "luminescence":
        raise ValueError(f"reader export mode '{meta.get('mode')}' is not luminescence")
    return {r["well"]: float(r["value"]) for r in csv.DictReader(rows)}


def _four_pl(x, bottom, top, ic50, hill):
    return bottom + (top - bottom) / (1.0 + (x / ic50) ** hill)


def analyze(plate_map: list, readings: dict) -> dict:
    neg = np.array([readings[w["well"]] for w in plate_map if w["type"] == "neg"])
    pos = np.array([readings[w["well"]] for w in plate_map if w["type"] == "pos"])
    mu_n, mu_p = neg.mean(), pos.mean()
    z = 1 - 3 * (neg.std(ddof=1) + pos.std(ddof=1)) / abs(mu_n - mu_p)
    qc = {"neg_mean_rlu": float(mu_n), "neg_cv_pct": float(neg.std(ddof=1) / mu_n * 100),
          "pos_mean_rlu": float(mu_p), "pos_cv_pct": float(pos.std(ddof=1) / mu_p * 100),
          "signal_to_background": float(mu_n / mu_p), "z_prime": float(z), "z_prime_pass": bool(z >= 0.5),
          "n_neg": int(neg.size), "n_pos": int(pos.size)}
    pct = {w["well"]: (readings[w["well"]] - mu_p) / (mu_n - mu_p) * 100.0 for w in plate_map}

    by_comp: dict[str, list] = {}
    for w in plate_map:
        if w["type"] == "test":
            by_comp.setdefault(w["compound_id"], []).append((w["final_uM"], pct[w["well"]]))
    fits = []
    for cid, pts in by_comp.items():
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        top_conc = float(x.max())
        row = {"compound_id": cid, "n_points": int(x.size), "top_conc_uM": top_conc,
               "pct_at_top_conc": float(y[x == top_conc].mean())}
        try:
            popt, _ = curve_fit(_four_pl, x, y, p0=[5.0, 100.0, float(np.median(x)), 1.0],
                                bounds=([-20, 60, x.min() / 100, 0.3], [40, 140, x.max() * 1000, 5.0]), maxfev=20000)
            resid = y - _four_pl(x, *popt)
            r2 = 1 - (resid ** 2).sum() / ((y - y.mean()) ** 2).sum()
            row.update(ic50_uM=float(popt[2]), hill=float(popt[3]), bottom_pct=float(popt[0]),
                       top_pct=float(popt[1]), r2=float(r2), fit_ok=True)
        except (RuntimeError, ValueError):
            row.update(ic50_uM=None, hill=None, bottom_pct=None, top_pct=None, r2=None, fit_ok=False)
        if row["pct_at_top_conc"] > 70:
            row["status"] = "inactive"
            row["note"] = "less than 30% effect at the top concentration; IC50 not reported"
            row["ic50_uM"] = None
        elif not row["fit_ok"]:
            row["status"] = "fit_failed"
            row["note"] = "4-parameter fit did not converge"
        elif row["ic50_uM"] > top_conc or row["r2"] < 0.9:
            row["status"] = "low_confidence"
            row["note"] = "IC50 outside tested range or poor fit (R2 < 0.9)"
        else:
            row["status"] = "active"
            row["note"] = ""
        fits.append(row)
    return {"qc": qc, "normalized_pct": pct, "fits": fits}
