#!/bin/sh
set -eu
backup_dir="${1:?Usage: scripts/restore.sh backup-directory}"
[ -s "$backup_dir/database.dump" ] && [ -s "$backup_dir/private-media.tar.gz" ]
echo 'Восстановление заменяет текущую БД. Остановите запись; сначала сделайте резервную копию.'
[ "${CONFIRM_RESTORE:-}" = YES ] || { echo 'Укажите CONFIRM_RESTORE=YES'; exit 1; }
docker compose stop web worker beat
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction' < "$backup_dir/database.dump"
docker compose run --rm -T --no-deps web tar -C /app/private_media -xzf - < "$backup_dir/private-media.tar.gz"
docker compose up -d web worker beat
