#!/usr/bin/env bash
# Pick where the release image is published.
#
# Docker Hub when both the DOCKERHUB_USERNAME variable and the DOCKERHUB_TOKEN secret
# are set; otherwise GitHub Container Registry (ghcr.io), which needs no extra secrets.
# This avoids docker/login-action failing with "Username and password required".
#
# Env in:  GITHUB_REPOSITORY (owner/name), DOCKERHUB_USERNAME, DOCKERHUB_REPO (optional),
#          DOCKERHUB_TOKEN_SET ("true" when the secret is non-empty; never pass the secret itself)
# Out:     registry=<docker.io|ghcr.io> and image=<registry>/<namespace>/<name> in $GITHUB_OUTPUT
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
out="${GITHUB_OUTPUT:-/dev/stdout}"
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

repo="$(lower "$GITHUB_REPOSITORY")"
user="${DOCKERHUB_USERNAME:-}"

if [[ -n "$user" && "${DOCKERHUB_TOKEN_SET:-false}" == "true" ]]; then
  registry="docker.io"
  image="docker.io/$(lower "${DOCKERHUB_REPO:-$user/${repo#*/}}")"
else
  registry="ghcr.io"
  image="ghcr.io/$repo"
  missing=()
  [[ -n "$user" ]] || missing+=("DOCKERHUB_USERNAME variable")
  [[ "${DOCKERHUB_TOKEN_SET:-false}" == "true" ]] || missing+=("DOCKERHUB_TOKEN secret")
  printf '::notice title=Publishing to GHCR::Docker Hub not configured (missing %s); publishing to %s\n' \
    "$(IFS=,; echo "${missing[*]}" | sed 's/,/ and /')" "$image"
fi

{
  echo "registry=$registry"
  echo "image=$image"
} >> "$out"
