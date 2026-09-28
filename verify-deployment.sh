#!/usr/bin/env bash

# NMS Dashboard - Pre-Deployment Checklist
# Run this script to verify all requirements are met before deploying

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║      NMS Dashboard - Pre-Deployment Verification Script       ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

check_pass() { echo -e "${GREEN}✓${NC} $1"; }
check_fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
check_warn() { echo -e "${YELLOW}⚠${NC} $1"; }

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "1. ENVIRONMENT SETUP"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
echo "Python version: $PYTHON_VERSION"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" && check_pass "Python 3.8+" || check_fail "Python 3.8+ required"

# Check .env file
if [ -f .env ]; then
    check_pass ".env file exists"
    
    # Check required env vars
    if grep -q "^SECRET_KEY=" .env && grep -q "^DASHBOARD_PASSWORD=" .env; then
        check_pass "SECRET_KEY and DASHBOARD_PASSWORD configured"
    else
        check_fail "Missing required env vars (SECRET_KEY, DASHBOARD_PASSWORD)"
    fi
else
    check_fail ".env file not found - copy from .env.example and configure"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "2. DEPENDENCIES"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python3 -m pip list | grep -q "Flask" && check_pass "Flask installed" || check_fail "Flask not installed"
python3 -m pip list | grep -q "APScheduler" && check_pass "APScheduler installed" || check_fail "APScheduler not installed"
python3 -m pip list | grep -q "gunicorn" && check_pass "Gunicorn installed" || check_fail "Gunicorn not installed"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "3. CODE QUALITY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python3 -m py_compile app.py nms/*.py && check_pass "Python syntax valid" || check_fail "Syntax errors found"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "4. DATABASE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ -f network.db ]; then
    check_pass "Database exists"
    DB_SIZE=$(du -h network.db | cut -f1)
    echo "Database size: $DB_SIZE"
else
    check_warn "Database doesn't exist - will be created on first run"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "5. CREDENTIALS & SECURITY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if grep -q "ChangeMe123" .env 2>/dev/null; then
    check_fail "Default password still set - update DASHBOARD_PASSWORD"
else
    check_pass "Default credentials changed"
fi

if grep -q "TELEGRAM_BOT_TOKEN=" .env && [ ! -z "$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2 | xargs)" ]; then
    check_pass "Telegram bot token configured"
else
    check_warn "Telegram integration not configured (optional)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "6. FILE PERMISSIONS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ -r app.py ] && check_pass "app.py readable" || check_fail "app.py not readable"
[ -d nms ] && check_pass "nms/ directory exists" || check_fail "nms/ directory missing"
[ -d templates ] && check_pass "templates/ directory exists" || check_fail "templates/ missing"
[ -d static ] && check_pass "static/ directory exists" || check_fail "static/ missing"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "7. DEPLOYMENT OPTIONS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "Available deployment methods:"
echo "  • Docker:      docker-compose up -d"
echo "  • Systemd:     sudo systemctl start nms-dashboard"
echo "  • Bare metal:  gunicorn -w 4 -b 0.0.0.0:5000 app:app"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "8. POST-DEPLOYMENT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "After deployment, verify:"
echo "  • Health check: curl http://localhost:5000/health"
echo "  • Login:        http://localhost:5000/login"
echo "  • Dashboard:    http://localhost:5000/"
echo "  • Logs:         tail -f logs/*.log"
echo ""

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║                ✓ ALL CHECKS PASSED                            ║"
echo "║              Ready for deployment!                             ║"
echo "╚════════════════════════════════════════════════════════════════╝"
