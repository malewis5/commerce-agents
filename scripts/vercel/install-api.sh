#!/usr/bin/env bash
# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
#
# The install command of a deployed example API (examples/<vertical>/vercel.json). Run
# from the API's service root; the platform has already made a virtualenv and put its pip
# on PATH.
#
# requirements.txt stays the one pin list. Its seven editable lines are this repository's
# own packages, and a function bundle carries site-packages rather than the repository
# tree, so they go in as ordinary wheels built from those same directories — never from a
# package index, where the names are unregistered (--no-deps, because the pin file above
# has already installed everything they need, and their sibling pins are dev versions no
# index carries).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PINS="$(mktemp)"
trap 'rm -f "$PINS"' EXIT
grep -v '^-e ' requirements.txt > "$PINS"

LOCAL=()
while IFS= read -r line; do
  LOCAL+=("$line")
done < <(sed -n 's/^-e  *//p' requirements.txt)

echo "install-api: $(python3 -c 'import sys; print(sys.executable)')"
pip install --no-cache-dir --disable-pip-version-check -r "$PINS"
pip install --no-cache-dir --disable-pip-version-check --no-deps "${LOCAL[@]}"
