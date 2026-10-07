#!/usr/bin/env bash
set -euo pipefail
DB=${DB_PATH:-/data/app.db}
DEST=/backup
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-30}
case "$RETENTION_DAYS" in
  ''|*[!0-9]*|0)
    echo "[backup] WARN: BACKUP_RETENTION_DAYS='$RETENTION_DAYS' is not a positive whole number; using 30"
    RETENTION_DAYS=30 ;;
esac
while true; do
  ts=$(date +%F)
  iso=$(date -u +%FT%TZ)
  tmp="$DEST/app-${ts}.db"
  echo "[backup] $iso snapshot → $tmp"
  # datenschutz.html promises no snapshot outlives RETENTION_DAYS. The prune
  # runs once a loop, at most a day apart (a restart only brings it forward),
  # so it takes everything older than RETENTION_DAYS - 1 days. It runs whether
  # or not tonight's snapshot works, because a failing backup must not stretch
  # the promise (its alert is the fix), and its pattern also takes a raw
  # app-*.db left by a container killed between .backup and gzip.
  find "$DEST" -name 'app-*.db*' -mmin "+$(( (RETENTION_DAYS - 1) * 1440 ))" -delete || true
  if sqlite3 "$DB" ".backup '$tmp'"; then
    gzip -f "$tmp"
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
    rm -f "$tmp"
    echo "$iso snapshot failed" > "$DEST/BACKUP-FAIL-${ts}.txt"
  fi
  sleep 86400
done
