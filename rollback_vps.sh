#!/usr/bin/env bash
# ==============================================================================
# ANTI-MATRIX INTERN PORTAL — Production VPS Emergency Rollback Script
# Target: https://intern.antimatrix.co.in
# Safety Standard: Restores exact previous commit, restores configuration
#                 and storage archive, restarts services, and validates health.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo " Starting Anti-Matrix Intern Portal VPS Rollback"
echo " Time: $(date -u)"
echo "=========================================================="

MANIFEST_FILE="backups/latest/manifest.env"
if [ ! -f "$MANIFEST_FILE" ]; then
    echo "[!] backups/latest/manifest.env not found. Searching latest backup directory..."
    LATEST_DIR=$(find backups -mindepth 1 -maxdepth 1 -type d -name "backup_*" | sort -r | head -n 1 || true)
    if [ -n "$LATEST_DIR" ] && [ -f "${LATEST_DIR}/manifest.env" ]; then
        MANIFEST_FILE="${LATEST_DIR}/manifest.env"
        echo "    [*] Using discovered manifest: $MANIFEST_FILE"
    else
        echo "[FATAL] No valid rollback manifest found! Cannot proceed with automated rollback." >&2
        exit 1
    fi
fi

# Load rollback variables from verified manifest
# shellcheck source=/dev/null
source "$MANIFEST_FILE"

if [ -z "${ROLLBACK_COMMIT:-}" ]; then
    echo "[FATAL] ROLLBACK_COMMIT is empty in $MANIFEST_FILE! Aborting." >&2
    exit 1
fi

echo "[+] Target rollback commit: ${ROLLBACK_COMMIT}"

# 1. Rollback Git Repository to Previous Commit
echo "[+] Resetting Git working tree to commit: ${ROLLBACK_COMMIT}..."
git checkout main 2>/dev/null || true
git reset --hard "${ROLLBACK_COMMIT}"
echo "    [OK] Git working tree restored to $(git rev-parse --short HEAD)."

# 2. Restore Storage & Configuration Archive
if [ -n "${STORAGE_ARCHIVE:-}" ] && [ -f "$STORAGE_ARCHIVE" ]; then
    echo "[+] Restoring configuration and storage files from ${STORAGE_ARCHIVE}..."
    tar -xzf "$STORAGE_ARCHIVE"
    echo "    [OK] Configuration (.env) and storage files restored."
fi

# Optional Database Rollback
if [[ "${1:-}" == "--with-db" ]] && [ -n "${DB_BACKUP_FILE:-}" ] && [ -f "$DB_BACKUP_FILE" ]; then
    echo "[!] Flag --with-db detected. Restoring database from ${DB_BACKUP_FILE}..."
    RAW_DB_URL=$(grep -E '^DATABASE_URL=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" || true)
    if [[ "$RAW_DB_URL" =~ ^postgres(ql)?:// ]]; then
        gunzip -c "$DB_BACKUP_FILE" | psql "$RAW_DB_URL"
        echo "    [OK] PostgreSQL database restored from backup."
    fi
fi

# 3. Virtual Environment Sync
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi
pip install -r requirements.txt || true

# 4. Service Restart
echo "[+] Restarting application services..."
SERVICE_NAME=""
for CANDIDATE in intern gunicorn antimatrix intern-portal; do
    if systemctl is-active --quiet "$CANDIDATE" 2>/dev/null; then
        SERVICE_NAME="$CANDIDATE"
        break
    fi
done

if [ -n "$SERVICE_NAME" ]; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "    [OK] Service '${SERVICE_NAME}' restarted."
else
    pkill -HUP -f gunicorn || true
fi

# 5. Nginx Reload
if command -v nginx >/dev/null 2>&1; then
    sudo nginx -t && sudo systemctl reload nginx
    echo "    [OK] Nginx reloaded."
fi

# 6. Health Verification
PORT=5050
if [ -f ".env" ]; then
    ENV_PORT=$(grep -E '^PORT=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" || true)
    [ -n "$ENV_PORT" ] && PORT="$ENV_PORT"
fi

sleep 2
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/api/health" || true)
echo "    [*] Post-rollback health check status: HTTP ${HTTP_CODE}"

echo "=========================================================="
echo " Anti-Matrix Intern Portal Rollback Completed Successfully!"
echo " Active Commit: $(git rev-parse HEAD)"
echo "=========================================================="
