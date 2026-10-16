#!/usr/bin/env bash
#
# run-tests.sh — Canonical backend pytest/ruff runner.
#
# Runs the backend test suite (or any backend command) in a throwaway container
# built from the backend dev image, mounting THIS worktree's code and the host
# docker socket. Use it from any worktree or subdirectory.
#
# Why a one-off `docker run` instead of `docker compose exec backend ...`:
#   - The operator may have `docker compose up` running against the MAIN
#     checkout, so `exec` would test the main code, not your worktree, and a
#     second `up` from a worktree collides on host ports 5434/8010/5180.
#   - This script publishes NO ports and bind-mounts the worktree resolved via
#     `git rev-parse --show-toplevel` (always the right code).
#   - It runs as root (-u 0:0) and mounts /var/run/docker.sock so testcontainers
#     (backend/tests/conftest.py) can spawn its ephemeral Postgres 17. Dev-only.
#
# Usage:
#   scripts/run-tests.sh                                   # full suite (pytest -q)
#   scripts/run-tests.sh pytest tests/test_health.py -q    # one file
#   scripts/run-tests.sh ruff check app tests alembic      # lint (CI gate)
#   scripts/run-tests.sh ruff format --check app tests alembic
#   scripts/run-tests.sh --build [...]                     # rebuild image first
#   scripts/run-tests.sh -h | --help
#
# Image: ${N16_TEST_IMAGE:-n16chat-backend-test}, built from backend/ when
# missing. It only supplies the Python environment (your code is bind-mounted),
# so a stale image is fine UNLESS backend/requirements.txt changed: pass --build.
#
# Empty-mount guard: bind mounts are resolved by the DOCKER DAEMON's
# filesystem. If the repo path exists only in the calling environment (e.g. a
# sandbox the daemon cannot see), docker silently mounts empty directories; the
# in-container check below refuses to run in that case (exit 2).

set -euo pipefail

usage() {
  sed -n '3,33p' "$0" | sed 's/^# \{0,1\}//'
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
IMAGE="${N16_TEST_IMAGE:-n16chat-backend-test}"
FORCE_BUILD=0

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--build" ]]; then
  FORCE_BUILD=1
  shift
fi

if [[ "$FORCE_BUILD" -eq 1 ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo ">> Building $IMAGE from $REPO_ROOT/backend ..." >&2
  docker build -q -t "$IMAGE" "$REPO_ROOT/backend" >/dev/null
fi

# No args → quiet full suite.
if [[ "$#" -eq 0 ]]; then
  set -- pytest -q
fi

MOUNT_GUARD='
for d in /app/app /app/tests /app/alembic; do
  if [ -z "$(ls -A "$d" 2>/dev/null)" ]; then
    echo "ERROR: run-tests.sh: $d is EMPTY inside the container." >&2
    echo "The docker daemon resolved the bind mounts against ITS filesystem and" >&2
    echo "found nothing at the repo path. Refusing to run against an empty tree." >&2
    exit 2
  fi
done
exec "$@"'

exec docker run --rm -u 0:0 \
  -v "$REPO_ROOT/backend/app:/app/app" \
  -v "$REPO_ROOT/backend/tests:/app/tests" \
  -v "$REPO_ROOT/backend/alembic:/app/alembic" \
  -v "$REPO_ROOT/backend/scripts:/app/scripts" \
  -v "$REPO_ROOT/backend/alembic.ini:/app/alembic.ini:ro" \
  -v "$REPO_ROOT/backend/pyproject.toml:/app/pyproject.toml:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e ENVIRONMENT=test \
  -e DATABASE_URL=postgresql://test:test@localhost:1/none \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -w /app "$IMAGE" sh -c "$MOUNT_GUARD" mount-guard "$@"
