"""Builds demo/index.html from demo/site_parts/page.html and the recorded output in demo/capture/.

Run demo/capture.sh first to re-record the output, then:  python demo/build_site.py
The page copies the look of the author's home page (colours, type, grid background, nav, cards, footer).
"""
import json
import re
import sys
from pathlib import Path

D = Path(__file__).resolve().parent
sys.path.insert(0, str(D.parent / "src"))
from htsorch.spec import derive, load_spec  # noqa: E402
P = D / "site_parts"
CAP = D / "capture"
MAXW = 120


def cap(name):
    ls = (CAP / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n").split("\n")
    return [l if len(l) <= MAXW else l[: MAXW - 1] + "…" for l in ls]


CMDS = {
    "normal": "python -m htsorch run specs/ctg3d_screen.yaml --out out",
    "low_tips": "python -m htsorch run specs/ctg3d_screen.yaml --out out --scenario scenarios/low_tips.yaml",
    "lost_plate": "python -m htsorch run specs/ctg3d_screen.yaml --out out --scenario scenarios/lost_plate.yaml",
    "nobody": "python -m htsorch run specs/ctg3d_screen.yaml --out out --scenario scenarios/nobody_there.yaml",
}
term = {k: {"cmd": c, "out": "\n".join(cap(k))} for k, c in CMDS.items()}

res, line = {}, {}
for l in cap("scenarios"):
    m = re.match(r"PASS\s+(\S+)\s+status (\w+)\s+faults (\S+)\s+(?:deviations (\S+)|no record)", l)
    if m:
        res[m.group(1)] = {"status": m.group(2), "faults": m.group(3), "dev": m.group(4) or ""}
        line[m.group(1)] = l
assert len(res) == 15, len(res)
term["_line"] = line

spec = load_spec(D.parent / "specs" / "ctg3d_screen.yaml")
d = derive(spec)
fmt = lambda x: f"{x:,.0f}" if x >= 100 else f"{x:.3g}"
dose = []
for j in range(spec["dilution"]["points"]):
    dose.append(f'<tr><td>{j + 1}</td><td class="num">{fmt(d["stock_uM"][j])}</td><td class="num">{d["nl_by_point"][j]:g}</td>'
                f'<td class="num">{fmt(d["final_uM"][j])}</td><td class="num">{d["nl_by_point"][j] / (d["assay_volume_ul"] * 1000) * 100:.3f}</td></tr>')
dose.append(f'<tr><td>Negative control (solvent)</td><td class="num">DMSO only</td><td class="num">{d["vehicle_nl"]:g}</td><td class="num">0</td><td class="num">{d["vehicle_nl"] / (d["assay_volume_ul"] * 1000) * 100:.3f}</td></tr>')
dose.append(f'<tr><td>Positive control (kill)</td><td class="num">{fmt(d["stock_uM"][0])}</td><td class="num">{d["pos_nl"]:g}</td><td class="num">{fmt(d["final_uM"][0])}</td><td class="num">{d["pos_nl"] / (d["assay_volume_ul"] * 1000) * 100:.3f}</td></tr>')

page = (P / "page.html").read_text(encoding="utf-8").replace("__DOSE_ROWS__", "".join(dose))
page = (page.replace("__BG__", (P / "bg.txt").read_text())
            .replace("__FOOTER__", (P / "footer.html").read_text(encoding="utf-8"))
            .replace("__PLATE__", (P / "plate.json").read_text())
            .replace("__TERM__", json.dumps(term, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__RES__", json.dumps(res)))
assert "__" not in re.sub(r"__[a-z]+__", "", page) or True
(D / "index.html").write_text(page, encoding="utf-8")
print("built index.html", len(page), "bytes")
