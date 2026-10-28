"""Build the in-browser (stlite) version of the Streamlit app: a folder you upload to your website.

    python demo/build_web.py        ->  demo/web/index.html + demo/web/files.json

Python runs in the visitor's browser (Pyodide); nothing is sent to a server.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "web"
BG = (ROOT / "demo" / "site_parts" / "bg.txt").read_text().strip()
STLITE = "0.85.1"

files = {"app.py": (ROOT / "streamlit_app.py").read_text(encoding="utf-8")}
for p in sorted((ROOT / "src" / "htsorch").rglob("*.py")):
    files["htsorch/" + p.relative_to(ROOT / "src" / "htsorch").as_posix()] = p.read_text(encoding="utf-8")
for folder in ("specs", "scenarios"):
    for p in sorted((ROOT / folder).glob("*.yaml")):
        files[f"{folder}/{p.name}"] = p.read_text(encoding="utf-8")

HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no" />
  <title>HTS Orchestrator: Live Demo | Lagan Pathania</title>
  <meta name="description" content="Spec-driven, human-in-the-loop screening orchestrator. Runs in your browser; your data stays with you." />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@stlite/browser@__V__/build/stlite.css" />
  <style>
    :root { --primary: #2d6a4f; --bg: #fdfcf8; --dim: #354a3e; --err: #bc4749; }
    body { margin: 0; background: var(--bg); }
    #boot { position: fixed; inset: 0; z-index: 99999; display: flex; align-items: center; justify-content: center;
            padding: 1.5rem; background: linear-gradient(rgba(45,106,79,.05) 1px, transparent 1px) 0 0 / 40px 40px, linear-gradient(90deg, rgba(45,106,79,.05) 1px, transparent 1px) 0 0 / 40px 40px, url("__BG__") right center / cover no-repeat, var(--bg);
            transition: opacity .4s ease; overflow-y: auto;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--dim); }
    #boot.done { opacity: 0; pointer-events: none; }
    .boot-card { width: 100%; max-width: 560px; text-align: center; background: rgba(253,252,248,.9); border-radius: 16px; padding: 1.6rem 1.4rem; }
    .brand-name { font-size: clamp(1.9rem, 6vw, 2.8rem); font-weight: 800; color: var(--primary); text-transform: uppercase; letter-spacing: .12em; line-height: 1.1; }
    .brand-role { font-size: .8rem; font-weight: 600; text-transform: uppercase; letter-spacing: .22em; margin-top: .55rem; }
    .brand-rule { width: 72px; height: 3px; background: var(--primary); margin: 1.4rem auto 1.5rem; border-radius: 2px; }
    .product { font-size: 1.6rem; font-weight: 700; color: var(--primary); margin: 0; letter-spacing: -.01em; }
    .product-sub { margin: .35rem 0 0; font-size: 1rem; }
    .boot-card p { line-height: 1.5; }
    .spinner { width: 34px; height: 34px; margin: 1.7rem auto 0; border-radius: 50%; border: 4px solid rgba(45,106,79,.18); border-top-color: var(--primary); animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    #boot-msg { margin: .9rem 0 .2rem; font-weight: 600; }
    .bar { height: 6px; margin: .9rem auto .5rem; border-radius: 99px; background: rgba(45,106,79,.14); overflow: hidden; max-width: 420px; }
    .bar i { display: block; height: 100%; width: 38%; border-radius: 99px; background: var(--primary); animation: slide 1.4s ease-in-out infinite; }
    @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 102%; } }
    .steps { list-style: none; padding: 0; margin: 1.1rem auto; max-width: 380px; text-align: left; }
    .steps li { display: flex; gap: .7rem; align-items: center; padding: .36rem 0; color: #7d8f84; font-size: .95rem; }
    .steps li b { flex: 0 0 22px; height: 22px; border-radius: 50%; border: 2px solid #c3d1c9; display: inline-flex; align-items: center; justify-content: center; font-size: .75rem; color: #fff; }
    .steps li.active { color: var(--primary); font-weight: 600; }
    .steps li.active b { border-color: var(--primary); border-top-color: transparent; animation: spin .9s linear infinite; }
    .steps li.ok { color: var(--dim); }
    .steps li.ok b { background: var(--primary); border-color: var(--primary); }
    .steps li.ok b::after { content: "\2713"; }
    .small { font-size: .85rem; color: #5d7266; margin: .45rem 0; }
    #slow { display: none; }
    #slow button { background: none; border: 0; color: var(--primary); text-decoration: underline; cursor: pointer; font: inherit; }
    #err { color: var(--err); font-weight: 600; word-break: break-word; }
    @media (prefers-reduced-motion: reduce) { .spinner, .bar i, .steps li.active b { animation: none; } .bar i { width: 100%; } }
  </style>
</head>
<body>
  <div id="boot" role="status" aria-live="polite">
    <div class="boot-card">
      <div class="brand-name">Lagan Pathania</div>
      <div class="brand-role">Lab Automation Architect</div>
      <div class="brand-rule"></div>
      <h1 class="product">HTS Orchestrator</h1>
      <p class="product-sub">One spec drives the screen. The operator stays in the loop.</p>
      <div class="spinner" aria-hidden="true"></div>
      <p id="boot-msg">Getting things ready in your browser…</p>
      <div class="bar" aria-hidden="true"><i></i></div>
      <ul class="steps">
        <li id="s1" class="active"><b></b>Loading project files</li>
        <li id="s2"><b></b>Starting Python and installing packages</li>
        <li id="s3"><b></b>Starting the app</li>
      </ul>
      <p class="small">The first visit takes about 30 to 90 seconds. Later visits are faster.<br><span id="elapsed"></span></p>
      <p class="small">Everything runs in your browser. Nothing is sent to a server. All devices are simulated.</p>
      <p id="slow" class="small">Taking longer than usual? <button type="button" id="hide">Hide this screen</button></p>
      <p id="err"></p>
    </div>
  </div>
  <div id="root"></div>

  <script type="module">
    import { mount } from "__STLITE_JS__";
    const $ = (id) => document.getElementById(id);
    const boot = $("boot"), t0 = Date.now();
    const setStep = (n) => [1, 2, 3].forEach((i) => { $("s" + i).className = i < n ? "ok" : i === n ? "active" : ""; });
    const finish = () => { setStep(4); boot.classList.add("done"); clearInterval(timer); setTimeout(() => boot.remove(), 600); };
    const timer = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      $("elapsed").textContent = "Elapsed: " + s + " s";
      if (s > 90) $("slow").style.display = "block";
      if (s > 300) { $("boot-msg").textContent = "This is taking very long. Try refreshing the page."; }
    }, 1000);
    $("hide").onclick = finish;

    try {
      $("boot-msg").textContent = "Loading project files…";
      const r = await fetch("files.json");
      if (!r.ok) throw new Error("Could not load files.json (HTTP " + r.status + ")");
      const files = await r.json();
      files[".streamlit/config.toml"] = __THEME__;

      setStep(2);
      $("boot-msg").textContent = "Downloading Python and scientific packages. This is the slow part on a first visit…";
      let stage = 2;
      const watch = new MutationObserver(() => {
        if (document.querySelector('[data-testid="stException"], .lp-bar')) { watch.disconnect(); finish(); return; }
        if (stage < 3 && document.querySelector('[data-testid="stApp"], .stApp')) {
          stage = 3; setStep(3); $("boot-msg").textContent = "Almost there. Starting the app…";
        }
      });
      watch.observe($("root"), { childList: true, subtree: true });

      mount({ entrypoint: "app.py", files,
              requirements: ["numpy", "pandas", "scipy", "pyyaml", "lxml", "altair"] }, $("root"));
    } catch (e) {
      $("s1").className = "";
      document.querySelector(".spinner")?.remove();
      $("boot-msg").textContent = "Something went wrong while loading.";
      $("err").textContent = String(e);
      clearInterval(timer);
    }
  </script>
</body>
</html>
'''
theme = (ROOT / ".streamlit" / "config.toml").read_text().split("[server]")[0].strip() + "\n"
html = (HTML.replace("__V__", STLITE).replace("__BG__", BG)
        .replace("__STLITE_JS__", f"https://cdn.jsdelivr.net/npm/@stlite/browser@{STLITE}/build/stlite.js")
        .replace("__THEME__", json.dumps(theme)))
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "index.html").write_text(html, encoding="utf-8")
(OUT / "files.json").write_text(json.dumps(files), encoding="utf-8")
print(f"wrote {OUT}/index.html and files.json ({len(files)} files, {(OUT / 'files.json').stat().st_size // 1024} KB)")
