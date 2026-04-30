# Whisper Network — Trustless AI Agent Compute Market on AXL

## The Problem

Every distributed AI system has a coordinator: a server that hands out jobs, tracks results, and becomes the single point of failure. When it dies, work stops. When it's compromised, results can be forged. When demand spikes, it bottlenecks.

This is not a deployment problem. It is an architectural one.

## The Solution

Whisper Network is a **coordinator-free compute market** where AI inference tasks are submitted, tracked, and executed entirely through AXL. There is no master server. Every node holds a full replica of the job ledger, and any surviving node can complete any task.

The result is a system that degrades gracefully: lose half the cluster and the other half finishes the work — automatically, verifiably, without human intervention.

---

## AXL Integration Depth

AXL is not a transport layer bolted on for the demo. It is the entire infrastructure:

| Role | How AXL is used |
|------|-----------------|
| **Identity** | Each node's ed25519 AXL key IS its cluster identity — used to sign every ledger update |
| **Transport** | All gossip (heartbeats, task claims, results) flows as AXL messages |
| **Peer registry** | `GET /topology` is the source of truth for cluster membership; `axl_sync()` sends `node_join` to newly appeared peers |
| **Encryption keys** | X25519 keys are derived from the ed25519 AXL identity via HKDF — no separate key management |
| **Threshold crypto** | Each share of a Shamir-split AES key is encrypted to a different node's AXL X25519 pubkey |

Remove AXL and the system cannot function. It is not a convenience — it is the foundation.

---

## Technical Depth

**SWIM-lite gossip** — heartbeats, suspicion, two-independent-reports-to-confirm-dead, hop-limited fanout. No central membership server.

**Lease-based distributed ledger** — tasks transition `pending → in_progress → completed`. Version-vector conflict resolution ensures convergence even when the same task is claimed by two nodes simultaneously.

**Shard-affinity routing with quorum guard** — home node claims first; designated replica claims second; general survivors only act when they see a majority of the cluster, preventing split-brain duplicate execution.

**Lease duration gossip convergence** — nodes advertise their configured lease timeout; the cluster auto-adopts the minimum, so a single misconfigured node cannot starve the cluster of fast recovery.

**GF(256) Shamir threshold encryption** — the AES key encrypting a task payload is split into `n` shares via Lagrange interpolation over GF(2⁸). Any `t` nodes can reconstruct it cooperatively without revealing their individual shares.

---

## Five Demo Scenarios

| Demo | Script | What it proves |
|------|--------|----------------|
| **Judge demo** | `demo/judge_demo.sh` | Start → converge → submit → kill 3 nodes → watch survivors recover in ~12s |
| **Chaos mode** | `demo/chaos.sh` | Continuous random kills and restarts; auto-submits queries; cluster never stops |
| **Redis comparison** | `demo/versus.sh` | Kill coordinator: Redis goes dark, Whisper keeps running |
| **Partition recovery** | `demo/partition_demo.sh` | SIGSTOP half the cluster; reconnect; both sides re-sync the ledger |
| **Threshold query** | `python3 -m demo.submit_task --threshold` | Query requires 3-of-6 shares; no single node can decrypt alone |

---

## Key Metrics

- **MTTR**: ~12 s (3-node failure with 5 s leases, 1 s heartbeats)
- **Fault tolerance**: 50% node loss (3 of 6) — fully recovers
- **Threshold**: 3-of-6 Shamir — no single node sees the plaintext
- **Coordinator nodes**: 0
- **External services required**: 0 (AXL is the only dependency)

---

## Why This Matters for Gensyn / AXL

The AI compute market problem is exactly this: untrusted workers, no central coordinator, results that must be verifiable. Whisper Network demonstrates that AXL's identity + transport primitives are sufficient to build a fully decentralized job market from scratch — no blockchain required, no trusted coordinator, no single point of failure.

This is a working proof of concept for AXL as compute market infrastructure.
