#!/usr/bin/env bash
# Mnemos — nightly Postgres backup.
#
# Usage: ./scripts/backup.sh [backup_dir]
#   default backup_dir: ./backups
#
# Emits one compressed custom-format dump per run, prunes files older
# than MNEMOS_BACKUP_RETENTION_DAYS (default 14). Safe to run from cron
# or a systemd timer. The script never touches Redis (ephemeral queue
# state) or the ``platform_data`` volume (rebuildable repo checkouts).
#
# Exit codes:
#   0 — backup succeeded
#   1 — required tooling missing
#   2 — pg_dump failed (dump file removed)
#
set -euo pipefail

backup_dir="${1:-./backups}"
retention_days="${MNEMOS_BACKUP_RETENTION_DAYS:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
outfile="${backup_dir}/mnemos-${stamp}.pgdump"

for tool in docker pg_dump; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    # pg_dump is only needed if we're not running through docker; docker
    # exec is the preferred path.
    if [ "${tool}" = "pg_dump" ] && command -v docker >/dev/null 2>&1; then
      continue
    fi
    echo "missing required tool: ${tool}" >&2
    exit 1
  fi
done

mkdir -p "${backup_dir}"

echo "$(date -Iseconds) backup start -> ${outfile}" >&2

if docker compose ps postgres >/dev/null 2>&1; then
  docker compose exec -T postgres pg_dump \
    -U "${POSTGRES_USER:-mnemos}" \
    -d "${POSTGRES_DB:-mnemos}" \
    -Fc \
    > "${outfile}" || {
      rc=$?
      rm -f "${outfile}"
      echo "pg_dump failed (rc=${rc})" >&2
      exit 2
    }
else
  PGPASSWORD="${POSTGRES_PASSWORD:-mnemos}" pg_dump \
    -h "${POSTGRES_HOST:-localhost}" \
    -U "${POSTGRES_USER:-mnemos}" \
    -d "${POSTGRES_DB:-mnemos}" \
    -Fc \
    > "${outfile}" || {
      rc=$?
      rm -f "${outfile}"
      echo "pg_dump failed (rc=${rc})" >&2
      exit 2
    }
fi

size_bytes="$(stat -c%s "${outfile}" 2>/dev/null || stat -f%z "${outfile}")"
echo "$(date -Iseconds) backup done size=${size_bytes}B" >&2

# Prune old backups.
if [ -d "${backup_dir}" ]; then
  find "${backup_dir}" -maxdepth 1 -type f -name 'mnemos-*.pgdump' \
    -mtime "+${retention_days}" -print -delete >&2 || true
fi
