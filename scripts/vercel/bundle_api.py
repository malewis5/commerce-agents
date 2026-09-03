# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The build command of a deployed example API (``examples/<vertical>/vercel.json``).

A platform bundles a service's own directory into its function, and nothing above it. The
API's directory is ``examples/<vertical>/api/``, but the process needs three things that
live elsewhere in the repository: the shared host code, the vertical's fixtures, and both
roles' skill files. This copies that slice into ``api/_deploy/``, keeping the
repository's own layout so every path the code already computes resolves inside it;
``api/index.py`` points ``COMMERCE_REPO_ROOT`` and ``COMMERCE_DATA_DIR`` at the copy and
imports the app.

    python3 ../../../scripts/vercel/bundle_api.py     # from examples/<vertical>/api

Run it by hand to see what a deployed function carries. Nothing else reads ``_deploy/``,
and it is gitignored.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = "_deploy"
# What a running API never reads: the suites, the caches, this script's own output, and
# anything a local run left behind.
SKIP = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "tests",
    BUNDLE,
    # The platform's own directory inside the service root, which holds the virtualenv
    # this build just filled: the function already carries it, and copying it in would
    # put a second one in the bundle.
    ".vercel",
    ".next",
    "node_modules",
    ".env",
    ".env.*",
    # A local run's memory store and seed marker sit beside the fixtures. They are
    # somebody's own remembered facts and have no business in a deployment.
    ".memory-*.json",
)


def copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, ignore=SKIP, dirs_exist_ok=True)
    elif source.exists():
        shutil.copy2(source, target)
    else:
        return
    print(f"bundle_api: {source.relative_to(REPO_ROOT)} -> {target.relative_to(Path.cwd())}")


def main(api_dir: Path) -> int:
    example = api_dir.parent
    vertical = example.name
    if not (example / "data").is_dir():
        print(f"bundle_api: {api_dir} is not an example API directory", file=sys.stderr)
        return 1

    bundle = api_dir / BUNDLE
    shutil.rmtree(bundle, ignore_errors=True)

    # The host code the four APIs share, and this vertical's package: the entrypoint
    # imports ``<vertical>.api.main``, exactly as `uvicorn --app-dir examples` does.
    copy(REPO_ROOT / "examples" / "demo_common", bundle / "examples" / "demo_common")
    copy(example / "__init__.py", bundle / "examples" / vertical / "__init__.py")
    copy(api_dir, bundle / "examples" / vertical / "api")
    copy(example / "data", bundle / "examples" / vertical / "data")

    # The skills each agent's prompt is assembled from, under the repository layout the
    # verticals compute them with (``REPO_ROOT / "<role>-agent" / "skills"``).
    for role in ("shopping-agent", "merchant-agent"):
        copy(REPO_ROOT / role / "skills", bundle / role / "skills")

    total = sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
    print(f"bundle_api: {vertical} bundle is {total / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()))
