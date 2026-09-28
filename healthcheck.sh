#!/bin/bash

# NMS Dashboard - Health Check & Status API
# Used for monitoring and alerting

PROJECT_DIR="/home/sandayy/nms-dashboard1"
HEALTH_URL="http://localhost:5000/health"
API_URL="http://localhost:5000/api"
LOG_FILE="$PROJECT_DIR/logs/healthcheck.log"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Check health endpoint
check_health() {
    echo "🔍 Checking health endpoint..."
    
    RESPONSE=$(curl -s -w "\n%{http_code}" "$HEALTH_URL" 2>/dev/null)
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "${GREEN}✓ Health Check PASSED${NC} (HTTP $HTTP_CODE)"
        log "✓ Health check passed (HTTP $HTTP_CODE)"
        return 0
    else
        echo -e "${RED}✗ Health Check FAILED${NC} (HTTP $HTTP_CODE)"
        log "✗ Health check failed (HTTP $HTTP_CODE)"
        return 1
    fi
}

# Check database
check_db() {
    echo "🔍 Checking database..."
    
    if sqlite3 "$PROJECT_DIR/network.db" "SELECT COUNT(*) FROM hosts;" > /dev/null 2>&1; then
        HOST_COUNT=$(sqlite3 "$PROJECT_DIR/network.db" "SELECT COUNT(*) FROM hosts;")
        echo -e "${GREEN}✓ Database OK${NC} ($HOST_COUNT hosts)"
        log "✓ Database OK ($HOST_COUNT hosts)"
        return 0
    else
        echo -e "${RED}✗ Database ERROR${NC}"
        log "✗ Database check failed"
        return 1
    fi
}

# Check API endpoints
check_api() {
    echo "🔍 Checking API endpoints..."
    
    # Check dashboard summary
    RESPONSE=$(curl -s -w "%{http_code}" -o /dev/null "$API_URL/dashboard/summary" -H "Authorization: Bearer test" 2>/dev/null)
    
    if [ "$RESPONSE" = "200" ] || [ "$RESPONSE" = "401" ]; then
        echo -e "${GREEN}✓ API Endpoints OK${NC}"
        log "✓ API endpoints responding"
        return 0
    else
        echo -e "${RED}✗ API Endpoints ERROR${NC} (HTTP $RESPONSE)"
        log "✗ API endpoints failed (HTTP $RESPONSE)"
        return 1
    fi
}

# Check process
check_process() {
    echo "🔍 Checking process..."
    
    if pgrep -f "python3 app.py\|gunicorn.*app:app\|flask.*run" > /dev/null; then
        echo -e "${GREEN}✓ Process Running${NC}"
        log "✓ Application process running"
        return 0
    else
        echo -e "${RED}✗ Process Not Running${NC}"
        log "✗ Application process not running"
        return 1
    fi
}

# Check disk space
check_disk() {
    echo "🔍 Checking disk space..."
    
    USAGE=$(df "$PROJECT_DIR" | awk 'NR==2 {print $5}' | sed 's/%//')
    
    if [ "$USAGE" -lt 80 ]; then
        echo -e "${GREEN}✓ Disk Space OK${NC} ($USAGE% used)"
        log "✓ Disk space OK ($USAGE% used)"
        return 0
    elif [ "$USAGE" -lt 90 ]; then
        echo -e "${YELLOW}⚠ Disk Space WARNING${NC} ($USAGE% used)"
        log "⚠ Disk space warning ($USAGE% used)"
        return 0
    else
        echo -e "${RED}✗ Disk Space CRITICAL${NC} ($USAGE% used)"
        log "✗ Disk space critical ($USAGE% used)"
        return 1
    fi
}

# Summary report
generate_report() {
    echo ""
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║            NMS Dashboard - Health Check Report                 ║"
    echo "║            $(date '+%Y-%m-%d %H:%M:%S')                                   ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""
    
    FAILED=0
    
    check_process || ((FAILED++))
    check_health || ((FAILED++))
    check_db || ((FAILED++))
    check_api || ((FAILED++))
    check_disk || ((FAILED++))
    
    echo ""
    
    if [ $FAILED -eq 0 ]; then
        echo -e "${GREEN}━━━ ALL CHECKS PASSED ✓ ━━━${NC}"
        echo "Status: HEALTHY"
        return 0
    else
        echo -e "${RED}━━━ $FAILED CHECK(S) FAILED ✗ ━━━${NC}"
        echo "Status: DEGRADED"
        return 1
    fi
}

# Show usage
usage() {
    echo "Usage: $0 [OPTION]"
    echo ""
    echo "Options:"
    echo "  (no args)    Run full health check"
    echo "  --report     Generate detailed report"
    echo "  --logs       Show recent logs"
    echo "  --help       Show this help"
}

case "${1:-}" in
    --report)
        generate_report
        ;;
    --logs)
        tail -50 "$LOG_FILE" 2>/dev/null || echo "No logs yet"
        ;;
    --help)
        usage
        ;;
    *)
        generate_report
        ;;
esac
