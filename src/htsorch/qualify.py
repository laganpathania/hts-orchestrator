"""Qualification: check the validation documents against the code, run the OQ protocol, write the report.

Inputs (validation/): urs.yaml, risk.yaml, oq.yaml.
`review()` is the paper check (every requirement and risk is covered, every code is provoked, every referenced
test exists). `execute()` runs the referenced tests and scenarios. `write_report()` produces the traceability
matrix and the OQ report. Nothing here is a substitute for a person signing the report.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

CODE_RE = re.compile(r"^[VEKDCQ]\d{3}$")
SRC_CODE_RE = re.compile(r"""["']([VEKDCQ]\d{3})["']""")
LEVELS = (("low", "rpn_low_max"), ("medium", "rpn_medium_max"))


def load_docs(vdir: Path) -> dict:
    out = {}
    for name in ("urs", "risk", "oq"):
        p = vdir / f"{name}.yaml"
        if not p.exists():
            raise FileNotFoundError(f"missing validation document {p}")
        out[name] = yaml.safe_load(p.read_text(encoding="utf-8"))
    return out


def code_inventory(src_dir: Path) -> set[str]:
    """Every V/E/K/D/C/Q code the code base can emit (quoted codes in the source plus the fault catalogue)."""
    found: set[str] = set()
    for f in sorted(src_dir.rglob("*.py")):
        found.update(SRC_CODE_RE.findall(f.read_text(encoding="utf-8")))
    from .faults import FAULTS
    found.update(FAULTS)
    return found


def rpn(s: int, o: int, d: int) -> int:
    return s * o * d


def level(value: int, acceptance: dict) -> str:
    for name, key in LEVELS:
        if value <= acceptance[key]:
            return name
    return "high"


def split_ref(ref: str) -> tuple[str, str]:
    kind, _, rest = ref.partition(":")
    if kind not in ("pytest", "scenario") or not rest:
        raise ValueError(f"bad test reference '{ref}': use pytest:<file>::<test> or scenario:<name>")
    return kind, rest


def review(docs: dict, root: Path) -> list[str]:
    """The paper check. Returns problems; an empty list means the documents and the code agree."""
    urs, risk, oq = docs["urs"], docs["risk"], docs["oq"]
    problems: list[str] = []
    reqs = {r["id"]: r for r in urs["requirements"]}
    if len(reqs) != len(urs["requirements"]):
        problems.append("URS: duplicate requirement ids")
    risks = {r["id"]: r for r in risk["risks"]}
    if len(risks) != len(risk["risks"]):
        problems.append("risk: duplicate risk ids")
    cases = {c["id"]: c for c in oq["cases"]}
    if len(cases) != len(oq["cases"]):
        problems.append("OQ: duplicate case ids")
    inventory = code_inventory(root / "src" / "htsorch")

    for c in oq["cases"]:
        for u in c.get("urs", []):
            if u not in reqs:
                problems.append(f"{c['id']}: unknown requirement {u}")
        for r in c.get("risks", []):
            if r not in risks:
                problems.append(f"{c['id']}: unknown risk {r}")
        for code in c.get("codes", []):
            if code not in inventory:
                problems.append(f"{c['id']}: code {code} is not emitted anywhere in the code")
        if not c.get("tests"):
            problems.append(f"{c['id']}: no tests")
        if not c.get("accept"):
            problems.append(f"{c['id']}: no acceptance criterion")
        for ref in c.get("tests", []):
            try:
                kind, rest = split_ref(ref)
            except ValueError as e:
                problems.append(f"{c['id']}: {e}")
                continue
            if kind == "scenario":
                if not (root / "scenarios" / f"{rest}.yaml").exists():
                    problems.append(f"{c['id']}: scenario file {rest}.yaml does not exist")
            else:
                file, _, name = rest.partition("::")
                p = root / "tests" / file
                if not p.exists() or not re.search(rf"^def {re.escape(name)}\(", p.read_text(encoding="utf-8"), re.M):
                    problems.append(f"{c['id']}: test {rest} does not exist")

    covered_urs = {u for c in oq["cases"] for u in c.get("urs", [])}
    for u in reqs:
        if u not in covered_urs:
            problems.append(f"{u}: no OQ case covers this requirement")
    covered_risks = {r for c in oq["cases"] for r in c.get("risks", [])}
    covered_codes = {code for c in oq["cases"] for code in c.get("codes", [])}
    for rid, r in risks.items():
        for u in r.get("urs", []):
            if u not in reqs:
                problems.append(f"{rid}: unknown requirement {u}")
        if rid not in covered_risks:
            problems.append(f"{rid}: no OQ case covers this risk")
        if not r.get("controls"):
            problems.append(f"{rid}: no controls")
        b, a = r["before"], r["after"]
        for k in ("s",):
            if not 1 <= r[k] <= 5:
                problems.append(f"{rid}: severity must be 1-5")
        for part in (b, a):
            if not (1 <= part["o"] <= 5 and 1 <= part["d"] <= 5):
                problems.append(f"{rid}: occurrence and detectability must be 1-5")
        if rpn(r["s"], a["o"], a["d"]) > rpn(r["s"], b["o"], b["d"]):
            problems.append(f"{rid}: residual risk is higher than the initial risk")
        if level(rpn(r["s"], a["o"], a["d"]), risk["acceptance"]) == "high":
            problems.append(f"{rid}: residual risk is high and needs a further control")
        # a control that is a code must be provoked by a case that covers this risk
        tested = {code for c in oq["cases"] if rid in c.get("risks", []) for code in c.get("codes", [])}
        for ctl in r.get("controls", []):
            if CODE_RE.match(str(ctl)):
                if ctl not in inventory:
                    problems.append(f"{rid}: control {ctl} is not a code in the code base")
                elif ctl not in tested:
                    problems.append(f"{rid}: control {ctl} is not provoked by any case that covers this risk")
    for code in sorted(inventory - covered_codes):
        problems.append(f"code {code} is emitted by the code but provoked by no OQ case")
    return problems


# ---- execution ---------------------------------------------------------------------------------------------------------------

def _run_pytest(nodes: list[str], root: Path) -> dict[tuple[str, str], str]:
    """Run the given test nodes once; return {(file, test name without params): 'pass'|'fail'|'skip'} (worst result wins)."""
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "junit.xml"
        env = {**__import__("os").environ, "PYTHONPATH": str(root / "src")}
        subprocess.run([sys.executable, "-m", "pytest", *nodes, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"],
                       cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        results: dict[tuple[str, str], str] = {}
        if not xml.exists():
            return results
        rank = {"pass": 0, "skip": 1, "fail": 2}
        for tc in ET.parse(xml).getroot().iter("testcase"):
            file = tc.get("classname", "").split(".")[-1] + ".py"
            name = tc.get("name", "").split("[")[0]
            state = "fail" if tc.find("failure") is not None or tc.find("error") is not None else \
                    "skip" if tc.find("skipped") is not None else "pass"
            key = (file, name)
            if key not in results or rank[state] > rank[results[key]]:
                results[key] = state
    return results


def execute(docs: dict, root: Path, work: Path) -> list[dict]:
    from . import runner
    cases = docs["oq"]["cases"]
    nodes = sorted({f"tests/{split_ref(t)[1]}" for c in cases for t in c["tests"] if split_ref(t)[0] == "pytest"})
    py = _run_pytest(nodes, root)
    spec_path = root / "specs" / "ctg3d_screen.yaml"
    ctx = runner.build_context(spec_path, work)
    scen: dict[str, runner.ScenarioResult] = {}
    out = []
    for c in cases:
        tests = []
        for ref in c["tests"]:
            kind, rest = split_ref(ref)
            if kind == "pytest":
                file, _, name = rest.partition("::")
                state = py.get((file, name), "missing")
                tests.append({"ref": ref, "result": state, "detail": ""})
            else:
                if rest not in scen:
                    scen[rest] = runner.run_scenario(root / "scenarios" / f"{rest}.yaml", ctx, work / "runs")
                r = scen[rest]
                tests.append({"ref": ref, "result": "pass" if r.passed else "fail", "detail": "; ".join(r.failures)})
        states = {t["result"] for t in tests}
        status = "fail" if "fail" in states else "incomplete" if states - {"pass"} else "pass"
        out.append({"id": c["id"], "title": c["title"], "status": status, "tests": tests})
    return out


# ---- environment and report -------------------------------------------------------------------------------------------------

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment(root: Path) -> dict:
    """Installation record (IQ in miniature): what exactly was qualified, and on what."""
    files = sorted(p for p in (root / "src" / "htsorch").rglob("*.py")) + sorted((root / "specs").glob("*.yaml")) \
        + sorted((root / "validation").glob("*.yaml"))
    tree = hashlib.sha256("".join(f"{p.relative_to(root)}:{_sha(p)}\n" for p in files).encode()).hexdigest()
    versions = {}
    for mod in ("numpy", "scipy", "lxml", "yaml", "opentrons", "pytest"):
        try:
            versions[mod] = __import__(mod).__version__
        except Exception:
            versions[mod] = "not installed"
    return {"python": platform.python_version(), "platform": platform.platform(), "packages": versions,
            "spec_sha256": _sha(root / "specs" / "ctg3d_screen.yaml"), "code_and_documents_sha256": tree,
            "executed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def traceability(docs: dict, results: list[dict] | None) -> list[dict]:
    """One row per requirement x case: URS, risks, codes, case, status."""
    status = {r["id"]: r["status"] for r in results} if results else {}
    rows = []
    risk_by_urs: dict[str, set[str]] = {}
    for r in docs["risk"]["risks"]:
        for u in r.get("urs", []):
            risk_by_urs.setdefault(u, set()).add(r["id"])
    for u in docs["urs"]["requirements"]:
        for c in docs["oq"]["cases"]:
            if u["id"] in c.get("urs", []):
                rows.append({"urs": u["id"], "requirement": u["title"], "risks": " ".join(sorted(risk_by_urs.get(u["id"], []))),
                             "case": c["id"], "case_title": c["title"], "codes": " ".join(c.get("codes", [])),
                             "result": status.get(c["id"], "not run")})
    return rows


def write_report(docs: dict, problems: list[str], results: list[dict] | None, env: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    rows = traceability(docs, results)
    with (out / "traceability.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    acc = docs["risk"]["acceptance"]
    n_pass = sum(r["status"] == "pass" for r in results or [])
    verdict = "PASS" if not problems and results and n_pass == len(results) else "FAIL"
    L = [f"# OQ report: {docs['urs']['system']} {docs['urs']['version']}", "",
         f"**Verdict: {verdict}** ({n_pass} of {len(results or [])} OQ cases passed, {len(problems)} review problem(s))", "",
         f"Intended use: {docs['urs']['intended_use'].strip()}", "",
         "This system and all instruments are simulated. The report shows the software behaves as specified; it is not a GxP validation.", "",
         "## Installation record", ""]
    L += [f"- {k}: {v}" for k, v in env.items() if k != "packages"] + [f"- packages: {env['packages']}", ""]
    L += ["## Document review", ""] + ([f"- {p}" for p in problems] if problems else ["Every requirement and risk is covered, every code is provoked, every referenced test exists."]) + [""]
    L += ["## Risk assessment", "", "| Risk | Failure | S | RPN before | RPN after | Level after | Controls |", "|---|---|---|---|---|---|---|"]
    for r in docs["risk"]["risks"]:
        b, a = rpn(r["s"], r["before"]["o"], r["before"]["d"]), rpn(r["s"], r["after"]["o"], r["after"]["d"])
        L.append(f"| {r['id']} | {r['failure']} | {r['s']} | {b} ({level(b, acc)}) | {a} | {level(a, acc)} | {', '.join(map(str, r['controls']))} |")
    L += ["", "## OQ cases", "", "| Case | Title | Result |", "|---|---|---|"]
    for r in results or []:
        L.append(f"| {r['id']} | {r['title']} | {r['status']} |")
    bad = [(r["id"], t) for r in results or [] for t in r["tests"] if t["result"] != "pass"]
    if bad:
        L += ["", "### Not passed", ""] + [f"- {cid}: {t['ref']} -> {t['result']} {t['detail']}" for cid, t in bad]
    L += ["", "## Traceability (requirement -> case)", "", "| URS | Requirement | Risks | Case | Codes | Result |", "|---|---|---|---|---|---|"]
    L += [f"| {r['urs']} | {r['requirement']} | {r['risks']} | {r['case']} | {r['codes']} | {r['result']} |" for r in rows]
    L += ["", "## Sign-off", "", "| Role | Name | Date | Signature |", "|---|---|---|---|",
          "| Author | | | |", "| Reviewer | | | |", "| Approver | | | |", ""]
    (out / "OQ_report.md").write_text("\n".join(L), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"verdict": verdict, "problems": problems, "environment": env,
                                                  "cases": results}, indent=2), encoding="utf-8")
    return out / "OQ_report.md"
