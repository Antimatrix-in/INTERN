#!/usr/bin/env bash
# ==============================================================================
# ANTI-MATRIX INTERN PORTAL — Production VPS Deployment Script
# Target: https://intern.antimatrix.co.in (Hostinger VPS: 145.223.23.169)
# ==============================================================================
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="backups/backup_${TIMESTAMP}"

echo "=========================================================="
echo " Starting Anti-Matrix Intern Portal VPS Deployment"
echo " Time: $(date -u) (Local: ${TIMESTAMP})"
echo "=========================================================="

# 1. Resolve Application Directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "[+] Working directory: $PWD"

# 2. Pre-Deployment Safety Backup
echo "[+] Performing automated pre-deployment backup..."
mkdir -p "$BACKUP_DIR"
git rev-parse HEAD > "${BACKUP_DIR}/PREVIOUS_COMMIT"
echo "    [*] Saved previous commit: $(cat "${BACKUP_DIR}/PREVIOUS_COMMIT")"

if [ -f ".env" ]; then
    cp .env "${BACKUP_DIR}/.env.backup"
    echo "    [OK] Backed up .env configuration."
fi

if [ -d "app/static/uploads" ]; then
    mkdir -p "${BACKUP_DIR}/static_uploads"
    cp -r app/static/uploads/* "${BACKUP_DIR}/static_uploads/" 2>/dev/null || true
    echo "    [OK] Backed up static uploads directory."
fi

if [ -d "instance" ]; then
    mkdir -p "${BACKUP_DIR}/instance_backup"
    cp -r instance/* "${BACKUP_DIR}/instance_backup/" 2>/dev/null || true
    echo "    [OK] Backed up instance storage."
fi

# Link latest backup for quick rollback
rm -f backups/latest
ln -s "backup_${TIMESTAMP}" backups/latest 2>/dev/null || true

# 3. Fetch & Fast-Forward to Target Main Commit
echo "[+] Fetching and fast-forwarding to origin/main..."
git fetch origin main
CURRENT_MAIN=$(git rev-parse origin/main)
echo "    [*] Target deployment commit: ${CURRENT_MAIN}"
git checkout main
git reset --hard origin/main

# 4. Virtual Environment Activation & Dependency Sync
if [ -d "venv" ]; then
    echo "[+] Activating venv..."
    source venv/bin/activate
elif [ -d ".venv" ]; then
    echo "[+] Activating .venv..."
    source .venv/bin/activate
else
    echo "[!] Creating new virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
fi

echo "[+] Syncing Python dependencies from requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

# 5. Safe Non-Destructive Database Migration
echo "[+] Running safe database schema verification..."
if [ -f "migrate_db.py" ]; then
    python migrate_db.py
else
    python -c "
from app import create_app
from app.extensions import db
app = create_app()
with app.app_context():
    db.create_all()
    print('    [OK] Database schema verified.')
"
fi

# 6. Service Reload / Restart
echo "[+] Reloading WSGI application service..."
if systemctl is-active --quiet intern; then
    sudo systemctl restart intern
    echo "    [OK] Service 'intern' restarted successfully."
elif systemctl is-active --quiet gunicorn; then
    sudo systemctl restart gunicorn
    echo "    [OK] Service 'gunicorn' restarted successfully."
else
    echo "[*] Triggering graceful reload on running gunicorn processes..."
    pkill -HUP gunicorn || true
fi

# 7. Nginx Configuration Check & Reload
if command -v nginx >/dev/null 2>&1; then
    echo "[+] Testing and reloading Nginx..."
    sudo nginx -t && sudo systemctl reload nginx
    echo "    [OK] Nginx reloaded successfully."
fi

# 8. Post-Deployment Health Check
echo "[+] Performing post-deployment health check..."
sleep 3
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5050/api/health || curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/health || true)
if [ "$HEALTH_STATUS" = "200" ]; then
    echo "    [OK] Local application responded with HTTP 200 OK!"
else
    echo "    [*] Local health check status: $HEALTH_STATUS"
fi

echo "=========================================================="
echo " Anti-Matrix Intern Portal Deployment Completed Successfully!"
echo " Deployed Commit: $(git rev-parse --short HEAD)"
echo " Live URL: https://intern.antimatrix.co.in"
echo " Rollback point: ${BACKUP_DIR}/PREVIOUS_COMMIT"
echo "=========================================================="
