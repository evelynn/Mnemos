#!/usr/bin/env bash
# Mnemos — Postgres restore from a pg_dump custom-format backup.
#
# Usage: ./scripts/restore.sh <backup-file>
#
# Refuses to run unless MNEMOS_RESTORE_CONFIRM=yes, because the restore
# drops and recreates the Mnemos database. Reviews existing migrations
# state after restore so the operator can confirm the schema is at the
# expected revision.
#
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <backup-file.pgdump>" >&2
  exit 2
fi

backup_file="$1"
if [ ! -f "${backup_file}" ]; then
  echo "backup file not found: ${backup_file}" >&2
  exit 2
fi

if [ "${MNEMOS_RESTORE_CONFIRM:-}" != "yes" ]; then
  cat >&2 <<EOF
Refusing to run: set MNEMOS_RESTORE_CONFIRM=yes to proceed.
This script DROPS and recreates database '${POSTGRES_DB:-mnemos}'.
EOF
  exit 3
fi

echo "$(date -Iseconds) stopping platform + worker to quiesce DB writes" >&2
docker compose stop platform worker >/dev/null 2>&1 || true

echo "$(date -Iseconds) dropping + recreating database" >&2
docker compose exec -T postgres psql -U "${POSTGRES_USER:-mnemos}" -d postgres <<SQL
DROP DATABASE IF EXISTS ${POSTGRES_DB:-mnemos};
CREATE DATABASE ${POSTGRES_DB:-mnemos};
SQL

echo "$(date -Iseconds) restoring from ${backup_file}" >&2
docker compose exec -T postgres pg_restore \
  --clean --if-exists \
  -U "${POSTGRES_USER:-mnemos}" \
  -d "${POSTGRES_DB:-mnemos}" \
  --no-owner --no-privileges \
  < "${backup_file}"

echo "$(date -Iseconds) restarting platform + worker" >&2
docker compose start platform worker

echo "$(date -Iseconds) alembic current:" >&2
docker compose exec -T platform alembic current || true

echo "$(date -Iseconds) restore complete" >&2
