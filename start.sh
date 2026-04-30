#!/usr/bin/env bash
# Container entrypoint: start AXL, wait for it, then start the whisper node.
set -euo pipefail

NODE_NUM="${NODE_NUM:-1}"
API_PORT="${API_PORT:-9002}"
DEBUG_PORT="${DEBUG_PORT:-8888}"
SHARD_ID="${SHARD_ID:-1}"
CLUSTER_SIZE="${CLUSTER_SIZE:-6}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

KEY_FILE="/keys/private.pem"
CONFIG_FILE="/config/node-config-${NODE_NUM}.json"
SHARD_FILE="/shards/shard-${SHARD_ID}.txt"
LEDGER_FILE="/data/ledger-${NODE_NUM}.json"

# ── Generate persistent identity key if not present ───────────────────────────
if [ ! -f "${KEY_FILE}" ]; then
    echo "[start.sh] generating ed25519 key at ${KEY_FILE}"
    openssl genpkey -algorithm ed25519 -out "${KEY_FILE}"
fi

# ── Start AXL in the background ───────────────────────────────────────────────
echo "[start.sh] starting AXL node ${NODE_NUM} (api_port=${API_PORT})"
/axl/node -config "${CONFIG_FILE}" &
AXL_PID=$!

# ── Wait for AXL HTTP API to become ready ────────────────────────────────────
echo "[start.sh] waiting for AXL API on 127.0.0.1:${API_PORT}..."
for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${API_PORT}/topology" > /dev/null 2>&1; then
        echo "[start.sh] AXL ready after ${i}s"
        break
    fi
    sleep 1
done

# ── Start the whisper node ────────────────────────────────────────────────────
echo "[start.sh] starting whisper node (shard=${SHARD_ID}, debug=:${DEBUG_PORT})"
exec python -m whisper.node \
    --api-base     "http://127.0.0.1:${API_PORT}" \
    --shard-id     "${SHARD_ID}" \
    --shard-file   "${SHARD_FILE}" \
    --ledger-file  "${LEDGER_FILE}" \
    --debug-port   "${DEBUG_PORT}" \
    --cluster-size "${CLUSTER_SIZE}" \
    --log-level    "${LOG_LEVEL}"
