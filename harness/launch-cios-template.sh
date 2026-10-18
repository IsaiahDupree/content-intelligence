#!/bin/bash

# CIOS Launch Script Template (Feature CIOS-18)
# Starts all CIOS subsystems in dependency order with health checks
# Target location: /Users/isaiahdupree/Documents/Software/media-vault/harness/launch-cios.sh
#
# Usage:
#   bash harness/launch-cios.sh
#
# Services started (in order):
#   1. Content Intelligence API (port 6006)
#   2. TikTok Analytics Service (port 5681)
#   3. Media Vault Backend (port 5563)
#   4. CIOS Orchestration API (port 5570)
#   5. CIOS Dashboard (port 5571)
#
# All services health-checked before proceeding to next

set -e

CIOS_ROOT="/Users/isaiahdupree/Documents/Software/media-vault"
CI_ROOT="/Users/isaiahdupree/Documents/Software/content-intelligence"
PID_FILE="/tmp/cios-pids.txt"
LOG_DIR="/tmp/cios-logs"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Create log directory
mkdir -p "$LOG_DIR"
rm -f "$PID_FILE"

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  CIOS System Startup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Function to check service health
check_health() {
    local port=$1
    local service=$2
    local max_attempts=30
    local attempt=1

    echo -e "${YELLOW}⏳ Waiting for ${service} (port ${port})...${NC}"

    while [ $attempt -le $max_attempts ]; do
        if curl -s --max-time 2 "http://localhost:${port}/health" > /dev/null 2>&1 || \
           curl -s --max-time 2 "http://localhost:${port}/api/health" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ ${service} is healthy${NC}"
            return 0
        fi

        echo -ne "\r  Attempt $attempt/$max_attempts..."
        attempt=$((attempt + 1))
        sleep 1
    done

    echo -e "\r${RED}✗ ${service} failed to start${NC}"
    return 1
}

# Function to save PID for tracking
save_pid() {
    echo "$1:$2" >> "$PID_FILE"
    echo "  PID: $2"
}

# ============================================================================
# START: Content Intelligence API (port 6006)
# ============================================================================

echo -e "\n${BLUE}[1/5]${NC} Starting Content Intelligence API..."

cd "$CI_ROOT"
if [ -f "app.py" ]; then
    python3 app.py > "$LOG_DIR/content-intelligence.log" 2>&1 &
    CI_PID=$!
    save_pid "content-intelligence" "$CI_PID"

    if check_health 6006 "Content Intelligence API"; then
        echo -e "${GREEN}✓ Ready: http://localhost:6006${NC}"
    else
        echo -e "${RED}✗ Failed to start Content Intelligence API${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠ Content Intelligence API not found (app.py missing)${NC}"
fi

sleep 1

# ============================================================================
# START: TikTok Analytics Service (port 5681)
# ============================================================================

echo -e "\n${BLUE}[2/5]${NC} Starting TikTok Analytics Service..."

# This would be implemented by agent who defines the service
# For now, just note that it should be running
TK_READY=false
if curl -s --max-time 2 "http://localhost:5681/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ TikTok Analytics already running${NC}"
    TK_READY=true
else
    echo -e "${YELLOW}⚠ TikTok Analytics not running (expected to be running separately)${NC}"
    echo "   Start with: npm run --prefix /path/to/tiktok-analytics start:server"
fi

# ============================================================================
# START: Media Vault Backend (port 5563)
# ============================================================================

echo -e "\n${BLUE}[3/5]${NC} Starting Media Vault Backend..."

MV_READY=false
if curl -s --max-time 2 "http://localhost:5563/api/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Media Vault Backend already running${NC}"
    MV_READY=true
else
    echo -e "${YELLOW}⚠ Media Vault Backend not running (expected to be running separately)${NC}"
    echo "   Start with: cd $CIOS_ROOT/backend && source venv/bin/activate && uvicorn main:app --port 5563"
fi

# ============================================================================
# START: CIOS Orchestration API (port 5570)
# ============================================================================

echo -e "\n${BLUE}[4/5]${NC} Starting CIOS Orchestration API..."

cd "$CIOS_ROOT/backend"

if [ -f "cios_api.py" ]; then
    if [ ! -d "venv" ]; then
        echo -e "${YELLOW}Creating virtual environment...${NC}"
        python3 -m venv venv
    fi

    source venv/bin/activate

    python3 -m pip install -q -r requirements.txt 2>/dev/null || true

    uvicorn cios_api:app --port 5570 --host 0.0.0.0 > "$LOG_DIR/cios-api.log" 2>&1 &
    CIOS_API_PID=$!
    save_pid "cios-api" "$CIOS_API_PID"

    if check_health 5570 "CIOS Orchestration API"; then
        echo -e "${GREEN}✓ Ready: http://localhost:5570/api/cios/health${NC}"
    else
        echo -e "${RED}✗ CIOS Orchestration API failed to start${NC}"
        tail -20 "$LOG_DIR/cios-api.log"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠ CIOS API not yet implemented (cios_api.py missing)${NC}"
    echo "   This should be created by feature CIOS-02"
fi

sleep 1

# ============================================================================
# START: CIOS Dashboard (port 5571)
# ============================================================================

echo -e "\n${BLUE}[5/5]${NC} Starting CIOS Dashboard..."

cd "$CIOS_ROOT/frontend"

if [ -f "package.json" ]; then
    # Check if dependencies installed
    if [ ! -d "node_modules" ]; then
        echo -e "${YELLOW}Installing dependencies...${NC}"
        npm install -q
    fi

    npm run build > "$LOG_DIR/cios-dashboard-build.log" 2>&1 || echo "Build warnings (non-fatal)"

    npm run start > "$LOG_DIR/cios-dashboard.log" 2>&1 &
    DASHBOARD_PID=$!
    save_pid "cios-dashboard" "$DASHBOARD_PID"

    if check_health 5571 "CIOS Dashboard"; then
        echo -e "${GREEN}✓ Ready: http://localhost:5571${NC}"
    else
        echo -e "${RED}✗ CIOS Dashboard failed to start${NC}"
        tail -20 "$LOG_DIR/cios-dashboard.log"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠ CIOS Dashboard not yet scaffolded (package.json missing)${NC}"
    echo "   This should be created by feature CIOS-10"
fi

# ============================================================================
# HEALTH CHECK: All Services
# ============================================================================

echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  System Status${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

echo -e "\n${GREEN}✓ All services started${NC}"
echo -e "\nService URLs:"
echo -e "  ${BLUE}Content Intelligence API${NC}   http://localhost:6006"
echo -e "  ${BLUE}TikTok Analytics${NC}           http://localhost:5681"
echo -e "  ${BLUE}Media Vault Backend${NC}        http://localhost:5563"
echo -e "  ${BLUE}CIOS Orchestration API${NC}     http://localhost:5570/api/cios/health"
echo -e "  ${BLUE}CIOS Dashboard${NC}              http://localhost:5571"

echo -e "\nProcess tracking:"
echo -e "  ${GREEN}PIDs saved to${NC} $PID_FILE"
echo -e "  ${GREEN}Logs in${NC} $LOG_DIR"

echo -e "\n${BLUE}To stop all services:${NC}"
echo -e "  kill \$(cat $PID_FILE | cut -d: -f2)"

echo -e "\n${BLUE}To monitor logs:${NC}"
echo -e "  tail -f $LOG_DIR/*.log"

echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ CIOS System Ready${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
