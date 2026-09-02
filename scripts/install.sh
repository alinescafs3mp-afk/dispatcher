#!/usr/bin/env bash
set -euo pipefail

repo_path="${1:-}"
if [[ -z "$repo_path" ]]; then
  printf 'usage: %s /absolute/path/to/friday\n' "$0" >&2
  exit 2
fi

python_bin="${PYTHON:-python3}"
"$python_bin" -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/nightshift init --repo "$repo_path" --force

printf '\nInstalled. Run:\n  source .venv/bin/activate\n  nightshift doctor\n  nightshift serve\n'
