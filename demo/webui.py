"""
Web UI: live topology graph for the Whisper Network.

Polls all 6 whisper debug APIs (/state) every 2 seconds and pushes
structured updates to connected browsers via Socket.IO.  The frontend
renders a D3.js force-directed graph showing:
  - Nodes as circles (colour = alive/suspected/dead)
  - AXL mesh edges between peers that are alive
  - Task dots animating from submitter to executor
  - Real-time metrics panel

Usage:
    python -m demo.webui                   # all 6 nodes on default ports
    python -m demo.webui --ports 8888-8893 # explicit port range
"""
import argparse
import json
import os
import threading
import time

import requests
from flask import Flask, send_from_directory
from flask_socketio import SocketIO

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(_HERE, "static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── State ──────────────────────────────────────────────────────────────────────

_node_states: dict[int, dict] = {}  # port -> /state response
_lock = threading.Lock()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/snapshot")
def snapshot():
    with _lock:
        return json.dumps(_build_graph())


@socketio.on("connect")
def on_connect():
    with _lock:
        socketio.emit("graph", _build_graph())


# ── Background poller ──────────────────────────────────────────────────────────

def _poll_node(port: int):
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/state", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _build_graph() -> dict:
    """Convert per-node /state snapshots into a unified graph payload."""
    nodes = []
    edges = set()
    tasks_by_id: dict[str, dict] = {}

    for port, state in _node_states.items():
        if not state:
            continue
        our_key   = state.get("our_key", "")
        key_short = state.get("key_short", our_key[:8])
        shard_id  = state.get("shard_id", 0)
        axl_mesh  = state.get("axl_mesh", {})
        metrics   = state.get("metrics", {})
        peers     = state.get("peers", {})

        # Determine this node's status as seen from itself
        status = "alive"

        nodes.append({
            "id":          our_key,
            "short":       key_short,
            "shard_id":    shard_id,
            "port":        port,
            "status":      status,
            "up_peers":    axl_mesh.get("up_peers", 0),
            "total_peers": axl_mesh.get("total_peers", 0),
            "metrics":     metrics,
        })

        # Build edges from this node to each alive peer
        for peer_short, peer_info in peers.items():
            peer_full = peer_info.get("full_key", "")
            peer_st   = peer_info.get("status", "dead")
            if peer_st != "dead" and peer_full and our_key:
                key = tuple(sorted([our_key, peer_full]))
                edges.add(key)

        # Merge tasks
        for tid, t in state.get("tasks", {}).items():
            existing = tasks_by_id.get(tid)
            if existing is None or t.get("version", 0) > existing.get("version", 0):
                tasks_by_id[tid] = {**t, "executor": our_key}

    # Mark nodes as suspected/dead from peer reports
    for port, state in _node_states.items():
        if not state:
            continue
        for peer_short, peer_info in state.get("peers", {}).items():
            full_key = peer_info.get("full_key", "")
            p_status = peer_info.get("status", "alive")
            if p_status in ("suspected", "dead"):
                for n in nodes:
                    if n["id"] == full_key and n["status"] == "alive":
                        n["status"] = p_status

    return {
        "nodes": nodes,
        "edges": [list(e) for e in edges],
        "tasks": list(tasks_by_id.values()),
        "ts":    time.time(),
    }


def _poller(ports: list[int]):
    while True:
        updated = False
        for port in ports:
            state = _poll_node(port)
            with _lock:
                if _node_states.get(port) != state:
                    _node_states[port] = state
                    updated = True
        if updated:
            with _lock:
                graph = _build_graph()
            socketio.emit("graph", graph)
        time.sleep(2)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Whisper Network Web UI")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--port",  type=int, default=5000)
    parser.add_argument("--nodes", default="8888-8893",
                        help="Port range or comma-separated list of whisper debug ports")
    args = parser.parse_args()

    if "-" in args.nodes:
        lo, hi = args.nodes.split("-")
        ports = list(range(int(lo), int(hi) + 1))
    else:
        ports = [int(p) for p in args.nodes.split(",")]

    print(f"Whisper Web UI  →  http://{args.host}:{args.port}")
    print(f"Polling nodes on ports: {ports}")

    threading.Thread(target=_poller, args=(ports,), daemon=True, name="poller").start()
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
