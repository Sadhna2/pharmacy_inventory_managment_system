#!/usr/bin/env bash
# Project-local PostgreSQL control.
#
# Everything lives inside the repo (api/.pgdata) on a non-default port, so this
# never touches a system-wide Postgres and never runs at login.
#
#   ./scripts/db.sh start|stop|status|psql|reset

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGBIN="/opt/homebrew/opt/postgresql@16/bin"
PGDATA="$ROOT/api/.pgdata"
PGPORT=55432
PGSOCK=/tmp

export PATH="$PGBIN:$PATH"
export LC_ALL=C LANG=C   # macOS: postgres refuses to start without this

case "${1:-status}" in
  start)
    if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
      echo "already running on :$PGPORT"
    else
      pg_ctl -D "$PGDATA" -o "-p $PGPORT -k $PGSOCK" -l "$PGDATA/server.log" start
    fi
    ;;
  stop)
    pg_ctl -D "$PGDATA" stop
    ;;
  status)
    pg_ctl -D "$PGDATA" status || true
    ;;
  psql)
    shift
    psql -h "$PGSOCK" -p "$PGPORT" -U pharmacy -d pharmacy "$@"
    ;;
  reset)
    echo "Dropping and recreating the pharmacy database..."
    # A running API server holds pooled connections, and DROP DATABASE fails
    # while any session is attached. Terminate them first, and use ON_ERROR_STOP
    # so a failure is loud rather than silently leaving the old data in place.
    psql -h "$PGSOCK" -p "$PGPORT" -U pharmacy -d postgres -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
          WHERE datname = 'pharmacy' AND pid <> pg_backend_pid();" \
      -c "DROP DATABASE IF EXISTS pharmacy;" \
      -c "CREATE DATABASE pharmacy OWNER pharmacy;" >/dev/null
    echo "Done. Run: cd api && .venv/bin/alembic upgrade head"
    ;;
  *)
    echo "usage: $0 {start|stop|status|psql|reset}" >&2
    exit 1
    ;;
esac
