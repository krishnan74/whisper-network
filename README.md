# Whisper Network

**A trustless AI agent compute market built on [Gensyn AXL](https://github.com/gensyn-ai/axl).**

Compute providers join a peer-to-peer mesh via AXL. Agents submit inference jobs across the mesh. Providers bid, claim, and execute jobs — coordinated entirely through a gossip-replicated ledger. No central broker. No single point of failure.

Kill half the providers mid-inference. The jobs complete anyway.

---

## What This Solves

**The Coordinator Problem:** Every existing AI inference marketplace routes jobs through a central API or broker. When the broker dies, all in-flight work is lost. Providers sit idle. Users wait forever.

**Whisper Network eliminates the coordinator.** Job state is replicated peer-to-peer across every node in real time. When a provider fails, the other providers detect it via gossip, reclaim its jobs, and complete them — without any human intervention or central authority.

**Why AXL?** AXL gives every provider a cryptographic identity (ed25519) and an encrypted overlay network (Yggdrasil). Whisper Network uses these directly:
- Provider identities are AXL ed25519 keys — no separate registration
- All job messages travel over the AXL encrypted mesh — no separate transport layer
- Job results are pushed back to submitters via AXL — bidirectional application bus
- The AXL topology is the authoritative peer registry — no separate discovery service
- X25519 payload encryption is derived from AXL identity keys — no separate key management

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Application: Distributed AI inference across 6 compute providers            │
│  Each shard = one provider's local model / document corpus segment           │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 3: Agent Runtime (Provider Execution)                                  │
│  polls job ledger → claims jobs → decrypts payload → executes → pushes result│
│  ↳ shard-affinity: home provider claims first; survivors rescue on failure   │
│  ↳ quorum guard: dead providers subtracted from effective cluster size       │
│  ↳ payload decryption: X25519 ECDH + AES-GCM using AXL identity keys        │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 2: Trustless Job Ledger (Gossip-Replicated)                           │
│  lease-based job ownership • version-vector conflict resolution               │
│  ↳ AXL-topology-aware fanout: mesh-connected peers get gossip priority       │
│  ↳ ed25519 signed: every ledger_update signed with provider's AXL key        │
│  ↳ push notifications: job_result sent to submitter's AXL identity           │
│  ↳ payload encryption: job payloads encrypted to home provider's X25519 key  │
├──────────────────────────────────────────────────────────────────────────────┤
│  Layer 1: Gossip Membership (SWIM-lite Failure Detection)                    │
│  heartbeats • suspicion • confirmation dead • dynamic join                   │
│  ↳ AXL topology = authoritative provider registry                            │
│  ↳ node_join broadcast on startup — instant mesh convergence                 │
│  ↳ AXL-corroborated failure detection: ~5s (half normal gossip window)       │
│  ↳ dynamic cluster size: quorum adapts as providers join/leave               │
├──────────────────────────────────────────────────────────────────────────────┤
│  AXL (Gensyn Agent eXchange Layer) — the foundation                         │
│  POST /send  GET /recv  GET /topology                                        │
│  ↳ ed25519 identity per provider (same PEM for signing + X25519 derivation) │
│  ↳ encrypted Yggdrasil overlay — all job traffic is private end-to-end      │
│  ↳ bidirectional bus: job_submit in / job_result out                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

**AXL as the authoritative peer registry.** Every 5 seconds each provider polls `/topology`. New providers in the AXL overlay are immediately added to membership. Providers that disappear from the AXL mesh *and* have been silent for >5s are fast-tracked to SUSPECTED — cutting failure detection time roughly in half.

**Dynamic cluster join.** When a provider starts (or restarts), it broadcasts a `node_join` AXL message to all directly-connected peers. Existing providers add the newcomer to membership immediately — no waiting for heartbeat cycles or topology sync. The join gossips outward with hop-limited fanout.

**Cryptographic ledger integrity.** Every `ledger_update` gossip message is signed with the sender's ed25519 private key (the same PEM used by AXL). Receiving providers verify the signature before accepting any state change. Forged or tampered updates are silently dropped.

**Payload encryption via AXL identity.** When a job is submitted to shard-N, the payload is encrypted using X25519 ECDH — key derived from the home provider's ed25519 AXL identity. Only the home provider can decrypt and execute it. If the home provider fails and a survivor reclaims the job, it reports honestly: `[encrypted payload — home provider offline]`.

**Trustless job lifecycle via AXL:**
1. Submitter sends `task_submit` → AXL encrypted mesh → home provider
2. Home provider decrypts payload, executes, writes result to gossip ledger
3. Home provider sends `task_result` → AXL encrypted mesh → back to submitter's AXL key
4. If home provider dies: gossip detects → lease expires → survivor reclaims → executes

**Shard-affinity routing with adaptive quorum.** Each provider has a home shard and claims it first. Orphaned jobs (dead provider's shards) are claimed only when survivors have confirmed-dead quorum. Confirmed-dead providers are subtracted from the effective cluster size — so 3 survivors can act after 3 providers die in a 6-provider cluster. When `--cluster-size 0`, quorum adapts dynamically to the live peer count.

**Identity recovery.** A provider that restarts with the same AXL key re-adopts its in-progress jobs by refreshing lease expiry. Peers see updated leases within one gossip round — no job loss on restart.

---

## Requirements

- Python 3.11+ (`requests`, `cryptography`, `flask`, `flask-socketio` — see `requirements.txt`)
- AXL binary at `axl/node` (pre-built Linux x86-64)
- `openssl` (key generation — called automatically by `run_local.sh`)
- Docker (optional — only for `versus.sh` Redis comparison)

---

## Quickstart

```bash
# 1. Start 6 providers (FAST_MODE: 5s leases, quick recovery)
FAST_MODE=1 ./run_local.sh

# 2. Automated end-to-end verification (5 checks including fault-tolerance)
./demo/verify.sh

# 3. One-button judge demo (start → converge → submit → kill 3 → recover → results)
./demo/judge_demo.sh "neural network training"

# 4. Open Web UI — http://localhost:5000
.venv/bin/python -m demo.webui
```

---

## Demo Scripts

### One-Button Judge Demo

```bash
./demo/judge_demo.sh ["your query"]
```

Fully automated ~60 second demo: starts all 6 providers, waits for gossip convergence, submits a 6-shard query, kills providers 4–6 mid-execution, monitors recovery in real time, prints results and MTTR.

### Kill 3 Providers Mid-Execution

```bash
# Option A — automated
./demo/run_demo.sh "gossip"

# Option B — manual kill mid-flight
.venv/bin/python -m demo.submit_task "gossip" --api http://localhost:8888 --timeout 120 &
sleep 1
kill -9 $(pgrep -f "shard-id 4") $(pgrep -f "shard-id 5") $(pgrep -f "shard-id 6")
```

**With `FAST_MODE=1`:**

| Time | Event |
|------|-------|
| t+0s  | Providers 4, 5, 6 killed |
| t+4s  | AXL mesh drop → fast-suspect |
| t+8s  | 2 independent reports → CONFIRMED DEAD |
| t+5s  | Leases expire (5s in FAST_MODE) |
| t+10s | Survivors claim + execute orphaned jobs |
| t+12s | All 6/6 COMPLETED |

### Chaos Mode (continuous self-healing)

```bash
FAST_MODE=1 KILL_N=2 INTERVAL=20 ./demo/chaos.sh
```

Kills `KILL_N` random providers every `INTERVAL` seconds, submits a query during each chaos window, waits for recovery, restarts providers (identity recovery activates), repeats indefinitely.

### Network Partition + Heal

```bash
./demo/partition_demo.sh "transformer"
```

Uses `SIGSTOP`/`SIGCONT` on AXL processes to simulate a true network partition — both sides become isolated, the majority side reclaims jobs, then the mesh heals on `SIGCONT` and ledgers converge.

### Whisper vs Redis Side-by-Side

```bash
FAST_MODE=1 ./run_local.sh &
./demo/versus.sh --query "gossip" --kill-after 4
```

Submits the same query to both Whisper Network and a Redis broker simultaneously. At `t+4s` kills Redis **and** kills whisper providers 4–6. One side recovers in ~12s; the other freezes permanently.

### AXL P2P Submission (end-to-end AXL flow)

```bash
.venv/bin/python -m demo.submit_p2p "attention" --axl http://localhost:9002
```

Injects jobs directly through the AXL encrypted overlay (`task_submit`), receives `task_result` push notifications back to the submitter's AXL identity via AXL `/recv` — no debug HTTP anywhere in the critical path.

---

## Web UI

```bash
.venv/bin/python -m demo.webui   # http://localhost:5000
```

Three-panel live interface:

**Left — Provider cards:** per-provider status badge (alive/suspected/dead), shard ID, AXL mesh connectivity, active jobs held, completion metrics.

**Center — D3.js topology graph:** force-directed, draggable. Provider circles colour-coded by status; glow pulse on suspected providers; AXL mesh edges; shard label and active-job count badge inside each circle. **Animated job flow:** particles fly toward the executing provider when a job is claimed; amber dots orbit the node while in_progress; green ripple rings expand on completion.

**Right — Live event feed + job list:** colour-coded stream of membership and ledger events merged across all 6 providers (red = death, yellow = suspicion, green = recovery/join, blue = job events).

**Header bar:** alive/suspected/dead counts, total/done/active jobs, rescued jobs, MTTR (mean time to recovery), average job completion time, WebSocket connection indicator.

---

## Terminal Dashboard (alternative to Web UI)

```bash
.venv/bin/python -m demo.dashboard
```

---

## Project Layout

```
axl/
  node                  pre-built AXL binary (statically linked Linux x86-64)
axl-configs/
  node-config-*.json    Docker AXL configs (Docker hostname peers)
axl-local/              per-node AXL configs from run_local.sh (gitignored)
keys/                   ed25519 identity keys (gitignored)
logs/                   per-node AXL + whisper logs

whisper/
  transport.py          AXL HTTP bridge: /send, /recv, /topology
  membership.py         Layer 1: SWIM-lite gossip + dynamic join + adaptive quorum
  ledger.py             Layer 2: lease-based job ledger + gossip replication
  crypto.py             ed25519 sign/verify + X25519 ECDH + AES-GCM payload cipher
  runtime.py            Layer 3: provider execution loop (shard-affinity + quorum)
  node.py               entry point: wires all layers + debug HTTP (:8888+n)

demo/
  webui.py              Flask+SocketIO: polls /state, tracks MTTR, animated D3 graph
  static/index.html     3-panel D3.js frontend with particle animations
  dashboard.py          rich terminal UI
  judge_demo.sh         one-button judge demo (start → kill → recover → results)
  verify.sh             automated end-to-end verification (5 checks)
  run_demo.sh           automated kill-3 demo with timing
  chaos.sh              continuous kill/restart chaos loop
  versus.sh             Whisper vs Redis side-by-side comparison
  partition_demo.sh     SIGSTOP/SIGCONT network partition + heal
  submit_task.py        submit via debug HTTP, poll for results
  submit_p2p.py         submit + receive via AXL P2P (no HTTP)
  shards/shard-*.txt    6 AI/ML research document corpus shards

comparison/
  redis_broker.py       centralized equivalent — freezes when Redis dies

docker-compose.yml      6-provider Docker Compose setup
Dockerfile              single-container image (AXL binary + Python app)
start.sh                container entrypoint (generates key, starts AXL + whisper)
run_local.sh            6-provider local setup (no Docker, verified working)
```

---

## Tuning Constants

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
| Cluster size (quorum) | 6 | 6 | `--cluster-size` (0 = auto) |

---

## Troubleshooting

**All nodes yellow (SUSPECTED) in Web UI**
AXL overlay connected but `/send` returning 502 — `tcp_port` differs across nodes. Stop everything, delete `axl-local/`, restart `./run_local.sh` (regenerates configs with shared `tcp_port: 7000`).

**`submit_p2p` submits but never shows results**
The whisper node on the same AXL instance consumes `task_result` messages off `/recv` before the script polls. The node buffers them and exposes via `GET /results` — `submit_p2p` polls that endpoint automatically.

**`no quorum` — orphaned jobs not claimed**
Survivors can't see enough peers. Check AXL connectivity (Step 2 of `verify.sh`). With `cluster_size=6`, need at least 3 confirmed-dead + 3 alive for quorum.

**Jobs stuck in `in_progress` after kill**
Lease hasn't expired yet. `FAST_MODE=1` → 5s wait; default → 30s wait.

---

## AXL Gotchas

Discovered during implementation — not in AXL docs:

1. **`X-From-Peer-Id` is not the full public key.** It is a partial identifier derived from the Yggdrasil IPv6 address. Never use it for peer routing — always read `msg["from"]` from the JSON body.

2. **All providers on the same machine must share `tcp_port: 7000`.** This port is the bridge routing destination. Using unique values per provider causes `/send` to return **502** even though the Yggdrasil mesh shows peers as `"up": true`. `run_local.sh` hard-codes `tcp_port: 7000` for all providers.

3. **Heartbeats loop back via gossip relay.** The dedup cache handles most cases, but `_on_heartbeat` must explicitly skip messages where `msg["from"] == our_key` to avoid a provider adding itself to its own peer list.
