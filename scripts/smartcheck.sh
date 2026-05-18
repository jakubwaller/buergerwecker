#!/usr/bin/env bash
# Runs on the Pi host (NOT inside Docker) via a systemd timer.
# Appends SMART output to a file under the bind-mounted backup volume,
# where the backup container can pick it up and email it.
set -euo pipefail
DEVICE=${1:-/dev/sda}
OUTDIR=${OUTDIR:-/mnt/backup/smart}
mkdir -p "$OUTDIR"
ts=$(date +%F)
smartctl -a "$DEVICE" > "$OUTDIR/smart-${ts}.txt" || true
# Alert via diff of reallocated-sector count from previous snapshot
prev=$(ls -1t "$OUTDIR"/smart-*.txt 2>/dev/null | sed -n 2p || true)
if [ -n "$prev" ]; then
  prev_reall=$(grep -E 'Reallocated_Sector_Ct' "$prev"   | awk '{print $10}' || echo 0)
  curr_reall=$(grep -E 'Reallocated_Sector_Ct' "$OUTDIR/smart-${ts}.txt" | awk '{print $10}' || echo 0)
  if [ "${curr_reall:-0}" != "${prev_reall:-0}" ]; then
    echo "SMART: reallocated sectors changed from ${prev_reall} to ${curr_reall}" \
      > "$OUTDIR/ALERT-${ts}.txt"
  fi
fi
