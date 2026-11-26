#!/usr/bin/env bash
# ==============================================================================
# ANTI-MATRIX INTERN PORTAL — Emergency Rollback Script
# Target: https://intern.antimatrix.co.in (Hostinger VPS: 145.223.23.169)
# ==============================================================================
set -euo pipefail

echo "=========================================================="
echo " Starting Anti-Matrix Intern Portal VPS Rollback"
echo " Time: $(date -u)"
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "backups/latest/PREVIOUS_COMMIT" ]; then
    echo "[ERROR] No previous commit reference found in backups/latest/PREVIOUS_COMMIT"
    echo "Available backups in backups/:"
    ls -la backups/ || true
    exit 1
fi

ROLLBACK_COMMIT=$(cat backups/latest/PREVIOUS_COMMIT)
echo "[+] Rolling back to previous commit: ${ROLLBACK_COMMIT}"

# 1. Checkout Previous Commit
git checkout "${ROLLBACK_COMMIT}"

# 2. Restore Configuration if backed up
if [ -f "backups/latest/.env.backup" ]; then
    echo "[+] Restoring .env configuration..."
    cp backups/latest/.env.backup .env
fi

# 3. Reload Services
echo "[+] Reloading services to previous state..."
if systemctl is-active --quiet intern; then
    sudo systemctl restart intern
elif systemctl is-active --quiet gunicorn; then
    sudo systemctl restart gunicorn
else
    pkill -HUP gunicorn || true
fi

if command -v nginx >/dev/null 2>&1; then
    sudo nginx -t && sudo systemctl reload nginx
fi

# 4. Verify Health
sleep 2
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5050/api/health || curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/health || true)
echo "    [OK] Rollback complete. Local health status: ${HEALTH_STATUS}"

echo "=========================================================="
echo " Anti-Matrix Intern Portal Rollback Completed"
echo " Active Commit: $(git rev-parse --short HEAD)"
echo "=========================================================="
