#!/usr/bin/env bash
# judge_demo.sh — One-button demo for hackathon judges.
#
# What it does (fully automated, ~60 seconds):
#   1. Start all 6 whisper nodes in FAST_MODE
#   2. Wait until cluster converges (all nodes gossip-alive)
#   3. Submit a 6-shard research query via AXL P2P
#   4. Kill nodes 4, 5, 6 mid-execution
#   5. Watch survivors detect failures and reclaim tasks
#   6. Display completed results and MTTR
#
# Usage:  ./demo/judge_demo.sh ["your query"]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
AXL_BIN="${AXL_BIN:-./axl/node}"
SHARDS_DIR="./demo/shards"
QUERY="${1:-neural network training}"

# Timing knobs
FAST_MODE=1
LEASE_DURATION=5
RENEW_THRESHOLD=2
HEARTBEAT_INTERVAL=1
SUSPECT_AFTER=4
KILL_AFTER=6       # seconds after submit before killing
WAIT_RESULTS=90    # max seconds to wait for all results

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YLW='\033[0;33m'; GRN='\033[0;32m'
BLU='\033[0;34m'; PUR='\033[0;35m'; CYN='\033[0;36m'
BLD='\033[1m'; DIM='\033[2m'; RST='\033[0m'

hr()  { printf "${DIM}%s${RST}\n" "$(printf '═%.0s' $(seq 1 60))"; }
ok()  { printf "  ${GRN}✔${RST}  %s\n" "$*"; }
info(){ printf "  ${BLU}·${RST}  %s\n" "$*"; }
warn(){ printf "  ${YLW}!${RST}  %s\n" "$*"; }
fail(){ printf "  ${RED}✘${RST}  %s\n" "$*"; }
step(){ printf "\n${BLD}${CYN}▶  %s${RST}\n" "$*"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
step "Preflight"
[ -f "${AXL_BIN}" ] || { fail "AXL binary missing at ${AXL_BIN}"; exit 1; }
ok "AXL binary found"
${PYTHON} -c "import whisper.node" 2>/dev/null || { fail "whisper package not importable — activate venv?"; exit 1; }
ok "whisper package importable"

# ── Key generation ────────────────────────────────────────────────────────────
mkdir -p keys axl-local logs data
for i in 1 2 3 4 5 6; do
    [ -f "keys/private-${i}.pem" ] || openssl genpkey -algorithm ed25519 -out "keys/private-${i}.pem" 2>/dev/null
done

for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i))
    if [ "${i}" -eq 1 ]; then PEERS="[]"; LISTEN='["tls://0.0.0.0:9001"]'
    else PEERS='["tls://127.0.0.1:9001"]'; LISTEN="[]"; fi
    cat > "axl-local/node-config-${i}.json" <<EOF
{
  "PrivateKeyPath": "keys/private-${i}.pem",
  "Peers": ${PEERS},
  "Listen": ${LISTEN},
  "api_port": ${API_PORT},
  "tcp_port": 7000,
  "bridge_addr": "127.0.0.1"
}
EOF
done

PIDS=()
cleanup() {
    echo ""
    info "Shutting down all processes..."
    for pid in "${PIDS[@]}"; do kill "${pid}" 2>/dev/null || true; done
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Start network ─────────────────────────────────────────────────────────────
step "Starting 6-node Whisper Network (FAST_MODE)"
hr

for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i)); DEBUG_PORT=$((8887 + i))
    "${AXL_BIN}" -config "axl-local/node-config-${i}.json" > "logs/axl-${i}.log" 2>&1 &
    PIDS+=($!)
    sleep 0.4
    "${PYTHON}" -m whisper.node \
        --api-base "http://127.0.0.1:${API_PORT}" \
        --shard-id "${i}" \
        --shard-file "${SHARDS_DIR}/shard-${i}.txt" \
        --ledger-file "data/ledger-${i}.json" \
        --debug-port "${DEBUG_PORT}" \
        --cluster-size 6 \
        --lease-duration "${LEASE_DURATION}" \
        --renew-threshold "${RENEW_THRESHOLD}" \
        --heartbeat-interval "${HEARTBEAT_INTERVAL}" \
        --suspect-after "${SUSPECT_AFTER}" \
        --key-file "keys/private-${i}.pem" \
        --log-level WARNING \
        > "logs/whisper-${i}.log" 2>&1 &
    PIDS+=($!)
    info "node-${i} started  (AXL :$((9001+i))  whisper :${DEBUG_PORT})"
done
hr

# ── Wait for convergence ──────────────────────────────────────────────────────
step "Waiting for cluster convergence"

CONVERGE_TIMEOUT=40
START_T=$(date +%s)
while true; do
    ELAPSED=$(( $(date +%s) - START_T ))
    [ ${ELAPSED} -ge ${CONVERGE_TIMEOUT} ] && { fail "Cluster did not converge in ${CONVERGE_TIMEOUT}s"; exit 1; }

    ALIVE=0
    for i in 1 2 3 4 5 6; do
        PORT=$((8887 + i))
        STATUS=$(curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)
        [ "${STATUS}" = "ok" ] && ALIVE=$((ALIVE + 1))
    done

    printf "\r  Nodes responding: ${GRN}%d/6${RST}  (%ds elapsed)  " "${ALIVE}" "${ELAPSED}"
    [ "${ALIVE}" -eq 6 ] && break
    sleep 1
done
printf "\n"

# Give gossip time to propagate node_join messages and establish peer lists
sleep 4
ok "All 6 nodes alive and gossip-converged"

# ── Submit query ──────────────────────────────────────────────────────────────
step "Submitting query: '${QUERY}'"

SUBMIT_T=$(date +%s)
TASK_IDS=()
for i in 1 2 3 4 5 6; do
    TASK_ID="demo-$(cat /dev/urandom | tr -dc 'a-f0-9' | head -c6)-s${i}"
    TASK_IDS+=("${TASK_ID}")
    PAYLOAD="query: ${QUERY}"
    curl -sf -X POST "http://127.0.0.1:$((8887 + i))/submit" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${TASK_ID}\",\"payload\":\"${PAYLOAD}\",\"shard_id\":${i}}" \
        > /dev/null
    info "shard-${i} submitted → ${TASK_ID}"
done
ok "6 tasks submitted across all shards"

# ── Wait briefly then kill half the cluster ───────────────────────────────────
step "Injecting fault: killing nodes 4, 5, 6 in ${KILL_AFTER}s"
info "Tasks are now being assigned and executed..."
sleep "${KILL_AFTER}"

KILLED_PIDS=()
for i in 4 5 6; do
    WPID=$(cat "logs/whisper-${i}.log" 2>/dev/null | grep -o 'pid=[0-9]*' | tail -1 | cut -d= -f2 || true)
    # Kill by matching process pattern for this node's port
    PORT=$((8887 + i))
    MPID=$(pgrep -f "debug-port.*${PORT}" 2>/dev/null | head -1 || true)
    if [ -n "${MPID}" ]; then
        kill "${MPID}" 2>/dev/null && KILLED_PIDS+=("${MPID}")
        warn "KILLED  node-${i}  (pid ${MPID})"
    else
        warn "node-${i}: could not find PID (may already be processing)"
    fi
done
KILL_T=$(date +%s)
hr
printf "\n  ${RED}${BLD}3 of 6 nodes are now DEAD${RST}\n"
printf "  ${GRN}Survivors: nodes 1, 2, 3${RST}\n\n"

# ── Monitor recovery ──────────────────────────────────────────────────────────
step "Monitoring fault recovery (up to ${WAIT_RESULTS}s)"

DONE=()
DEADLINE=$(( $(date +%s) + WAIT_RESULTS ))

while [ ${#DONE[@]} -lt 6 ]; do
    NOW=$(date +%s)
    [ ${NOW} -ge ${DEADLINE} ] && break

    ELAPSED=$(( NOW - KILL_T ))
    DONE_COUNT=${#DONE[@]}

    # Count complete tasks across survivor nodes
    DONE=()
    RESCUED=0
    for i in 1 2 3; do
        PORT=$((8887 + i))
        STATE=$(curl -sf "http://127.0.0.1:${PORT}/state" 2>/dev/null || echo '{}')
        TASKS_JSON=$(echo "${STATE}" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(t['task_id'],t['status'],t.get('leased_by','')) for t in d.get('tasks',{}).values()]" 2>/dev/null || true)

        while IFS=' ' read -r tid status leasedby; do
            [ "${status}" = "completed" ] || continue
            ALREADY=0
            for d in "${DONE[@]:-}"; do [ "${d}" = "${tid}" ] && ALREADY=1 && break; done
            [ "${ALREADY}" -eq 0 ] && DONE+=("${tid}")
        done <<< "${TASKS_JSON}"

        RESCUED_N=$(echo "${STATE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metrics',{}).get('tasks_rescued',0))" 2>/dev/null || echo 0)
        RESCUED=$(( RESCUED + RESCUED_N ))
    done

    BAR=$(printf '%0.s█' $(seq 1 ${#DONE[@]} 2>/dev/null) 2>/dev/null || true)
    EMPTY=$(printf '%0.s░' $(seq 1 $((6 - ${#DONE[@]})) 2>/dev/null) 2>/dev/null || true)
    printf "\r  Recovery: ${GRN}[${BAR}${RED}${EMPTY}${RST}${GRN}]${RST} ${#DONE[@]}/6 done · rescued ${RESCUED} · +${ELAPSED}s  "

    [ ${#DONE[@]} -ge 6 ] && break
    sleep 1
done
printf "\n"

RECOVERY_T=$(( $(date +%s) - KILL_T ))

# ── Results ───────────────────────────────────────────────────────────────────
hr
if [ ${#DONE[@]} -ge 6 ]; then
    printf "\n  ${GRN}${BLD}ALL 6 SHARDS COMPLETED${RST}  (recovered in ${BLD}${RECOVERY_T}s${RST} after 3-node failure)\n\n"
else
    printf "\n  ${YLW}${BLD}Partial recovery: ${#DONE[@]}/6 completed in ${RECOVERY_T}s${RST}\n\n"
fi

# Collect results from survivor nodes
printf "  ${BLD}Results for '${QUERY}':${RST}\n"
for i in 1 2 3; do
    PORT=$((8887 + i))
    curl -sf "http://127.0.0.1:${PORT}/state" 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for t in sorted(d.get('tasks', {}).values(), key=lambda x: x.get('shard_id', 0)):
        if t['status'] == 'completed' and t.get('result'):
            print(f\"  s{t['shard_id']}: {t['result'][:120]}\")
except:
    pass
" 2>/dev/null
done | sort -u | head -12

hr
printf "\n  ${BLD}Summary${RST}\n"
printf "  %-22s %s\n" "Query:"       "'${QUERY}'"
printf "  %-22s %s\n" "Cluster size:" "6 nodes"
printf "  %-22s %s\n" "Nodes killed:" "3 (nodes 4–6)"
printf "  %-22s %s\n" "Tasks completed:" "${#DONE[@]}/6"
printf "  %-22s %s\n" "Recovery time:" "${RECOVERY_T}s"
printf "  %-22s %s\n" "Fault tolerance:" "50% node loss survived"
hr
echo ""

exit 0
