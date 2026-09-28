#!/bin/bash

# NMS Dashboard - Monitoring Script
# Real-time monitoring of application health

set -e

PROJECT_DIR="/home/sandayy/nms-dashboard1"
LOG_DIR="$PROJECT_DIR/logs"
APP_LOG="$LOG_DIR/app.log"
HEALTH_CHECK_URL="http://localhost:5000/health"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         NMS Dashboard - Real-time Monitoring                  ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if app is running
check_status() {
    if curl -s "$HEALTH_CHECK_URL" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Application is ${GREEN}RUNNING${NC}"
        return 0
    else
        echo -e "${RED}✗${NC} Application is ${RED}DOWN${NC}"
        return 1
    fi
}

# Check CPU & Memory
check_resources() {
    echo ""
    echo -e "${BLUE}━━━ SYSTEM RESOURCES ━━━${NC}"
    
    if pgrep -f "python3 app.py" > /dev/null; then
        PID=$(pgrep -f "python3 app.py" | head -1)
        
        if [ -f "/proc/$PID/stat" ]; then
            MEM=$(ps -p $PID -o %mem= | xargs)
            CPU=$(ps -p $PID -o %cpu= | xargs)
            echo -e "CPU Usage:    ${YELLOW}${CPU}%${NC}"
            echo -e "Memory Usage: ${YELLOW}${MEM}%${NC}"
        fi
    fi
    
    FREE_MEM=$(free -h | awk '/^Mem:/ {print $7}')
    echo -e "Free Memory:  $FREE_MEM"
}

# Check database
check_database() {
    echo ""
    echo -e "${BLUE}━━━ DATABASE ━━━${NC}"
    
    if [ -f "$PROJECT_DIR/network.db" ]; then
        DB_SIZE=$(du -h "$PROJECT_DIR/network.db" | awk '{print $1}')
        echo -e "Database:     ${GREEN}OK${NC} ($DB_SIZE)"
        
        # Check if readable
        if sqlite3 "$PROJECT_DIR/network.db" "SELECT COUNT(*) FROM hosts;" > /dev/null 2>&1; then
            HOST_COUNT=$(sqlite3 "$PROJECT_DIR/network.db" "SELECT COUNT(*) FROM hosts;")
            echo -e "Hosts:        $HOST_COUNT monitored"
        fi
    else
        echo -e "Database:     ${RED}NOT FOUND${NC}"
    fi
}

# Check logs for errors
check_logs() {
    echo ""
    echo -e "${BLUE}━━━ RECENT LOGS (Last 20 lines) ━━━${NC}"
    
    if [ -f "$APP_LOG" ]; then
        tail -20 "$APP_LOG"
    else
        echo -e "${YELLOW}Log file not found: $APP_LOG${NC}"
    fi
}

# Health check
health_check() {
    echo ""
    echo -e "${BLUE}━━━ HEALTH CHECK ━━━${NC}"
    
    RESPONSE=$(curl -s -w "\n%{http_code}" "$HEALTH_CHECK_URL" 2>/dev/null || echo "000")
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "Status:       ${GREEN}HEALTHY${NC} (HTTP $HTTP_CODE)"
        echo -e "Response:     $BODY"
    else
        echo -e "Status:       ${RED}UNHEALTHY${NC} (HTTP $HTTP_CODE)"
    fi
}

# Main monitoring loop
monitor_loop() {
    while true; do
        clear
        echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${BLUE}║         NMS Dashboard - Real-time Monitoring                  ║${NC}"
        echo -e "${BLUE}║         $(date '+%Y-%m-%d %H:%M:%S')                                   ║${NC}"
        echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        
        check_status
        check_resources
        check_database
        health_check
        
        echo ""
        echo -e "${YELLOW}Refreshing in 10 seconds... (Press Ctrl+C to exit)${NC}"
        sleep 10
    done
}

# Show help
show_help() {
    echo "Usage: $0 [OPTION]"
    echo ""
    echo "Options:"
    echo "  -m, --monitor     Continuous monitoring (default)"
    echo "  -s, --status      Show status once"
    echo "  -l, --logs        Show last logs"
    echo "  -h, --health      Health check"
    echo "  --help            Show this help"
}

# Parse arguments
case "${1:-}" in
    -s|--status)
        check_status
        ;;
    -l|--logs)
        check_logs
        ;;
    -h|--health)
        health_check
        ;;
    -m|--monitor)
        monitor_loop
        ;;
    --help)
        show_help
        ;;
    *)
        monitor_loop
        ;;
esac
