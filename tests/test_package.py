import csv
import hashlib
import json
import statistics
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from htsorch import datapackage, readout
from htsorch.cli import main
from htsorch.spec import load_spec

SPECS = Path(__file__).resolve().parents[1] / "specs"
MAIN = SPECS / "ctg3d_screen.yaml"


def run_cli(tmp_path, spec_path=MAIN, *extra):
    rc = main(["run", str(spec_path), "--out", str(tmp_path), "--no-simulate", "--operator", "T.Operator", *extra])
    assert rc == 0
    pkgs = sorted(tmp_path.glob("package-*"))
    assert pkgs
    return pkgs[-1], json.loads((pkgs[-1] / "datapackage.json").read_text())


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    return run_cli(tmp_path_factory.mktemp("base"))


# ---- identity, provenance, integrity ------------------------------------------

def test_package_verifies_and_has_identity(baseline):
    pkg, doc = baseline
    assert datapackage.verify_package(pkg) == []
    assert str(uuid.UUID(doc["identifiers"]["run_id"])) == doc["identifiers"]["run_id"]
    assert doc["provenance"]["operator_id"] == "T.Operator"
    assert doc["provenance"]["simulated"] is True


def test_protocol_hashes_match_the_included_files(baseline):
    pkg, doc = baseline
    assert set(doc["provenance"]["protocol_sha256"]) == {"flex_1_dilution.py", "flex_2_seeding.py", "flex_3_readout.py"}
    for name, h in doc["provenance"]["protocol_sha256"].items():
        assert hashlib.sha256((pkg / "inputs" / name).read_bytes()).hexdigest() == h
    assert doc["provenance"]["spec_sha256"] == hashlib.sha256(MAIN.read_bytes()).hexdigest()


def test_timestamps_are_iso_and_incubation_is_72h(baseline):
    _, doc = baseline
    inc = doc["timeline"]["incubation"]
    start, end = datetime.fromisoformat(inc["start_utc"]), datetime.fromisoformat(inc["end_utc"])
    assert (end - start).total_seconds() / 3600 == pytest.approx(72.0)
    assert inc["within_tolerance"] is True
    assert all(datetime.fromisoformat(e["start_utc"]).tzinfo for e in doc["timeline"]["events"])


def test_instrument_states_are_recorded_and_marked_simulated(baseline):
    _, doc = baseline
    states = doc["instrument_states_at_read"]
    assert {"flex", "idot", "arm", "incubator", "reader"} <= set(states)
    assert all(s["simulated"] for s in states.values())


def test_tampering_with_events_or_data_is_detected(tmp_path):
    pkg, doc = run_cli(tmp_path)
    doc["timeline"]["events"][3]["duration_min"] = 999
    (pkg / "datapackage.json").write_text(json.dumps(doc))
    assert "event hash chain broken" in datapackage.verify_package(pkg)
    (pkg / "wells.csv").write_text((pkg / "wells.csv").read_text().replace("pos", "neg", 1))
    assert any("hash mismatch wells.csv" in p for p in datapackage.verify_package(pkg))


# ---- flags travel with the data -----------------------------------------------------

def test_preflight_warnings_travel_with_every_well(baseline):
    pkg, doc = baseline
    codes = {f["code"]: f for f in doc["flags"] if f["phase"] == "preflight"}
    assert {"V001", "V042"} <= set(codes)
    assert all(f["interpretation"] for f in codes.values())
    rows = list(csv.DictReader(open(pkg / "wells.csv")))
    assert all("V001" in r["flags"] and "V042" in r["flags"] for r in rows)


def test_baseline_has_no_runtime_deviations(baseline):
    _, doc = baseline
    assert not [f for f in doc["flags"] if f["phase"] == "runtime"]


def test_handoff_delay_and_overrun_become_flags(tmp_path):
    pkg, doc = run_cli(tmp_path, MAIN, "--extra-compound-wait-min", "55", "--extra-incubation-h", "3")
    runtime = {f["code"]: f for f in doc["flags"] if f["phase"] == "runtime"}
    assert {"D040", "D043"} <= set(runtime)
    assert doc["timeline"]["incubation"]["within_tolerance"] is False
    rows = list(csv.DictReader(open(pkg / "wells.csv")))
    assert all("D040" in r["flags"] for r in rows)
    assert datapackage.verify_package(pkg) == []


def test_partial_delay_flags_only_the_late_wells(tmp_path):
    sc = tmp_path / "instant_operator.yaml"          # the decision itself would otherwise add minutes to every well
    sc.write_text("operator: {response_min: 0}\n")
    pkg, doc = run_cli(tmp_path, MAIN, "--extra-compound-wait-min", "44", "--scenario", str(sc))
    d040 = next(f for f in doc["flags"] if f["code"] == "D040")
    assert isinstance(d040["affected_wells"], list) and 0 < len(d040["affected_wells"]) < 384
    rows = {r["well"]: r for r in csv.DictReader(open(pkg / "wells.csv"))}
    flagged = {w for w, r in rows.items() if "D040" in r["flags"]}
    assert flagged == set(d040["affected_wells"])
    assert all(float(rows[w]["compound_to_cells_min"]) > 60 for w in flagged)


# ---- files, dictionary, simulation hygiene ---------------------------------------------

def test_data_dictionary_matches_wells_csv(baseline):
    pkg, doc = baseline
    header = next(csv.reader(open(pkg / "wells.csv")))
    assert header == [c["column"] for c in doc["data_dictionary"]]
    assert all("unit" in c for c in doc["data_dictionary"])
    assert len(list(csv.DictReader(open(pkg / "wells.csv")))) == 384


def test_simulation_truth_is_kept_out_of_the_main_record(baseline):
    pkg, doc = baseline
    assert "ic50_truth" not in json.dumps(doc)
    roles = {f["path"]: f["role"] for f in doc["files"]}
    assert roles["simulation_truth.json"] == "simulation_only"
    assert any("project-specific JSON layout" in x for x in doc["limitations"])


def test_pbs_fill_package_has_blank_wells_and_edge_protocol(tmp_path):
    s = yaml.safe_load((SPECS / "example_edge_pbs_timing.yaml").read_text())
    s["timing"]["windows"]["cell_suspension_max_age_min"] = 60     # relaxed for this test only
    s["timing"]["windows"]["compound_to_cells_max_min"] = 120
    p = tmp_path / "pbs.yaml"
    p.write_text(yaml.safe_dump(s, sort_keys=False))
    pkg, doc = run_cli(tmp_path, p)
    rows = list(csv.DictReader(open(pkg / "wells.csv")))
    pbs = [r for r in rows if r["type"] == "pbs"]
    assert len(pbs) == 100 and all(r["normalized_pct"] == "" and r["cell_age_min"] == "" for r in pbs)
    assert "flex_0_edge_fill.py" in doc["provenance"]["protocol_sha256"]
    assert datapackage.verify_package(pkg) == []


# ---- reader ingest and analysis --------------------------------------------------------

def test_reader_export_for_another_plate_is_rejected():
    s = load_spec(MAIN)
    text = readout.write_reader_export(s, {"A1": 1.0})
    with pytest.raises(ValueError, match="expected 'OTHER-PLATE'"):
        readout.parse_reader_export(text, "OTHER-PLATE")
    with pytest.raises(ValueError, match="not luminescence"):
        readout.parse_reader_export(text.replace("luminescence", "absorbance"), s["assay"]["plate"]["barcode"])


def test_ic50_recovery_against_known_truth():
    from htsorch.platemap import build_plate_map
    from htsorch.spec import derive
    s = load_spec(MAIN)
    pm = build_plate_map(s, derive(s))
    truth = readout.simulate_truth(s, 7)
    a = readout.analyze(pm, readout.simulate_reader(s, pm, truth, 7))
    assert a["qc"]["z_prime_pass"] and a["qc"]["z_prime"] > 0.5
    folds = []
    for f in a["fits"]:
        t = truth[f["compound_id"]]
        if f["status"] == "active":
            assert t["active"], f"{f['compound_id']} reported active but is inactive in truth"
            folds.append(max(f["ic50_uM"] / t["ic50_uM"], t["ic50_uM"] / f["ic50_uM"]))
        if not t["active"]:
            assert f["status"] in ("inactive", "low_confidence")
    assert len(folds) >= 10
    assert statistics.median(folds) < 1.6 and max(folds) < 3.0     # 5 points at 1:4 spacing limit resolution


def test_poor_plate_fails_z_prime():
    from htsorch.platemap import build_plate_map
    from htsorch.spec import derive
    s = load_spec(MAIN)
    pm = build_plate_map(s, derive(s))
    readings = {w["well"]: (900.0 if w["type"] == "pos" else 1000.0) for w in pm}
    import random
    rnd = random.Random(1)
    readings = {k: v * rnd.uniform(0.8, 1.2) for k, v in readings.items()}
    assert readout.analyze(pm, readings)["qc"]["z_prime_pass"] is False
