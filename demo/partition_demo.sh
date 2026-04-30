#!/usr/bin/env bash
# Simulate a network partition: freeze AXL processes for nodes 4-6 (SIGSTOP),
# let nodes 1-3 detect the failure and reclaim tasks, then heal (SIGCONT)
# and watch the mesh reconverge.
#
# Usage (network must already be running via ./run_local.sh):
#   ./demo/partition_demo.sh [query]
#   ./demo/partition_demo.sh "transformer"

set -euo pipefail

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
QUERY="${1:-attention}"
API="http://localhost:8888"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

step() { echo -e "\n${BLD}${CYN}[$(date +%T)] $*${RST}"; }
info() { echo -e "  ${YLW}$*${RST}"; }
ok()   { echo -e "  ${GRN}✓ $*${RST}"; }
fail() { echo -e "  ${RED}✗ $*${RST}"; }

# ── Sanity check: network must be running ─────────────────────────────────────
step "Checking network health..."
if ! curl -sf "${API}/health" > /dev/null 2>&1; then
    fail "Cannot reach node-1 debug API at ${API}"
    echo "  Start the network first:  ./run_local.sh"
    exit 1
fi
ok "Network is up"

# ── Find AXL PIDs for group B (nodes 4-6, configs node-config-[456].json) ────
step "Locating AXL processes for group B (nodes 4, 5, 6)..."
AXLB_PIDS=()
for n in 4 5 6; do
    pid=$(pgrep -f "node-config-${n}\.json" 2>/dev/null || true)
    if [ -z "$pid" ]; then
        fail "Cannot find AXL process for node-${n} (expected config: axl-local/node-config-${n}.json)"
        exit 1
    fi
    AXLB_PIDS+=("$pid")
    info "  node-${n} AXL pid=${pid}"
done
ok "Found group B: PIDs ${AXLB_PIDS[*]}"

# ── Submit query before partition ─────────────────────────────────────────────
step "Submitting query '${QUERY}' across all 6 shards..."
${PYTHON} -m demo.submit_task "${QUERY}" --api "${API}" --timeout 30 &
SUBMIT_PID=$!

sleep 4  # let tasks spread across all nodes before partitioning

# ── Partition ─────────────────────────────────────────────────────────────────
step "PARTITIONING — freezing group B (nodes 4, 5, 6) with SIGSTOP"
info "Group A (nodes 1-3) can no longer reach group B."
info "Heartbeats will stop flowing. Leases held by group B will expire."
kill -STOP "${AXLB_PIDS[@]}"
PARTITION_TIME=$(date +%s)
ok "Group B frozen at $(date +%T)"

echo ""
echo -e "  ${BLD}Timeline from here:${RST}"
echo "    t+10s  — nodes 1-3 mark nodes 4-6 as SUSPECTED (heartbeat silence)"
echo "    t+11s  — 2 independent suspicion reports → CONFIRMED DEAD"
echo "    t+30s  — dead nodes' leases expire"
echo "    t+35s  — survivors claim and execute orphaned shard-4,5,6 tasks"
echo ""
info "Watching for recovery (check dashboard for live event log)..."

# Wait for task recovery to complete (or original submit to time out)
wait "${SUBMIT_PID}" 2>/dev/null || true
RECOVER_TIME=$(date +%s)

# ── Heal ─────────────────────────────────────────────────────────────────────
step "HEALING — resuming group B (nodes 4, 5, 6) with SIGCONT"
kill -CONT "${AXLB_PIDS[@]}"
HEAL_TIME=$(date +%s)
ok "Group B resumed at $(date +%T)"
info "Gossip will reconverge within 2-3 heartbeat intervals (~6s)."
info "Completed task results from group A will propagate to group B's ledger."
info "Dashboard will show REVIVED events as heartbeats resume."

echo ""
PARTITION_DURATION=$((HEAL_TIME - PARTITION_TIME))
RECOVERY_DURATION=$((RECOVER_TIME - PARTITION_TIME))
echo -e "${BLD}═══════════════════════════════════════════${RST}"
echo -e "  Partition duration   : ${PARTITION_DURATION}s"
echo -e "  Recovery completed   : ${RECOVERY_DURATION}s after partition"
echo -e "${BLD}═══════════════════════════════════════════${RST}"
echo ""
echo "Run the dashboard to see gossip reconvergence in real time:"
echo "  ${PYTHON} -m demo.dashboard"
