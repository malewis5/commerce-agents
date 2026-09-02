import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "examples"))

from retail.api.main import app  # noqa: E402

__all__ = ["app"]
