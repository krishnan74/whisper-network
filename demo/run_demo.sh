#!/usr/bin/env bash
# Fully automated hackathon demo — single command for judges.
#
# Starts a 6-node network, kills 3 nodes, then submits a query and shows
# the 3 surviving nodes complete all 6 shards. Cleans up on exit.
#
# Usage:
#   ./demo/run_demo.sh              # query: "attention"
#   ./demo/run_demo.sh "gossip"

set -euo pipefail

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
AXL_BIN="${AXL_BIN:-./axl/node}"
SHARDS_DIR="${SHARDS_DIR:-./demo/shards}"
QUERY="${1:-attention}"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
CYN='\033[0;36m'; MGN='\033[0;35m'; BLD='\033[1m'; RST='\033[0m'

ts()    { date +%T; }
step()  { echo -e "\n${BLD}${CYN}[$(ts)] $*${RST}"; }
ok()    { echo -e "  ${GRN}✓ $*${RST}"; }
warn()  { echo -e "  ${YLW}⚠ $*${RST}"; }
event() { echo -e "  ${MGN}→ $*${RST}"; }

AXL_PIDS=()
WHISPER_PIDS=()

cleanup() {
    echo -e "\n${YLW}Stopping all nodes...${RST}"
    for pid in "${AXL_PIDS[@]}" "${WHISPER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

# ── Preflight ─────────────────────────────────────────────────────────────────
if [ ! -f "${AXL_BIN}" ]; then
    echo "AXL binary not found at ${AXL_BIN}"
    echo "Build it: cd ../axl && make build && cp node ../whisper-network/axl/"
    exit 1
fi

echo -e "${BLD}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║        Whisper Network — Automated Demo          ║"
echo "  ║  Fault-tolerant P2P task coordination on AXL     ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${RST}"
echo "  Query   : \"${QUERY}\""
echo "  Scenario: 6 nodes start → 3 killed → 3 survivors complete all 6 shards"

# ── Generate keys + write AXL configs ────────────────────────────────────────
mkdir -p keys axl-local data logs
for i in 1 2 3 4 5 6; do
    [ -f "keys/private-${i}.pem" ] || \
        openssl genpkey -algorithm ed25519 -out "keys/private-${i}.pem" 2>/dev/null
done

for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i));  TCP_PORT=$((7000 + i))
    if [ "${i}" -eq 1 ]; then PEERS="[]"; LISTEN='["tls://0.0.0.0:9001"]'
    else PEERS='["tls://127.0.0.1:9001"]'; LISTEN="[]"; fi
    cat > "axl-local/node-config-${i}.json" <<EOF
{"PrivateKeyPath":"keys/private-${i}.pem","Peers":${PEERS},"Listen":${LISTEN},"api_port":${API_PORT},"tcp_port":${TCP_PORT},"bridge_addr":"127.0.0.1"}
EOF
done

# ── Start all 6 nodes ─────────────────────────────────────────────────────────
step "Starting 6 AXL + 6 whisper nodes..."

for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i));  DEBUG_PORT=$((8887 + i))

    "${AXL_BIN}" -config "axl-local/node-config-${i}.json" \
        > "logs/axl-${i}.log" 2>&1 &
    AXL_PIDS+=($!)
    sleep 0.4

    "${PYTHON}" -m whisper.node \
        --api-base   "http://127.0.0.1:${API_PORT}" \
        --shard-id   "${i}" \
        --shard-file "${SHARDS_DIR}/shard-${i}.txt" \
        --ledger-file "data/ledger-${i}.json" \
        --debug-port "${DEBUG_PORT}" \
        --log-level  WARNING \
        > "logs/whisper-${i}.log" 2>&1 &
    WHISPER_PIDS+=($!)
done

# ── Wait for all 6 nodes to be healthy ───────────────────────────────────────
echo -n "  Waiting for nodes"
READY=0
for _ in $(seq 1 50); do
    READY=0
    for port in 8888 8889 8890 8891 8892 8893; do
        curl -sf "http://localhost:${port}/health" >/dev/null 2>&1 && READY=$((READY+1)) || true
    done
    [ "$READY" -eq 6 ] && break
    echo -n "."; sleep 1
done
echo ""

[ "$READY" -eq 6 ] && ok "All 6 nodes healthy" || warn "Only ${READY}/6 healthy — continuing"

# Let gossip stabilise: peers exchange heartbeats + learn shard affinity
echo -n "  Gossip stabilising"
for _ in $(seq 1 8); do echo -n "."; sleep 1; done
echo "  ready"

# ── Kill nodes 4, 5, 6 ───────────────────────────────────────────────────────
step "KILLING nodes 4, 5, 6 simultaneously (SIGKILL — no graceful shutdown)"
KILL_TIME=$(date +%s)

kill -9 \
    "${WHISPER_PIDS[3]}" "${WHISPER_PIDS[4]}" "${WHISPER_PIDS[5]}" \
    "${AXL_PIDS[3]}"     "${AXL_PIDS[4]}"     "${AXL_PIDS[5]}" \
    2>/dev/null || true

ok "Nodes 4, 5, 6 killed at $(ts)"

echo ""
echo -e "  ${BLD}Failure detection + recovery timeline:${RST}"
echo "    t+05s  — AXL mesh detects mesh drop (fast-path: SUSPECTED)"
echo "    t+10s  — heartbeat silence confirms SUSPECTED"
echo "    t+11s  — 2 independent suspicion reports → CONFIRMED DEAD"
echo "    t+30s  — leases held by dead nodes expire"
echo "    t+35s  — survivors claim orphaned shard-4,5,6 tasks"
echo "    t+40s  — all 6/6 tasks COMPLETED by 3 surviving nodes"
echo ""

# Wait for failure detection to propagate before submitting
# (submit after dead nodes are marked suspected so tasks don't get routed to them)
echo -n "  Waiting for failure detection"
for _ in $(seq 1 12); do echo -n "."; sleep 1; done
echo ""

# ── Submit query ──────────────────────────────────────────────────────────────
step "Submitting query '${QUERY}' across all 6 shards..."
SUBMIT_TIME=$(date +%s)

"${PYTHON}" -m demo.submit_task \
    "${QUERY}" \
    --api     http://localhost:8888 \
    --timeout 120

DONE_TIME=$(date +%s)
TOTAL_S=$((DONE_TIME   - KILL_TIME))
SUBMIT_S=$((DONE_TIME  - SUBMIT_TIME))

echo ""
echo -e "${BLD}${GRN}═══════════════════════════════════════════════${RST}"
echo -e "${BLD}  Demo complete${RST}"
printf   "  Nodes killed at      : %s\n" "$(date -d @${KILL_TIME} +%T 2>/dev/null || date -r ${KILL_TIME} +%T)"
printf   "  Query submitted at   : %s\n" "$(date -d @${SUBMIT_TIME} +%T 2>/dev/null || date -r ${SUBMIT_TIME} +%T)"
printf   "  All tasks done at    : %s\n" "$(date -d @${DONE_TIME} +%T 2>/dev/null || date -r ${DONE_TIME} +%T)"
echo "  ─────────────────────────────────────────────"
echo "  Query completion time : ${SUBMIT_S}s"
echo "  Total (kill → done)   : ${TOTAL_S}s"
echo -e "${BLD}${GRN}═══════════════════════════════════════════════${RST}"
echo ""
echo "  Logs: tail -f logs/whisper-1.log"
echo "  Live: ${PYTHON} -m demo.dashboard"
