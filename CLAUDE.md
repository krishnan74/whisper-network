# Whisper Network — Claude Code Guide

Trustless AI inference marketplace built on Gensyn AXL. ETHGlobal hackathon project.

## Stack
- **Python 3.11**, Flask + SocketIO (web UI), `requests` (AXL + Ollama HTTP)
- **AXL binary** at `axl/node` — Gensyn's encrypted P2P overlay (ed25519 identity, Yggdrasil mesh)
- **Ollama** (optional) — local LLM inference at `localhost:11434`
- **JustaName REST API** — ENS subname registration on Sepolia

## Architecture (3 layers)

```
runtime.py  →  execute() calls inference.py (Ollama or keyword fallback)
ledger.py   →  lease-based job state, ed25519-signed gossip, threshold Shamir encryption
membership.py → SWIM-lite: heartbeat / suspicion / dead confirmation / fast-suspect via AXL topology
transport.py  → thin wrapper around AXL /send /recv /topology
node.py       → entry point, wires all layers, price auction, debug HTTP on :8888+shard_id
```

## Key files

| File | Role |
|------|------|
| `whisper/node.py` | Entry point. `WhisperNode` class. Debug HTTP server (`:8888+n`). Price auction logic. |
| `whisper/runtime.py` | `AgentRuntime`. Scan loop, shard-affinity claim, auction fast-path. `execute()` → inference. |
| `whisper/inference.py` | POST to Ollama `/api/chat`. Falls back to keyword grep. `WHISPER_MODEL`, `OLLAMA_BASE_URL` env vars. |
| `whisper/membership.py` | `MembershipProtocol`. `_suspicions` dict — must be cleared on revival. Fast-suspect on AXL drop. |
| `whisper/ledger.py` | `Ledger`. Task state machine: pending → in_progress → completed. Version-vector conflict resolution. |
| `whisper/ens.py` | Background thread. JustaName REST API. Response envelope: `result.data.data[]`. Text records in `records.texts[]`. |
| `demo/webui.py` | Flask app. Polls `/state` on each node's debug port. `_load_dotenv()` reads `.env`. |
| `demo/static/index.html` | D3.js force graph + Socket.IO. All UI in one file. |
| `demo/shards/shard-*.txt` | Domain knowledge context injected into Ollama system prompt. |
| `run_local.sh` | Starts N AXL + whisper processes. Port cleanup before start. Sources `.env`. |

## Running locally

```bash
# Start Ollama with parallel support (required for 6 concurrent nodes)
OLLAMA_NUM_PARALLEL=6 ollama serve &
ollama pull llama3.2

# Start cluster
FAST_MODE=1 ./run_local.sh            # 6 nodes, 5s leases
FAST_MODE=1 ./run_local.sh --count 3  # 3 nodes

# Web UI
.venv/bin/python -m demo.webui --count 6   # http://localhost:5000

# Submit a task (P2P via AXL)
.venv/bin/python -m demo.submit_p2p "How does attention mechanism work?"
```

## Ports

| Component | Port |
|-----------|------|
| AXL TLS listen (node-1) | 9001 |
| AXL API node-i | 9001 + i |
| Whisper debug API node-i | 8887 + i |
| Web UI | 5000 |
| Ollama | 11434 |

## Environment variables (`.env`)

| Var | Purpose |
|-----|---------|
| `JUSTANAME_API_KEY` | ENS subname registration — omit to disable silently |
| `WHISPER_MODEL` | Ollama model name (default: `llama3.2`) |
| `OLLAMA_BASE_URL` | Ollama endpoint (default: `http://localhost:11434`) |
| `FAST_MODE=1` | 5s leases, 1s heartbeat, 4s suspect threshold |
| `EXEC_DELAY=N` | Pause Ns before completing tasks — creates kill window for demos |

## Known gotchas

1. **AXL `tcp_port` must be identical across all nodes** — different values cause `/send` 502 even though mesh shows `"up": true`.
2. **AXL `X-From-Peer-Id` is not the full pubkey** — always read `msg["from"]` from the JSON body.
3. **AXL does not loopback `/send` to self** — auction awardee == self case handled by direct call in `node.py`.
4. **Stagger startup**: AXL needs ~1.5s to bind before whisper polls `/topology` — `run_local.sh` enforces this.
5. **`_suspicions` dict must be cleared on revival** — `membership.py` pops it in all three recovery paths (`_on_heartbeat`, `_on_suspicion` timestamp guard, `_on_node_join`).
6. **ENS peer discovery is disabled** — seeding membership from ENS adds stale registrations from old runs. AXL topology sync already discovers all live peers.
7. **Ollama queues serially by default** — run `OLLAMA_NUM_PARALLEL=6 ollama serve` so all 6 nodes can query concurrently.
8. **JustaName response envelope**: `result.data.data[]` (array), text records at `records.texts[].key/value`, full ENS name at `ens` field.

## Capability → Ollama system prompt mapping

Each node's `--capabilities` flag (search / summarize / reason) selects a different system prompt in `inference.py`. The shard file content is injected as context. Nodes without Ollama fall back to keyword grep over shard lines.
