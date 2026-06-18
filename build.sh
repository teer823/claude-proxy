#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="claude-proxy"
IMAGE_TAG="${1:-latest}"

echo "Building image: ${IMAGE_NAME}:${IMAGE_TAG}"

podman build \
  --tag "${IMAGE_NAME}:${IMAGE_TAG}" \
  --file Containerfile \
  .

echo "Build complete: ${IMAGE_NAME}:${IMAGE_TAG}"