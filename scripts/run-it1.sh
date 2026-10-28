#!/usr/bin/env bash
set -euo pipefail
project_root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
project_name="dsm-it1-$(date +%s)-$$"
artifact_dir="${IT1_ARTIFACT_DIR:-$project_root/.it1-artifacts/$project_name}"
mkdir -p "$artifact_dir"
compose=(docker compose --project-name "$project_name" -f "$project_root/compose.it1.yml")

cleanup() {
    status=$?
    trap - EXIT
    set +e
    "${compose[@]}" logs --no-color > "$artifact_dir/compose.log" 2>&1
    log_status=$?
    if ((status != 0)); then
        cat "$artifact_dir/compose.log"
    fi
    "${compose[@]}" down --volumes --remove-orphans
    cleanup_status=$?
    if ((status == 0 && log_status != 0)); then status=$log_status; fi
    if ((status == 0 && cleanup_status != 0)); then status=$cleanup_status; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" build postgres stub service
"${compose[@]}" pull hindsight
"${compose[@]}" up --detach --wait --wait-timeout 180 postgres stub hindsight service
# Hindsight can pass /health before it logs that it is running, so poll the log.
ready_deadline=$((SECONDS + ${IT1_HINDSIGHT_READY_TIMEOUT:-60}))
until "${compose[@]}" logs --no-color hindsight > "$artifact_dir/hindsight-startup.log" &&
    grep -Fq 'Hindsight is running' "$artifact_dir/hindsight-startup.log"; do
    if ((SECONDS >= ready_deadline)); then
        cat "$artifact_dir/hindsight-startup.log"
        exit 1
    fi
    sleep 1
done
"${compose[@]}" run --rm --no-deps tests uv run --no-sync pytest -m it1 \
    --allow-hosts=postgres,hindsight,service -q "$@"
