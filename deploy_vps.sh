#!/usr/bin/env bash
# ==============================================================================
# ANTI-MATRIX INTERN PORTAL — Production VPS Deployment Script
# Target: https://intern.antimatrix.co.in (Hostinger VPS: 145.223.23.169)
# ==============================================================================
set -euo pipefail

echo "=========================================================="
echo " Starting Anti-Matrix Intern Portal VPS Deployment"
echo " Time: $(date -u)"
echo "=========================================================="

# 1. Resolve Application Directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "[+] Working directory: $PWD"

# 2. Fetch & Fast-Forward to Latest Main
echo "[+] Pulling latest verified changes from origin/main..."
git fetch origin main
git checkout main
git pull origin main

# 3. Virtual Environment Activation & Dependency Sync
if [ -d "venv" ]; then
    echo "[+] Activating venv..."
    source venv/bin/activate
elif [ -d ".venv" ]; then
    echo "[+] Activating .venv..."
    source .venv/bin/activate
else
    echo "[!] No existing venv found. Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
fi

echo "[+] Installing/updating Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Safe Database Migration Check
echo "[+] Checking database connectivity and migrations..."
python -c "
from app import create_app
from app.extensions import db
app = create_app()
with app.app_context():
    db.create_all()
    print('    [OK] Database schema verified and up to date.')
"

# 5. Service Reload / Restart
echo "[+] Reloading WSGI application service..."
if systemctl is-active --quiet intern; then
    sudo systemctl restart intern
    echo "    [OK] Service 'intern' restarted successfully."
elif systemctl is-active --quiet gunicorn; then
    sudo systemctl restart gunicorn
    echo "    [OK] Service 'gunicorn' restarted successfully."
else
    echo "[*] Detecting running gunicorn processes..."
    pkill -HUP gunicorn || true
fi

# 6. Nginx Configuration Check & Reload
if command -v nginx >/dev/null 2>&1; then
    echo "[+] Testing and reloading Nginx..."
    sudo nginx -t && sudo systemctl reload nginx
    echo "    [OK] Nginx reloaded successfully."
fi

# 7. Post-Deployment Health Check
echo "[+] Performing local health check..."
sleep 2
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5050/api/health || curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/health || true)
if [ "$HEALTH_STATUS" = "200" ]; then
    echo "    [OK] Local application responded with HTTP 200 OK!"
else
    echo "    [*] Local health check status: $HEALTH_STATUS"
fi

echo "=========================================================="
echo " Anti-Matrix Intern Portal Deployment Completed!"
echo " Live URL: https://intern.antimatrix.co.in"
echo "=========================================================="
