#!/usr/bin/env bash
set -euo pipefail
umask 077
project_root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
project_name=dsm-hindsight-it2
generation_model=${IT2_GENERATION_MODEL-gemma4-12b}
required_vram_mib=${IT2_REQUIRED_VRAM_MIB-11000}
artifact_dir=$(realpath -m -- "${IT2_ARTIFACT_DIR:-$project_root/.it2-artifacts/$(date -u +%Y%m%dT%H%M%SZ)-$$}")
export IT2_GENERATION_MODEL=$generation_model
export IT2_ARTIFACT_DIR=$artifact_dir
test_pid=

fail() { printf '%s\n' "$1" >&2; exit 1; }
case "$generation_model" in
    gemma4-12b|gpt-6-luna) ;;
    *) fail 'Unsupported IT2 generation model' ;;
esac
[[ "$required_vram_mib" =~ ^[1-9][0-9]*$ ]] || fail 'Invalid required VRAM'

# The default is shared across worktrees; tests use an isolated lock file.
exec 9>"${IT2_LOCK_FILE:-/tmp/dsm-hindsight-it2.lock}"
flock 9
[[ -n ${IT2_MODEL_DIR:-} && -d $IT2_MODEL_DIR ]] || fail 'Model directory is required'
IT2_MODEL_DIR=$(realpath -- "$IT2_MODEL_DIR")
export IT2_MODEL_DIR

verify_model() {
    local filename=$1 expected=$2 actual
    [[ -f "$IT2_MODEL_DIR/$filename" && -r "$IT2_MODEL_DIR/$filename" ]] || fail 'Model file is missing or unreadable'
    actual=$(sha256sum -- "$IT2_MODEL_DIR/$filename" 2>/dev/null) || fail 'Model hash could not be read'
    [[ ${actual%% *} == "$expected" ]] || fail 'Model sha256 mismatch'
}
verify_model gemma4-12b.gguf 1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606
verify_model bge-m3-q8_0.gguf aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173
verify_model bge-reranker-v2-m3-Q8_0.gguf a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3

compose=(docker compose --project-name "$project_name" -f "$project_root/compose.it2.yml")
if [[ $generation_model == gpt-6-luna ]]; then
    [[ -n ${IT2_CODEX_HOME:-} && -d $IT2_CODEX_HOME ]] || fail 'Persistent credentials directory is required'
    IT2_CODEX_HOME=$(realpath -- "$IT2_CODEX_HOME")
    [[ -w $IT2_CODEX_HOME && -r $IT2_CODEX_HOME/auth.json ]] || fail 'Credentials directory must be writable and contain auth.json'
    case "$IT2_CODEX_HOME/" in
        "$project_root/"*) fail 'Credentials directory must be outside the repository' ;;
        "$artifact_dir/"*) fail 'Credentials directory must be outside the artifacts' ;;
        /tmp/*|/dev/shm/*|/run/*) fail 'Credentials directory must be persistent' ;;
    esac
    export IT2_CODEX_HOME
    compose+=(-f "$project_root/compose.it2.gpt-6-luna.yml")
fi

"${compose[@]}" down --volumes --remove-orphans
gpu_free=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits 2>/dev/null) || fail 'GPU memory is unavailable'
max_free=-1
selected_gpu=
while IFS=, read -r index free; do
    index=${index//[[:space:]]/}
    free=${free//[[:space:]]/}
    [[ $index =~ ^[0-9]+$ && $free =~ ^[0-9]+$ ]] || fail 'Invalid GPU memory result'
    if ((10#$free > max_free)); then max_free=$((10#$free)); selected_gpu=$index; fi
done <<< "$gpu_free"
((max_free >= required_vram_mib)) || fail 'Insufficient free VRAM; nothing was started'
export IT2_GPU_DEVICE=$selected_gpu
mkdir -p "$artifact_dir"

cleanup() {
    local status=$? cleanup_status
    trap - EXIT
    set +e
    if [[ -n $test_pid ]]; then kill -TERM "$test_pid" 2>/dev/null; fi
    # Hindsight logs and host diagnostics are deliberately not collected.
    "${compose[@]}" down --volumes --remove-orphans
    cleanup_status=$?
    if [[ -n $test_pid ]]; then
        kill -KILL "$test_pid" 2>/dev/null
        wait "$test_pid" 2>/dev/null
    fi
    if ((status == 0 && cleanup_status != 0)); then status=$cleanup_status; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" build postgres service
"${compose[@]}" pull hindsight llama-generation llama-embedding llama-reranker
"${compose[@]}" up --detach --wait --wait-timeout 600 postgres llama-generation llama-embedding llama-reranker hindsight service
gpu_usage=$(nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null) || fail 'GPU usage could not be recorded'
while IFS=, read -r index used total; do
    index=${index//[[:space:]]/}
    used=${used//[[:space:]]/}
    total=${total//[[:space:]]/}
    [[ $index =~ ^[0-9]+$ && $used =~ ^[0-9]+$ && $total =~ ^[0-9]+$ ]] || fail 'Invalid GPU usage result'
done <<< "$gpu_usage"
printf '%s\n' "$gpu_usage" > "$artifact_dir/gpu-usage.csv"

check_llama() {
    local exited
    exited=$("${compose[@]}" ps --all --status exited --quiet llama-generation llama-embedding llama-reranker) || return 1
    [[ -z $exited ]]
}
"${compose[@]}" run --rm --no-deps tests uv run --no-sync pytest -m it2 \
    --allow-hosts=postgres,hindsight,service,llama-generation,llama-embedding,llama-reranker -q "$@" &
test_pid=$!
while kill -0 "$test_pid" 2>/dev/null; do
    check_llama || fail 'A llama service exited (including OOM); IT2 failed'
    sleep 1
done
set +e
wait "$test_pid"
test_status=$?
set -e
test_pid=
check_llama || fail 'A llama service exited (including OOM); IT2 failed'
exit "$test_status"
