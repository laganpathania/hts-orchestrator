import copy
import json
from pathlib import Path

import pytest

from htsorch import qualify
from htsorch.cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def docs():
    return qualify.load_docs(ROOT / "validation")


def test_the_shipped_documents_agree_with_the_code(docs):
    assert qualify.review(docs, ROOT) == []


def test_every_requirement_risk_and_code_is_traced(docs):
    rows = qualify.traceability(docs, None)
    assert {r["urs"] for r in rows} == {r["id"] for r in docs["urs"]["requirements"]}
    inv = qualify.code_inventory(ROOT / "src" / "htsorch")
    assert {"V001", "K015", "E302", "D051", "C001", "Q002"} <= inv
    assert inv <= {c for case in docs["oq"]["cases"] for c in case["codes"]}


def problems_after(docs, change):
    d = copy.deepcopy(docs)
    change(d)
    return qualify.review(d, ROOT)


def test_an_uncovered_requirement_is_reported(docs):
    d = copy.deepcopy(docs)
    d["urs"]["requirements"].append({"id": "URS-99", "title": "x", "text": "y"})
    assert "URS-99: no OQ case covers this requirement" in qualify.review(d, ROOT)


def test_an_uncovered_risk_and_an_untested_control_are_reported(docs):
    d = copy.deepcopy(docs)
    d["risk"]["risks"].append({"id": "R-99", "failure": "f", "effect": "e", "s": 3, "before": {"o": 3, "d": 3},
                               "controls": ["E301"], "after": {"o": 1, "d": 1}, "urs": ["URS-01"]})
    p = qualify.review(d, ROOT)
    assert "R-99: no OQ case covers this risk" in p
    d["oq"]["cases"][0]["risks"].append("R-99")                      # covered, but OQ-01 never provokes E301
    assert "R-99: control E301 is not provoked by any case that covers this risk" in qualify.review(d, ROOT)


def test_a_code_nobody_provokes_is_reported(docs):
    p = problems_after(docs, lambda d: d["oq"]["cases"][8]["codes"].remove("K014"))
    assert "code K014 is emitted by the code but provoked by no OQ case" in p


def test_a_misspelled_test_or_scenario_or_code_is_reported(docs):
    def change(d):
        d["oq"]["cases"][0]["tests"].append("pytest:test_platform.py::test_that_does_not_exist")
        d["oq"]["cases"][0]["tests"].append("scenario:no_such_scenario")
        d["oq"]["cases"][0]["tests"].append("nonsense")
        d["oq"]["cases"][0]["codes"].append("V999")
    p = " | ".join(problems_after(docs, change))
    for part in ("does not exist", "scenario file no_such_scenario.yaml", "bad test reference", "V999 is not emitted"):
        assert part in p


def test_a_high_residual_risk_is_reported(docs):
    def change(d):
        d["risk"]["risks"][0].update(s=5, after={"o": 5, "d": 5}, before={"o": 5, "d": 5})
    p = problems_after(docs, change)
    assert any("residual risk is high" in x for x in p)
    p = problems_after(docs, lambda d: d["risk"]["risks"][0].update(after={"o": 5, "d": 5}))
    assert any("higher than the initial risk" in x for x in p)


def test_pytest_results_are_read_correctly_including_skips_and_parameters(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "import pytest\n"
        "def test_ok(): pass\n"
        "def test_bad(): assert 0\n"
        "@pytest.mark.skip\n"
        "def test_skipped(): pass\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_param(n): assert n != 2\n")
    r = qualify._run_pytest(["tests/test_x.py"], tmp_path)
    assert r == {("test_x.py", "test_ok"): "pass", ("test_x.py", "test_bad"): "fail",
                 ("test_x.py", "test_skipped"): "skip", ("test_x.py", "test_param"): "fail"}


def test_the_report_and_matrix_are_written_and_a_failure_changes_the_verdict(docs, tmp_path):
    results = [{"id": c["id"], "title": c["title"], "status": "pass", "tests": []} for c in docs["oq"]["cases"]]
    env = qualify.environment(ROOT)
    assert len(env["code_and_documents_sha256"]) == 64
    out = qualify.write_report(docs, [], results, env, tmp_path / "a")
    text = out.read_text()
    assert "**Verdict: PASS**" in text and "Sign-off" in text and "| R-01 |" in text
    results[3]["status"] = "fail"
    results[3]["tests"] = [{"ref": "pytest:x::y", "result": "fail", "detail": "boom"}]
    out = qualify.write_report(docs, [], results, env, tmp_path / "b")
    assert "**Verdict: FAIL**" in out.read_text() and "pytest:x::y" in out.read_text()
    rows = (tmp_path / "a" / "traceability.csv").read_text().splitlines()
    assert rows[0].startswith("urs,requirement") and len(rows) > 20
    assert json.loads((tmp_path / "b" / "results.json").read_text())["verdict"] == "FAIL"


def test_cli_review_only_exits_0_and_1(tmp_path, capsys):
    assert main(["qualify", "--root", str(ROOT), "--review-only", "--out", str(tmp_path / "r")]) == 0
    root2 = tmp_path / "proj"
    root2.mkdir()
    for sub in ("src", "tests", "scenarios", "specs"):
        (root2 / sub).symlink_to(ROOT / sub)
    (root2 / "validation").mkdir()
    for f in ("urs", "risk", "oq"):
        (root2 / "validation" / f"{f}.yaml").write_text((ROOT / "validation" / f"{f}.yaml").read_text())
    oq = (root2 / "validation" / "oq.yaml")
    oq.write_text(oq.read_text().replace("codes: [V001, V002, V003, V010, V011, V012, V020, V021, V030, V060]", "codes: []"))
    assert main(["qualify", "--root", str(root2), "--review-only", "--out", str(tmp_path / "r2")]) == 1
    assert "provoked by no OQ case" in capsys.readouterr().out
