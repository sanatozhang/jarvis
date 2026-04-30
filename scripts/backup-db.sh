#!/usr/bin/env bash
# SQLite online-backup of appllo.db, run via cron.
#
# Uses Python's sqlite3.backup() inside the backend container. This is the
# only safe way to copy a live SQLite db — `cp` mid-write produces a torn
# file. The backup is consistent even if writes are happening.
#
# Usage:
#   ./backup-db.sh            # backup to data/backups/appllo_YYYYMMDD_HHMM.db
#   ./backup-db.sh --quick    # exit silently if container not running (cron-friendly)
#
# Install cron (macOS host, runs daily at 02:00):
#   crontab -e
#   0 2 * * * /Users/mac/jarvis/scripts/backup-db.sh >> /Users/mac/jarvis/data/backup.log 2>&1

set -euo pipefail

CONTAINER="${BACKUP_CONTAINER:-jarvis-backend-1}"
HOST_DATA_DIR="${BACKUP_HOST_DIR:-/Users/mac/jarvis/data}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
DATE_TAG="$(date +%Y%m%d_%H%M)"

# Make docker discoverable when invoked from cron (cron's PATH is minimal).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# In --quick mode, exit silently if container not running (avoid cron spam).
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    if [[ "${1:-}" == "--quick" ]]; then
        exit 0
    fi
    log "ERROR: container $CONTAINER not running"
    exit 1
fi

mkdir -p "$HOST_DATA_DIR/backups"

BACKUP_NAME="appllo_${DATE_TAG}.db"
BACKUP_HOST_PATH="$HOST_DATA_DIR/backups/$BACKUP_NAME"

# Run backup inside container — sqlite3.backup() is atomic and consistent.
docker exec "$CONTAINER" python3 -c "
import sqlite3, sys
try:
    src = sqlite3.connect('/data/appllo.db')
    dst = sqlite3.connect('/data/backups/$BACKUP_NAME')
    src.backup(dst)
    src.close(); dst.close()
except Exception as e:
    print('backup failed:', e, file=sys.stderr)
    sys.exit(1)
"

if [[ ! -s "$BACKUP_HOST_PATH" ]]; then
    log "ERROR: backup file $BACKUP_HOST_PATH missing or empty"
    exit 1
fi

# Verify integrity of the backup (cheap insurance — costs ~50ms on a 3MB db)
INTEG=$(docker exec "$CONTAINER" python3 -c "
import sqlite3
c = sqlite3.connect('/data/backups/$BACKUP_NAME')
print(c.execute('PRAGMA integrity_check').fetchone()[0])
")

if [[ "$INTEG" != "ok" ]]; then
    log "ERROR: backup integrity_check failed: $INTEG"
    mv "$BACKUP_HOST_PATH" "$BACKUP_HOST_PATH.bad"
    exit 1
fi

SIZE=$(du -h "$BACKUP_HOST_PATH" | cut -f1)
log "Backup OK: $BACKUP_NAME ($SIZE)"

# Rotate: delete backups older than KEEP_DAYS
DELETED=$(find "$HOST_DATA_DIR/backups" -name 'appllo_*.db' -type f -mtime +"$KEEP_DAYS" -delete -print 2>/dev/null | wc -l | tr -d ' ')
if [[ "$DELETED" != "0" ]]; then
    log "Rotated: deleted $DELETED old backup(s)"
fi
