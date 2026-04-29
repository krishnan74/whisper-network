# Whisper Network

Decentralized task coordination on top of [Gensyn AXL](https://github.com/gensyn-ai/axl).

Submit a task across 6 nodes. Kill 3 mid-execution. The task completes anyway.
This is impossible with a centralized broker. It is an emergent property of P2P mesh topology.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Layer 4: Demo — distributed document query  │
├─────────────────────────────────────────────┤
│  Layer 3: Agent Runtime                      │
│  polls ledger, claims tasks, executes        │
├─────────────────────────────────────────────┤
│  Layer 2: Distributed Task Ledger            │
│  gossip-replicated, lease-based, append-only │
├─────────────────────────────────────────────┤
│  Layer 1: Gossip Membership (SWIM-lite)      │
│  heartbeats, failure detection, peer gossip  │
├─────────────────────────────────────────────┤
│  AXL (pre-built binary)                      │
│  POST /send  GET /recv  GET /topology        │
└─────────────────────────────────────────────┘
```

**Failure detection**: peer silent for >6s → SUSPECTED. 2 independent reports → CONFIRMED DEAD → expired leases reclaimed.

**Lease mechanism**: 60s leases, renewed every 30s. If a node dies, its lease expires and any survivor claims the task within one scan cycle (5s).

---

## Quick Start (Docker Compose)

### 1. Build

The AXL binary must be built first and placed at `axl/node`:

```bash
cd ../axl
make build
cp node ../whisper-network/axl/node
cd ../whisper-network
```

### 2. Run all 6 nodes

```bash
docker compose up --build
```

Node debug APIs are available at `localhost:8888` through `localhost:8893`.

### 3. Open the dashboard (separate terminal)

```bash
pip install -r requirements.txt
python -m demo.dashboard
```

### 4. Submit a query

```bash
python -m demo.submit_task "neural network" --api http://localhost:8888
python -m demo.submit_task "gossip"
python -m demo.submit_task "attention"
```

### 5. Kill 3 nodes mid-execution (the money shot)

```bash
docker compose kill node-4 node-5 node-6
```

Watch the dashboard: nodes go SUSPECTED → DEAD → surviving nodes reclaim tasks → all 6 tasks complete.

---

## Running Without Docker (local dev)

You need 6 AXL instances with different ports. A convenience script:

```bash
./run_local.sh
```

Or manually:

```bash
# Terminal per node (open 6):
./axl/node -config axl/node-config-1.json &
python -m whisper.node --shard-id 1 --shard-file demo/shards/shard-1.txt \
    --api-base http://127.0.0.1:9002 --debug-port 8888

./axl/node -config axl/node-config-2.json &   # needs unique tcp_port + api_port
python -m whisper.node --shard-id 2 --shard-file demo/shards/shard-2.txt \
    --api-base http://127.0.0.1:9012 --debug-port 8889
# ... etc.
```

Local node-config files for running multiple nodes on one machine:
- Each needs a unique `api_port` and `tcp_port`
- Nodes 2-6 peer to node-1 via `tls://127.0.0.1:9001`

---

## Centralized Comparison (Redis)

Run alongside Whisper to show the contrast:

```bash
# Start Redis
docker compose --profile comparison up redis

# Run the broker demo
python -m comparison.redis_broker --query "neural network"

# Kill Redis mid-execution (from another terminal):
docker compose kill redis
# The broker freezes. No recovery. Contrast with Whisper Network.
```

Or inject a timed kill:

```bash
python -m comparison.redis_broker --query "neural network" --kill-at 3
```

---

## Project Layout

```
whisper/
  transport.py   — thin wrapper around AXL /send /recv /topology
  membership.py  — Layer 1: SWIM-lite gossip membership + failure detection
  ledger.py      — Layer 2: distributed task ledger with lease management
  runtime.py     — Layer 3: per-node task execution loop
  node.py        — main entry point; wires layers together + debug HTTP server

demo/
  submit_task.py — CLI to submit a query and wait for results
  dashboard.py   — rich live terminal UI
  shards/        — 6 document corpus text files (AI/ML research notes)

comparison/
  redis_broker.py — identical system using Redis; freezes when Redis dies

axl/
  node-config-*.json — AXL configs for 6 nodes
```

## Tuning Constants

| Parameter | Default | File |
|-----------|---------|------|
| Heartbeat interval | 2s | `membership.py` |
| Suspect threshold | 6s | `membership.py` |
| Dead reports needed | 2 | `membership.py` |
| Gossip fanout | 3 | both |
| Gossip hops | 8 | both |
| Lease duration | 60s | `ledger.py` |
| Lease renew threshold | 30s | `ledger.py` |
| Agent scan interval | 5s | `runtime.py` |
