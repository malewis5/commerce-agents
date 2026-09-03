#!/usr/bin/env bash
# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
#
# The install command of a deployed example API (examples/<vertical>/vercel.json). Run
# from the API's service root, in the virtualenv the platform has already made.
#
# requirements.txt stays the one pin list: everything third-party is installed from the
# exact versions it names. What differs from a local install is which of the repository's
# own packages go in, and how.
#
#   * Five of the seven, not all: an example API is a host application on the Messages
#     API runtimes. The two Agent SDK runtimes are a different way to run the same agent,
#     with a CLI and its own dependency tree, and nothing here imports them.
#   * As built wheels rather than editable installs: a function bundle carries
#     site-packages, not the repository tree an editable install points back at. They are
#     still built from these directories and never from a package index, where the seven
#     names are unregistered.
#
# Their sibling dependencies are exact dev versions no index carries, which is why all
# five are named in one pip call: pip resolves each from the local path beside it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PINS="$(mktemp)"
trap 'rm -f "$PINS"' EXIT
grep -v '^-e ' requirements.txt > "$PINS"

echo "install-api: $(python3 -c 'import sys; print(sys.version.split()[0], sys.prefix)')"
pip install --no-cache-dir --disable-pip-version-check --constraint "$PINS" \
  "./commerce-common[examples]" \
  ./shopping-agent/core \
  ./shopping-agent/runtime-messages-api \
  ./merchant-agent/core \
  ./merchant-agent/runtime-messages-api
