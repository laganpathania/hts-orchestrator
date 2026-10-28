"""Command line: validate a spec, or validate and build all artifacts.

    python -m htsorch validate specs/ctg3d_screen.yaml
    python -m htsorch build specs/ctg3d_screen.yaml --out out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .generators import idot, opentrons
from .platemap import build_plate_map
from .spec import derive, load_spec
from .validator import has_errors, to_dicts, validate


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _simulate(path: Path) -> dict:
    exe = shutil.which("opentrons_simulate")
    if not exe:
        return {"status": "skipped", "detail": "opentrons_simulate not installed"}
    try:
        r = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": "simulation timed out"}
    ok = r.returncode == 0
    return {"status": "pass" if ok else "fail",
            "detail": "" if ok else (r.stderr or r.stdout)[-600:],
            "lines": len(r.stdout.splitlines())}


def cmd_validate(spec_path: str) -> int:
    spec = load_spec(spec_path)
    findings = validate(spec)
    for f in findings:
        print(f.line())
    errs = sum(f.level == "ERROR" for f in findings)
    warns = sum(f.level == "WARN" for f in findings)
    print(f"\n{errs} error(s), {warns} warning(s)")
    return 1 if errs else 0


def cmd_build(spec_path: str, out_dir: str, simulate: bool = True) -> int:
    spec = load_spec(spec_path)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    findings = validate(spec, d, pm)
    for f in findings:
        if f.level != "INFO":
            print(f.line())
    if has_errors(findings):
        print("\nBuild blocked by validation errors. Nothing was written.")
        return 1

    out = Path(out_dir) / spec["experiment"]["id"]
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "plate_map.csv": idot.plate_map_csv(pm),
        "idot_worklist.csv": idot.generate_worklist(spec, pm),
        "flex_1_dilution.py": opentrons.generate_dilution(spec, d),
        "flex_2_seeding.py": opentrons.generate_seeding(spec, d, pm),
        "flex_3_readout.py": opentrons.generate_readout(spec, d),
    }
    if d["edge_policy"] == "pbs_fill":
        files["flex_0_edge_fill.py"] = opentrons.generate_edge_fill(spec, d, pm)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")

    sims = {}
    if simulate:
        for name in files:
            if name.startswith("flex_"):
                sims[name] = _simulate(out / name)

    manifest = {
        "experiment_id": spec["experiment"]["id"],
        "spec_sha256": spec["_meta"]["sha256"],
        "derived": d,
        "validation": to_dicts(findings),
        "files": {n: _sha(t) for n, t in files.items()},
        "opentrons_simulate": sims,
        "boundaries": [
            "iDOT worklist is our own representation, not verified against the vendor import format",
            "Opentrons protocols are checked with the vendor simulator only; no hardware run",
            "Devices are simulated (state, inventories, faults); no instrument was driven and no vendor driver was used",
            "Limits marked VERIFY in the spec are placeholders",
            "Practice for qualification-ready work, not GxP validated",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    warns = sum(f.level == "WARN" for f in findings)
    print(f"\nBuilt {len(files)} files + manifest in {out} ({warns} warning(s))")
    for n, s in sims.items():
        print(f"  simulate {n}: {s['status']}")
    return 1 if any(s["status"] == "fail" for s in sims.values()) else 0


def _package(args, spec, d, pm, findings, manifest, build_dir, run, seed) -> int:
    from . import datapackage, readout

    truth = readout.simulate_truth(spec, seed)
    readings = readout.simulate_reader(spec, pm, truth, seed)
    export = readout.write_reader_export(spec, readings)
    analysis = readout.analyze(pm, readout.parse_reader_export(export, spec["assay"]["plate"]["barcode"]))
    pkg = datapackage.write_package(args.out, spec=spec, d=d, plate_map=pm, findings=findings, manifest=manifest,
                                    build_dir=build_dir, run=run, truth=truth, readings=readings,
                                    export_text=export, analysis=analysis,
                                    animl_mode=args.animl, animl_schema_dir=args.animl_schema_dir)
    problems = datapackage.verify_package(pkg)
    qc = analysis["qc"]
    print(f"\nRun {run['run_id']} (simulated) -> {pkg}")
    print(f"  Z' {qc['z_prime']:.2f}, S/B {qc['signal_to_background']:.1f}, "
          f"{sum(f['status'] == 'active' for f in analysis['fits'])} active compounds")
    for dev in run["deviations"]:
        print(f"  [DEVIATION] {dev['code']}: {dev['what']}")
    am = json.loads((pkg / "datapackage.json").read_text(encoding="utf-8"))["animl"]
    if am.get("exported"):
        v = am["validated_against"]
        print(f"  AnIML: experiment.animl validated against {v['core_namespace']} (version {v['core_version']})")
    else:
        print(f"  AnIML: not exported ({am['reason']})")
    print("  package verification:", "OK" if not problems else problems)
    return 1 if problems else 0


def _report(o) -> int:
    """Print where a run stands; exit code 0 = complete, 2 = needs attention, 3 = aborted."""
    print(f"\nRun {o.run_id}: {o.status}  (simulated clock {o.clock.now().isoformat()})")
    if o.status == "waiting":
        print(f"  incubating; wakes at {o.marks['wake_at']}")
        print(f"  continue with:  python -m htsorch resume {o.state_path} --until-wake")
    elif o.status == "paused":
        p = o.pending
        what = p["fault"]["code"] + " " + p["fault"]["message"] if p["kind"] == "fault" else p["code"] + " " + p["reason"]
        print(f"  PAUSED: {what}")
        print(f"  needs a person: {p['reason']}")
        print(f"  continue with:  python -m htsorch resume {o.state_path} --operator NAME --decision retry|abort|continue")
    elif o.status == "aborted":
        last = [e for e in o.events if e["step"] == "aborted"][-1]
        print(f"  ABORTED: {last['reason']}; plates quarantined, no data package")
    return {"complete": 0, "aborted": 3}.get(o.status, 2)


def cmd_run(args) -> int:
    """Build artifacts, run the workflow on the simulated devices, and write the FAIR data package.

    The orchestrator stops at the 72 h incubation (state saved) and, in this one-shot command,
    is resumed straight away with the clock moved to the wake-up time."""
    from datetime import datetime, timezone

    from .runner import build_orchestrator
    from .scenario import load_scenario, parse_scenario

    rc = cmd_build(args.spec, args.out, simulate=not args.no_simulate)
    if rc:
        return rc
    spec = load_spec(args.spec)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    findings = validate(spec, d, pm)
    build_dir = Path(args.out) / spec["experiment"]["id"]
    manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))

    sc = load_scenario(args.scenario) if args.scenario else parse_scenario({})
    wake_delay_h = sc["wake_delay_h"] + args.extra_incubation_h
    start = None
    if args.start:
        start = datetime.fromisoformat(args.start)
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    protocols = {n: h for n, h in manifest["files"].items() if n.endswith(".py")}
    o = build_orchestrator(spec, sc, operator_id=args.operator, start=start, protocols=protocols,
                           state_path=build_dir / "state.json", meta={"seed": args.seed},
                           extra_compound_wait_min=args.extra_compound_wait_min)
    status = o.run()
    if status == "waiting" and not args.stop_at_wait:
        status = o.resume(until_wake=True, wake_delay_h=wake_delay_h)
    if status != "complete":
        return _report(o)
    return _package(args, spec, d, pm, findings, manifest, build_dir, o.run_record(), args.seed)


def cmd_scenarios(args) -> int:
    """Run scenario files on the simulated plant and check each one's expected outcome."""
    from .runner import run_all

    results = run_all(args.paths, args.spec, work_dir=args.work)
    for r in results:
        print(r.line())
        if args.verbose and r.description:
            print(f"        {r.description}")
    failed = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} scenarios behave as expected"
          + (f"; FAILED: {', '.join(r.name for r in failed)}" if failed else ""))
    return 1 if failed else 0


def cmd_qualify(args) -> int:
    """Check the validation documents against the code, run the OQ protocol and write the report."""
    import tempfile

    from . import qualify

    root = Path(args.root).resolve()
    docs = qualify.load_docs(root / "validation")
    problems = qualify.review(docs, root)
    for p in problems:
        print(f"REVIEW  {p}")
    results = None
    if not args.review_only:
        with tempfile.TemporaryDirectory() as td:
            results = qualify.execute(docs, root, Path(td))
        for r in results:
            print(f"{r['status'].upper():10} {r['id']}  {r['title']}")
    env = qualify.environment(root)
    report = qualify.write_report(docs, problems, results, env, Path(args.out))
    ok = not problems and (args.review_only or all(r["status"] == "pass" for r in results))
    print(f"\n{'QUALIFIED' if ok else 'NOT QUALIFIED'}: report in {report}")
    return 0 if ok else 1


def cmd_identify(args) -> int:
    """Manual baseline, automation-fit score and ROI from the spec and identify/assumptions.yaml."""
    from .identify import run

    r = run(args.spec, args.assumptions)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "identify.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    b, a, e, f = r["baseline"], r["automated"], r["economics"], r["fit"]
    print(f"manual hands-on {b['hands_on_h']} h/plate; with automation {a['hands_on_h']} h/plate")
    print(f"manual ({b['compound_mode']}) compound-to-cells {b['compound_to_cells_min']} min vs {b['compound_to_cells_window_min']} min window; "
          f"reagent lag {b['reagent_lag_min']} vs {b['reagent_lag_window_min']} min")
    h = r["reference_idot_assisted"]
    print(f"reference, iDOT doses and people do the rest: {h['hands_on_h']} h, compound-to-cells {h['compound_to_cells_min']} min")
    print(f"automation fit {f['percent']}% ({f['verdict']})")
    print(f"one-off {e['one_off_total_eur']} EUR; net/year {e['net_annual_eur']} EUR; payback {e['payback_years']} y; NPV {e['npv_5y_eur']} EUR")
    print(f"break-even for 3 y payback: {r['breakeven_runs_per_week_3y']} runs/week "
          f"({r['breakeven_runs_per_week_3y_high_cost']} if costs are 1.5x)")
    print(f"{len(r['placeholders'])} assumptions are placeholders: see {out / 'identify.json'}")
    return 0


def cmd_resume(args) -> int:
    """Continue a saved run: let time pass, give a paused run its operator response, finish and package."""
    from .engine import Orchestrator

    o = Orchestrator.load(args.state, args.spec)
    spec = o.spec
    d = o.d
    pm = o.pm
    findings = validate(spec, d, pm)
    build_dir = Path(args.state).parent
    manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    response = None
    if args.operator:
        response = {"actor": args.operator, "note": args.note or ""}
        if args.decision:
            response["decision"] = args.decision       # otherwise: retry for a fault, continue for a window
    elif args.decision:
        print("--decision needs --operator NAME (a named person decides, not the system)")
        return 1
    status = o.resume(until_wake=args.until_wake, wake_delay_h=args.wake_delay_h, advance_min=args.advance_min,
                      response=response)
    if status != "complete":
        return _report(o)
    args.out = args.out or str(build_dir.parent)
    return _package(args, spec, d, pm, findings, manifest, build_dir, o.run_record(), o.meta.get("seed", 7))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="htsorch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("spec")
    b = sub.add_parser("build"); b.add_argument("spec"); b.add_argument("--out", default="out")
    b.add_argument("--no-simulate", action="store_true")
    r = sub.add_parser("run", help="build, run the workflow on simulated devices and write the FAIR data package")
    r.add_argument("spec"); r.add_argument("--out", default="out")
    r.add_argument("--operator", default="SIMULATED-OPERATOR")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--start", help="simulated start time, ISO 8601 (UTC if no offset)")
    r.add_argument("--scenario", help="YAML file with faults, operator behaviour, policy changes and delays")
    r.add_argument("--extra-compound-wait-min", type=float, default=0.0, help="inject a handoff delay before seeding")
    r.add_argument("--extra-incubation-h", type=float, default=0.0, help="wake the orchestrator this many hours late")
    r.add_argument("--stop-at-wait", action="store_true", help="stop when the incubation starts (resume later)")
    r.add_argument("--no-simulate", action="store_true")
    r.add_argument("--animl", choices=["auto", "require", "off"], default="auto",
                   help="AnIML export: auto = if schemas are present, require = fail without them, off")
    r.add_argument("--animl-schema-dir", default=None, help="folder with the AnIML XSDs (default schemas/animl)")
    sc = sub.add_parser("scenarios", help="run scenario files and check each one's expected outcome")
    sc.add_argument("paths", nargs="*", default=["scenarios"], help="scenario files or folders (default: scenarios/)")
    sc.add_argument("--spec", default="specs/ctg3d_screen.yaml")
    sc.add_argument("--work", default=None, help="keep run folders here (default: a temporary folder)")
    sc.add_argument("-v", "--verbose", action="store_true")
    q = sub.add_parser("qualify", help="check URS, risk assessment and OQ protocol against the code, run the OQ, write the report")
    q.add_argument("--root", default=".", help="project folder (default: the current folder)")
    q.add_argument("--out", default="validation/report")
    q.add_argument("--review-only", action="store_true", help="only check the documents against the code, do not run the tests")
    ide = sub.add_parser("identify", help="manual time baseline, automation-fit score and ROI from the spec and assumptions")
    ide.add_argument("--spec", default="specs/ctg3d_screen.yaml")
    ide.add_argument("--assumptions", default="identify/assumptions.yaml")
    ide.add_argument("--out", default="identify/report")
    rs = sub.add_parser("resume", help="continue a saved run (after the incubation, or after a pause)")
    rs.add_argument("state", help="state.json in the build folder")
    rs.add_argument("--spec", default=None, help="spec file (default: the one recorded in the state)")
    rs.add_argument("--out", default=None)
    rs.add_argument("--until-wake", action="store_true", help="let simulated time pass until the incubation ends")
    rs.add_argument("--wake-delay-h", type=float, default=0.0, help="wake this many hours after the planned time")
    rs.add_argument("--advance-min", type=float, default=None, help="let this many simulated minutes pass first")
    rs.add_argument("--operator", help="name of the person responding to a paused run")
    rs.add_argument("--decision", choices=["retry", "abort", "continue"], default=None)
    rs.add_argument("--note", default="")
    rs.add_argument("--animl", choices=["auto", "require", "off"], default="auto")
    rs.add_argument("--animl-schema-dir", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "resume":
        return cmd_resume(a)
    if a.cmd == "scenarios":
        return cmd_scenarios(a)
    if a.cmd == "identify":
        return cmd_identify(a)
    if a.cmd == "qualify":
        return cmd_qualify(a)
    if a.cmd == "validate":
        return cmd_validate(a.spec)
    return cmd_build(a.spec, a.out, simulate=not a.no_simulate)


if __name__ == "__main__":
    sys.exit(main())
