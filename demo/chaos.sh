#!/usr/bin/env bash
# Chaos mode: continuously kill and restart random nodes to demonstrate
# self-healing. Keeps submitting queries so the Web UI stays lively.
#
# Usage (from project root, network must already be running):
#   ./demo/chaos.sh            # default: kill 2 nodes every 20s
#   KILL_N=1 INTERVAL=10 ./demo/chaos.sh
#
# Node 1 is excluded — it hosts the AXL bootstrap listener (port 9001).
# Killing its AXL would break the mesh for all other nodes on rejoin.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
KILL_N="${KILL_N:-2}"        # nodes to kill per round
INTERVAL="${INTERVAL:-20}"   # seconds between chaos rounds
QUERIES=("attention" "gossip" "transformer" "alignment" "neural network" "consensus")

if [ "${FAST_MODE:-0}" = "1" ]; then
    LEASE_DURATION=5; RENEW_THRESHOLD=2; HEARTBEAT_INTERVAL=1; SUSPECT_AFTER=4
else
    LEASE_DURATION=30; RENEW_THRESHOLD=15; HEARTBEAT_INTERVAL=2; SUSPECT_AFTER=10
fi

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ROUND=0

cleanup() {
    echo -e "\n${YELLOW}Chaos stopped after ${ROUND} round(s).${NC}"
}
trap cleanup EXIT INT TERM

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Whisper Network — Chaos Mode${NC}"
echo -e "${CYAN}  Killing ${KILL_N} random node(s) every ${INTERVAL}s${NC}"
echo -e "${CYAN}  Ctrl-C to stop${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

_node_running() {
    pgrep -f "shard-id ${1} " > /dev/null 2>&1
}

_restart_node() {
    local i="$1"
    local API_PORT=$((9001 + i))
    local DEBUG_PORT=$((8887 + i))

    # Restart AXL first
    ./axl/node -config "axl-local/node-config-${i}.json" \
        >> "logs/axl-${i}.log" 2>&1 &

    sleep 0.5

    # Restart whisper node
    "${PYTHON}" -m whisper.node \
        --api-base            "http://127.0.0.1:${API_PORT}" \
        --shard-id            "${i}" \
        --shard-file          "demo/shards/shard-${i}.txt" \
        --ledger-file         "data/ledger-${i}.json" \
        --debug-port          "${DEBUG_PORT}" \
        --cluster-size        6 \
        --lease-duration      "${LEASE_DURATION}" \
        --renew-threshold     "${RENEW_THRESHOLD}" \
        --heartbeat-interval  "${HEARTBEAT_INTERVAL}" \
        --suspect-after       "${SUSPECT_AFTER}" \
        --key-file            "keys/private-${i}.pem" \
        --log-level           INFO \
        >> "logs/whisper-${i}.log" 2>&1 &
}

_submit_query() {
    local q="${QUERIES[$((RANDOM % ${#QUERIES[@]}))]}"
    "${PYTHON}" -m demo.submit_task "${q}" --api http://localhost:8888 --timeout 60 \
        > /dev/null 2>&1 &
}

while true; do
    ROUND=$((ROUND + 1))
    TS=$(date '+%H:%M:%S')

    # Pick KILL_N distinct random targets from nodes 2-6
    TARGETS=$(shuf -i 2-6 -n "${KILL_N}" | sort)

    echo -e "${RED}[${TS}] Round ${ROUND} — killing node(s): ${TARGETS//$'\n'/, }${NC}"

    for i in $TARGETS; do
        # Kill whisper process
        pids=$(pgrep -f "shard-id ${i} " 2>/dev/null || true)
        [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
        # Kill AXL process
        pids=$(pgrep -f "node-config-${i}\.json" 2>/dev/null || true)
        [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
    done

    # Submit a query immediately — it will have to survive the chaos
    _submit_query
    echo -e "  ${YELLOW}Submitted query — watching recovery...${NC}"

    # Wait for detection + recovery (FAST_MODE: ~12s, normal: ~40s)
    WAIT=$(( INTERVAL / 2 ))
    sleep "${WAIT}"

    echo -e "${GREEN}[$(date '+%H:%M:%S')] Round ${ROUND} — restarting node(s): ${TARGETS//$'\n'/, }${NC}"

    for i in $TARGETS; do
        _restart_node "${i}"
        echo -e "  restarted node-${i} (identity recovery active)"
    done

    REMAINING=$(( INTERVAL - WAIT ))
    echo -e "  next round in ${REMAINING}s..."
    sleep "${REMAINING}"
    echo ""
done
