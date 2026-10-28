#!/usr/bin/env bash
set -euo pipefail
project_root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
export PYTHONPATH="$project_root/src"
exec uv run --project "$project_root" --no-sync uvicorn digital_souls_memory_hindsight.app:app --host 0.0.0.0 --port 8000
