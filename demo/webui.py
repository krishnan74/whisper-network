"""
Web UI: live topology graph + event feed for the Whisper Network.

Polls all 6 whisper debug APIs (/state) every 2 seconds and pushes
structured updates to connected browsers via Socket.IO.

Features:
  - D3.js force-directed topology graph (node status, AXL mesh edges)
  - Live event feed (merged membership + ledger events from all nodes)
  - MTTR tracking (time from node death to task recovery)
  - Cluster metrics panel (tasks, alive nodes, rescued, avg completion)

Usage:
    python -m demo.webui
    python -m demo.webui --nodes 8888-8893 --port 5000
"""
import argparse
import json
import os
import re
import signal
import subprocess
import random
import threading
import time
import uuid
from collections import deque

import requests
from flask import Flask, send_from_directory, request as flask_request
from flask_socketio import SocketIO

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(_HERE, "static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Shared state ───────────────────────────────────────────────────────────────

_node_states: dict[int, dict] = {}       # port -> /state snapshot
_prev_node_status: dict[str, str] = {}   # node_key -> last known status
_kill_events: list[dict] = []            # [{ts, key, recovered_at}]
_recovery_times: list[float] = []        # completed MTTR samples (seconds)
_global_events: deque = deque(maxlen=120)  # merged event log across all nodes
_seen_events: set = set()                # dedup event strings
_lock = threading.Lock()

# Economy state
_provider_balances: dict = {}   # node_key -> cumulative AXL earned
_credited_tasks: set = set()    # task_ids already credited

# Kill/revive state
_node_pids: dict[int, int] = {}  # debug_port -> whisper node PID


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data  = flask_request.get_json(force=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query required"}), 400

    with _lock:
        alive = {p: s for p, s in _node_states.items() if s}
    if not alive:
        return json.dumps({"error": "no nodes available"}), 503

    # Build shard_id → port map (prefer the node that owns that shard)
    shard_to_port: dict[int, int] = {}
    for port, state in alive.items():
        sid = state.get("shard_id")
        if sid:
            shard_to_port[sid] = port

    submitted = []
    fallback_ports = list(alive.keys())
    for shard_id in range(1, 7):
        target = shard_to_port.get(shard_id) or random.choice(fallback_ports)
        task_id = str(uuid.uuid4())
        try:
            r = requests.post(
                f"http://127.0.0.1:{target}/submit",
                json={"task_id": task_id, "payload": query, "shard_id": shard_id},
                timeout=3,
            )
            if r.status_code == 200:
                submitted.append({"task_id": task_id, "shard_id": shard_id})
        except Exception:
            pass

    if not submitted:
        return json.dumps({"error": "all node submissions failed"}), 503
    return json.dumps({"ok": True, "submitted": submitted, "query": query})


def _find_whisper_pid(port: int) -> int | None:
    """Find the PID of the whisper node listening on debug_port."""
    # Try ss (Linux, fast)
    try:
        out = subprocess.check_output(
            ["ss", "-tlnpH", f"sport = :{port}"],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        m = re.search(r"pid=(\d+)", out)
        if m:
            pid = int(m.group(1))
            _node_pids[port] = pid
            return pid
    except Exception:
        pass
    # Fallback: lsof
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        pids = [int(p) for p in out.strip().split() if p.strip().isdigit()]
        if pids:
            _node_pids[port] = pids[0]
            return pids[0]
    except Exception:
        pass
    return _node_pids.get(port)  # last cached value (works for SIGSTOP'd procs)


@app.route("/api/kill", methods=["POST"])
def api_kill():
    data = flask_request.get_json(force=True) or {}
    port = int(data.get("port", 0))
    pid  = _find_whisper_pid(port)
    if not pid:
        return json.dumps({"error": f"no process found on :{port}"}), 404
    try:
        os.kill(pid, signal.SIGSTOP)
        ts = time.strftime("%H:%M:%S")
        with _lock:
            _global_events.appendleft(f"[{ts}] ⏹ node :{port} killed by operator (PID {pid})")
        return json.dumps({"ok": True, "pid": pid, "port": port})
    except Exception as e:
        return json.dumps({"error": str(e)}), 500


@app.route("/api/revive", methods=["POST"])
def api_revive():
    data = flask_request.get_json(force=True) or {}
    port = int(data.get("port", 0))
    pid  = _find_whisper_pid(port)
    if not pid:
        return json.dumps({"error": f"no process found on :{port}"}), 404
    try:
        os.kill(pid, signal.SIGCONT)
        ts = time.strftime("%H:%M:%S")
        with _lock:
            _global_events.appendleft(f"[{ts}] ▶ node :{port} revived by operator (PID {pid})")
        return json.dumps({"ok": True, "pid": pid, "port": port})
    except Exception as e:
        return json.dumps({"error": str(e)}), 500


@app.route("/snapshot")
def snapshot():
    with _lock:
        return json.dumps(_build_payload())


@socketio.on("connect")
def on_connect():
    with _lock:
        payload = _build_payload()
        _update_economy(payload["nodes"], payload["tasks"])
    socketio.emit("update", payload)


# ── Reputation + economy ──────────────────────────────────────────────────────

def _compute_reputation(m: dict) -> int:
    completed = m.get("completed", 0)
    rescued   = m.get("tasks_rescued", 0)
    avg_s     = m.get("avg_completion_s")
    total = completed + rescued
    if total == 0:
        return 0
    volume = min(total / 8, 1.0) * 40
    speed  = max(0, 1 - (avg_s / 10)) * 40 if avg_s is not None else 20
    rescue = min(rescued / 4, 1.0) * 20
    return min(100, round(volume + speed + rescue))


def _update_economy(nodes: list, tasks: list):
    """Credit newly completed tasks, update per-node balances and reputation in-place."""
    for t in tasks:
        tid = t.get("task_id")
        if t.get("status") == "completed" and tid not in _credited_tasks:
            _credited_tasks.add(tid)
            exec_short = t.get("leased_by")
            if not exec_short:
                continue
            for n in nodes:
                if (n.get("short") or n["id"][:8]) == exec_short:
                    _provider_balances[n["id"]] = _provider_balances.get(n["id"], 0.0) + 0.01
                    ts = time.strftime("%H:%M:%S")
                    ev = f"[{ts}] {exec_short} +0.010 AXL · shard-{t.get('shard_id')}"
                    ev_key = f"eco:{tid}"
                    if ev_key not in _seen_events:
                        _seen_events.add(ev_key)
                        _global_events.appendleft(ev)
                    break
    for n in nodes:
        n["balance"]    = round(_provider_balances.get(n["id"], 0.0), 3)
        n["reputation"] = _compute_reputation(n.get("metrics", {}))


# ── Graph + payload builder ────────────────────────────────────────────────────

def _build_payload() -> dict:
    nodes = []
    edges = set()
    tasks_by_id: dict[str, dict] = {}

    for port, state in _node_states.items():
        if not state:
            # Node unreachable — represent as offline placeholder
            nodes.append({
                "id": f"offline-{port}", "short": f":{port}",
                "shard_id": 0, "port": port, "status": "offline",
                "up_peers": 0, "total_peers": 0, "metrics": {},
            })
            continue

        our_key  = state.get("our_key", "")
        axl_mesh = state.get("axl_mesh", {})
        metrics  = state.get("metrics", {})
        peers    = state.get("peers", {})

        nodes.append({
            "id":          our_key,
            "short":       state.get("key_short", our_key[:8]),
            "shard_id":    state.get("shard_id", 0),
            "port":        port,
            "status":      "alive",
            "up_peers":    axl_mesh.get("up_peers", 0),
            "total_peers": axl_mesh.get("total_peers", 0),
            "metrics":     metrics,
            "tasks_held":  len([t for t in state.get("tasks", {}).values()
                                if t.get("status") == "in_progress"
                                and t.get("leased_by", "")[:8] == our_key[:8]]),
        })

        for peer_info in peers.values():
            peer_full = peer_info.get("full_key", "")
            if peer_info.get("status") != "dead" and peer_full and our_key:
                edges.add(tuple(sorted([our_key, peer_full])))

        for tid, t in state.get("tasks", {}).items():
            existing = tasks_by_id.get(tid)
            if existing is None or t.get("version", 0) > existing.get("version", 0):
                tasks_by_id[tid] = {**t, "reporter": our_key}

    # Propagate suspected/dead status from peer reports
    for state in _node_states.values():
        if not state:
            continue
        for peer_info in state.get("peers", {}).values():
            full_key = peer_info.get("full_key", "")
            p_status = peer_info.get("status", "alive")
            if p_status in ("suspected", "dead"):
                for n in nodes:
                    if n["id"] == full_key and n["status"] == "alive":
                        n["status"] = p_status

    # Aggregate cluster metrics
    all_tasks = list(tasks_by_id.values())
    total     = len(all_tasks)
    completed = sum(1 for t in all_tasks if t.get("status") == "completed")
    in_prog   = sum(1 for t in all_tasks if t.get("status") == "in_progress")
    pending   = sum(1 for t in all_tasks if t.get("status") == "pending")
    alive     = sum(1 for n in nodes if n["status"] == "alive")
    rescued   = sum(n["metrics"].get("tasks_rescued", 0) for n in nodes)

    times = [
        n["metrics"]["avg_completion_s"]
        for n in nodes
        if n["metrics"].get("avg_completion_s") is not None
    ]
    avg_s = round(sum(times) / len(times), 1) if times else None

    mttr = round(sum(_recovery_times) / len(_recovery_times), 1) if _recovery_times else None

    return {
        "nodes":  nodes,
        "edges":  [list(e) for e in edges],
        "tasks":  all_tasks,
        "events": list(_global_events),
        "cluster": {
            "total": total, "completed": completed,
            "in_progress": in_prog, "pending": pending,
            "alive": alive, "rescued": rescued,
            "avg_completion_s": avg_s, "mttr_s": mttr,
            "total_balance": round(sum(_provider_balances.values()), 3),
        },
        "ts": time.time(),
    }


# ── MTTR tracking ──────────────────────────────────────────────────────────────

def _update_mttr(nodes: list[dict], tasks: list[dict]):
    now = time.time()

    for n in nodes:
        key    = n["id"]
        status = n["status"]
        prev   = _prev_node_status.get(key)

        if prev == "alive" and status == "dead":
            _kill_events.append({"ts": now, "key": key, "recovered_at": None})

        _prev_node_status[key] = status

    # Mark kill events recovered when no in_progress tasks remain
    in_prog = sum(1 for t in tasks if t.get("status") == "in_progress")
    if in_prog == 0 and tasks:
        for ev in _kill_events:
            if ev["recovered_at"] is None:
                ev["recovered_at"] = now
                _recovery_times.append(now - ev["ts"])


# ── Event merging ──────────────────────────────────────────────────────────────

def _merge_events(port: int, state: dict):
    m_events = state.get("events", [])
    for ev in m_events:
        key = f"{port}:{ev}"
        if key not in _seen_events:
            _seen_events.add(key)
            _global_events.appendleft(ev)


# ── Background poller ──────────────────────────────────────────────────────────

def _poll_node(port: int) -> dict | None:
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/state", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _poller(ports: list[int]):
    while True:
        changed = False
        for port in ports:
            state = _poll_node(port)
            with _lock:
                if state != _node_states.get(port):
                    _node_states[port] = state
                    changed = True
                if state:
                    _merge_events(port, state)

        if changed:
            with _lock:
                payload = _build_payload()
                _update_mttr(payload["nodes"], payload["tasks"])
                _update_economy(payload["nodes"], payload["tasks"])
            socketio.emit("update", payload)

        time.sleep(2)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Whisper Network Web UI")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--port",  type=int, default=5000)
    parser.add_argument("--nodes", default="8888-8893",
                        help="Port range or comma list of whisper debug ports")
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
