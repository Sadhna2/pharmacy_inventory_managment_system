#!/usr/bin/env bash
# Project-local PostgreSQL control.
#
# Everything lives inside the repo (api/.pgdata) on a non-default port, so this
# never touches a system-wide Postgres and never runs at login.
#
#   ./scripts/db.sh start|stop|status|psql|reset

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="$ROOT/api/.pgdata"

# The defaults describe a developer's machine. CI overrides them, because the
# test suite reaches the database through this script and a Linux runner has
# neither Homebrew nor the local cluster — its Postgres is a service container
# on TCP 5432 with no socket directory to share.
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
PGPORT="${PGPORT:-55432}"
PGSOCK="${PGSOCK:-/tmp}"
# A directory here means a unix socket; a hostname means TCP.
PGHOST="${PGHOST:-$PGSOCK}"

export PATH="$PGBIN:$PATH"
export LC_ALL=C LANG=C   # macOS: postgres refuses to start without this

case "${1:-status}" in
  # First run on a new clone. `api/.pgdata` is gitignored — a cluster is a
  # database, not source — so a fresh checkout has nothing for `start` to
  # start. Without this the documented native path failed on the first
  # command, at `pg_ctl: directory "api/.pgdata" does not exist`, which reads
  # like a broken repository rather than a step nobody had written down.
  init)
    if [ -d "$PGDATA" ]; then
      echo "already initialised at $PGDATA — run '$0 start'"
      exit 0
    fi
    command -v initdb >/dev/null 2>&1 || {
      echo "initdb not found. Install PostgreSQL 16 first:" >&2
      echo "  macOS:  brew install postgresql@16" >&2
      echo "  Linux:  sudo apt install postgresql-16" >&2
      echo "Or skip all of this and use Docker: docker compose up" >&2
      exit 1
    }
    # --auth=trust is safe here and only here: the cluster listens on a unix
    # socket inside the repo, on a non-default port, and is never exposed.
    #
    # --encoding=UTF8 is not optional. This script exports LC_ALL=C (macOS
    # postgres refuses to start without it), and under a C locale initdb
    # defaults the cluster to SQL_ASCII. psycopg then returns bytes rather
    # than str for server strings, and the first thing SQLAlchemy does with a
    # new connection is regex the version banner — so every command dies at
    # "cannot use a string pattern on a bytes-like object", a message that
    # says nothing whatsoever about database encoding.
    initdb -D "$PGDATA" -U pharmacy --auth=trust \
      --encoding=UTF8 --locale=C >/dev/null
    pg_ctl -D "$PGDATA" -o "-p $PGPORT -k $PGSOCK" -l "$PGDATA/server.log" start
    createdb -h "$PGHOST" -p "$PGPORT" -U pharmacy pharmacy
    echo "initialised and running on :$PGPORT"
    echo "next: cd api && .venv/bin/alembic upgrade head"
    ;;
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
    psql -h "$PGHOST" -p "$PGPORT" -U pharmacy -d pharmacy "$@"
    ;;
  reset)
    echo "Dropping and recreating the pharmacy database..."
    # A running API server holds pooled connections, and DROP DATABASE fails
    # while any session is attached. Terminate them first, and use ON_ERROR_STOP
    # so a failure is loud rather than silently leaving the old data in place.
    psql -h "$PGHOST" -p "$PGPORT" -U pharmacy -d postgres -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
          WHERE datname = 'pharmacy' AND pid <> pg_backend_pid();" \
      -c "DROP DATABASE IF EXISTS pharmacy;" \
      -c "CREATE DATABASE pharmacy OWNER pharmacy;" >/dev/null
    echo "Done. Run: cd api && .venv/bin/alembic upgrade head"
    ;;
  *)
    echo "usage: $0 {init|start|stop|status|psql|reset}" >&2
    exit 1
    ;;
esac
