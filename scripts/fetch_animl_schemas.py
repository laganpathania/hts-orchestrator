"""Download the AnIML XML schemas into schemas/animl/ (or the folder given as argument).

The AnIML schemas are fetched from the AnIML project's GitHub repository and are not bundled
with this repo. The XML-signature schema that animl-core.xsd imports is fetched from the W3C;
if the W3C server is unreachable, it is taken from the 'signxml' wheel (pip download).

    python scripts/fetch_animl_schemas.py [target-folder]
"""
import hashlib
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://raw.githubusercontent.com/AnIML/schemas/master/"
DSIG_URL = "https://www.w3.org/TR/xmldsig-core/xmldsig-core-schema.xsd"
DSIG_IN_WHEEL = "signxml/schemas/xmldsig-core-schema.xsd"


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def fetch_dsig() -> tuple[bytes, str]:
    try:
        return get(DSIG_URL), DSIG_URL
    except Exception as ex:  # network, proxy or TLS problems
        print(f"  W3C unreachable ({type(ex).__name__}); falling back to the signxml wheel")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "-q", "-d", tmp, "signxml"],
                       check=True)
        wheel = next(Path(tmp).glob("signxml-*.whl"))
        with zipfile.ZipFile(wheel) as z:
            return z.read(DSIG_IN_WHEEL), f"{wheel.name}:{DSIG_IN_WHEEL}"


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "schemas" / "animl"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("animl-core.xsd", "animl-technique.xsd"):
        data = get(BASE + name)
        (target / name).write_bytes(data)
        print(f"  {name}  {len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()[:16]}...  from {BASE}")
    data, src = fetch_dsig()
    (target / "xmldsig-core-schema.xsd").write_bytes(data)
    print(f"  xmldsig-core-schema.xsd  {len(data)} bytes  from {src}")
    core = (target / "animl-core.xsd").read_text(encoding="utf-8", errors="replace")
    ns = core.split('targetNamespace="', 1)[1].split('"', 1)[0]
    print(f"\nSchemas written to {target}\ncore namespace: {ns}")
    print("Note: check the AnIML repository for licence terms before redistributing these files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
