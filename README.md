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
│  ↳ quorum guard: orphaned tasks require confirmed-dead nodes │
│    subtracted from effective cluster size                     │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: Distributed Task Ledger                             │
│  gossip-replicated, lease-based, conflict-free               │
│  ↳ topology-aware fanout: AXL-connected peers first          │
│  ↳ ed25519 signed: every ledger_update signed with AXL key   │
│  ↳ push notifications: task_result sent to submitter via AXL │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Gossip Membership (SWIM-lite)                       │
│  heartbeats, failure detection, peer gossip                   │
│  ↳ AXL topology is the authoritative peer registry           │
│  ↳ dynamic join: node_join broadcast on startup              │
│  ↳ AXL-corroborated failure: detects in ~5s not 10s         │
├──────────────────────────────────────────────────────────────┤
│  AXL (Gensyn Agent eXchange Layer)                           │
│  POST /send  GET /recv  GET /topology                        │
│  ↳ encrypted overlay (Yggdrasil + ed25519 identity)          │
│  ↳ polled every 5s — drives peer discovery & failover        │
│  ↳ bidirectional bus: task_submit in, task_result out        │
└──────────────────────────────────────────────────────────────┘
```

**AXL as the authoritative peer registry.** Every 5 seconds each node polls `/topology`. New peers in the AXL overlay are immediately added to whisper membership. Peers that disappear from the AXL mesh *and* have been silent for >5s are fast-tracked to SUSPECTED — cutting failure detection time roughly in half.

**Dynamic cluster join.** When a node starts (or restarts), it broadcasts a `node_join` AXL message to all directly-connected peers. Existing nodes add the joiner to membership immediately — no waiting for the next heartbeat or topology-sync cycle. The join gossips outward with hop-limited fanout so the whole mesh knows within one round.

**Topology-aware gossip.** Both layers sort gossip targets so AXL-directly-connected peers always get priority. Messages travel fewer overlay hops before reaching the full mesh.

**Cryptographic ledger integrity.** Every `ledger_update` gossip message is signed with the sending node's ed25519 private key (the same PEM used by AXL). Receiving nodes verify the signature before accepting any state change. Forged or tampered updates are dropped.

**AXL as a bidirectional application bus.** Tasks can be injected via `task_submit` AXL messages (no debug HTTP required). When each shard completes, the executing node sends a `task_result` push notification directly back to the submitter's AXL identity.

**Shard-affinity routing with smart quorum.** Each node has a home shard and claims it first. Orphaned tasks (dead node's shards) are claimed only when the survivor has quorum — confirmed-dead nodes are subtracted from the effective cluster size, so 3 survivors can act after killing 3 nodes in a 6-node cluster.

**Failure detection:** AXL topology drop + silence → SUSPECTED (fast path, ~5s). Otherwise: peer silent → SUSPECTED. 2 independent gossip reports → CONFIRMED DEAD → leases expire → survivors reclaim.

**Lease mechanism:** Configurable (default 30s, renewed every 15s). Graceful shutdown releases leases immediately. `FAST_MODE=1` uses 5s leases for demo pacing.

**Identity recovery.** A node that restarts with the same AXL key re-adopts its in-progress tasks by refreshing lease expiry. Peers see updated leases within one gossip round.

---

## Requirements

- Python 3.11+ (`requests`, `rich`, `cryptography`, `flask`, `flask-socketio` — see `requirements.txt`)
- AXL binary at `axl/node` (pre-built from `../axl/`)
- `openssl` (for key generation, called automatically by `run_local.sh`)
- Docker (optional — only for `versus.sh` Redis comparison)

---

## Quickstart

```bash
# 1. Start 6 nodes (FAST_MODE for quicker demos)
FAST_MODE=1 ./run_local.sh

# 2. Verify everything works
./demo/verify.sh

# 3. Open Web UI — http://localhost:5000
.venv/bin/python -m demo.webui

# 4. Submit a query
.venv/bin/python -m demo.submit_task "attention" --api http://localhost:8888
```

---

## Demo Scripts

### Kill 3 Nodes Mid-Execution (core demo)

```bash
# Option A — automated (recommended)
./demo/run_demo.sh "gossip"

# Option B — manual kill mid-flight
.venv/bin/python -m demo.submit_task "gossip" --api http://localhost:8888 --timeout 120 &
sleep 1
kill -9 $(pgrep -f "shard-id 4") $(pgrep -f "shard-id 5") $(pgrep -f "shard-id 6")
```

**With `FAST_MODE=1`:**

| Time | Event |
|------|-------|
| t+0s  | Nodes 4, 5, 6 killed |
| t+4s  | AXL mesh drop → fast-suspect |
| t+8s  | 2 independent reports → CONFIRMED DEAD |
| t+5s  | Leases expire (5s in FAST_MODE) |
| t+10s | Survivors claim + execute orphaned tasks |
| t+12s | All 6/6 COMPLETED |

### Chaos Mode (continuous self-healing)

```bash
# Kill and restart random nodes on a loop — keeps submitting queries
FAST_MODE=1 KILL_N=2 INTERVAL=20 ./demo/chaos.sh
```

Kills `KILL_N` random nodes every `INTERVAL` seconds, submits a query during each chaos window, waits for recovery, restarts nodes (identity recovery activates), repeats indefinitely.

### Network Partition + Heal

```bash
./demo/partition_demo.sh "transformer"
```

Uses `SIGSTOP`/`SIGCONT` on AXL processes to simulate a true network partition — both sides become isolated, the majority side reclaims tasks, then the mesh heals on `SIGCONT` and ledgers converge via gossip.

### Whisper vs Redis Side-by-Side

```bash
# Requires Docker (for Redis) + network running
FAST_MODE=1 ./run_local.sh &
./demo/versus.sh --query "gossip" --kill-after 4
```

Submits the same query to both Whisper Network and a Redis broker simultaneously. At `t+4s` kills Redis **and** kills whisper nodes 4-6 at the same time. One side recovers; the other freezes permanently.

### AXL P2P Submission

```bash
.venv/bin/python -m demo.submit_p2p "attention" --axl http://localhost:9002
```

Injects tasks directly through the AXL encrypted overlay (`task_submit` messages), receives `task_result` push notifications back via AXL `/recv` — no debug HTTP involved anywhere.

---

## Web UI

```bash
.venv/bin/python -m demo.webui   # http://localhost:5000
```

Three-panel live interface:

**Left — Node cards:** per-node status badge (alive/suspected/dead), shard ID, AXL mesh connectivity, active tasks held, completion metrics.

**Center — D3.js topology graph:** force-directed, draggable. Node circles colour-coded by status; glow pulse on suspected nodes; AXL mesh edges; shard label and active-task count badge inside each circle.

**Right — Live event feed + task list:** colour-coded stream of membership and ledger events merged across all 6 nodes (red = death, yellow = suspicion, green = recovery/join, blue = task events). Below: scrolling task list with status dots.

**Header bar:** alive/suspected/dead counts, total/done/active tasks, rescued tasks, MTTR (mean time to recovery across kill events), average task completion time, WebSocket connection indicator.

---

## Terminal Dashboard (alternative to Web UI)

```bash
.venv/bin/python -m demo.dashboard
```

---

## Project Layout

```
axl/
  node                  pre-built AXL binary
axl-configs/
  node-config-*.json    Docker-appropriate AXL configs (hostname-based peers)
axl-local/              per-node AXL configs generated by run_local.sh (gitignored)
keys/                   ed25519 keys generated by run_local.sh (gitignored)
logs/                   per-node logs

whisper/
  transport.py          AXL HTTP wrapper: /send, /recv, /topology
  membership.py         Layer 1: SWIM-lite gossip + dynamic node_join
  ledger.py             Layer 2: lease-based task ledger + gossip replication
  crypto.py             ed25519 sign/verify for ledger_update messages
  runtime.py            Layer 3: agent execution loop (shard-affinity + quorum)
  node.py               entry point: wires all layers + debug HTTP (:8888+n)

demo/
  webui.py              Flask+SocketIO server: polls /state, tracks MTTR, emits graph+events
  static/index.html     3-panel D3.js frontend
  dashboard.py          rich terminal UI
  verify.sh             automated end-to-end verification (5 checks)
  run_demo.sh           automated kill-3 demo with timing
  chaos.sh              continuous kill/restart chaos loop
  versus.sh             Whisper vs Redis side-by-side comparison
  partition_demo.sh     SIGSTOP/SIGCONT partition + heal
  submit_task.py        submit via debug HTTP, poll for results
  submit_p2p.py         submit via AXL P2P, receive via AXL /recv
  shards/shard-*.txt    6 AI/ML research document corpus files

comparison/
  redis_broker.py       centralized equivalent — freezes when Redis dies

docker-compose.yml      6-node Docker Compose setup
run_local.sh            6-node local setup (no Docker, verified working)
```

---

## Tuning Constants

All timing parameters are configurable via CLI flags passed through by `run_local.sh`.

| Parameter | Default | FAST_MODE | CLI flag |
|-----------|---------|-----------|----------|
| Heartbeat interval | 2s | 1s | `--heartbeat-interval` |
| Suspect threshold | 10s | 4s | `--suspect-after` |
| AXL fast-suspect | 5s | 2s | derived: `suspect_after / 2` |
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

**All nodes yellow (SUSPECTED) in Web UI**
AXL overlay connected but `/send` returning 502 — `tcp_port` differs across nodes. Stop everything, delete `axl-local/`, restart `./run_local.sh` (regenerates configs with shared `tcp_port: 7000`).

**`submit_p2p` submits but never shows results**
The whisper node on the same AXL instance consumes `task_result` messages off `/recv` before the script polls. Use `submit_task` for demos, or point `--axl` at a dedicated standalone AXL instance.

**`no quorum` — orphaned tasks not claimed**
Survivors can't see enough peers. Check if AXL sends work (Step 2 of `verify.sh`). With `cluster_size=6` you need at least 3 confirmed-dead + 3 alive for quorum to pass.

**Tasks stuck in `in_progress` after kill**
Lease hasn't expired. `FAST_MODE=1` → 5s wait; default → 30s wait.

**Chaos or versus script exits immediately**
Must be run from the project root with the network already running: `FAST_MODE=1 ./run_local.sh` first.

---

## AXL Gotchas

Discovered during implementation — not documented in AXL itself:

1. **`X-From-Peer-Id` is not the full public key.** It is a partial identifier derived from the Yggdrasil IPv6 address. Never use it for peer routing — always read `msg["from"]` from the JSON body.

2. **All nodes on the same machine must share `tcp_port: 7000`.** This port is the bridge routing destination. Using unique values per node causes `/send` to return **502** even though the Yggdrasil mesh shows peers as `"up": true`. `run_local.sh` hard-codes `tcp_port: 7000` for all nodes.

3. **Heartbeats loop back via gossip relay.** The dedup cache handles most cases, but `_on_heartbeat` must explicitly skip messages where `msg["from"] == our_key` to avoid a node adding itself to its own peer list.
