#!/usr/bin/env bash
# Run the whole suite the way CI runs it, against a database of its own.
#
#   ./scripts/test.sh                 # everything: backend + frontend
#   ./scripts/test.sh tests/test_e2e.py -k recall    # args go to pytest
#   ./scripts/test.sh --keep          # leave the container up to poke at
#   ./scripts/test.sh --backend       # skip the frontend checks
#
# WHY THIS EXISTS
# ---------------
# The suite is end-to-end on purpose. It exercises the append-only trigger, the
# balance projection and the row locks under concurrent allocation, and none of
# those survive being mocked — so it needs a real Postgres and a real server on
# the other end of a real socket.
#
# Which was fine, except that "a real server" meant *your* server, pointed at
# the demo database. Every run therefore raised a dozen purchase orders on the
# Purchasing screen, and one test recalls a batch — which quarantines stock and
# then scraps it, through a ledger that refuses UPDATE and DELETE by trigger.
# That last part cannot be tidied up afterwards by anyone, which is the whole
# point of an append-only ledger. So the demo data drifted a little further
# from the truth on every run, and the only fix was to rebuild it.
#
# CI never had this problem: it starts a throwaway `postgres:16`, migrates it
# from scratch, seeds it, tests it, and throws it away. This script is that,
# on your machine. Same image, same steps, same order. After it finishes the
# container is gone and your `pharmacy` database has not been opened.
#
# It is deliberately not a faster local variant — no reusing the cluster you
# already have, no skipping the seed. A local run that differs from CI is a
# local run that can pass while CI fails, and then the two of you are debugging
# the difference between the environments instead of the code.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Matching CI: same image tag, same credentials, same port. The container name
# is fixed so a run interrupted with Ctrl-C can be cleaned up by the next one
# rather than leaving something behind holding the port.
CONTAINER="${TEST_DB_CONTAINER:-sadhna-test-db}"
IMAGE="postgres:16"
DB_PORT="${TEST_DB_PORT:-5432}"

# Not CI's 8000. Your development API is already there, and taking the port
# from under it — or refusing to run until you stop it — would make this
# annoying enough to skip, which defeats the purpose. Nothing else about the
# run differs; the suite reaches the server through API_BASE either way.
API_PORT="${TEST_API_PORT:-8001}"

KEEP=0
FRONTEND=1
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --keep)     KEEP=1 ;;
    --backend)  FRONTEND=0 ;;
    *)          PYTEST_ARGS+=("$arg") ;;
  esac
done

say() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- guard rails

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

# The one thing this script must never do. If DATABASE_URL is inherited from a
# shell that was pointed at the demo cluster, every "throwaway" write lands in
# the database this exists to protect — silently, and looking like a pass.
unset DATABASE_URL PGDATABASE PGSERVICE

if lsof -ti:"$DB_PORT" -sTCP:LISTEN >/dev/null 2>&1 \
   && [ -z "$(docker ps -q -f "name=^${CONTAINER}$")" ]; then
  echo "Port $DB_PORT is already in use by something that is not our test" >&2
  echo "container. Free it, or set TEST_DB_PORT to another port." >&2
  exit 1
fi

# The API port matters more than it looks. The readiness check below polls
# /health/ready and cannot tell our server from anyone else's — so a stale
# uvicorn left on this port from a previous session answers the poll, the run
# proceeds, and the entire suite silently tests whatever that process is
# serving. That happened: a day-old server was still on 8001, our own failed
# to bind, and the schema check compared the committed types against a build
# from yesterday. Refuse instead.
if lsof -ti:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $API_PORT is already in use. Something is listening there —" >&2
  echo "possibly a uvicorn left over from an earlier run:" >&2
  lsof -ti:"$API_PORT" -sTCP:LISTEN | sed 's/^/  pid /' >&2
  echo "Stop it, or set TEST_API_PORT to another port." >&2
  exit 1
fi

# ------------------------------------------------------------------- teardown

cleanup() {
  local code=$?
  if [ -n "${API_PID:-}" ]; then
    kill "$API_PID" 2>/dev/null || true
    # Wait for the port to actually clear. Returning while uvicorn is still
    # shutting down means the next run trips the guard above on our own
    # leftovers.
    for _ in $(seq 1 10); do
      lsof -ti:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
      sleep 0.3
    done
    kill -9 "$API_PID" 2>/dev/null || true
  fi
  if [ "$KEEP" -eq 1 ]; then
    printf '\nLeft running (--keep):\n'
    printf '  database  postgresql://pharmacy:pharmacy@localhost:%s/pharmacy\n' "$DB_PORT"
    printf '  remove it  docker rm -f %s\n' "$CONTAINER"
  else
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  exit $code
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------------- database

say "Starting a throwaway $IMAGE"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_USER=pharmacy \
  -e POSTGRES_PASSWORD=pharmacy \
  -e POSTGRES_DB=pharmacy \
  -p "${DB_PORT}:5432" \
  --health-cmd "pg_isready -U pharmacy" \
  --health-interval 2s --health-timeout 5s --health-retries 15 \
  "$IMAGE" >/dev/null

printf '  waiting'
for _ in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" = "healthy" ]; then
    printf ' ready\n'
    break
  fi
  printf '.'
  sleep 1
done
if [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" != "healthy" ]; then
  printf '\n'
  echo "The database never became healthy. Its log:" >&2
  docker logs "$CONTAINER" 2>&1 | tail -30 >&2
  exit 1
fi

# Everything below talks to the container and nothing else. PGHOST and PGPORT
# are here because a handful of tests read the audit log straight from the
# database through scripts/db.sh, whose defaults point at the local cluster.
export DATABASE_URL="postgresql+psycopg://pharmacy:pharmacy@localhost:${DB_PORT}/pharmacy"
export SECRET_KEY="test-only-not-a-real-key"
export SEED_PASSWORD="CiTestPassword@2026"
export ENV=development
export PGHOST=localhost
export PGPORT="$DB_PORT"
export PGPASSWORD=pharmacy
export API_BASE="http://127.0.0.1:${API_PORT}"

VENV="$ROOT/api/.venv/bin"
if [ ! -x "$VENV/python" ]; then
  echo "No virtualenv at api/.venv — see the README for setup." >&2
  exit 1
fi

# --------------------------------------------------------------------- checks

say "Lint"
(cd "$ROOT/api" && "$VENV/ruff" check .)

# From scratch every time, which is what catches a migration that only works
# because your own database already had the column.
say "Migrations"
(cd "$ROOT/api" && "$VENV/alembic" upgrade head)

# The same command the container start-up and the README use. A different
# subset here would mean the suite passes against a dataset that exists
# nowhere else — which is how the Layer 2 tests once skipped themselves and
# the run reported green having checked none of them.
say "Seeding (a couple of minutes)"
(cd "$ROOT/api" && "$VENV/python" -m app.seed.demo --days 730 >/dev/null)

say "Starting the API on :$API_PORT"
API_LOG="$(mktemp -t sadhna-test-api)"
# `exec`, so $! is uvicorn itself. Without it $! is the subshell wrapping it,
# the trap kills the wrapper, and uvicorn survives the run — which is how the
# stale process described above came to exist in the first place.
(cd "$ROOT/api" && exec "$VENV/uvicorn" app.main:app \
  --port "$API_PORT" --log-level warning > "$API_LOG" 2>&1) &
API_PID=$!
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${API_PORT}/health/ready" >/dev/null && break
  sleep 1
done
if ! curl -sf "http://127.0.0.1:${API_PORT}/health/ready" >/dev/null; then
  echo "The API never became ready. Its log:" >&2
  tail -30 "$API_LOG" >&2
  exit 1
fi

# The browser's types are generated from this document, so a server-side rename
# has to reach the committed copy or the two describe different APIs while both
# compile happily.
say "API types are in sync with the schema"
npx --yes openapi-typescript@7.13.0 \
  "http://127.0.0.1:${API_PORT}/openapi.json" -o /tmp/schema.fresh.d.ts >/dev/null 2>&1
if ! diff -q /tmp/schema.fresh.d.ts "$ROOT/web/src/lib/schema.d.ts" >/dev/null; then
  echo "web/src/lib/schema.d.ts is stale — run 'npm run gen:api' in web/" >&2
  diff -u "$ROOT/web/src/lib/schema.d.ts" /tmp/schema.fresh.d.ts | head -40 >&2
  exit 1
fi
echo "  matches"

say "Backend tests"
set +e
# `${a[@]+"${a[@]}"}` rather than plain `"${a[@]}"`: macOS ships bash 3.2,
# where expanding an empty array under `set -u` is an unbound-variable error.
# It aborted the run before pytest started and reported it as a test failure.
(cd "$ROOT/api" && "$VENV/pytest" -q -rf --durations=10 \
  ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"})
BACKEND=$?
set -e

if [ "$BACKEND" -ne 0 ]; then
  say "API log"
  tail -40 "$API_LOG"
fi

FRONT=0
if [ "$FRONTEND" -eq 1 ] && [ ${#PYTEST_ARGS[@]} -eq 0 ]; then
  say "Frontend — lint, typecheck, build"
  set +e
  (cd "$ROOT/web" && npm run lint && npm run build)
  FRONT=$?
  set -e
fi

# ------------------------------------------------------------------- verdict

say "Result"
[ "$BACKEND" -eq 0 ] && echo "  backend   pass" || echo "  backend   FAIL"
if [ "$FRONTEND" -eq 1 ] && [ ${#PYTEST_ARGS[@]} -eq 0 ]; then
  [ "$FRONT" -eq 0 ] && echo "  frontend  pass" || echo "  frontend  FAIL"
fi
echo "  your demo database was not touched"

[ "$BACKEND" -eq 0 ] && [ "$FRONT" -eq 0 ]
