"""Vercel entrypoint probe."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "examples"))

print("PROBE cwd:", Path.cwd(), file=sys.stderr)
print("PROBE repo_root:", REPO_ROOT, "exists:", REPO_ROOT.exists(), file=sys.stderr)
print("PROBE listdir parent3:", sorted(p.name for p in REPO_ROOT.iterdir())[:40], file=sys.stderr)
print("PROBE sys.path:", sys.path, file=sys.stderr)
try:
    print("PROBE pip freeze:", subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout[:2000], file=sys.stderr)
except Exception as exc:  # noqa: BLE001
    print("PROBE pip freeze failed:", exc, file=sys.stderr)

from fastapi import FastAPI  # noqa: E402

app = FastAPI()


@app.get("/api/probe")
def probe() -> dict:
    import importlib.util

    return {
        "cwd": str(Path.cwd()),
        "repo_root": str(REPO_ROOT),
        "repo_root_exists": REPO_ROOT.exists(),
        "entries": sorted(p.name for p in Path(__file__).resolve().parent.iterdir()),
        "parent_entries": sorted(p.name for p in Path(__file__).resolve().parents[1].iterdir()),
        "has_demo_common": importlib.util.find_spec("demo_common") is not None,
        "has_shopping_agent": importlib.util.find_spec("shopping_agent") is not None,
        "bundled": sorted(str(p) for p in Path(__file__).resolve().parent.glob("_bundle/**"))[:20],
    }
