#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="claude-proxy"

if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
  echo "Stopping container: ${CONTAINER_NAME}"
  podman stop "${CONTAINER_NAME}"
  echo "Container '${CONTAINER_NAME}' stopped."
else
  echo "No running container named '${CONTAINER_NAME}' found."
fi