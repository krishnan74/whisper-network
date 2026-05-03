# Whisper Network — Trustless AI Agent Compute Market on AXL

## The Problem

Every distributed AI system has a coordinator: a server that hands out jobs, tracks results, and becomes the single point of failure. When it dies, work stops. When it is compromised, results can be forged. When demand spikes, it bottlenecks.

This is not a deployment problem. It is an architectural one.

## The Solution

Whisper Network is a **coordinator-free compute market** where AI inference tasks are submitted, auctioned, executed, and verified entirely through AXL. There is no master server. Every node holds a full replica of the job ledger, and any surviving node can complete any task.

The result is a system that degrades gracefully: lose half the cluster and the other half finishes the work — automatically, verifiably, without human intervention.

---

## AXL Integration Depth

AXL is not a transport layer bolted on for the demo. It is the entire infrastructure:

| Role | How AXL is used |
|------|-----------------|
| **Identity** | Each node's ed25519 AXL key IS its cluster identity — signs every ledger update |
| **Transport** | All gossip (heartbeats, bids, claims, results) flows as AXL messages |
| **Peer registry** | `GET /topology` is the source of truth for cluster membership |
| **Failure detection** | AXL mesh drop + silence triggers fast-suspect, halving detection time |
| **Encryption keys** | X25519 keys are derived from the ed25519 AXL identity via HKDF — no separate key management |
| **Threshold crypto** | Each Shamir share of the AES key is encrypted to a different node's AXL-derived X25519 pubkey |
| **Result delivery** | `task_result` is pushed back to the submitter's AXL identity via `/send` |
| **ENS identity** | Each node self-registers `node{N}.notdocker.eth` on Sepolia at startup — AXL peer ID stored as a text record, making nodes discoverable by name across the open ENS namespace |

Remove AXL and the system cannot function. It is not a convenience — it is the foundation.

---

## Technical Depth

**SWIM-lite gossip** — heartbeats, suspicion, two-independent-reports-to-confirm-dead, hop-limited fanout with AXL-topology-aware prioritisation. No central membership server.

**Price auction protocol** — on task arrival the receiving node broadcasts `task_bid_request` to all alive peers, collects bids for 400ms, and awards to the lowest-price bidder with a locality bonus for the home shard provider. The winner claims and executes immediately — bypassing the background scan-cycle entirely.

**Lease-based distributed ledger** — tasks transition `pending → in_progress → completed`. Version-vector conflict resolution ensures convergence even when the same task is claimed by two nodes simultaneously. Each claim records a SHA256 pre-commitment; each completion records a SHA256 result hash — verifiable compute without a trusted verifier.

**Shard-affinity routing with quorum guard** — home provider claims first; designated replica claims second; general survivors only act when they see a majority of the cluster, preventing split-brain duplicate execution.

**GF(256) Shamir threshold encryption** — the AES key encrypting a task payload is split into `n` shares via Lagrange interpolation over GF(2⁸). Any `t` of `n` providers can reconstruct it cooperatively without revealing their individual shares. All share requests and responses travel over the AXL mesh.

**Lease duration gossip convergence** — nodes advertise their configured lease timeout in heartbeats; the cluster auto-adopts the minimum, so a single misconfigured node cannot starve the cluster of fast recovery.

**Capability-aware marketplace** — each provider advertises its capabilities (`search`, `summarize`, `reason`) and price per job. The auction routing engine ensures tasks route to providers that can actually execute them.

**ENS self-registration** — on startup each provider fires a background thread that claims `node{N}.notdocker.eth` on Sepolia (Ethereum testnet) via the JustaName REST API. Text records encode the provider's full AXL public key, capabilities, price, and shard ID. No wallet is required — the domain operator's API key authorises registration via `overrideSignatureCheck`. Records are stored off-chain (CCIP-Read / ERC-3668) and resolvable by any ENS-compatible client. This gives every compute provider a permanent, human-readable identity that maps directly to its cryptographic AXL key.

---

## What It Does — End to End

1. Client submits query via browser UI or P2P AXL message
2. Receiving provider threshold-encrypts the payload (3-of-6 Shamir), stores it in its local ledger, gossips it to all peers, and runs a price auction
3. Lowest-price bidder wins, claims the task, broadcasts `task_bid_request` to peers for their Shamir shares, reconstructs the AES key, decrypts the payload, and executes
4. Result is gossip-committed to all nodes and pushed to the submitter via AXL
5. If the winner dies during step 3 — gossip detects it, lease expires, another provider runs the same flow from step 3

---

## Five Demo Scenarios

| Demo | Command | What it proves |
|------|---------|----------------|
| **Interactive kill/rescue** | `EXEC_DELAY=15 FAST_MODE=1 ./run_local.sh` + Web UI | Click Kill on any node mid-task; watch a survivor rescue it in ~9s |
| **One-button judge demo** | `./demo/judge_demo.sh "neural network"` | Start → converge → submit → kill 3 nodes → all 6 results still arrive |
| **Chaos mode** | `FAST_MODE=1 ./demo/chaos.sh` | Continuous random kills every 20s; cluster never stops processing |
| **Network partition** | `./demo/partition_demo.sh "transformer"` | SIGSTOP half the cluster; majority side reclaims; mesh heals and re-syncs |
| **Multi-client load** | `python -m demo.multi_client --clients 5` | 5 concurrent submitters; auction racing; all jobs complete |

---

## Key Metrics

| Metric | Value |
|--------|-------|
| MTTR (3-node failure, FAST_MODE) | ~9–12s |
| Fault tolerance | 50% node loss (3 of 6) — fully recovers |
| Threshold | 3-of-6 Shamir — no single node sees the plaintext |
| Coordinator nodes | 0 |
| External services required | 0 required (AXL only); JustaName optional for ENS names |
| ENS namespace | `notdocker.eth` on Sepolia — each node claims `node{N}.notdocker.eth` |
| Verifiability | SHA256 commitment + result hash per task |
| Max cluster size | Unlimited (quorum adapts; `--count N` in run_local.sh) |

---

## Why This Matters for Gensyn / AXL

The AI compute market problem is exactly this: untrusted workers, no central coordinator, results that must be verifiable. Whisper Network demonstrates that AXL's identity, transport, and topology primitives are sufficient to build a fully decentralised job market from scratch — no blockchain required, no trusted coordinator, no single point of failure.

The ENS integration extends this further: every compute provider gets a permanent, human-readable name (`node1.notdocker.eth`) that maps directly to its AXL cryptographic identity. Peer discovery, capability lookup, and price discovery become ENS record queries — no off-chain registry, no DNS, no central directory.

This is a working proof-of-concept for AXL as compute market infrastructure, with ENS as the open identity and discovery layer on top.
