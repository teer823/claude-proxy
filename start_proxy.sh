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

podman run -d \
  --name "${CONTAINER_NAME}" \
  --env-file "${SCRIPT_DIR}/.env" \
  -p 8082:8082 \
  --restart unless-stopped \
  "${IMAGE_NAME}:${IMAGE_TAG}"

echo "Container '${CONTAINER_NAME}' started."
echo "Logs: podman logs -f ${CONTAINER_NAME}"
echo "Stop: podman stop ${CONTAINER_NAME}"