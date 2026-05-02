# Whisper Network

**A trustless AI agent compute market built on [Gensyn AXL](https://github.com/gensyn-ai/axl).**

Compute providers join a peer-to-peer mesh via AXL. Clients submit inference jobs across the mesh. Providers bid, claim, and execute jobs — coordinated entirely through a gossip-replicated ledger with no central broker and no single point of failure.

Kill half the providers mid-inference. The jobs complete anyway.

---

## What This Solves

**The Coordinator Problem:** Every existing AI inference marketplace routes jobs through a central API or broker. When the broker dies, all in-flight work is lost. Providers sit idle. Users wait forever.

**Whisper Network eliminates the coordinator.** Job state is replicated peer-to-peer across every node in real time. When a provider fails, the other providers detect it via gossip, reclaim its jobs, and complete them automatically — without human intervention or central authority.

**Why AXL?** AXL gives every provider a cryptographic identity (ed25519) and an encrypted overlay network (Yggdrasil). Whisper Network uses these directly:

- Provider identities are AXL ed25519 keys — no separate registration
- All job messages travel over the AXL encrypted mesh — no separate transport layer
- Job results are pushed back to submitters via AXL — bidirectional application bus
- The AXL topology is the authoritative peer registry — no separate discovery service
- X25519 payload encryption is derived from AXL identity keys — no separate key management

**ENS identity layer.** Each provider self-registers a human-readable subname under `notdocker.eth` on Sepolia at startup (e.g. `node1.notdocker.eth`). Text records store the provider's AXL peer ID, capabilities, price, and shard — making nodes discoverable by name across the open ENS namespace without any central directory.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Application: Distributed AI inference across N compute providers            │
│  Each shard = one provider's local document corpus segment                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 3: Agent Runtime (Provider Execution)                                 │
│  auction winner → immediate claim + execute (no scan-cycle delay)           │
│  scan loop fallback every 5s for tasks that miss the auction                │
│  ↳ shard-affinity: home provider claims first; survivors rescue on failure  │
│  ↳ quorum guard: dead providers subtracted from effective cluster size      │
│  ↳ parallel execution: each claimed task runs in its own thread             │
│  ↳ payload decryption: threshold Shamir or per-shard X25519 ECDH           │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 2: Trustless Job Ledger (Gossip-Replicated)                          │
│  lease-based job ownership · version-vector conflict resolution              │
│  ↳ price auction: bid_request → bids → award → immediate execution         │
│  ↳ AXL-topology-aware fanout: mesh-connected peers get gossip priority     │
│  ↳ ed25519 signed: every ledger_update signed with provider's AXL key      │
│  ↳ push notifications: job_result sent to submitter's AXL identity         │
│  ↳ threshold encryption: Shamir t-of-n — any t providers can decrypt       │
├─────────────────────────────────────────────────────────────────────────────┤
│  Layer 1: Gossip Membership (SWIM-lite Failure Detection)                   │
│  heartbeats · suspicion · 2-report confirmation · dynamic join              │
│  ↳ AXL topology = authoritative provider registry                           │
│  ↳ node_join broadcast on startup — instant mesh convergence                │
│  ↳ fast-suspect: AXL mesh drop + silence → immediate suspicion             │
│  ↳ dynamic cluster size: quorum adapts as providers join/leave             │
├─────────────────────────────────────────────────────────────────────────────┤
│  AXL (Gensyn Agent eXchange Layer) — the foundation                        │
│  POST /send  GET /recv  GET /topology                                       │
│  ↳ ed25519 identity per provider (same PEM for signing + X25519 key)      │
│  ↳ encrypted Yggdrasil overlay — all job traffic is private end-to-end    │
│  ↳ bidirectional bus: task_submit in / task_result out                     │
├─────────────────────────────────────────────────────────────────────────────┤
│  ENS Identity Layer (JustaName / Sepolia)                                   │
│  on startup: claim node{N}.notdocker.eth · set text records via REST API   │
│  ↳ axl.peer_id — full AXL public key (lookup by human-readable name)      │
│  ↳ capabilities, price_axl, shard_id stored as ENS text records           │
│  ↳ signature-free: no wallet required — API key + overrideSignatureCheck  │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Provider price auction.** When a task arrives, the receiving node broadcasts a `task_bid_request` to all alive peers, collects bids for 400ms, and awards to the lowest-price bidder (with a locality bonus for the home shard's provider). The winner claims and executes immediately — no polling delay. All bid/award messages travel over AXL.

**AXL as the authoritative peer registry.** Every 5 seconds each provider polls `/topology`. Providers that appear in the AXL overlay are added to membership. Providers that disappear from the mesh *and* have been silent for longer than `suspect_after` are fast-tracked to SUSPECTED — cutting failure detection time roughly in half.

**Dynamic cluster join.** When a provider starts or restarts, it broadcasts a `node_join` AXL message to all directly-connected peers. Existing providers add the newcomer to membership immediately — no waiting for heartbeat cycles or topology sync. The join gossips outward with hop-limited fanout.

**Cryptographic ledger integrity.** Every `ledger_update` gossip message is signed with the sender's ed25519 private key (the same PEM used by AXL). Receiving providers verify the signature before accepting any state change. Forged or tampered updates are silently dropped.

**Threshold encryption (Shamir t-of-n).** When a task is submitted and at least 3 providers are alive, the payload is split into n shares via Lagrange interpolation over GF(2⁸). Each share is encrypted to a different provider's AXL-derived X25519 pubkey and embedded in the task payload. Any t providers can cooperate to reconstruct the AES key and decrypt. No single node sees the plaintext alone.

**Shard-affinity routing with adaptive quorum.** Each provider has a home shard and claims it first. Orphaned jobs are claimed only when survivors have confirmed-dead quorum — preventing split-brain duplicate execution. Confirmed-dead providers are subtracted from the effective cluster size so 3 survivors can act after 3 die. When `--cluster-size 0`, quorum adapts dynamically to the live peer count.

**Identity recovery.** A provider that restarts with the same AXL key re-adopts its in-progress jobs by refreshing lease expiry. Peers see updated leases within one gossip round — no job loss on restart.

**ENS self-registration.** On startup each provider fires a background thread that claims `node{N}.notdocker.eth` on Sepolia via the JustaName REST API. Text records `axl.peer_id`, `capabilities`, `price_axl`, and `shard_id` are set in the same call — no wallet signature needed (`overrideSignatureCheck: true`). The registered name appears on the provider's card in the Web UI within seconds. Disabled silently if `JUSTANAME_API_KEY` is not set.

---

## Requirements

- Python 3.11+ (`requests`, `cryptography`, `flask`, `flask-socketio` — see `requirements.txt`)
- AXL binary at `axl/node` (pre-built Linux x86-64)
- `openssl` (key generation — called automatically by `run_local.sh`)
- Docker (optional — only for the Docker Compose setup)
- `JUSTANAME_API_KEY` in `.env` (optional — enables ENS subname registration on Sepolia)

---

## Quickstart

```bash
# 0. (Optional) Add your JustaName API key for ENS self-registration
echo "JUSTANAME_API_KEY=your_key_here" >> .env

# 1. Start 6 providers (FAST_MODE: 5s leases, quick recovery)
FAST_MODE=1 ./run_local.sh

# 2. Open Web UI — http://localhost:5000
.venv/bin/python -m demo.webui

# 3. Automated end-to-end verification (5 checks including fault-tolerance)
./demo/verify.sh

# 4. One-button judge demo (start → converge → submit → kill 3 → recover → results)
./demo/judge_demo.sh "neural network training"
```

### Variable node count

```bash
# Start with 3 nodes instead of the default 6
FAST_MODE=1 ./run_local.sh --count 3

# Start the Web UI on the matching port range
.venv/bin/python -m demo.webui --nodes 8888-8890
```

---

## Demo Scenarios

### Web UI — interactive kill/rescue demo

```bash
EXEC_DELAY=15 FAST_MODE=1 ./run_local.sh
.venv/bin/python -m demo.webui   # http://localhost:5000
```

Submit a query from the browser. Each task shows `↻ in_progress` for 15 seconds. Click **Kill** on any node card during that window — the task's orbit dot disappears, the node turns red, and within ~9s a surviving node claims the lease, collects threshold shares, and completes the task. The `rescued` counter increments and the task turns green.

`EXEC_DELAY` is the number of seconds each provider pauses before completing a task — it exists solely to create a visible kill window for demos.

### One-button judge demo

```bash
./demo/judge_demo.sh "neural network training"
```

Fully automated ~60 second demo: starts all 6 providers, waits for gossip convergence, submits a 6-shard query, kills providers 4–6 mid-execution, monitors recovery in real time, prints results and MTTR.

**Typical timeline with `FAST_MODE=1`:**

| Time | Event |
|------|-------|
| t+0s  | Providers 4, 5, 6 killed |
| t+4s  | AXL mesh drop → fast-suspect |
| t+8s  | 2 independent reports → CONFIRMED DEAD |
| t+5s  | Leases expire (5s in FAST_MODE) |
| t+9s  | Survivors claim + execute orphaned jobs |
| t+11s | All 6/6 COMPLETED |

### Chaos mode (continuous self-healing)

```bash
FAST_MODE=1 KILL_N=2 INTERVAL=20 ./demo/chaos.sh
```

Kills `KILL_N` random providers every `INTERVAL` seconds, submits a query during each chaos window, waits for recovery, restarts providers (identity recovery activates), and repeats indefinitely.

### Network partition + heal

```bash
./demo/partition_demo.sh "transformer"
```

Uses `SIGSTOP`/`SIGCONT` on AXL processes to simulate a true network partition. Both sides become isolated, the majority side reclaims jobs, then the mesh heals on `SIGCONT` and the ledgers converge.

### Multi-client concurrent load

```bash
FAST_MODE=1 ./run_local.sh &
sleep 5
.venv/bin/python -m demo.multi_client --clients 5 --query "attention mechanism"
```

Fires N client threads simultaneously, each submitting a full 6-shard job. Tests concurrent auction racing, lease conflict resolution, and parallel execution.

### AXL P2P submission (full end-to-end AXL flow)

```bash
.venv/bin/python -m demo.submit_p2p "attention" --axl http://localhost:9002
```

Injects jobs through the AXL encrypted overlay (`task_submit`), receives `task_result` push notifications back to the submitter's AXL identity via AXL `/recv` — no debug HTTP anywhere in the critical path.

---

## Web UI

```bash
.venv/bin/python -m demo.webui              # default: ports 8888-8893, 6 nodes
.venv/bin/python -m demo.webui --nodes 8888-8890   # 3-node run
```

Three-panel live interface:

**Left — Provider cards:** per-provider status badge (alive/suspected/dead), ENS name (e.g. `node1.notdocker.eth` — appears within seconds of startup if `JUSTANAME_API_KEY` is set), shard ID, AXL mesh peers up/total, completed jobs, rescued jobs, average completion time, AXL balance, capability badges, reputation bar. Scrollable when more than ~4 nodes are visible. **Kill / Revive buttons** on each card let you simulate node failure and recovery directly from the browser.

**Center — D3.js topology graph:** force-directed, draggable, zoomable. Provider circles colour-coded by status; glow pulse on suspected providers; AXL mesh edges with flowing dashes; shard label and active-job count badge inside each circle. **Animated job flow:** particles fly toward the executing provider when a job is claimed; amber dots orbit the node while `in_progress`; green ripple rings expand on completion.

**Right — Live event feed + job list:** colour-coded stream of membership and ledger events merged across all providers (red = death, yellow = suspicion, green = recovery/join, blue = job events, gold = economy). Each job card shows its encryption badge (`3-of-n key` for threshold, `encrypted` for per-shard), a `✓ verified` badge once the result hash is confirmed, and a latency waterfall (`queued Xs · ran Ys · total Zs`).

**Header bar:** alive/suspected/dead counts, total/done/active jobs, rescued jobs, MTTR, average job completion time, total AXL paid, WebSocket connection indicator.

---

## Project Layout

```
axl/
  node                  pre-built AXL binary (statically linked Linux x86-64)
axl-configs/
  node-config-*.json    Docker AXL configs (Docker hostname peers)
axl-local/              per-node AXL configs generated by run_local.sh (gitignored)
keys/                   ed25519 identity keys (gitignored)
logs/                   per-node AXL + whisper logs (gitignored)
data/                   per-node ledger JSON files (gitignored)

whisper/
  transport.py          AXL HTTP bridge: /send, /recv, /topology
  membership.py         Layer 1: SWIM-lite gossip + fast-suspect + adaptive quorum
  ledger.py             Layer 2: lease-based job ledger + ed25519 gossip + threshold encryption
  crypto.py             ed25519 sign/verify + X25519 ECDH + AES-GCM + Shamir GF(256)
  runtime.py            Layer 3: auction-driven execution + shard-affinity + scan fallback
  node.py               entry point: wires all layers + price auction + debug HTTP (:8888+n)
  ens.py                ENS self-registration: claims node{N}.notdocker.eth on Sepolia at startup

demo/
  webui.py              Flask+SocketIO: polls /state, emits live updates, kill/revive API
  static/index.html     3-panel D3.js frontend — kill/revive buttons, job latency waterfall
  dashboard.py          rich terminal UI
  judge_demo.sh         one-button judge demo (start → kill → recover → results)
  verify.sh             automated end-to-end verification (5 checks)
  chaos.sh              continuous kill/restart chaos loop
  partition_demo.sh     SIGSTOP/SIGCONT network partition + heal
  multi_client.py       concurrent N-client load test
  submit_task.py        submit via debug HTTP, poll for results
  submit_p2p.py         submit + receive via pure AXL P2P (no HTTP)
  shards/shard-*.txt    6 AI/ML research document corpus shards

docker-compose.yml      6-provider Docker Compose setup
Dockerfile              single-container image (AXL binary + Python app)
start.sh                container entrypoint (generates key, starts AXL + whisper)
run_local.sh            N-provider local setup — supports --count, FAST_MODE, EXEC_DELAY; sources .env
.env                    local secrets (gitignored) — set JUSTANAME_API_KEY here
```

---

## Tuning Parameters

| Parameter | Default | FAST_MODE=1 | Flag / Env |
|-----------|---------|-------------|------------|
| Heartbeat interval | 2s | 1s | `--heartbeat-interval` |
| Suspect threshold | 10s | 4s | `--suspect-after` |
| Dead reports needed | 2 | 2 | hardcoded |
| Gossip fanout | 3 peers | 3 peers | hardcoded |
| Gossip hops | 8 | 8 | hardcoded |
| AXL topology sync | 5s | 5s | hardcoded |
| Lease duration | 30s | 5s | `--lease-duration` |
| Lease renew threshold | 15s | 2s | `--renew-threshold` |
| Agent scan interval | 5s | 5s | hardcoded |
| Cluster size (quorum) | 6 | 6 | `--cluster-size` (0 = auto) |
| Node count | 6 | 6 | `--count` (run_local.sh) |
| Execution delay | 0s | 0s | `EXEC_DELAY` (kill-rescue demo) |
| Auction window | 400ms | 400ms | hardcoded |

---

## Troubleshooting

**All nodes yellow (SUSPECTED) in Web UI**
AXL overlay connected but `/send` returning 502 — `tcp_port` differs across nodes. Stop everything, delete `axl-local/`, restart `./run_local.sh` (regenerates configs with shared `tcp_port: 7000`).

**`submit_p2p` submits but never shows results**
The whisper node on the same AXL instance buffers `task_result` messages — they are exposed via `GET /results` on the debug port, not via the raw `/recv` queue. `submit_p2p` polls that endpoint automatically.

**`no quorum` — orphaned jobs not claimed**
Survivors cannot see enough peers. Check AXL connectivity (`./demo/verify.sh` step 2). With `--cluster-size 6`, at least 3 confirmed-dead + 3 alive are needed for quorum to act.

**Jobs complete instantly — no time to kill a node**
Run with `EXEC_DELAY=15` to add a 15-second pause before each task completes. This creates a visible `in_progress` window to kill nodes from the UI or terminal.

**Jobs stuck `in_progress` after killing a node**
The lease has not expired yet. With `FAST_MODE=1` wait ~5s; default mode wait ~30s. The surviving nodes' scan loop or the next auction picks it up after expiry.

**Nodes 5/6 showing `0/1 up` peers**
Previous AXL process still holding the port from a prior run. Kill all AXL and whisper processes (`pkill -f 'axl/node'; pkill -f 'whisper.node'`), then restart.

**ENS name not appearing on node cards**
Either `JUSTANAME_API_KEY` is not set in `.env`, or the subname is already taken (another run claimed it). Check logs for `ENS registered` or `ENS registration failed` lines. Verify with: `curl "https://api.justaname.id/ens/v1/subname/subname?subname=node1.notdocker.eth&chainId=11155111"`

**ENS records show on JustaName but not on `sepolia.app.ens.domains`**
Expected — JustaName stores records off-chain via CCIP-Read (ERC-3668). The standard ENS app doesn't query the JustaName gateway. Records are fully resolvable via the JustaName API and any CCIP-Read-aware client.

---

## AXL Gotchas

Discovered during implementation — not in the AXL documentation:

1. **`X-From-Peer-Id` is not the full public key.** It is a partial identifier derived from the Yggdrasil IPv6 address. Never use it for peer routing — always read `msg["from"]` from the JSON body.

2. **All providers on the same machine must share `tcp_port: 7000`.** This port is the bridge routing destination. Using unique values per provider causes `/send` to return **502** even though the Yggdrasil mesh shows peers as `"up": true`. `run_local.sh` hard-codes `tcp_port: 7000` for all providers.

3. **Heartbeats loop back via gossip relay.** The dedup cache handles most cases, but `_on_heartbeat` must explicitly skip messages where `msg["from"] == our_key` to avoid a provider adding itself to its own peer list.

4. **AXL does not loopback `/send` to self.** When the auction awardee is the same node that ran the auction, the `task_award` message cannot be sent via AXL. `_run_auction` in `node.py` handles this by calling `_handle_task_award` directly when `winner_key == self.our_key`.

5. **Stagger AXL startup before whisper.** AXL needs ~1.5s to complete the TLS handshake and bind its ports before the whisper process starts polling `/topology`. `run_local.sh` enforces this with `sleep 1.5` between each pair.
