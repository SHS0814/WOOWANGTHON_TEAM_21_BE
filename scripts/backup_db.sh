#!/usr/bin/env bash
# DB 백업 (pg_dump). 저장소 루트에서 실행한다. 백업 파일은 ./backups 에 쌓이고 14일 지난 것은 지운다.
#   ./scripts/backup_db.sh
# 사진(./data/photos)도 함께 묶어 두려면 BACKUP_PHOTOS=1 을 준다.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
docker compose exec -T db pg_dump -U woowang -d woowang --format=custom > "backups/woowang-${stamp}.dump"
if [[ "${BACKUP_PHOTOS:-0}" == "1" ]]; then
  tar -czf "backups/photos-${stamp}.tar.gz" -C data photos
fi
find backups -type f -mtime +14 -delete
echo "backup ok: backups/woowang-${stamp}.dump"
