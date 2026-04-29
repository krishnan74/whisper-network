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

**Failure detection:** peer silent for >10s → SUSPECTED. 2 independent gossip reports → CONFIRMED DEAD → expired leases reclaimed by survivors.

**Lease mechanism:** 30s leases, renewed every 15s. If a node dies, renewal stops; lease expires; any survivor claims the task on its next 5s scan cycle.

**Any node handles any shard.** Each node loads all 6 document shards so surviving nodes can pick up work from dead ones.

---

## Requirements

- Python 3.11+ with `requests`, `rich`, and `redis` (see install step below)
- AXL binary at `axl/node` (pre-built from `../axl/` — see below)
- `openssl` (for key generation)

---

## Demo: Local (No Docker)

This is the verified path. All 6 AXL + whisper nodes run on one machine.

### 1. Ensure the AXL binary is present

```bash
ls axl/node   # should exist — was built from ../axl/
```

If missing:
```bash
cd ../axl && make build && cp node ../whisper-network/axl/node && cd ../whisper-network
```

### 2. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Start all 6 nodes

```bash
./run_local.sh
```

This generates 6 ed25519 keys (once), writes per-node AXL configs into `axl-local/`, then starts 6 AXL processes and 6 whisper nodes. Logs go to `logs/`. Leave this running in one terminal.

After ~15 seconds you should see output like:
```
  node-1: shard=1 peers=5 alive=5
  node-2: shard=2 peers=5 alive=5
  ...
```

### 4. Open the live dashboard (separate terminal)

```bash
cd /home/krish74/whisper-network
.venv/bin/python -m demo.dashboard
```

The dashboard polls all 6 nodes every second and shows node status, task ledger, and event log.

### 5. Submit a query

```bash
cd /home/krish74/whisper-network
.venv/bin/python -m demo.submit_task "attention" --api http://localhost:8888
```

Other good queries: `"gossip"`, `"neural network"`, `"alignment"`, `"transformer"`, `"consensus"`.

You will see all 6 tasks distributed and completed within ~10 seconds.

---

## The Money Shot: Kill 3 Nodes Mid-Execution

This is the core demo. Run it after the network is up and healthy.

### Option A — Kill before submitting (cleanest for judges)

```bash
# Kill nodes 4, 5, 6 with SIGKILL (no graceful shutdown)
kill -9 $(pgrep -f "shard-id 4") $(pgrep -f "shard-id 5") $(pgrep -f "shard-id 6")

# Now submit — the 3 surviving nodes must handle all 6 shards
.venv/bin/python -m demo.submit_task "gossip" --api http://localhost:8888 --timeout 120
```

### Option B — Kill mid-flight (more dramatic)

```bash
# Submit in the background
.venv/bin/python -m demo.submit_task "gossip" --api http://localhost:8888 --timeout 120 &

# Immediately kill 3 nodes
kill -9 $(pgrep -f "shard-id 4") $(pgrep -f "shard-id 5") $(pgrep -f "shard-id 6")

# Watch the progress bar stall, then recover
```

### What you will observe

| Time | Event |
|------|-------|
| t+0s  | Nodes 4, 5, 6 killed |
| t+10s | Surviving nodes mark them SUSPECTED (silent >10s) |
| t+11s | 2 independent suspicion reports → CONFIRMED DEAD |
| t+30s | Dead nodes' leases expire |
| t+35s | Surviving nodes claim and execute the 3 orphaned tasks |
| t+40s | All 6/6 tasks COMPLETED |

The dashboard's event log shows the exact SUSPECTED → CONFIRMED DEAD → claimed sequence in real time.

---

## Centralized Comparison (Redis)

Run this side-by-side to show what a centralized broker does when its coordinator dies.

```bash
# Start Redis (requires Docker or a local redis-server)
docker run --rm -d --name redis -p 6379:6379 redis:7

# Run the broker demo — same task, same shards
.venv/bin/python -m comparison.redis_broker --query "gossip"

# Kill Redis mid-execution (from another terminal):
docker kill redis
# The broker freezes instantly. No recovery. No reassignment.
```

Inject a timed kill for a scripted side-by-side:
```bash
.venv/bin/python -m comparison.redis_broker --query "gossip" --kill-at 3
```

---

## Project Layout

```
axl/
  node                  pre-built AXL binary
  node-config-*.json    AXL configs for Docker Compose
axl-local/              AXL configs generated by run_local.sh (gitignored)
keys/                   ed25519 keys generated by run_local.sh (gitignored)
logs/                   per-node logs written by run_local.sh

whisper/
  transport.py          thin wrapper: AXL /send, /recv, /topology
  membership.py         Layer 1: heartbeat + SWIM-lite failure detection
  ledger.py             Layer 2: lease-based task ledger + gossip replication
  runtime.py            Layer 3: agent execution loop (handles all shards)
  node.py               entry point: wires layers + debug HTTP server (:8888+n)

demo/
  submit_task.py        CLI: submit a query and wait for results
  dashboard.py          rich live terminal UI
  shards/shard-*.txt    6 AI/ML research document corpus files

comparison/
  redis_broker.py       centralized equivalent — freezes when Redis dies

docker-compose.yml      6-node Compose setup (requires `docker compose` plugin)
run_local.sh            6-node local setup (no Docker, verified working)
```

---

## Tuning Constants

| Parameter | Value | File |
|-----------|-------|------|
| Heartbeat interval | 2s | `membership.py` |
| Suspect threshold | 10s | `membership.py` |
| Dead reports needed | 2 | `membership.py` |
| Gossip fanout | 3 peers | both |
| Gossip hops | 8 | both |
| Lease duration | 30s | `ledger.py` |
| Lease renew threshold | 15s | `ledger.py` |
| Agent scan interval | 5s | `runtime.py` |

---

## AXL Gotchas

These were discovered during implementation and are not documented in AXL itself:

1. **`X-From-Peer-Id` is not the full public key.** It is a partial identifier derived from the Yggdrasil IPv6 address via `address.GetKey()`. Never use it to route AXL messages. Always read the sender key from the JSON message body (`msg["from"]`).

2. **All nodes on the same machine must share the same `tcp_port` (default 7000).** This port is used as both the local gVisor listener port AND the destination port when dialing remote peers. Only `api_port` needs to be unique per node. Using unique `tcp_port` values causes all cross-node sends to fail with "connection refused".

3. **Heartbeats loop back via gossip relay.** When a node forwards a heartbeat to its peers, those peers may forward it back. The dedup cache (`seen_ids`) handles most cases, but `_on_heartbeat` must explicitly check `if msg["from"] == our_key: return` to avoid a node adding itself to its own peer list.
