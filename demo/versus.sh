#!/usr/bin/env bash
# Side-by-side fault-tolerance comparison:
#   - Whisper Network (P2P, this project)  vs  Redis broker (centralized)
#
# Both run the same distributed query. At t+KILL_AFTER seconds we kill:
#   - Redis (coordinator)         → broker freezes permanently
#   - Nodes 4, 5, 6 in Whisper   → survivors detect, reclaim, complete
#
# Usage (from project root, network must already be running via run_local.sh):
#   ./demo/versus.sh
#   ./demo/versus.sh --query "transformer" --kill-after 5
#
# Requires: Docker (for Redis), run_local.sh already started with FAST_MODE=1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
QUERY="${QUERY:-gossip}"
KILL_AFTER="${KILL_AFTER:-4}"   # seconds after submit before killing both

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --query)       QUERY="$2";      shift 2 ;;
        --kill-after)  KILL_AFTER="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo -e "${CYAN}${BOLD}"
echo "═══════════════════════════════════════════════════════"
echo "  Whisper Network  vs  Redis Broker"
echo "  Query: '${QUERY}'  |  Kill at t+${KILL_AFTER}s"
echo "═══════════════════════════════════════════════════════"
echo -e "${NC}"

# ── Check prerequisites ───────────────────────────────────────────────────────
WHISPER_COUNT=$(pgrep -f "whisper.node" | wc -l)
if [ "$WHISPER_COUNT" -lt 6 ]; then
    echo -e "${RED}[ERROR] Whisper network not running (found ${WHISPER_COUNT}/6 nodes).${NC}"
    echo "  Run: FAST_MODE=1 ./run_local.sh"
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo -e "${RED}[ERROR] Docker not found — needed for Redis.${NC}"
    exit 1
fi

# ── Start Redis ───────────────────────────────────────────────────────────────
echo -e "${YELLOW}[setup]${NC} Starting Redis container..."
docker rm -f versus-redis 2>/dev/null || true
REDIS_ID=$(docker run -d --name versus-redis -p 6379:6379 redis:7 2>/dev/null)
sleep 1
echo -e "${GREEN}[setup]${NC} Redis running (container: ${REDIS_ID:0:12})"

cleanup() {
    docker rm -f versus-redis 2>/dev/null || true
}
trap cleanup EXIT

# ── Log files for side-by-side output ────────────────────────────────────────
WHISPER_LOG=$(mktemp /tmp/whisper_versus_XXXX.log)
REDIS_LOG=$(mktemp /tmp/redis_versus_XXXX.log)

# ── Launch both simultaneously ────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Submitting '${QUERY}' to both systems simultaneously...${NC}"
echo ""

START_TS=$SECONDS

"${PYTHON}" -m demo.submit_task "${QUERY}" \
    --api http://localhost:8888 --timeout 120 \
    > "${WHISPER_LOG}" 2>&1 &
WHISPER_PID=$!

"${PYTHON}" -m comparison.redis_broker \
    --query "${QUERY}" \
    > "${REDIS_LOG}" 2>&1 &
REDIS_PID=$!

# ── Kill both after KILL_AFTER seconds ───────────────────────────────────────
sleep "${KILL_AFTER}"
KILL_TS=$SECONDS

echo -e "${RED}[t+${KILL_AFTER}s] KILL EVENT${NC}"
echo -e "  ${RED}→ Killing Redis (coordinator)${NC}"
docker kill versus-redis > /dev/null 2>&1 || true

echo -e "  ${RED}→ Killing Whisper nodes 4, 5, 6 (half the cluster)${NC}"
for i in 4 5 6; do
    pids=$(pgrep -f "shard-id ${i} " 2>/dev/null || true)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
done
echo ""

# ── Wait for both to finish (or timeout) ─────────────────────────────────────
WHISPER_STATUS=0; REDIS_STATUS=0
wait "$WHISPER_PID" 2>/dev/null || WHISPER_STATUS=$?
wait "$REDIS_PID"   2>/dev/null || REDIS_STATUS=$?

END_TS=$SECONDS
WHISPER_TIME=$((END_TS - KILL_TS))
REDIS_TIME=$((END_TS - KILL_TS))

# ── Results ───────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════"
echo -e "${BOLD}  RESULTS${NC}"
echo "═══════════════════════════════════════════════════════"
echo ""

echo -e "${CYAN}${BOLD}  WHISPER NETWORK (P2P)${NC}"
if [ "$WHISPER_STATUS" -eq 0 ]; then
    echo -e "  ${GREEN}✓ COMPLETED in ~${WHISPER_TIME}s after 3 nodes killed${NC}"
    grep -E "shard-[0-9]:" "${WHISPER_LOG}" | sed 's/^/    /' || true
else
    echo -e "  ${RED}✗ Did not complete (exit ${WHISPER_STATUS})${NC}"
fi
echo ""

echo -e "${CYAN}${BOLD}  REDIS BROKER (centralized)${NC}"
if grep -q "TIMEOUT\|error\|Error\|freeze\|dead" "${REDIS_LOG}" 2>/dev/null || [ "$REDIS_STATUS" -ne 0 ]; then
    echo -e "  ${RED}✗ FROZEN — Redis is dead, no recovery possible${NC}"
    tail -3 "${REDIS_LOG}" | sed 's/^/    /' || true
else
    echo -e "  ${YELLOW}? Unexpected — check ${REDIS_LOG}${NC}"
fi
echo ""
echo "═══════════════════════════════════════════════════════"

# Clean up temp files
rm -f "${WHISPER_LOG}" "${REDIS_LOG}"
