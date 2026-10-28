import hashlib
import json
import math
from pathlib import Path

import pytest
import yaml
from lxml import etree

from htsorch import animl, datapackage
from htsorch.cli import main

SPECS = Path(__file__).resolve().parents[1] / "specs"
MAIN = SPECS / "ctg3d_screen.yaml"
NS = {"a": animl.CORE_NS, "t": animl.TECH_NS}

needs_schemas = pytest.mark.skipif(
    not animl.schemas_available(), reason="AnIML schemas not found; run scripts/fetch_animl_schemas.py")


def run_cli(tmp_path, spec_path=MAIN, *extra):
    rc = main(["run", str(spec_path), "--out", str(tmp_path), "--no-simulate", "--operator", "T.Operator", *extra])
    assert rc == 0
    pkg = sorted(tmp_path.glob("package-*"))[-1]
    return pkg, json.loads((pkg / "datapackage.json").read_text())


def series_values(root, result, series):
    r = next(x for x in root.iterfind(".//a:Result", NS) if x.get("name") == result)
    s = next(x for x in r.iterfind(".//a:Series", NS) if x.get("name") == series)
    return [v.text for v in s.find("a:IndividualValueSet", NS)]


@pytest.fixture(scope="module")
def pkg_ok(tmp_path_factory):
    return run_cli(tmp_path_factory.mktemp("animl_ok"))


# ---- export and validation ---------------------------------------------------------------

@needs_schemas
def test_export_is_validated_and_listed_in_the_package(pkg_ok):
    pkg, doc = pkg_ok
    assert doc["animl"]["exported"] and doc["animl"]["validated"]
    assert doc["animl"]["validated_against"]["core_namespace"] == animl.CORE_NS
    roles = {f["path"]: f["role"] for f in doc["files"]}
    assert roles["animl/experiment.animl"] == "animl" and roles["animl/techniques/hts-luminescence-viability.atdd"] == "animl"
    assert datapackage.verify_package(pkg) == []


@needs_schemas
def test_written_files_revalidate_independently(pkg_ok):
    pkg, _ = pkg_ok
    res = animl.validate_documents((pkg / "animl/experiment.animl").read_bytes(),
                                   (pkg / "animl/techniques/hts-luminescence-viability.atdd").read_bytes())
    assert res["valid"], res["errors"]


@needs_schemas
def test_document_carries_identity_provenance_and_data(pkg_ok):
    pkg, doc = pkg_ok
    root = etree.parse(str(pkg / "animl/experiment.animl")).getroot()
    params = {p.get("name"): p[0].text for p in root.iterfind(".//a:Parameter", NS)}
    assert params["run_id"] == doc["identifiers"]["run_id"]
    assert params["spec_sha256"] == doc["provenance"]["spec_sha256"]
    for name, h in doc["provenance"]["protocol_sha256"].items():
        assert params[name] == h
    assert root.find(".//a:Author[@userType='human']/a:Name", NS).text == "T.Operator"
    assert {s.get("barcode") for s in root.iterfind(".//a:Sample", NS)} == {"ASSAY-0001", "PREP-0001"}
    assert len(series_values(root, "Raw luminescence", "Luminescence")) == 384
    assert [s.get("experimentStepID") for s in root.iterfind(".//a:ExperimentStep", NS)][0] == "run-provenance"


@needs_schemas
def test_raw_values_match_the_reader_export(pkg_ok):
    pkg, _ = pkg_ok
    root = etree.parse(str(pkg / "animl/experiment.animl")).getroot()
    wells = series_values(root, "Raw luminescence", "Well")
    rlu = [float(v) for v in series_values(root, "Raw luminescence", "Luminescence")]
    export = {}
    for line in (pkg / "reader_export.csv").read_text().splitlines():
        if not line.startswith("#") and not line.startswith("well"):
            k, v = line.split(",")
            export[k] = float(v)
    assert dict(zip(wells, rlu)) == pytest.approx(export, rel=1e-6)


@needs_schemas
def test_technique_hash_in_document_matches_the_file(pkg_ok):
    pkg, _ = pkg_ok
    root = etree.parse(str(pkg / "animl/experiment.animl")).getroot()
    tq = root.find(".//a:Technique", NS)
    data = (pkg / "animl/techniques/hts-luminescence-viability.atdd").read_bytes()
    assert tq.get("sha256") == hashlib.sha256(data).hexdigest()
    assert tq.get("uri") == animl.TECHNIQUE_REL


@needs_schemas
def test_flags_travel_into_the_animl_document(tmp_path):
    pkg, doc = run_cli(tmp_path, MAIN, "--extra-compound-wait-min", "55", "--extra-incubation-h", "3")
    root = etree.parse(str(pkg / "animl/experiment.animl")).getroot()
    flags = next(r for r in root.iterfind(".//a:Result", NS) if r.get("name") == "Traceability flags")
    codes = {c.get("name") for c in flags}
    assert {"V001", "V042", "D040", "D043"} <= codes
    d043 = next(c for c in flags if c.get("name") == "D043")
    assert {p.get("name") for p in d043.iterfind("a:Parameter", NS)} >= {"message", "interpretation", "affected_wells"}


@needs_schemas
def test_pbs_blank_wells_are_nan_in_normalized_data(tmp_path):
    s = yaml.safe_load((SPECS / "example_edge_pbs_timing.yaml").read_text())
    s["timing"]["windows"]["cell_suspension_max_age_min"] = 60     # relaxed for this test only
    s["timing"]["windows"]["compound_to_cells_max_min"] = 120
    p = tmp_path / "pbs.yaml"
    p.write_text(yaml.safe_dump(s, sort_keys=False))
    pkg, _ = run_cli(tmp_path, p)
    root = etree.parse(str(pkg / "animl/experiment.animl")).getroot()
    vals = series_values(root, "Normalized data", "Normalized response")
    assert len(vals) == 384 and sum(v == "NaN" for v in vals) == 100
    assert sum(1 for v in vals if v != "NaN" and math.isfinite(float(v))) == 284


# ---- the validator must actually reject bad documents ------------------------------------

@needs_schemas
def test_schema_rejects_a_missing_required_attribute(pkg_ok):
    pkg, _ = pkg_ok
    core = etree.parse(str(pkg / "animl/experiment.animl"))
    core.find(".//a:Series", NS).attrib.pop("seriesID")
    res = animl.validate_documents(etree.tostring(core), (pkg / "animl/techniques/hts-luminescence-viability.atdd").read_bytes())
    assert not res["valid"] and any("seriesID" in e for e in res["errors"])


@needs_schemas
def test_conformance_check_catches_a_changed_technique_file(pkg_ok):
    pkg, _ = pkg_ok
    core = (pkg / "animl/experiment.animl").read_bytes()
    tech = (pkg / "animl/techniques/hts-luminescence-viability.atdd").read_bytes().replace(b"Plate reading", b"Other name")
    problems = animl.check_conformance(core, tech)
    assert any("sha256" in p for p in problems) and any("Plate reading" in p or "Other name" in p for p in problems)


@needs_schemas
def test_conformance_check_catches_a_wrong_series_type(pkg_ok):
    pkg, _ = pkg_ok
    core = etree.parse(str(pkg / "animl/experiment.animl"))
    r = next(x for x in core.iterfind(".//a:Result", NS) if x.get("name") == "Raw luminescence")
    next(s for s in r.iterfind(".//a:Series", NS) if s.get("name") == "Luminescence").set("seriesType", "String")
    problems = animl.check_conformance(etree.tostring(core), (pkg / "animl/techniques/hts-luminescence-viability.atdd").read_bytes())
    assert any("seriesType" in p for p in problems)


@needs_schemas
def test_invalid_export_aborts_and_leaves_no_package(tmp_path, monkeypatch):
    monkeypatch.setattr(animl, "build_core", lambda *a, **k: b'<AnIML xmlns="%s" version="0.90"><Bogus/></AnIML>' % animl.CORE_NS.encode())
    with pytest.raises(ValueError, match="AnIML export failed validation"):
        main(["run", str(MAIN), "--out", str(tmp_path), "--no-simulate"])
    assert not list(tmp_path.glob("package-*"))


# ---- behaviour without schemas ----------------------------------------------------------

def test_auto_mode_skips_with_a_reason_when_schemas_are_missing(tmp_path):
    pkg, doc = run_cli(tmp_path, MAIN, "--animl-schema-dir", str(tmp_path / "nowhere"))
    assert doc["animl"]["exported"] is False and "fetch_animl_schemas" in doc["animl"]["reason"]
    assert not (pkg / "animl").exists()
    assert datapackage.verify_package(pkg) == []


def test_require_mode_fails_and_leaves_no_package(tmp_path):
    with pytest.raises(FileNotFoundError, match="fetch_animl_schemas"):
        main(["run", str(MAIN), "--out", str(tmp_path), "--no-simulate", "--animl", "require",
              "--animl-schema-dir", str(tmp_path / "nowhere")])
    assert not list(tmp_path.glob("package-*"))


def test_off_mode_does_not_export(tmp_path):
    _, doc = run_cli(tmp_path, MAIN, "--animl", "off")
    assert doc["animl"] == {"exported": False, "reason": "disabled"}
