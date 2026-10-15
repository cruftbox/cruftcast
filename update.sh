#!/bin/sh
# Pull and redeploy. Run this on the machine cruftcast is deployed to.
#
#     ./update.sh
#
# Adapted from porchwatch's update.sh. Safe to run over a non-interactive SSH
# session, which is why it does not use sudo: on the QNAP the user is already
# in the docker group, and sudo would sit waiting for a password.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

# Container Station keeps docker off the default PATH.
for candidate in /share/CACHEDEV1_DATA/.qpkg/container-station/bin; do
    [ -d "$candidate" ] && PATH="$candidate:$PATH"
done
export PATH

# The docker CLI wants a writable config directory and does not have one
# under Container Station. Without this, `docker compose build` fails with
# a permission error while `docker ps` works fine.
export DOCKER_CONFIG="${DOCKER_CONFIG:-$REPO/.docker}"
mkdir -p "$DOCKER_CONFIG"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found on PATH" >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "No .env here. Copy .env.example to .env and fill it in first." >&2
    exit 1
fi

mkdir -p data

echo "==> pulling"
git -c safe.directory="$REPO" pull --ff-only

echo "==> building $(git -c safe.directory="$REPO" rev-parse --short HEAD)"
docker compose build

echo "==> restarting"
docker compose up -d

echo "==> waiting for health"
for _ in $(seq 1 30); do
    status=$(docker inspect cruftcast --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
    case "$status" in
        healthy) echo "healthy"; exit 0 ;;
        unhealthy) echo "unhealthy -- check: docker compose logs" >&2; exit 1 ;;
    esac
    sleep 2
done

echo "still not healthy after 60s -- check: docker compose logs" >&2
exit 1
