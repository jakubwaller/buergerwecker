#!/usr/bin/env bash
set -euo pipefail
DB=${DB_PATH:-/data/app.db}
DEST=/backup
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-30}
while true; do
  ts=$(date +%F)
  iso=$(date -u +%FT%TZ)
  tmp="$DEST/app-${ts}.db"
  echo "[backup] $iso snapshot → $tmp"
  if sqlite3 "$DB" ".backup '$tmp'"; then
    gzip -f "$tmp"
    find "$DEST" -name 'app-*.db.gz' -mtime "+$RETENTION_DAYS" -delete || true
    # Record success in meta. Retry up to 3× with backoff to handle a
    # transient SQLITE_BUSY when the poller or web container is mid-write.
    # If we still fail, write a sentinel file so the housekeeping pass
    # can surface the failure via the developer-alert path.
    recorded=0
    for attempt in 1 2 3; do
      if sqlite3 "$DB" "INSERT INTO meta (key, value) \
        VALUES ('last_backup_at', '$iso') \
        ON CONFLICT (key) DO UPDATE SET value=excluded.value, \
        updated_at=CURRENT_TIMESTAMP" 2>/dev/null; then
        recorded=1
        break
      fi
      sleep "$attempt"
    done
    if [ "$recorded" = "0" ]; then
      echo "[backup] WARN: could not record last_backup_at after 3 tries"
      echo "$iso snapshot OK but meta write failed" > "$DEST/BACKUP-METAFAIL-${ts}.txt"
    fi
  else
    echo "[backup] FAIL: sqlite3 .backup exited non-zero"
    echo "$iso snapshot failed" > "$DEST/BACKUP-FAIL-${ts}.txt"
  fi
  sleep 86400
done
