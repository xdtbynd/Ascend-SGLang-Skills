#!/usr/bin/env bash
# =============================================================================
# run_verification.sh — Docker-based fix verification runner
#
# Usage:
#   bash run_verification.sh <verification_config.json> <workspace_dir>
#
# Steps:
#   1. Pull the pre-built Docker image
#   2. Construct and print the run command (auditability)
#   3. Execute in a clean container
#   4. Capture stdout, stderr, exit code, and duration
#
# Exit codes:
#   0 - Verification executed (check output/exit_code.txt for container result)
#   1 - Missing arguments
#   2 - Config file not found or invalid
#   3 - Docker not available
#   4 - Image pull failed
# =============================================================================

set -euo pipefail

# ── Argument parsing ─────────────────────────────────────────────────────────
CONFIG_FILE="${1:-}"
WORKSPACE_DIR="${2:-}"

if [[ -z "${CONFIG_FILE}" || -z "${WORKSPACE_DIR}" ]]; then
    echo "Usage: bash run_verification.sh <verification_config.json> <workspace_dir>"
    exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "ERROR: Config file not found: ${CONFIG_FILE}"
    exit 2
fi

# ── Find a usable Python ─────────────────────────────────────────────────────
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "${candidate}" &>/dev/null; then
        PYTHON_BIN="${candidate}"
        break
    fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: Neither python3 nor python found — need one to parse JSON config"
    exit 2
fi

# ── Workspace setup ──────────────────────────────────────────────────────────
mkdir -p "${WORKSPACE_DIR}/output"
mkdir -p "${WORKSPACE_DIR}/logs"

OUTPUT_LOG="${WORKSPACE_DIR}/output/docker_run.log"
EXIT_CODE_FILE="${WORKSPACE_DIR}/output/exit_code.txt"
DURATION_FILE="${WORKSPACE_DIR}/output/duration.txt"

# ── JSON helper ──────────────────────────────────────────────────────────────
_parse_json() {
    ${PYTHON_BIN} -c "import json,sys; print(json.load(open('${CONFIG_FILE}'))${1})"
}

REPO_URL="$(_parse_json "['repo_url']")"
BRANCH_OR_REF="$(_parse_json "['branch_or_ref']")"
DOCKER_IMAGE="$(_parse_json "['docker_image']")"
BUILD_COMMAND="$(_parse_json "['build_command']")"
TIMEOUT_SECONDS="$(_parse_json "['timeout_seconds']")"
GPU_REQUIRED="$(_parse_json "['gpu_required']")"

# ── Pre-flight checks ────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker is not available on this system"
    exit 3
fi

if ! docker info &>/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running or not accessible"
    exit 3
fi

# ── Pull the pre-built image ─────────────────────────────────────────────────
echo ""
echo "=== PULLING DOCKER IMAGE ==="
echo "Command: docker pull ${DOCKER_IMAGE}"
if ! docker pull "${DOCKER_IMAGE}"; then
    echo "ERROR: Failed to pull image: ${DOCKER_IMAGE}"
    echo "Check network connectivity and Docker Hub access."
    exit 4
fi
echo "=== IMAGE PULLED SUCCESSFULLY ==="
echo ""

# ── Check GPU availability ───────────────────────────────────────────────────
GPU_FLAG=""
if [[ "${GPU_REQUIRED}" == "True" || "${GPU_REQUIRED}" == "true" ]]; then
    if docker run --rm --gpus all "${DOCKER_IMAGE}" nvidia-smi &>/dev/null 2>&1; then
        GPU_FLAG="--gpus all"
        echo "[INFO] GPU runtime available — enabling --gpus all"
    else
        echo "[WARN] GPU required but nvidia-docker runtime not available"
        echo "[WARN] Proceeding without GPU — tests may fail"
    fi
fi

# ── Build environment variable flags ─────────────────────────────────────────
ENV_FLAGS=""
ENV_VARS="$(_parse_json "['env_vars']" 2>/dev/null || echo "{}")"
if [[ "${ENV_VARS}" != "{}" && "${ENV_VARS}" != "" ]]; then
    while IFS='=' read -r key value; do
        if [[ -n "${key}" ]]; then
            ENV_FLAGS="${ENV_FLAGS} -e ${key}=${value}"
        fi
    done < <(${PYTHON_BIN} -c "
import json, sys
env = json.load(open('${CONFIG_FILE}')).get('env_vars', {})
for k, v in env.items():
    print(f'{k}={v}')
" 2>/dev/null || true)
fi

# ── Resolve repo path ────────────────────────────────────────────────────────
REPO_PATH="$(pwd)"
REMOTE_URL="$(git remote get-url origin 2>/dev/null || echo "")"
if [[ -z "${REMOTE_URL}" ]]; then
    echo "[WARN] Current directory is not a git repo — will clone inside container"
    CLONE_FIRST="git clone ${REPO_URL} /workspace && cd /workspace && "
else
    CLONE_FIRST=""
fi

# ── Generate unique container name ───────────────────────────────────────────
CONTAINER_NAME="fix-verify-$(date +%s)-$$"

# ── Construct Docker command (no install — image has all deps) ───────────────
INNER_SCRIPT="${CLONE_FIRST}git fetch origin ${BRANCH_OR_REF} && git checkout ${BRANCH_OR_REF} && ${BUILD_COMMAND}"

DOCKER_CMD="docker run --rm --name ${CONTAINER_NAME} \
    ${GPU_FLAG} \
    -v ${REPO_PATH}:/workspace \
    -w /workspace \
    -e CI=true \
    ${ENV_FLAGS} \
    ${DOCKER_IMAGE} \
    bash -c \"${INNER_SCRIPT}\""

# ── Print the command (auditability) ─────────────────────────────────────────
echo "=== VERIFICATION DOCKER COMMAND ==="
echo "docker pull ${DOCKER_IMAGE}"
echo "${DOCKER_CMD}"
echo "=== END COMMAND ==="
echo ""

# Save to config for later reference
${PYTHON_BIN} -c "
import json
c = json.load(open('${CONFIG_FILE}'))
c['resolved_docker_command'] = 'docker pull ${DOCKER_IMAGE} && ${DOCKER_CMD}'
json.dump(c, open('${CONFIG_FILE}', 'w'), indent=2)
" 2>/dev/null || true

# ── Execute ──────────────────────────────────────────────────────────────────
START_TIME="$(date +%s.%N)"

set +e
if command -v timeout &>/dev/null; then
    timeout ${TIMEOUT_SECONDS} docker run --rm --name "${CONTAINER_NAME}" \
        ${GPU_FLAG} \
        -v "${REPO_PATH}":/workspace \
        -w /workspace \
        -e CI=true \
        ${ENV_FLAGS} \
        "${DOCKER_IMAGE}" \
        bash -c "${INNER_SCRIPT}" \
        > "${OUTPUT_LOG}" 2>&1
    EXIT_CODE=$?
else
    docker run --rm --name "${CONTAINER_NAME}" \
        ${GPU_FLAG} \
        -v "${REPO_PATH}":/workspace \
        -w /workspace \
        -e CI=true \
        ${ENV_FLAGS} \
        "${DOCKER_IMAGE}" \
        bash -c "${INNER_SCRIPT}" \
        > "${OUTPUT_LOG}" 2>&1 &
    DOCKER_PID=$!

    ELAPSED=0
    while kill -0 ${DOCKER_PID} 2>/dev/null; do
        sleep 1
        ELAPSED=$((ELAPSED + 1))
        if [[ ${ELAPSED} -ge ${TIMEOUT_SECONDS} ]]; then
            echo "[TIMEOUT] Killing container ${CONTAINER_NAME} after ${TIMEOUT_SECONDS}s"
            docker kill "${CONTAINER_NAME}" 2>/dev/null || true
            EXIT_CODE=124
            break
        fi
    done
    if [[ -z "${EXIT_CODE:-}" ]]; then
        wait ${DOCKER_PID}
        EXIT_CODE=$?
    fi
fi
set -e

END_TIME="$(date +%s.%N)"

# ── Calculate duration ───────────────────────────────────────────────────────
DURATION="$(${PYTHON_BIN} -c "print(${END_TIME} - ${START_TIME})")"

# ── Handle timeout ───────────────────────────────────────────────────────────
if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "TIMEOUT" > "${EXIT_CODE_FILE}"
    echo "[TIMEOUT] Verification timed out after ${TIMEOUT_SECONDS}s"
else
    echo "${EXIT_CODE}" > "${EXIT_CODE_FILE}"
fi

echo "${DURATION}" > "${DURATION_FILE}"

# ── Ensure container is cleaned up ───────────────────────────────────────────
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== VERIFICATION COMPLETE ==="
echo "Exit code: $(cat ${EXIT_CODE_FILE})"
echo "Duration:  ${DURATION}s"
echo "Full log:  ${OUTPUT_LOG}"
echo ""

exit 0
