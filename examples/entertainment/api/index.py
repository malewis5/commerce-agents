# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The deployed entrypoint of the ACME Tickets example API: the app a platform's Python
runtime imports (``examples/entertainment/vercel.json`` names ``index:app``).

A function carries its own service directory and nothing above it, so the build step put
the slice of the repository this process needs — the shared host code, this vertical's
fixtures, and both roles' skill files — under ``_deploy/`` in the repository's own layout
(``scripts/vercel/bundle_api.py``). Pointing the two path variables at that copy and
importing through ``examples/`` is the same resolution a local run gets from
``uvicorn entertainment.api.main:app --app-dir examples``; nothing below here knows which
of the two it is.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent / "_deploy"
if DEPLOY.is_dir():
    sys.path.insert(0, str(DEPLOY / "examples"))
    os.environ.setdefault("COMMERCE_REPO_ROOT", str(DEPLOY))
    os.environ.setdefault("COMMERCE_DATA_DIR", str(DEPLOY / "examples" / "entertainment" / "data"))

from entertainment.api.main import app  # noqa: E402

__all__ = ["app"]
