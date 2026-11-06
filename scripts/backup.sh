#!/bin/sh
set -eu
umask 077
backup_dir="${1:-backups/$(date +%Y%m%d-%H%M%S)}"
# Never overwrite an earlier backup, even when a caller reuses the directory.
[ ! -e "$backup_dir" ] || { echo 'Каталог резервной копии уже существует' >&2; exit 1; }
mkdir -p "$(dirname "$backup_dir")"
mkdir "$backup_dir"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
docker compose run --rm -T --no-deps web tar -C /app/private_media -czf - . > "$backup_dir/private-media.tar.gz"
echo "Резервная копия: $backup_dir"
