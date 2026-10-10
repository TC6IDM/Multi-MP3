#!/usr/bin/env bash
set -e

IMAGE_NAME="playlist-downloader"

# Build image (cached if unchanged)
docker build -t "$IMAGE_NAME" .

# Ensure downloads folder exists
mkdir -p downloads

# Use links.txt by default, or first arg if provided
INPUT_FILE="${1:-links.txt}"

# Forward additional args (e.g. --parallel) to the container
shift 2>/dev/null || true
EXTRA_ARGS=("$@")

docker run --rm \
  -v "$(pwd)/.spotdl:/root/.config/spotdl" \
  -v "$(pwd)/$INPUT_FILE:/app/input_links.txt:ro" \
  -v "$(pwd)/downloads:/app/music" \
  -v "$(pwd)/.env:/app/.env:ro" \
  "$IMAGE_NAME" \
  "input_links.txt" "/app/music" "${EXTRA_ARGS[@]}"
