#!/usr/bin/env bash
# Argo custom-lint entrypoint. Sets up a venv, installs deps, runs checks.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv lint-venv
# shellcheck disable=SC1091
source lint-venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet pyyaml sqlfluff

python3 scripts/custom_sql_checks.py
