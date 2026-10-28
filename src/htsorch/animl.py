"""AnIML export of a Day 3 data package, validated against the published XML schemas.

What this does
  * writes an AnIML core document (experiment.animl) and a project-specific
    Technique Definition (techniques/*.atdd) for the luminescence read
  * validates both against animl-core.xsd and animl-technique.xsd when those
    files are available (scripts/fetch_animl_schemas.py downloads them)
  * runs a small project-level check that the document matches its technique definition

What this does not do
  * It is not the official AnIML validator and does not replace it.
  * The schemas are version 0.90 with a ':draft:' namespace, as published by the AnIML project.
    This code makes no claim about the final ASTM E1947 text.
  * The Technique Definition is ours, not a community-published one.
  * Documents are not digitally signed (no SignatureSet).
  * Units: only min, %, uM and RLU carry SI definitions. Volume units are labels without an
    SI mapping because the schema does not define how factor and exponent combine."""
from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from lxml import etree

from . import __version__

CORE_NS = "urn:org:astm:animl:schema:core:draft:0.90"
TECH_NS = "urn:org:astm:animl:schema:technique:draft:0.90"
SCHEMA_FILES = ("animl-core.xsd", "animl-technique.xsd", "xmldsig-core-schema.xsd")
SCHEMA_SOURCE = "https://github.com/AnIML/schemas"
TECHNIQUE_NAME = "HTS luminescence viability endpoint"
TECHNIQUE_REL = "techniques/hts-luminescence-viability.atdd"
DOC_REL = "experiment.animl"

# label -> list of (SI unit name, factor, exponent); None = label only, no SI mapping
UNITS = {
    "min": [("s", 60, 1)],
    "%": [("1", 0.01, 1)],
    "uM": [("mol", 1e-3, 1), ("m", 1, -3)],
    "RLU": [("1", 1, 1)],
    "1": [("1", 1, 1)],
    "nL": None,
    "uL": None,
    "h": [("s", 3600, 1)],
}

_TAGS = {"String": "S", "Float64": "D", "Int32": "I", "Boolean": "Boolean", "DateTime": "DateTime"}


def default_schema_dir() -> Path:
    env = os.environ.get("HTS_ANIML_SCHEMA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "schemas" / "animl"


def schemas_available(schema_dir=None) -> bool:
    d = Path(schema_dir) if schema_dir else default_schema_dir()
    return all((d / f).exists() for f in SCHEMA_FILES)


# ---- small XML helpers -------------------------------------------------------------

def _q(tag, ns=CORE_NS):
    return f"{{{ns}}}{tag}"


def _sub(parent, tag, text=None, ns=CORE_NS, **attrs):
    el = etree.SubElement(parent, _q(tag, ns), {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _fmt(value, kind) -> str:
    if kind == "Float64":
        if value is None or value == "":
            return "NaN"
        v = float(value)
        return "NaN" if math.isnan(v) else format(v, ".10g")
    if kind == "Int32":
        return str(int(value))
    if kind == "Boolean":
        return "true" if value in (True, "True", "true", 1) else "false"
    return "" if value is None else str(value)


def _unit(parent, label, ns=CORE_NS):
    u = _sub(parent, "Unit", ns=ns, label=label)
    for name, factor, exp in (UNITS.get(label) or []):
        attrs = {}
        if factor != 1:
            attrs["factor"] = format(factor, "g")
        if exp != 1:
            attrs["exponent"] = exp
        _sub(u, "SIUnit", name, ns=ns, **attrs)
    return u


def _param(parent, name, kind, value, unit=None):
    p = _sub(parent, "Parameter", name=name, parameterType=kind)
    _sub(p, _TAGS[kind], _fmt(value, kind))
    if unit:
        _unit(p, unit)
    return p


def _series(sset, name, sid, kind, values, dependency="dependent", unit=None):
    s = _sub(sset, "Series", name=name, seriesID=sid, dependency=dependency, seriesType=kind)
    ivs = _sub(s, "IndividualValueSet")
    for v in values:
        _sub(ivs, _TAGS[kind], _fmt(v, kind))
    if unit:
        _unit(s, unit)
    return s


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _tostring(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ---- technique definition ----------------------------------------------------------

def build_technique() -> bytes:
    """Project-specific Technique Definition for the luminescence endpoint read."""
    T = TECH_NS
    root = etree.Element(_q("Technique", T), nsmap={None: T}, version="0.90", name=TECHNIQUE_NAME)
    _sub(root, "Documentation", "Plate-reader luminescence read of a 384-well viability assay. Defined by "
         "hts-orchestrator for demonstration; not a community-published technique definition.", ns=T)
    _sub(root, "SampleRoleBlueprint", ns=T, name="Assay plate", samplePurpose="consumed")
    rb = _sub(root, "ResultBlueprint", ns=T, name="Raw luminescence")
    ssb = _sub(rb, "SeriesSetBlueprint", ns=T, name="Plate reading")
    _sub(ssb, "SeriesBlueprint", ns=T, name="Well", seriesType="String", dependency="independent")
    choice = _sub(ssb, "SeriesBlueprintChoice", ns=T)
    lum = _sub(choice, "SeriesBlueprint", ns=T, name="Luminescence", seriesType="Float", dependency="dependent")
    q = _sub(lum, "Quantity", ns=T, name="Luminescence")
    u = _sub(q, "Unit", ns=T, label="RLU")
    _sub(u, "SIUnit", "1", ns=T)
    return _tostring(root)


# ---- core document -----------------------------------------------------------------

STEPS = [
    ("flex_edge_fill", "edge-fill", "Edge fill with PBS", "flex", "assay"),
    ("flex_dilution_series", "dilution", "Dilution series", "flex", "prep"),
    ("idot_dispense", "compound", "Compound dispense", "idot", "both"),
    (("flex_media", "flex_cells"), "seeding", "Media and cell seeding", "flex", "assay"),
    ("centrifuge", "spin", "Spheroid formation spin", "centrifuge", "assay"),
    ("incubate", "incubation", "Incubation", "incubator", "assay"),
    ("flex_reagent", "reagent", "Reagent addition", "flex", "assay"),
    (("shake", "lysis_incubation"), "lysis", "Shake and lysis incubation", "shaker", "assay"),
    ("read_luminescence", "read", "Luminescence read", "reader", "assay"),
]


def _step_events(run, wanted):
    names = (wanted,) if isinstance(wanted, str) else wanted
    evs = [e for e in run["events"] if e["step"] in names]
    return evs


def _refs(refs, assay_id, prep_id, which, key=None):
    """SampleReference needs a role and a purpose; the assay plate role matches the technique definition."""
    if which in ("prep", "both"):
        _sub(refs, "SampleReference", sampleID=prep_id, role="Prep plate",
             samplePurpose="produced" if key == "dilution" else "consumed")
    if which in ("assay", "both"):
        _sub(refs, "SampleReference", sampleID=assay_id, role="Assay plate", samplePurpose="consumed")


def _method(step, device, cats_fn=None, human=None, software=True):
    m = _sub(step, "Method")
    if human:
        a = _sub(m, "Author", userType="human")
        _sub(a, "Name", human)
    else:
        a = _sub(m, "Author", userType="software")
        _sub(a, "Name", "htsorch")
    if device:
        d = _sub(m, "Device")
        _sub(d, "Name", device)
    if software:
        s = _sub(m, "Software")
        _sub(s, "Name", "htsorch")
        _sub(s, "Version", __version__)
    if cats_fn:
        cats_fn(m)
    return m


def _step_params(key, evs, doc):
    a = doc["experiment"]
    protocols = doc["provenance"]["protocol_sha256"]

    def fn(m):
        c = _sub(m, "Category", name="Timing")
        _param(c, "start_utc", "DateTime", min(e["start_utc"] for e in evs))
        _param(c, "duration", "Float64", sum(e["duration_min"] for e in evs), "min")
        c = _sub(m, "Category", name="Settings")
        files = sorted({e["protocol"] for e in evs if e.get("protocol")})
        for f in files:
            _param(c, "protocol", "String", f)
            _param(c, "protocol_sha256", "String", protocols.get(f, ""))
        v = a["volumes"]
        if key == "compound":
            _param(c, "dispense_volume", "Float64", v["compound_nL"], "nL")
            _param(c, "final_dmso", "Float64", a["dmso_percent"], "%")
        elif key == "seeding":
            _param(c, "media_volume", "Float64", v["media_uL"], "uL")
            _param(c, "cell_volume", "Float64", v["cells_uL"], "uL")
        elif key == "reagent":
            _param(c, "reagent", "String", a["reagent"]["name"])
            _param(c, "reagent_volume", "Float64", v["reagent_uL"], "uL")
        elif key == "lysis":
            _param(c, "shake", "Float64", a["reagent"]["shake_min"], "min")
            _param(c, "incubate", "Float64", a["reagent"]["incubate_min"], "min")
        elif key == "incubation":
            inc = doc["timeline"]["incubation"]
            _param(c, "planned", "Float64", inc["planned_h"], "h")
            _param(c, "actual", "Float64", inc["actual_h"], "h")
            _param(c, "within_tolerance", "Boolean", inc["within_tolerance"])
        elif key == "read":
            _param(c, "mode", "String", "luminescence")
    return fn


def build_core(doc, rows, analysis, run, readings, plate_map, tech_sha) -> bytes:
    ident = doc["identifiers"]
    root = etree.Element(_q("AnIML"), nsmap={None: CORE_NS}, version="0.90")

    # --- samples -------------------------------------------------------------
    ss = _sub(root, "SampleSet")
    assay_id, prep_id = ident["assay_plate_barcode"], ident["prep_plate_barcode"]
    s = _sub(ss, "Sample", name="Assay plate", sampleID=assay_id, barcode=assay_id, containerType="384 wells",
             comment="Ultra-low-attachment round-bottom 384-well plate (stand-in labware definition)")
    cat = _sub(s, "Category", name="Plate layout")
    lay = _sub(cat, "SeriesSet", name="Well assignment", length=len(plate_map))
    _series(lay, "Well", "well", "String", [w["well"] for w in plate_map], "independent")
    _series(lay, "Type", "type", "String", [w["type"] for w in plate_map])
    _series(lay, "Compound", "compound", "String", [w["compound_id"] for w in plate_map])
    _series(lay, "Concentration", "conc", "Float64",
            [None if w["type"] == "pbs" else (0 if w["type"] == "neg" else w["final_uM"]) for w in plate_map], unit="uM")
    _series(lay, "Source well", "source", "String", [w["source_well"] for w in plate_map])
    _series(lay, "Edge", "edge", "Boolean", [w["edge"] for w in plate_map])
    p = _sub(ss, "Sample", name="iDOT prep plate", sampleID=prep_id, barcode=prep_id, containerType="384 wells",
             derived="true", comment="Dilution series built on the Flex from master stock aliquots")
    tags = _sub(p, "TagSet")
    _sub(tags, "Tag", name="dilution_factor", value=doc["experiment"]["dilution"]["factor"])
    _sub(tags, "Tag", name="points", value=len(doc["experiment"]["dilution"]["final_uM"]))

    # --- experiment steps ----------------------------------------------------
    es = _sub(root, "ExperimentStepSet")

    st = _sub(es, "ExperimentStep", name="Run provenance", experimentStepID="run-provenance",
              comment="Identity, operator, software and hashes for this run. All times and instrument states are simulated.")
    inf = _sub(st, "Infrastructure")
    refs = _sub(inf, "SampleReferenceSet")
    _refs(refs, assay_id, prep_id, "both")
    _sub(inf, "Timestamp", run["start_utc"])

    def prov(m):
        c = _sub(m, "Category", name="Run")
        _param(c, "run_id", "String", ident["run_id"])
        _param(c, "experiment_id", "String", ident["experiment_id"])
        _param(c, "simulated", "Boolean", True)
        _param(c, "simulation_statement", "String", doc["provenance"]["simulation_statement"])
        _param(c, "spec_sha256", "String", doc["provenance"]["spec_sha256"])
        _param(c, "event_chain_head", "String", run["event_chain_head"])
        _param(c, "edge_policy", "String", doc["experiment"]["edge_policy"])
        c = _sub(m, "Category", name="Protocol hashes")
        for name, h in sorted(doc["provenance"]["protocol_sha256"].items()):
            _param(c, name, "String", h)
        c = _sub(m, "Category", name="Instrument states at read")
        for dev, stt in sorted(run["instrument_states_at_read"].items()):
            d = _sub(c, "Category", name=dev)
            _param(d, "state", "String", stt["state"])
            _param(d, "simulated", "Boolean", stt["simulated"])
        c = _sub(m, "Category", name="Timing windows")
        for k, v in doc["timeline"]["window_summary"]["windows"].items():
            _param(c, k, "Float64", v, "min")
        for k in ("compound_to_cells_max_min", "cell_age_max_min", "reagent_lag_min"):
            _param(c, "observed_" + k, "Float64", doc["timeline"]["window_summary"][k], "min")
    op = doc["provenance"]["operator_id"]
    _method(st, None, prov, human=op)

    for wanted, key, title, device, which in STEPS:
        evs = _step_events(run, wanted)
        if not evs:
            continue
        attrs = {"name": title, "experimentStepID": key}
        note = "; ".join(sorted({e["note"] for e in evs if e.get("note")}))
        if note:
            attrs["comment"] = note
        st = _sub(es, "ExperimentStep", **attrs)
        if key == "read":
            _sub(st, "Technique", name=TECHNIQUE_NAME, uri=TECHNIQUE_REL, sha256=tech_sha)
        inf = _sub(st, "Infrastructure")
        refs = _sub(inf, "SampleReferenceSet")
        _refs(refs, assay_id, prep_id, which, key)
        _sub(inf, "Timestamp", max(e["end_utc"] for e in evs))
        _method(st, device, _step_params(key, evs, doc))
        if key == "read":
            r = _sub(st, "Result", name="Raw luminescence")
            sset = _sub(r, "SeriesSet", name="Plate reading", length=len(plate_map))
            order = [w["well"] for w in plate_map]
            _series(sset, "Well", "well", "String", order, "independent")
            _series(sset, "Luminescence", "rlu", "Float64", [readings[w] for w in order], unit="RLU")

    # --- analysis step -------------------------------------------------------
    st = _sub(es, "ExperimentStep", name="Data analysis", experimentStepID="analysis",
              comment="Normalisation, plate QC and 4-parameter logistic fits on synthetic reader data")
    inf = _sub(st, "Infrastructure")
    refs = _sub(inf, "SampleReferenceSet")
    _refs(refs, assay_id, prep_id, "assay")
    _sub(inf, "Timestamp", run["end_utc"])

    def meth(m):
        c = _sub(m, "Category", name="Analysis method")
        _param(c, "model", "String", "4-parameter logistic, least squares")
        _param(c, "normalisation", "String", "(raw - mean kill) / (mean solvent control - mean kill) x 100")
        _param(c, "ic50_withheld_below_effect", "Float64", 30, "%")
    _method(st, None, meth)

    r = _sub(st, "Result", name="Normalized data")
    sset = _sub(r, "SeriesSet", name="Per-well values", length=len(rows))
    _series(sset, "Well", "well", "String", [x["well"] for x in rows], "independent")
    _series(sset, "Normalized response", "pct", "Float64", [x["normalized_pct"] for x in rows], unit="%")
    _series(sset, "Cell age", "cell_age", "Float64", [x["cell_age_min"] for x in rows], unit="min")
    _series(sset, "Compound to cells", "c2c", "Float64", [x["compound_to_cells_min"] for x in rows], unit="min")
    _series(sset, "Reagent offset", "reag", "Float64", [x["reagent_offset_min"] for x in rows], unit="min")
    _series(sset, "Flags", "flags", "String", [x["flags"] for x in rows])

    fits = analysis["fits"]
    r = _sub(st, "Result", name="Curve fits")
    sset = _sub(r, "SeriesSet", name="Per-compound fits", length=len(fits))
    _series(sset, "Compound", "compound", "String", [f["compound_id"] for f in fits], "independent")
    _series(sset, "Status", "status", "String", [f["status"] for f in fits])
    _series(sset, "IC50", "ic50", "Float64", [f["ic50_uM"] for f in fits], unit="uM")
    _series(sset, "Hill slope", "hill", "Float64", [f["hill"] for f in fits], unit="1")
    _series(sset, "R squared", "r2", "Float64", [f["r2"] for f in fits], unit="1")
    _series(sset, "Effect at top concentration", "top", "Float64", [f["pct_at_top_conc"] for f in fits], unit="%")

    r = _sub(st, "Result", name="Plate QC")
    c = _sub(r, "Category", name="Plate QC")
    q = analysis["qc"]
    for k, unit in (("z_prime", "1"), ("signal_to_background", "1"), ("neg_cv_pct", "%"), ("pos_cv_pct", "%"),
                    ("neg_mean_rlu", "RLU"), ("pos_mean_rlu", "RLU")):
        _param(c, k, "Float64", q[k], unit)
    _param(c, "z_prime_pass", "Boolean", q["z_prime_pass"])

    r = _sub(st, "Result", name="Traceability flags")
    for fl in doc["flags"]:
        c = _sub(r, "Category", name=fl["code"])
        _param(c, "level", "String", fl["level"])
        _param(c, "phase", "String", fl["phase"])
        _param(c, "message", "String", fl["message"])
        _param(c, "interpretation", "String", fl.get("interpretation", ""))
        _param(c, "responsible", "String", fl.get("responsible", ""))
        aw = fl["affected_wells"]
        _param(c, "affected_wells", "String", "all" if aw == "all" else ",".join(aw))

    # --- audit trail -----------------------------------------------------------
    at = _sub(root, "AuditTrailEntrySet")
    e = _sub(at, "AuditTrailEntry")
    _sub(e, "Timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    a = _sub(e, "Author", userType="software")
    _sub(a, "Name", "htsorch")
    sw = _sub(e, "Software")
    _sub(sw, "Name", "htsorch")
    _sub(sw, "Version", __version__)
    _sub(e, "Action", "created")
    _sub(e, "Reason", "Export of the FAIR data package to AnIML")
    _sub(e, "Comment", f"run_id {ident['run_id']}; event chain head {run['event_chain_head']}; source datapackage.json")
    return _tostring(root)


# ---- validation --------------------------------------------------------------------

class _LocalDsig(etree.Resolver):
    def __init__(self, path):
        super().__init__()
        self.path = str(path)

    def resolve(self, url, pubid, context):
        if "xmldsig-core-schema.xsd" in url:
            return self.resolve_filename(self.path, context)
        return None


@lru_cache(maxsize=4)
def _load_schemas(schema_dir: str):
    d = Path(schema_dir)
    parser = etree.XMLParser(load_dtd=False, no_network=True)
    parser.resolvers.add(_LocalDsig(d / "xmldsig-core-schema.xsd"))
    core = etree.XMLSchema(etree.parse(str(d / "animl-core.xsd"), parser))
    tech = etree.XMLSchema(etree.parse(str(d / "animl-technique.xsd"), parser))
    info = {
        "source": SCHEMA_SOURCE,
        "core_namespace": etree.parse(str(d / "animl-core.xsd")).getroot().get("targetNamespace"),
        "core_version": etree.parse(str(d / "animl-core.xsd")).getroot().get("version"),
        "core_xsd_sha256": _sha((d / "animl-core.xsd").read_bytes()),
        "technique_xsd_sha256": _sha((d / "animl-technique.xsd").read_bytes()),
    }
    return core, tech, info


def validate_documents(core_bytes: bytes, tech_bytes: bytes, schema_dir=None) -> dict:
    d = Path(schema_dir) if schema_dir else default_schema_dir()
    core, tech, info = _load_schemas(str(d.resolve()))
    errors = []
    for label, schema, data in (("core document", core, core_bytes), ("technique definition", tech, tech_bytes)):
        doc = etree.fromstring(data)
        if not schema.validate(doc):
            errors += [f"{label}: line {e.line}: {e.message}" for e in schema.error_log][:10]
    errors += check_conformance(core_bytes, tech_bytes)
    return {"valid": not errors, "errors": errors, **info}


_FAMILY = {"Int32": "Int", "Int64": "Int", "Float32": "Float", "Float64": "Float"}


def _type_matches(doc_type: str, blueprint_type: str) -> bool:
    """The technique schema names types Int/Float/Numeric; documents use Int32/Float64 and so on."""
    fam = _FAMILY.get(doc_type, doc_type)
    return fam == blueprint_type or (blueprint_type == "Numeric" and fam in ("Int", "Float"))


def check_conformance(core_bytes: bytes, tech_bytes: bytes) -> list[str]:
    """Project-level check that a document follows its technique definition. Not an AnIML validator."""
    core, tech = etree.fromstring(core_bytes), etree.fromstring(tech_bytes)
    nc, nt = {"a": CORE_NS}, {"t": TECH_NS}
    problems = []
    for step in core.iterfind(".//a:ExperimentStep", nc):
        tq = step.find("a:Technique", nc)
        if tq is None:
            continue
        sid = step.get("experimentStepID")
        if tq.get("sha256") != _sha(tech_bytes):
            problems.append(f"step {sid}: technique sha256 does not match the definition file")
        if tq.get("name") != tech.get("name"):
            problems.append(f"step {sid}: technique name differs from the definition")
        for rb in tech.iterfind("t:ResultBlueprint", nt):
            res = [r for r in step.iterfind("a:Result", nc) if r.get("name") == rb.get("name")]
            if not res:
                if rb.get("modality", "required") == "required":
                    problems.append(f"step {sid}: missing required Result '{rb.get('name')}'")
                continue
            for ssb in rb.iterfind("t:SeriesSetBlueprint", nt):
                sets = [s for s in res[0].iterfind("a:SeriesSet", nc) if s.get("name") == ssb.get("name")]
                if not sets:
                    problems.append(f"step {sid}: missing SeriesSet '{ssb.get('name')}'")
                    continue
                have = {s.get("name"): s for s in sets[0].iterfind("a:Series", nc)}
                for sb in ssb.iter(_q("SeriesBlueprint", TECH_NS)):
                    s = have.get(sb.get("name"))
                    if s is None:
                        problems.append(f"step {sid}: missing Series '{sb.get('name')}'")
                        continue
                    if not _type_matches(s.get("seriesType"), sb.get("seriesType")):
                        problems.append(f"step {sid}: Series '{sb.get('name')}' seriesType is "
                                        f"{s.get('seriesType')}, definition says {sb.get('seriesType')}")
                    if s.get("dependency") != sb.get("dependency"):
                        problems.append(f"step {sid}: Series '{sb.get('name')}' dependency is "
                                        f"{s.get('dependency')}, definition says {sb.get('dependency')}")
                    if len(s.find("a:IndividualValueSet", nc)) != int(sets[0].get("length")):
                        problems.append(f"step {sid}: Series '{sb.get('name')}' length differs from SeriesSet length")
    return problems


def export(doc, rows, analysis, run, readings, plate_map, mode="auto", schema_dir=None):
    """Build and validate. Returns (files, info). files maps package-relative path to bytes.

    mode: 'off' skip, 'auto' export when schemas are present (skip with a reason otherwise),
    'require' fail if the schemas are missing. An invalid document always raises."""
    if mode == "off":
        return {}, {"exported": False, "reason": "disabled"}
    d = Path(schema_dir) if schema_dir else default_schema_dir()
    if not schemas_available(d):
        if mode == "require":
            raise FileNotFoundError(f"AnIML schemas not found in {d}. Run: python scripts/fetch_animl_schemas.py")
        return {}, {"exported": False, "reason": f"AnIML schemas not found in {d}; run scripts/fetch_animl_schemas.py"}
    tech = build_technique()
    core = build_core(doc, rows, analysis, run, readings, plate_map, _sha(tech))
    res = validate_documents(core, tech, d)
    if not res["valid"]:
        raise ValueError("AnIML export failed validation:\n  " + "\n  ".join(res["errors"][:10]))
    info = {"exported": True, "validated": True, "validated_against": {k: res[k] for k in
            ("source", "core_namespace", "core_version", "core_xsd_sha256", "technique_xsd_sha256")},
            "documents": {"core": DOC_REL, "technique_definition": TECHNIQUE_REL},
            "scope_note": "Core document and project-specific technique definition validated against the AnIML "
                          "0.90 draft-namespace XSDs plus a project-level technique conformance check. "
                          "Not the official AnIML validator; not digitally signed."}
    return {f"animl/{DOC_REL}": core, f"animl/{TECHNIQUE_REL}": tech}, info
