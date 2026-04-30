#!/usr/bin/env bash
# Run all 6 nodes locally (no Docker) on a single machine.
# Each AXL instance gets a unique api_port and tcp_port.
# All 6 nodes use the same Yggdrasil peer address (node-1 listens on 9001).
#
# Usage: ./run_local.sh [query]
# Ctrl-C to stop all nodes.
set -euo pipefail

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
AXL_BIN="${AXL_BIN:-./axl/node}"
SHARDS_DIR="${SHARDS_DIR:-./demo/shards}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

if [ ! -f "${AXL_BIN}" ]; then
    echo "AXL binary not found at ${AXL_BIN}"
    echo "Build it first:  cd ../axl && make build && cp node ../whisper-network/axl/"
    exit 1
fi

# Generate keys if needed
mkdir -p keys
for i in 1 2 3 4 5 6; do
    if [ ! -f "keys/private-${i}.pem" ]; then
        echo "Generating key for node-${i}..."
        openssl genpkey -algorithm ed25519 -out "keys/private-${i}.pem"
    fi
done

# Write per-node AXL configs with unique ports
mkdir -p axl-local
for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i))
    TCP_PORT=$((7000 + i))
    if [ "${i}" -eq 1 ]; then
        PEERS="[]"
        LISTEN='["tls://0.0.0.0:9001"]'
    else
        PEERS='["tls://127.0.0.1:9001"]'
        LISTEN="[]"
    fi
    cat > "axl-local/node-config-${i}.json" <<EOF
{
  "PrivateKeyPath": "keys/private-${i}.pem",
  "Peers": ${PEERS},
  "Listen": ${LISTEN},
  "api_port": ${API_PORT},
  "tcp_port": ${TCP_PORT},
  "bridge_addr": "127.0.0.1"
}
EOF
done

PIDS=()
cleanup() {
    echo ""
    echo "Stopping all nodes..."
    for pid in "${PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

mkdir -p logs data

echo "Starting 6 AXL + whisper nodes..."
for i in 1 2 3 4 5 6; do
    API_PORT=$((9001 + i))
    DEBUG_PORT=$((8887 + i))
    echo "  node-${i}: AXL api_port=${API_PORT}  whisper debug=:${DEBUG_PORT}"

    "${AXL_BIN}" -config "axl-local/node-config-${i}.json" \
        > "logs/axl-${i}.log" 2>&1 &
    PIDS+=($!)

    sleep 0.5  # stagger startup slightly

    "${PYTHON}" -m whisper.node \
        --api-base     "http://127.0.0.1:${API_PORT}" \
        --shard-id     "${i}" \
        --shard-file   "${SHARDS_DIR}/shard-${i}.txt" \
        --ledger-file  "data/ledger-${i}.json" \
        --debug-port   "${DEBUG_PORT}" \
        --cluster-size 6 \
        --log-level    "${LOG_LEVEL}" \
        > "logs/whisper-${i}.log" 2>&1 &
    PIDS+=($!)
done

echo ""
echo "All nodes started. Debug APIs on ports 8888-8893."
echo "  Dashboard:  .venv/bin/python -m demo.dashboard"
echo "  Submit:     .venv/bin/python -m demo.submit_task 'neural network'"
echo "  Logs:       tail -f logs/whisper-1.log"
echo ""
echo "Press Ctrl-C to stop all nodes."
wait
