#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="claude-proxy"
IMAGE_TAG="${1:-latest}"
CONTAINER_NAME="claude-proxy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Remove any existing container with the same name
if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
  echo "Removing existing container: ${CONTAINER_NAME}"
  podman rm -f "${CONTAINER_NAME}"
fi

echo "Starting Claude Code Proxy container on port 8082..."

# Host directory where the container's log files are persisted
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# --userns=keep-id maps the host user to appuser (uid/gid 1000) inside the
# container so it can write to the mounted logs folder and files stay owned
# by the host user. DEBUG_LOG_DIR is forced to the mount point (overrides .env).
podman run -d \
  --name "${CONTAINER_NAME}" \
  --env-file "${SCRIPT_DIR}/.env" \
  -e DEBUG_LOG_DIR=/app/logs \
  -v "${LOG_DIR}:/app/logs" \
  -v "${SCRIPT_DIR}/model_routing.json:/app/model_routing.json:ro" \
  --userns=keep-id:uid=1000,gid=1000 \
  -p 8082:8082 \
  --restart unless-stopped \
  "${IMAGE_NAME}:${IMAGE_TAG}"

echo "Container '${CONTAINER_NAME}' started."
echo "Logs: podman logs -f ${CONTAINER_NAME}"
echo "Log files: ${LOG_DIR}"
echo "Stop: podman stop ${CONTAINER_NAME}"