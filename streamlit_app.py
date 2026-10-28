"""HTS Orchestrator: a browser interface for the spec-driven, human-in-the-loop screening orchestrator.

Run locally:      streamlit run streamlit_app.py
Free hosting:     Streamlit Community Cloud (main file: streamlit_app.py)

Everything here calls the real orchestrator code in src/htsorch. All devices are simulated.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import zipfile
from argparse import Namespace
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))      # repo layout; the in-browser build has htsorch/ beside this file
sys.path.insert(0, str(ROOT))

from htsorch.cli import _package                                    # noqa: E402
from htsorch.engine import EngineError                              # noqa: E402
from htsorch.platemap import build_plate_map                        # noqa: E402
from htsorch.runner import build_context, build_orchestrator        # noqa: E402
from htsorch.scenario import load_scenario, parse_scenario                          # noqa: E402
from htsorch.spec import ROWS, derive, load_spec                    # noqa: E402
from htsorch.validator import has_errors, validate                  # noqa: E402

# Streamlit renamed use_container_width to width="stretch" in 1.50; support both (the in-browser build runs 1.45).
STRETCH = {"width": "stretch"} if tuple(int(x) for x in st.__version__.split(".")[:2]) >= (1, 50) else {"use_container_width": True}

GREEN, INK, DIM, BG = "#2d6a4f", "#0a140f", "#354a3e", "#fdfcf8"
TYPE_COLORS = {"test": GREEN, "neg": "#8d99ae", "pos": "#bc4749", "pbs": "#e4e9e5", "empty": "#f1f3f1"}
TYPE_LABELS = {"test": "Compound", "neg": "Negative control (solvent)", "pos": "Positive control (kill)",
               "pbs": "PBS fill"}

st.set_page_config(page_title="HTS Orchestrator | Lagan Pathania", page_icon="🧪", layout="wide")

st.markdown(f"""
<style>
  .block-container {{ padding-top: 1.2rem; max-width: 1180px; }}
  header[data-testid="stHeader"] {{ background: transparent; }}
  .lp-bar {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:.6rem;
             border-bottom:1px solid #dfe6e1; padding:.2rem 0 .9rem; margin-bottom:1.1rem; }}
  .lp-name {{ font-weight:800; letter-spacing:.12em; text-transform:uppercase; color:{GREEN}; font-size:1.15rem; }}
  .lp-role {{ font-size:.68rem; letter-spacing:.2em; text-transform:uppercase; font-weight:600; color:{DIM}; }}
  .lp-tag {{ font-size:.8rem; color:{DIM}; }}
  h1.prod {{ color:{GREEN}; font-size:2rem; margin:.1rem 0 .1rem; letter-spacing:-.01em; font-weight:700; }}
  .sub {{ color:{DIM}; margin:0 0 1rem; font-size:1.02rem; }}
  .pill {{ display:inline-block; padding:.12rem .6rem; border-radius:99px; font-size:.78rem; font-weight:700;
           letter-spacing:.04em; text-transform:uppercase; }}
  .pill.ok {{ background:#d8efe1; color:#1b4332; }} .pill.warn {{ background:#fff0c9; color:#7a5a00; }}
  .pill.err {{ background:#f8d9da; color:#8c1c1f; }} .pill.info {{ background:#e3ebe6; color:{DIM}; }}
  .finding {{ border-left:4px solid #c8d4cc; padding:.35rem .8rem; margin:.3rem 0; background:#fff; border-radius:0 8px 8px 0; }}
  .finding.ERROR {{ border-color:#bc4749; }} .finding.WARN {{ border-color:#e9b44c; }} .finding.INFO {{ border-color:{GREEN}; }}
  .finding small {{ color:{DIM}; display:block; }}
  div[data-testid="stMetric"] {{ background:#fff; border:1px solid #dfe6e1; border-radius:12px; padding:.7rem .9rem; }}
  .stTabs [data-baseweb="tab"] {{ font-weight:600; }}
  .desk {{ border:2px solid #e9b44c; background:#fffaf0; border-radius:14px; padding:1rem 1.2rem; margin:.8rem 0; }}
  .note {{ color:{DIM}; font-size:.9rem; }}
</style>
<div class="lp-bar">
  <div><div class="lp-name">Lagan Pathania</div><div class="lp-role">Lab Automation Architect</div></div>
  <div class="lp-tag">Simulated devices · no hardware · practice for qualification-ready work, not GxP validated</div>
</div>
<h1 class="prod">HTS Orchestrator</h1>
<p class="sub">One spec drives the whole screen: validation, iDOT worklist, Opentrons Flex protocols, a 72 h spheroid assay
with the operator in the loop, and a FAIR data package.</p>
""", unsafe_allow_html=True)


# ============================================================================== helpers
def spec_files() -> dict[str, Path]:
    return {p.stem: p for p in sorted((ROOT / "specs").glob("*.yaml"))}


def scenario_files() -> dict[str, Path]:
    return {p.stem: p for p in sorted((ROOT / "scenarios").glob("*.yaml"))}


def workdir() -> Path:
    if "work" not in st.session_state:
        st.session_state.work = Path(tempfile.mkdtemp(prefix="hts_"))
    return st.session_state.work


def edited_spec(base_text: str, ov: dict) -> tuple[dict, Path]:
    raw = yaml.safe_load(base_text)
    raw["dilution"].update({k: ov[k] for k in ("top_final_uM", "factor", "points", "replicates")})
    raw["experiment"]["random_seed"] = int(ov["seed"])
    raw["assay"]["compound_dispense"]["top_nl"] = ov["top_nl"]
    raw["assay"]["limits"]["dmso_max_percent"] = ov["dmso_max"]
    path = workdir() / "spec_edited.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_spec(path), path


def reset_run():
    for k in ("o", "ctx", "pkg", "log", "mode", "sc_name"):
        st.session_state.pop(k, None)


def pill(level: str) -> str:
    cls = {"ERROR": "err", "WARN": "warn", "INFO": "info"}[level]
    return f'<span class="pill {cls}">{level}</span>'


def plate_chart(df: pd.DataFrame, color, title: str, height=330):
    base = alt.Chart(df).mark_rect(stroke="white", strokeWidth=1).encode(
        x=alt.X("col:O", title=None, axis=alt.Axis(labelAngle=0, labelFontSize=9, labelOverlap=True)),
        y=alt.Y("row_l:O", title=None, sort=list(ROWS[:16]), axis=alt.Axis(labelFontSize=9, labelOverlap=False)),
        color=color,
        tooltip=[c for c in ("well", "type", "compound_id", "final_uM", "dispense_nl", "normalized_pct") if c in df.columns])
    return base.properties(height=height, title=title).configure_view(strokeWidth=0)


def run_step_table(o) -> pd.DataFrame:
    rows = []
    t0 = None
    for e in o.events:
        if e["step"] in ("time_passed",):
            continue
        if t0 is None:
            t0 = pd.Timestamp(e["start_utc"])
        rows.append({"Step": e["step"].replace("_", " "), "By": ("Operator · " + str(e.get("actor"))) if e.get("human") else e.get("device") or e.get("actor") or "",
                     "Start (h)": round((pd.Timestamp(e["start_utc"]) - t0).total_seconds() / 3600, 2),
                     "Minutes": round(e.get("duration_min") or 0, 1),
                     "Note": str(e.get("reason") or e.get("note") or e.get("hint") or e.get("decision") or "")})
    return pd.DataFrame(rows)


def finish_run(o, ctx, sc):
    """Let the 72 h incubation pass, then write the data package."""
    status = o.status
    if status == "waiting":
        status = o.resume(until_wake=True, wake_delay_h=sc["wake_delay_h"])
    if status == "complete" and "pkg" not in st.session_state:
        out = workdir() / "package_out"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        buf = io.StringIO()
        args = Namespace(out=str(out), animl="off", animl_schema_dir=None)
        with contextlib.redirect_stdout(buf):
            _package(args, ctx["spec"], ctx["d"], ctx["pm"], ctx["findings"], ctx["manifest"], ctx["build_dir"],
                     o.run_record(), st.session_state.seed_used)
        st.session_state.pkg = next(out.glob("package-*"))
        st.session_state.log = buf.getvalue()
    return status


def zip_dir(p: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(p.parent))
    return buf.getvalue()


def hill(x, bottom, top, ic50, h):
    return bottom + (top - bottom) / (1 + (x / ic50) ** h)


# ============================================================================== sidebar: the spec
with st.sidebar:
    st.markdown("### Screen design")
    specs = spec_files()
    name = st.selectbox("Spec", list(specs), index=list(specs).index("ctg3d_screen") if "ctg3d_screen" in specs else 0,
                        help="The YAML file that drives everything. The shipped examples include ones built to fail validation.")
    up = st.file_uploader("…or upload your own spec (YAML)", type=["yaml", "yml"])
    base_text = up.getvalue().decode("utf-8") if up else specs[name].read_text(encoding="utf-8")
    try:
        base = yaml.safe_load(base_text)
        dil, cd = base["dilution"], base["assay"]["compound_dispense"]
        lim = base["assay"]["limits"]
    except Exception as e:                                           # noqa: BLE001
        st.error(f"This spec could not be read: {e}")
        st.stop()
    tweak = st.toggle("Adjust parameters", value=False, help="Edit a few numbers without touching the YAML.")
    ov = {"top_final_uM": dil["top_final_uM"], "factor": dil["factor"], "points": dil["points"],
          "replicates": dil["replicates"], "seed": base["experiment"].get("random_seed", 1),
          "top_nl": cd["top_nl"], "dmso_max": lim["dmso_max_percent"]}
    if tweak:
        ov["top_final_uM"] = st.number_input("Top concentration (µM)", 0.01, 1000.0, float(ov["top_final_uM"]))
        ov["factor"] = st.number_input("Dilution factor", 1.5, 10.0, float(ov["factor"]), 0.5)
        ov["points"] = st.slider("Concentrations per compound", 3, 8, int(ov["points"]))
        ov["replicates"] = st.slider("Replicates", 1, 6, int(ov["replicates"]))
        ov["top_nl"] = st.number_input("iDOT volume at top concentration (nL)", 5.0, 200.0, float(ov["top_nl"]), 5.0)
        ov["dmso_max"] = st.number_input("DMSO limit (%)", 0.01, 1.0, float(ov["dmso_max"]), 0.01, format="%.2f")
        ov["seed"] = st.number_input("Randomisation seed", 0, 99999999, int(ov["seed"]))
    st.caption("Everything below is computed from this one spec.")

try:
    spec, spec_path = edited_spec(base_text, ov)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    findings = validate(spec, d, pm)
except Exception as e:                                               # noqa: BLE001
    st.error(f"The spec could not be processed: {type(e).__name__}: {e}")
    st.stop()
blocked = has_errors(findings)

tab_about, tab_design, tab_run, tab_results = st.tabs(["About", "1 · Design and check", "2 · Run with operator", "3 · Results"])

# ============================================================================== tab 1: design
with tab_design:
    n_err = sum(f.level == "ERROR" for f in findings)
    n_warn = sum(f.level == "WARN" for f in findings)
    c = st.columns(5)
    c[0].metric("Final DMSO", f"{d['dmso_percent']:.3f} %", help="Must stay below the limit in the spec")
    c[1].metric("iDOT volume", (f"{d['compound_nl_min']:g} nL" if d['compound_nl_min'] == d['compound_nl_max'] else f"{d['compound_nl_min']:g}–{d['compound_nl_max']:g} nL"))
    c[2].metric("Compounds × points", f"{d['n_compounds']} × {len(d['final_uM'])}")
    c[3].metric("Test wells", d["n_test_wells"])
    c[4].metric("Validation", "Blocked" if blocked else "Passed", f"{n_err} errors · {n_warn} warnings",
                delta_color="inverse" if blocked else "off")
    if blocked:
        st.error("The spec has errors, so the build is blocked and no files are generated. This is by design: "
                 "fix the number the finding names (change it in the sidebar) and the check clears.")

    left, right = st.columns([1, 1.15], gap="large")
    with left:
        st.subheader("Checks")
        for f in findings:
            st.markdown(f'<div class="finding {f.level}">{pill(f.level)} <b>{f.code}</b> {f.what}'
                        f'<small>{(f.system_action + " · " + f.who) if f.level != "INFO" else ""}</small></div>', unsafe_allow_html=True)
        st.subheader("Dose series")
        dose = pd.DataFrame({"Point": range(1, len(d["final_uM"]) + 1),
                             "Final (µM)": [round(x, 4) for x in d["final_uM"]],
                             "Stock (µM)": [round(x, 1) for x in d["stock_uM"]],
                             "iDOT (nL)": d["nl_by_point"]})
        st.dataframe(dose, hide_index=True, **STRETCH)
        st.caption(f"Volume per well = final concentration × assay volume ({d['assay_volume_ul']:g} µL) ÷ stock. "
                   "It is never below the iDOT minimum and its DMSO share stays under the limit.")
    with right:
        st.subheader("Randomised plate map")
        df = pd.DataFrame(pm).astype({"conc_index": str})
        df["col"] = df["col"] + 1
        df["row_l"] = [ROWS[r] for r in df["row"]]
        df["type"] = df["type"].map(lambda t: TYPE_LABELS.get(t, t))
        colors = alt.Color("type:N", legend=alt.Legend(title=None, orient="bottom", columns=2),
                           scale=alt.Scale(domain=list(TYPE_LABELS.values()),
                                           range=[TYPE_COLORS[k] for k in TYPE_LABELS]))
        st.altair_chart(plate_chart(df, colors, f"{spec['experiment']['id']} · 384-well plate, seed {spec['experiment']['random_seed']}"),
                        **STRETCH)
        st.caption("Stratified and seeded, so the same spec always gives the same layout.")
        with st.expander("Worklist and protocols that this spec builds"):
            from htsorch.generators import idot
            st.code("\n".join(idot.generate_worklist(spec, pm).splitlines()[:14]) + "\n…", language="text")
            st.caption("First lines of the iDOT worklist, which is grouped by source well. The three Flex protocols are generated "
                       "from the same spec. Download them from the Results tab after a run.")

# ============================================================================== tab 2: run
with tab_run:
    scs = scenario_files()
    labels = {"(clean run)": None, **{k: v for k, v in scs.items()}}
    a, b = st.columns([1.2, 1], gap="large")
    with a:
        pick = st.selectbox("What goes wrong during the run?", list(labels),
                            help="Scenarios inject device faults, missing stock or a late hand-off. The orchestrator responds by its recovery policy.")
        sc = load_scenario(labels[pick]) if labels[pick] else parse_scenario({"description": "Nothing goes wrong: the happy path."})
        st.caption(sc["description"] or "")
    with b:
        who = st.radio("Who answers when it needs a person?",
                       ["Simulated operator (follows the scenario)", "I will, at the operator desk"],
                       help="With the second option, any fault pauses the run and waits for your decision.")
        seed_used = st.number_input("Readout seed", 0, 99999, 7, help="Seeds the simulated plate reader (cell kill and noise).")
    go = st.button("▶  Start the run", type="primary", disabled=blocked,
                   help="Blocked while the spec has errors" if blocked else None)
    if go:
        reset_run()
        st.session_state.seed_used = int(seed_used)
        if who.startswith("I will"):
            sc["operator"].available = False
        try:
            with st.spinner("Building protocols and starting the workflow…"):
                ctx = build_context(spec_path, workdir() / "ctx")
                o = build_orchestrator(ctx["spec"], sc, operator_id="SIMULATED-OPERATOR", start=None,
                                       protocols=ctx["protocols"], state_path=workdir() / "state.json",
                                       meta={"seed": int(seed_used)})
                o.run()
                st.session_state.update(o=o, ctx=ctx, sc=sc, mode="me" if who.startswith("I will") else "sim", sc_name=pick)
                finish_run(o, ctx, sc)
        except EngineError as e:
            st.error(f"Engine: {e}")

    o = st.session_state.get("o")
    if o is None:
        st.info("Pick a scenario and start the run. The orchestrator walks the workflow on simulated devices and "
                "records every step in a hash-chained log.")
    else:
        sc = st.session_state.sc
        ctx = st.session_state.ctx
        tone = {"complete": "ok", "paused": "warn", "aborted": "err", "waiting": "info"}[o.status]
        st.markdown(f'Run `{o.run_id[:8]}` · scenario **{st.session_state.sc_name}** · '
                    f'<span class="pill {tone}">{o.status}</span> · simulated clock {o.clock.now():%d %b %H:%M} UTC',
                    unsafe_allow_html=True)

        if o.status == "paused":
            p = o.pending
            what = (p["fault"]["code"] + ": " + p["fault"]["message"]) if p["kind"] == "fault" else p["code"] + ": " + p["reason"]
            st.markdown('<div class="desk"><b>Operator desk</b>: the run is paused and needs a person.</div>', unsafe_allow_html=True)
            st.warning(what)
            st.write(p["reason"])
            hint = (p.get("fault") or {}).get("hint") if p["kind"] == "fault" else None
            if hint:
                st.caption("Hint: " + hint)
            c1, c2 = st.columns(2)
            actor = c1.text_input("Your name", "A.Operator")
            default = "retry" if p["kind"] == "fault" else "continue"
            opts = ["retry", "abort"] if p["kind"] == "fault" else ["continue", "abort"]
            decision = c2.radio("Decision", opts, horizontal=True, index=opts.index(default))
            note = st.text_input("Note for the audit trail (optional)")
            delay = st.slider("Minutes until you act", 0, 240, 15,
                              help="Time passes on the simulated clock. Long waits can breach a timing window.")
            if st.button("Submit decision", type="primary"):
                try:
                    o.resume(advance_min=float(delay), response={"actor": actor or "A.Operator", "decision": decision, "note": note})
                    finish_run(o, ctx, sc)
                except EngineError as e:
                    st.error(str(e))
                st.rerun()
        elif o.status == "aborted":
            last = [e for e in o.events if e["step"] == "aborted"][-1]
            st.error(f"Aborted: {last.get('reason')}. Plates are quarantined and no data package is written. "
                     "That is the correct, safe outcome.")
        elif o.status == "complete":
            st.success("The run is complete. The 72 h incubation was simulated, the plate was read and the data package was written. "
                       "Open the Results tab.")

        c = st.columns(4)
        c[0].metric("Steps logged", len(o.events))
        c[1].metric("Faults", sum(e["step"] == "fault" for e in o.events))
        c[2].metric("Operator actions", len(o.interventions))
        c[3].metric("Plate flags", sum(len(pl.flags) for pl in o.cust.plates.values()))
        with st.expander("Run timeline", expanded=True):
            st.dataframe(run_step_table(o), hide_index=True, **STRETCH, height=360)
        with st.expander("Plate custody (the flags stay with the plate for good)"):
            rows = [{"Plate": pid, "Flag": f["code"], "Meaning": f.get("what", f.get("reason", ""))}
                    for pid, pl in o.cust.plates.items() for f in pl.flags]
            st.dataframe(pd.DataFrame(rows) if rows else pd.DataFrame({"Plate": [], "Flag": [], "Meaning": []}),
                         hide_index=True, **STRETCH)
            if not rows:
                st.caption("No flags: custody was never in doubt.")

# ============================================================================== tab 3: results
with tab_results:
    pkg = st.session_state.get("pkg")
    if not pkg:
        st.info("Complete a run on the previous tab to see results here.")
    else:
        wells = pd.read_csv(pkg / "wells.csv")
        fits = pd.read_csv(pkg / "analysis" / "curve_fits.csv")
        dp = json.loads((pkg / "datapackage.json").read_text(encoding="utf-8"))
        log = st.session_state.get("log", "")
        qc_line = [ln for ln in log.splitlines() if "Z'" in ln]
        qc = qc_line[0].strip() if qc_line else ""
        z = qc.split("Z' ")[1].split(",")[0] if qc else "?"
        sb = qc.split("S/B ")[1].split(",")[0] if qc else "?"
        active = int((fits["status"] == "active").sum())
        c = st.columns(4)
        c[0].metric("Z′", z, help="Plate quality. 0.5 or more is a robust screen.")
        c[1].metric("Signal / background", sb)
        c[2].metric("Active compounds", f"{active} of {len(fits)}")
        c[3].metric("Package check", "OK" if "verification: OK" in log else "see log")

        l, r = st.columns([1, 1.1], gap="large")
        with l:
            st.subheader("Dose response")
            cid = st.selectbox("Compound", fits["compound_id"], index=int(fits.index[fits["status"] == "active"][0]) if active else 0)
            f = fits[fits["compound_id"] == cid].iloc[0]
            pts = wells[(wells["compound_id"] == cid) & (wells["type"] == "test")]
            pts = pts.assign(conc=pts["concentration_uM"].astype(float))
            sc_pts = alt.Chart(pts).mark_circle(size=70, color=GREEN, opacity=.75).encode(
                x=alt.X("conc:Q", scale=alt.Scale(type="log"), title="Concentration (µM)"),
                y=alt.Y("normalized_pct:Q", title="Viability (% of solvent control)", scale=alt.Scale(domain=[0, 130])),
                tooltip=["well", "conc", "normalized_pct"])
            layers = [sc_pts]
            if f["status"] == "active" and not np.isnan(f["ic50_uM"]):
                xs = np.logspace(np.log10(pts["conc"].min()), np.log10(pts["conc"].max()), 80)
                curve = pd.DataFrame({"conc": xs, "y": hill(xs, f["bottom_pct"], f["top_pct"], f["ic50_uM"], f["hill"])})
                layers.append(alt.Chart(curve).mark_line(color=INK).encode(x="conc:Q", y="y:Q"))
                layers.append(alt.Chart(pd.DataFrame({"x": [f["ic50_uM"]]})).mark_rule(color="#bc4749", strokeDash=[4, 4]).encode(x="x:Q"))
                st.caption(f"IC50 {f['ic50_uM']:.2f} µM · Hill {f['hill']:.2f} · R² {f['r2']:.2f}")
            else:
                st.caption(str(f["note"]) if isinstance(f["note"], str) else "No reliable fit.")
            st.altair_chart(alt.layer(*layers).properties(height=300), **STRETCH)
        with r:
            st.subheader("Plate readout")
            w = wells.copy()
            w["row_l"], w["col"] = w["plate_row"].map(lambda i: ROWS[int(i)] if str(i).isdigit() else str(i)), w["plate_col"] + 1
            w = w[w["normalized_pct"].notna()]
            heat = alt.Color("normalized_pct:Q", title="% of solvent control",
                             scale=alt.Scale(scheme="redyellowgreen", domain=[0, 110]))
            if w["plate_row"].dtype == object:
                w["row_l"] = w["plate_row"]
            st.altair_chart(plate_chart(w, heat, "Normalised signal per well"), **STRETCH)
            st.caption("Dark red wells are dead spheroids: the positive controls and the active compounds at high dose.")

        st.subheader("Compound summary")
        show = fits[["compound_id", "status", "ic50_uM", "hill", "pct_at_top_conc", "r2"]].copy()
        show.columns = ["Compound", "Status", "IC50 (µM)", "Hill", "Viability at top dose (%)", "R²"]
        st.dataframe(show.round(3), hide_index=True, **STRETCH)

        st.subheader("Take it with you")
        d1, d2, d3 = st.columns(3)
        d1.download_button("Data package (.zip)", zip_dir(pkg), file_name=f"{pkg.name}.zip", mime="application/zip",
                           help="FAIR package: wells, fits, audit log, inputs and checksums.")
        d2.download_button("wells.csv", (pkg / "wells.csv").read_bytes(), file_name="wells.csv", mime="text/csv")
        d3.download_button("Custody log (JSON)", (pkg / "audit" / "custody_log.json").read_bytes(),
                           file_name="custody_log.json", mime="application/json")
        with st.expander("Run record"):
            st.code(log.strip(), language="text")
            st.json(dp, expanded=False)

# ============================================================================== about
with tab_about:
    st.subheader("What this is")
    st.markdown("""
A **spec-driven orchestrator** for a three-day 3D screening workflow: iDOT compound dispensing, Opentrons Flex liquid
handling, robot-arm transfers, a 72 h spheroid incubation and a CellTiter-Glo 3D readout. One YAML spec is validated, then
turned into the worklist, the protocols and a plate map. A workflow engine runs it on simulated devices, with the
**operator in the loop** where judgement is needed. Every step lands in a hash-chained log, and the result is a FAIR data package.
""")
    st.markdown("**How to read this page**")
    st.markdown("""
1. **Design and check.** Change the numbers and watch the validator respond. DMSO has to stay below 0.1 %, and the iDOT volume
   per well is derived from the concentration series and never drops below the instrument minimum.
2. **Run with operator.** Pick something that goes wrong (a lost plate, low tips, nobody at the bench) and either let the simulated
   operator follow the scenario or take the operator desk yourself.
3. **Results.** QC, dose-response curves, plate readout and the downloadable package.
""")
    st.markdown("**Honest limits**")
    st.markdown("""
- Every device is simulated. No instrument was driven and no vendor driver was used.
- The iDOT worklist is our own representation, not verified against the vendor import format.
- Opentrons protocols are checked only with the vendor simulator, when it is installed (not in the hosted app).
- Limits marked VERIFY in the spec are placeholders. This is practice for qualification-ready work, not GxP validated.
""")
    st.caption("Source: github.com/laganpathania (add your repository link here). Built by Lagan Pathania.")
