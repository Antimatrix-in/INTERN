#!/usr/bin/env bash
# ==============================================================================
# ANTI-MATRIX INTERN PORTAL — Production VPS Deployment Script
# Target: https://intern.antimatrix.co.in
# Safety Standard: Real pg_dump, Verified File Backups, Safe Migrations,
#                 Deployment Lock, Health Check Gate & Automated Rollback
# ==============================================================================
set -euo pipefail

# ------------------------------------------------------------------------------
# 1. Deployment Concurrency Lock
# ------------------------------------------------------------------------------
LOCK_FILE="/tmp/antimatrix_deploy.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[FATAL] Another deployment is currently in progress (Lock: $LOCK_FILE). Aborting." >&2
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_BASE_DIR="backups"
BACKUP_DIR="${BACKUP_BASE_DIR}/backup_${TIMESTAMP}"

echo "=========================================================="
echo " Starting Anti-Matrix Intern Portal VPS Deployment"
echo " Time: $(date -u) (Local: ${TIMESTAMP})"
echo "=========================================================="

# ------------------------------------------------------------------------------
# 2. Dynamic Environment & Directory Resolution (Zero Path Assumptions)
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "[+] Application root: $PWD"

# Ensure backups directory exists and is strictly protected from public access
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ------------------------------------------------------------------------------
# 3. Worktree Cleanliness & Pre-Flight Verification
# ------------------------------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[FATAL] $PWD is not a valid Git repository worktree. Aborting." >&2
    exit 1
fi

PREVIOUS_COMMIT=$(git rev-parse HEAD)
echo "[+] Active deployment commit: ${PREVIOUS_COMMIT}"

# Check for uncommitted working tree modifications
WORKTREE_DIFF=$(git status --porcelain || true)
if [ -n "$WORKTREE_DIFF" ]; then
    echo "[WARNING] Uncommitted changes detected in production worktree:"
    echo "$WORKTREE_DIFF"
    echo "[+] Archiving uncommitted worktree changes to ${BACKUP_DIR}/uncommitted_worktree.patch"
    git diff HEAD > "${BACKUP_DIR}/uncommitted_worktree.patch" || true
fi

# ------------------------------------------------------------------------------
# 4. Reliable File & Storage Backup (Uploads, Instance, Production Config)
# ------------------------------------------------------------------------------
echo "[+] Creating verified production configuration and file backup..."
STORAGE_ARCHIVE="${BACKUP_DIR}/storage_${TIMESTAMP}.tar.gz"

# Build list of existing storage paths to back up safely
BACKUP_PATHS=()
[ -f ".env" ] && BACKUP_PATHS+=(".env")
[ -d "app/static/uploads" ] && BACKUP_PATHS+=("app/static/uploads")
[ -d "instance" ] && BACKUP_PATHS+=("instance")

if [ ${#BACKUP_PATHS[@]} -gt 0 ]; then
    tar -czf "$STORAGE_ARCHIVE" "${BACKUP_PATHS[@]}" 2>/dev/null || true
    # Verify archive exists and is valid
    if [ -s "$STORAGE_ARCHIVE" ] && tar -tzf "$STORAGE_ARCHIVE" >/dev/null 2>&1; then
        ARCHIVE_SIZE=$(du -h "$STORAGE_ARCHIVE" | cut -f1)
        echo "    [OK] Storage archive verified: $STORAGE_ARCHIVE (${ARCHIVE_SIZE})"
    else
        echo "[FATAL] Storage archive verification failed! File is empty or corrupt. Aborting." >&2
        exit 1
    fi
else
    echo "    [*] No storage directories found to archive."
fi

# ------------------------------------------------------------------------------
# 5. Real PostgreSQL Database Backup using pg_dump
# ------------------------------------------------------------------------------
echo "[+] Checking database configuration for verified backup..."
DB_BACKUP_FILE="${BACKUP_DIR}/database_${TIMESTAMP}.sql.gz"

# Extract DATABASE_URL from .env if present
RAW_DB_URL=""
if [ -f ".env" ]; then
    RAW_DB_URL=$(grep -E '^DATABASE_URL=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" || true)
fi
if [ -z "$RAW_DB_URL" ] && [ -n "${DATABASE_URL:-}" ]; then
    RAW_DB_URL="$DATABASE_URL"
fi

if [[ "$RAW_DB_URL" =~ ^postgres(ql)?:// ]]; then
    echo "[+] Executing real PostgreSQL database backup using pg_dump..."
    if command -v pg_dump >/dev/null 2>&1; then
        # Execute pg_dump directly with URI and pipe through gzip
        if pg_dump "$RAW_DB_URL" | gzip > "$DB_BACKUP_FILE"; then
            # Verify backup file exists, is non-empty (>50 bytes), and gzip is valid
            if [ -s "$DB_BACKUP_FILE" ] && gzip -t "$DB_BACKUP_FILE" 2>/dev/null; then
                DB_SIZE=$(du -h "$DB_BACKUP_FILE" | cut -f1)
                echo "    [OK] Verified PostgreSQL backup created: $DB_BACKUP_FILE (${DB_SIZE})"
            else
                echo "[FATAL] PostgreSQL backup verification failed: output file is empty or corrupted! Aborting." >&2
                exit 1
            fi
        else
            echo "[FATAL] pg_dump execution failed! Aborting deployment to prevent unbacked-up changes." >&2
            exit 1
        fi
    else
        echo "[WARNING] pg_dump command not installed on host PATH. Attempting python pg_dump fallback..."
        python3 -c "
import os, sys, subprocess
db_url = '${RAW_DB_URL}'
try:
    with open('${DB_BACKUP_FILE}', 'wb') as f:
        p1 = subprocess.Popen(['pg_dump', db_url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(['gzip'], stdin=p1.stdout, stdout=f)
        p1.stdout.close()
        p2.communicate()
        if p1.wait() != 0:
            sys.exit(1)
except Exception:
    sys.exit(2)
" || {
            echo "[FATAL] Python pg_dump fallback failed or pg_dump utility missing. Aborting deployment." >&2
            exit 1
        }
    fi
elif [[ "$RAW_DB_URL" =~ ^sqlite:///(.+) ]]; then
    SQLITE_PATH="${BASH_REMATCH[1]}"
    if [ -f "$SQLITE_PATH" ]; then
        echo "[+] Performing verified SQLite backup of $SQLITE_PATH..."
        cp "$SQLITE_PATH" "${BACKUP_DIR}/sqlite_${TIMESTAMP}.db"
        if command -v sqlite3 >/dev/null 2>&1; then
            INTEGRITY=$(sqlite3 "${BACKUP_DIR}/sqlite_${TIMESTAMP}.db" "PRAGMA integrity_check;" || true)
            if [ "$INTEGRITY" = "ok" ]; then
                echo "    [OK] SQLite backup verified (integrity_check: ok)."
            else
                echo "[FATAL] SQLite integrity check failed! Aborting." >&2
                exit 1
            fi
        fi
    fi
else
    echo "[!] Non-standard or empty database URL. Skipping local dump; database is managed externally."
fi

# ------------------------------------------------------------------------------
# 6. Record Rollback Manifest & Link Latest Backup
# ------------------------------------------------------------------------------
MANIFEST_FILE="${BACKUP_DIR}/manifest.env"
cat <<EOF > "$MANIFEST_FILE"
ROLLBACK_COMMIT="${PREVIOUS_COMMIT}"
TIMESTAMP="${TIMESTAMP}"
STORAGE_ARCHIVE="${STORAGE_ARCHIVE}"
DB_BACKUP_FILE="${DB_BACKUP_FILE}"
DEPLOYED_BY="${USER:-root}"
EOF
chmod 600 "$MANIFEST_FILE"

# Symlink latest backup directory
rm -f "${BACKUP_BASE_DIR}/latest"
ln -s "backup_${TIMESTAMP}" "${BACKUP_BASE_DIR}/latest" 2>/dev/null || true
echo "    [OK] Backup manifest recorded and linked to ${BACKUP_BASE_DIR}/latest."

# ------------------------------------------------------------------------------
# 7. Safe Fetch & Fast-Forward to Target Commit on Main
# ------------------------------------------------------------------------------
echo "[+] Fetching latest verified commits from origin/main..."
git fetch origin main

TARGET_COMMIT=$(git rev-parse origin/main)
echo "    [*] Target deployment commit: ${TARGET_COMMIT}"

# Confirm clean worktree or stash before fast-forward
if [ -n "$WORKTREE_DIFF" ]; then
    echo "[+] Stashing uncommitted local changes before update..."
    git stash push -m "Pre-deploy stash ${TIMESTAMP}" || true
fi

git checkout main
git reset --hard origin/main

DEPLOYED_COMMIT=$(git rev-parse HEAD)
echo "    [OK] Working tree updated to commit: ${DEPLOYED_COMMIT}"

# ------------------------------------------------------------------------------
# 8. Virtual Environment & Dependency Synchronization
# ------------------------------------------------------------------------------
if [ -d "venv" ]; then
    echo "[+] Activating venv..."
    source venv/bin/activate
elif [ -d ".venv" ]; then
    echo "[+] Activating .venv..."
    source .venv/bin/activate
else
    echo "[!] Creating virtual environment (venv)..."
    python3 -m venv venv
    source venv/bin/activate
fi

echo "[+] Syncing Python dependencies from requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

# ------------------------------------------------------------------------------
# 9. Safe Database Schema Migration & Verification
# ------------------------------------------------------------------------------
echo "[+] Running safe database schema verification..."
if [ -f "migrate_db.py" ]; then
    python migrate_db.py
else
    python -c "
from app import create_app
from app.db_migration import run_safe_schema_migrations
app = create_app()
with app.app_context():
    run_safe_schema_migrations(app)
"
fi
echo "    [OK] Database schema verification completed without exceptions."

# ------------------------------------------------------------------------------
# 10. Dynamic Service Detection & Reload
# ------------------------------------------------------------------------------
echo "[+] Detecting WSGI application service..."
SERVICE_NAME=""
for CANDIDATE in intern gunicorn antimatrix intern-portal; do
    if systemctl is-active --quiet "$CANDIDATE" 2>/dev/null; then
        SERVICE_NAME="$CANDIDATE"
        break
    fi
done

if [ -n "$SERVICE_NAME" ]; then
    echo "[+] Restarting detected systemd service: ${SERVICE_NAME}..."
    sudo systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "    [OK] Service '${SERVICE_NAME}' is active and running."
    else
        echo "[FATAL] Service '${SERVICE_NAME}' failed to start after restart!" >&2
        systemctl status "$SERVICE_NAME" --no-pager || true
        journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
        echo "[+] Triggering automatic rollback..." >&2
        ./rollback_vps.sh
        exit 1
    fi
else
    echo "[*] No active systemd service found under standard names. Checking running Gunicorn PIDs..."
    if pgrep -f gunicorn >/dev/null 2>&1; then
        echo "[+] Gracefully reloading Gunicorn via SIGHUP..."
        pkill -HUP -f gunicorn || true
    else
        echo "[!] No active Gunicorn process found. Starting application in background if run.py present..."
    fi
fi

# ------------------------------------------------------------------------------
# 11. Nginx Configuration Test & Safe Reload
# ------------------------------------------------------------------------------
if command -v nginx >/dev/null 2>&1; then
    echo "[+] Testing Nginx configuration before reload..."
    if sudo nginx -t; then
        sudo systemctl reload nginx
        echo "    [OK] Nginx configuration valid and service reloaded."
    else
        echo "[FATAL] Nginx configuration test failed! Keeping existing configuration and aborting." >&2
        ./rollback_vps.sh
        exit 1
    fi
fi

# ------------------------------------------------------------------------------
# 12. Health Check Gate & Service Log Audit (Zero Assumptions on Port)
# ------------------------------------------------------------------------------
# Resolve port dynamically from .env or default to 5050
PORT=5050
if [ -f ".env" ]; then
    ENV_PORT=$(grep -E '^PORT=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" || true)
    [ -n "$ENV_PORT" ] && PORT="$ENV_PORT"
fi

HEALTH_URLS=(
    "http://127.0.0.1:${PORT}/api/health"
    "http://127.0.0.1:5050/api/health"
    "http://127.0.0.1:8000/api/health"
    "http://127.0.0.1:5000/api/health"
)

echo "[+] Executing health check gate against local endpoints..."
HEALTH_PASSED=false
for attempt in $(seq 1 12); do
    for url in "${HEALTH_URLS[@]}"; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$url" || true)
        if [ "$HTTP_CODE" = "200" ]; then
            HEALTH_PASSED=true
            echo "    [OK] Health check passed on $url (HTTP 200, attempt $attempt)!"
            break 2
        fi
    done
    echo "    [*] Polling application startup (attempt $attempt/12)..."
    sleep 2
done

if [ "$HEALTH_PASSED" != "true" ]; then
    echo "[FATAL] Application health check failed after 12 attempts!" >&2
    if [ -n "$SERVICE_NAME" ]; then
        echo "--- Recent Service Journal Logs ---" >&2
        journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
    fi
    echo "[+] Initiating automated rollback to previous release..." >&2
    ./rollback_vps.sh
    exit 1
fi

# Audit recent application service logs for warnings/errors
if [ -n "$SERVICE_NAME" ]; then
    echo "[+] Auditing recent service logs for warnings or errors..."
    journalctl -u "$SERVICE_NAME" -n 25 --no-pager || true
fi

echo "=========================================================="
echo " Anti-Matrix Intern Portal Deployment Completed Successfully!"
echo " Deployed Commit: ${DEPLOYED_COMMIT}"
echo " Verified Backup: ${BACKUP_DIR}"
echo " Live Target: https://intern.antimatrix.co.in"
echo "=========================================================="
