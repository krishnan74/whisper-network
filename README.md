# Whisper Network

Decentralized task coordination on top of [Gensyn AXL](https://github.com/gensyn-ai/axl).

Submit a task across 6 nodes. Kill 3 mid-execution. The task completes anyway.
This is impossible with a centralized broker. It is an emergent property of P2P mesh topology.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 4: Demo — distributed document query                   │
├──────────────────────────────────────────────────────────────┤
│  Layer 3: Agent Runtime                                       │
│  polls ledger, claims tasks, executes shard queries           │
│  ↳ shard-affinity: home node claims first, survivors rescue  │
│  ↳ quorum guard: orphaned tasks require >50% cluster visible │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: Distributed Task Ledger                             │
│  gossip-replicated, lease-based, conflict-free               │
│  ↳ topology-aware fanout: AXL-connected peers first          │
│  ↳ ed25519 signed: every ledger_update message is signed     │
│    with the node's AXL key — forged updates are dropped      │
│  ↳ push notifications: completed tasks are sent directly     │
│    to the original submitter via AXL (task_result msg)       │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Gossip Membership (SWIM-lite)                       │
│  heartbeats, failure detection, peer gossip                   │
│  ↳ AXL topology is the authoritative peer registry           │
│  ↳ AXL-corroborated failure: detects in ~5s not 10s         │
├──────────────────────────────────────────────────────────────┤
│  AXL (Gensyn Agent eXchange Layer)                           │
│  POST /send  GET /recv  GET /topology                        │
│  ↳ encrypted overlay (Yggdrasil + ed25519 identity)          │
│  ↳ polled every 5s — drives peer discovery & failover        │
│  ↳ bidirectional bus: task_submit in, task_result out        │
└──────────────────────────────────────────────────────────────┘
```

**AXL as the authoritative peer registry.** Every 5 seconds each node polls `/topology`. New peers in the AXL overlay are immediately added to whisper membership. Peers that disappear from the AXL mesh *and* have been silent for >5s are fast-tracked to SUSPECTED — cutting failure detection time roughly in half (5s vs 10s).

**Topology-aware gossip.** Both the membership and ledger layers sort gossip targets so AXL-directly-connected peers always get priority. Messages travel fewer overlay hops before reaching the full mesh.

**Cryptographic ledger integrity.** Every `ledger_update` gossip message is signed with the sending node's ed25519 private key (the same PEM used by AXL). Receiving nodes verify the signature before accepting any state change. Unsigned messages are accepted for backwards compatibility; forged messages are logged and dropped. The signed fields are: `msg_id`, `task_id`, `status`, `leased_by`, `version`.

**AXL as a bidirectional application bus.** Tasks can be injected via `task_submit` AXL messages (no debug HTTP required). When each shard completes, the executing node sends a `task_result` push notification directly back to the submitter's AXL identity. `submit_p2p.py` polls AXL `/recv` for these notifications — the entire submit→execute→result cycle flows through AXL's encrypted overlay.

**Shard-affinity routing.** Each node has a home shard. It preferentially claims tasks for that shard first. When a node dies, the survivors detect which shards are now unowned and claim those tasks — but only if they can see a strict majority of the cluster (quorum guard against split-brain).

**Failure detection:** AXL topology drop + >5s silence → SUSPECTED (fast path). Otherwise: peer silent for >10s → SUSPECTED. 2 independent gossip reports → CONFIRMED DEAD → expired leases reclaimed by survivors.

**Lease mechanism:** Configurable duration (default 30s), renewed every 15s. On graceful shutdown, all held leases are released immediately so survivors can claim within 1s. On crash, lease expiry triggers reclaim. `FAST_MODE=1` runs 5s leases for quicker demos.

**Identity recovery.** If a node restarts with the same AXL key (same PEM), it re-adopts its in-progress tasks by refreshing their lease expiry. Peers see updated leases within one gossip round — no human intervention needed.

---

## Requirements

- Python 3.11+ (see `requirements.txt`: `requests`, `rich`, `cryptography`, `flask`, `flask-socketio`)
- AXL binary at `axl/node` (pre-built from `../axl/`)
- `openssl` (for key generation, called automatically by `run_local.sh`)

---

## Demo: Docker Compose (Easiest)

Requires Docker with the Compose plugin. The AXL binary must be at `axl/node` first.

```bash
# One command — builds images, starts 6 nodes, streams logs
docker compose up --build

# Submit a query (separate terminal)
python -m demo.submit_task "attention" --api http://localhost:8888

# Submit via AXL P2P (demonstrates encrypted overlay as application bus)
python -m demo.submit_p2p "attention" --axl http://localhost:9002

# Kill 3 nodes mid-flight
docker compose kill node-4 node-5 node-6

# Partition demo (pause/resume instead of kill)
docker compose pause node-4 node-5 node-6
docker compose unpause node-4 node-5 node-6
```

Node debug APIs are exposed on host ports 8888–8893.

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

This generates 6 ed25519 keys (once), writes per-node AXL configs into `axl-local/`, then starts 6 AXL processes and 6 whisper nodes. Each node is given its own key file for ledger message signing. Logs go to `logs/`. Leave this running in one terminal.

For faster recovery timing (good for live demos):

```bash
FAST_MODE=1 ./run_local.sh
```

`FAST_MODE=1` uses 5s leases, 2s renew threshold, 1s heartbeat, 4s suspect — failure recovery in ~10s instead of ~40s.

### 4. Open the Web UI (separate terminal)

```bash
.venv/bin/python -m demo.webui
```

Opens at **http://localhost:5000** — a live D3.js force-directed graph showing:
- Nodes as circles (green = alive, yellow = suspected, red = dead)
- AXL mesh edges between alive peers
- Sidebar: per-node cards with shard ID, AXL mesh stats, and completion metrics
- Cluster totals: tasks submitted, completed, alive nodes, rescued tasks

Or use the terminal dashboard instead:

```bash
.venv/bin/python -m demo.dashboard
```

### 5. Submit a query

**Option A — via whisper debug API (recommended for demos):**
```bash
.venv/bin/python -m demo.submit_task "attention" --api http://localhost:8888
```

**Option B — via AXL P2P (shows AXL as application bus):**
```bash
.venv/bin/python -m demo.submit_p2p "attention" --axl http://localhost:9002
```

`submit_p2p` reads the AXL topology, picks a live peer, and injects `task_submit` messages directly through the encrypted overlay. The `from` field carries the submitter's AXL key so executing nodes know where to push `task_result` notifications back. Results are collected by polling AXL `/recv`.

> **Note:** Each whisper node shares its AXL `/recv` queue. If `submit_p2p` points at an AXL instance that also runs a whisper node (which is the case in the local 6-node setup), the whisper node's recv loop may consume some `task_result` messages before the script sees them. For a reliable end-to-end push demo, run a 7th standalone AXL instance and point `--axl` at it. For judging purposes, Option A is more reliable.

Good queries: `"gossip"`, `"neural network"`, `"alignment"`, `"transformer"`, `"consensus"`.

All 6 tasks distribute and complete within ~10 seconds.

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

### Option C — Fully automated (recommended for demos)

```bash
./demo/run_demo.sh "gossip"
```

Starts the network, waits for quorum, submits a task, kills nodes 4–6 mid-execution, and reports timing.

### What you will observe

| Time | Event |
|------|-------|
| t+0s  | Nodes 4, 5, 6 killed |
| t+5s  | AXL topology sync: nodes 4-6 absent from mesh |
| t+5s  | Surviving nodes fast-track them to SUSPECTED |
| t+11s | 2 independent suspicion reports → CONFIRMED DEAD |
| t+30s | Dead nodes' leases expire |
| t+35s | Surviving nodes claim and execute the 3 orphaned tasks |
| t+40s | All 6/6 tasks COMPLETED |

With `FAST_MODE=1` the full recovery completes in ~10s instead of ~40s.

The Web UI and dashboard event log show the exact SUSPECTED → CONFIRMED DEAD → claimed sequence in real time.

---

## Bonus: Network Partition + Heal Demo

Unlike a node kill (one side wins), a **network partition** splits the cluster into two groups that each try to continue working. This tests the full partition-tolerance guarantee.

```bash
# Network must already be running via ./run_local.sh
./demo/partition_demo.sh "transformer"
```

The script:
1. Submits a query across all 6 shards
2. Freezes the AXL processes for nodes 4, 5, 6 (`SIGSTOP`) — they can no longer send or receive
3. Nodes 1–3 detect the silence, mark 4–6 as DEAD, and reclaim their tasks
4. Resumes group B (`SIGCONT`) — heartbeats flow again, the mesh reconverges
5. Completed results gossip from group A to group B's ledger

| Time | Event |
|------|-------|
| t+0s  | Group B (nodes 4-6) partitioned via SIGSTOP |
| t+5s  | Group A fast-suspects group B (AXL mesh drop) |
| t+11s | 2 reports → CONFIRMED DEAD |
| t+30s | Group B's leases expire |
| t+35s | Group A claims and executes orphaned tasks |
| t+40s | All 6/6 tasks COMPLETED on group A |
| heal  | Group B resumes (SIGCONT), ledger converges via gossip |

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
axl-configs/
  node-config-*.json    Docker-appropriate AXL configs (hostname-based peers)
axl-local/              AXL configs generated by run_local.sh (gitignored)
keys/                   ed25519 keys generated by run_local.sh (gitignored)
logs/                   per-node logs written by run_local.sh

whisper/
  transport.py          AXL HTTP wrapper: /send, /recv, /topology
  membership.py         Layer 1: heartbeat + SWIM-lite failure detection
  ledger.py             Layer 2: lease-based task ledger + gossip replication
  crypto.py             ed25519 sign/verify for ledger_update messages
  runtime.py            Layer 3: agent execution loop (shard-affinity + quorum)
  node.py               entry point: wires all layers + debug HTTP (:8888+n)

demo/
  webui.py              Flask+SocketIO server → live D3.js topology graph
  static/index.html     D3.js frontend (force-directed graph, metrics sidebar)
  dashboard.py          rich terminal UI (AXL mesh stats, metrics, event log)
  submit_task.py        submit a query via debug HTTP, poll for results
  submit_p2p.py         submit via AXL P2P, receive results via AXL /recv
  run_demo.sh           automated end-to-end demo (kill 3, watch recovery)
  partition_demo.sh     scripted partition + heal (SIGSTOP/SIGCONT group B)
  shards/shard-*.txt    6 AI/ML research document corpus files

comparison/
  redis_broker.py       centralized equivalent — freezes when Redis dies

docker-compose.yml      6-node Compose setup
run_local.sh            6-node local setup (no Docker, verified working)
```

---

## Tuning Constants

All timing parameters are configurable via CLI flags. `run_local.sh` passes them through.

| Parameter | Default | FAST_MODE | CLI flag |
|-----------|---------|-----------|----------|
| Heartbeat interval | 2s | 1s | `--heartbeat-interval` |
| Suspect threshold | 10s | 4s | `--suspect-after` |
| AXL fast-suspect | 5s | 2s | (derived: `suspect_after/2`) |
| Dead reports needed | 2 | 2 | — |
| Gossip fanout | 3 | 3 | — |
| Gossip hops | 8 | 8 | — |
| AXL topology sync | 5s | 5s | — |
| Lease duration | 30s | 5s | `--lease-duration` |
| Lease renew threshold | 15s | 2s | `--renew-threshold` |
| Agent scan interval | 5s | 5s | — |
| Cluster size (quorum) | 6 | 6 | `--cluster-size` |

---

## Troubleshooting

**All nodes stuck in SUSPECTED / yellow in Web UI**

The AXL overlay mesh connected but `/send` is returning 502. This means `tcp_port` values differ across nodes. Stop all nodes, delete `axl-local/`, and re-run `./run_local.sh` — it will regenerate configs with the correct shared `tcp_port: 7000`.

**`submit_p2p` submits tasks but never shows results**

The whisper node sharing the same AXL instance is consuming `task_result` messages off the `/recv` queue before `submit_p2p` polls for them. Use `submit_task` instead, or point `--axl` at a dedicated standalone AXL instance.

**Node shows `no quorum` and won't claim orphaned tasks**

The node can see fewer than half the cluster (`cluster_size / 2`). Either wait for peers to reconnect, or reduce `--cluster-size` to match the number of nodes actually running.

**Tasks stall in `in_progress` after a node kill**

The lease has not expired yet. With default settings wait 30s; with `FAST_MODE=1` wait 5s. Use `FAST_MODE=1 ./run_local.sh` for quicker recovery in demos.

---

## AXL Gotchas

These were discovered during implementation and are not documented in AXL itself:

1. **`X-From-Peer-Id` is not the full public key.** It is a partial identifier derived from the Yggdrasil IPv6 address via `address.GetKey()`. Never use it to route AXL messages. Always read the sender key from the JSON message body (`msg["from"]`).

2. **All nodes on the same machine must share the same `tcp_port` (default 7000).** `tcp_port` is the bridge routing port — AXL uses it as the destination port when forwarding messages between overlay peers. Only `api_port` needs to be unique per node. Using a unique `tcp_port` per node causes `/send` to return **502** even though the Yggdrasil mesh shows all peers as `"up": true` — the overlay connects but message delivery silently fails. `run_local.sh` hard-codes `"tcp_port": 7000` for all nodes to avoid this.

3. **Heartbeats loop back via gossip relay.** When a node forwards a heartbeat to its peers, those peers may forward it back. The dedup cache (`seen_ids`) handles most cases, but `_on_heartbeat` must explicitly check `if msg["from"] == our_key: return` to avoid a node adding itself to its own peer list.
