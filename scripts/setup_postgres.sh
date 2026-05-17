#!/usr/bin/env bash
# Bring up a local Postgres 16 + pgvector and an empty `memlayer` db, so
# scripts/e2e_test.py can run. Idempotent-ish; safe to re-run after a reset.
# Mirrors the manual steps used to verify Phases 0-3.
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/pgdata

command -v "$PGBIN/initdb" >/dev/null || {
  apt-get install -y postgresql-16 postgresql-16-pgvector
}

id postgres >/dev/null 2>&1 || useradd -m postgres
mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  su postgres -c "$PGBIN/initdb -D $PGDATA -A trust -U postgres"
fi

su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $PGDATA/log -w start" || true
su postgres -c "$PGBIN/createdb -U postgres memlayer" 2>/dev/null || true

"$PGBIN/pg_isready"
echo "memlayer db ready -- set DATABASE_URL=postgresql://postgres@localhost:5432/memlayer"
