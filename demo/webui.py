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
_node_pids: dict[int, int] = {}      # debug_port -> whisper node PID
_partitioned_ports: set = set()      # ports SIGSTOP'd via partition action
_chaos_killed_ports: set = set()     # ports SIGSTOP'd via kill_random action

# Dynamic node management
_node_urls: dict[int, str] = {}          # fake sequential port key -> actual polling URL
_dynamic_procs: dict[int, tuple] = {}    # debug_port -> (axl_proc, whisper_proc)
_spawn_cfg: dict = {}                    # paths for spawning new nodes

# Throughput history (for sparkline)
_throughput_history: list = []       # [{ts, c}] — last 30 samples of completed count
_THROUGHPUT_MAXLEN = 30

# AXL gateway (node-1's AXL API — used for direct AXL sends from the webui)
_axl_base: str = "http://127.0.0.1:9002"
_our_axl_key: str = ""   # lazily fetched from /topology


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

    # Build shard_id → AXL key map from polled node states
    shard_to_key: dict[int, str] = {}
    for state in alive.values():
        sid = state.get("shard_id")
        key = state.get("our_key")
        if sid and key:
            shard_to_key[sid] = key

    our_key   = _get_our_axl_key()
    submitted = []

    for shard_id, target_key in shard_to_key.items():
        task_id = str(uuid.uuid4())
        msg = {
            "type":     "task_submit",
            "msg_id":   str(uuid.uuid4()),
            "from":     our_key,
            "task_id":  task_id,
            "payload":  query,
            "shard_id": shard_id,
        }
        try:
            r = requests.post(
                f"{_axl_base}/send",
                headers={"X-Destination-Peer-Id": target_key},
                data=json.dumps(msg).encode(),
                timeout=3,
            )
            if r.status_code == 200:
                submitted.append({"task_id": task_id, "shard_id": shard_id})
        except Exception:
            pass

    if not submitted:
        return json.dumps({"error": "all AXL submissions failed"}), 503
    return json.dumps({"ok": True, "submitted": submitted, "query": query, "via": "axl"})


def _get_our_axl_key() -> str:
    global _our_axl_key
    if _our_axl_key:
        return _our_axl_key
    try:
        topo = requests.get(f"{_axl_base}/topology", timeout=3).json()
        _our_axl_key = topo.get("our_public_key", "")
    except Exception:
        pass
    return _our_axl_key


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


@app.route("/api/kill_random", methods=["POST"])
def api_kill_random():
    data  = flask_request.get_json(force=True) or {}
    count = int(data.get("count", 3))
    with _lock:
        alive_ports = [p for p, s in _node_states.items() if s]
    targets = [(p, _find_whisper_pid(p)) for p in alive_ports]
    targets = [(p, pid) for p, pid in targets if pid]
    if not targets:
        return json.dumps({"ok": False, "error": "no alive nodes found"}), 404
    chosen = random.sample(targets, min(count, len(targets)))
    killed = []
    for port, pid in chosen:
        try:
            os.kill(pid, signal.SIGSTOP)
            _chaos_killed_ports.add(port)
            ts = time.strftime("%H:%M:%S")
            with _lock:
                _global_events.appendleft(f"[{ts}] ⚡ CHAOS: node :{port} killed (PID {pid})")
            killed.append(port)
        except Exception:
            pass
    return json.dumps({"ok": True, "killed": killed})


@app.route("/api/partition", methods=["POST"])
def api_partition():
    with _lock:
        alive_ports = sorted([p for p, s in _node_states.items() if s])
    if not alive_ports:
        return json.dumps({"ok": False, "error": "no alive nodes"}), 404
    half = alive_ports[: len(alive_ports) // 2]
    stopped = []
    for port in half:
        pid = _find_whisper_pid(port)
        if pid:
            try:
                os.kill(pid, signal.SIGSTOP)
                _partitioned_ports.add(port)
                ts = time.strftime("%H:%M:%S")
                with _lock:
                    _global_events.appendleft(f"[{ts}] ✂ CHAOS: node :{port} partitioned")
                stopped.append(port)
            except Exception:
                pass
    return json.dumps({"ok": True, "partitioned": stopped})


@app.route("/api/heal", methods=["POST"])
def api_heal():
    healed = []
    for port in list(_partitioned_ports):
        pid = _find_whisper_pid(port)
        if pid:
            try:
                os.kill(pid, signal.SIGCONT)
                ts = time.strftime("%H:%M:%S")
                with _lock:
                    _global_events.appendleft(f"[{ts}] ✓ CHAOS: node :{port} partition healed")
                healed.append(port)
            except Exception:
                pass
    _partitioned_ports.clear()
    return json.dumps({"ok": True, "healed": healed})


@app.route("/api/revive_all", methods=["POST"])
def api_revive_all():
    all_stopped = set(_partitioned_ports) | set(_chaos_killed_ports)
    revived = []
    for port in all_stopped:
        pid = _find_whisper_pid(port)
        if pid:
            try:
                os.kill(pid, signal.SIGCONT)
                ts = time.strftime("%H:%M:%S")
                with _lock:
                    _global_events.appendleft(f"[{ts}] ▶ CHAOS: node :{port} revived")
                revived.append(port)
            except Exception:
                pass
    _partitioned_ports.clear()
    _chaos_killed_ports.clear()
    return json.dumps({"ok": True, "revived": revived})


@app.route("/api/node/<int:port>")
def api_node_detail(port):
    with _lock:
        state = _node_states.get(port)
    if not state:
        return json.dumps({"error": "node offline or not found"}), 404
    return json.dumps({
        "port":         port,
        "our_key":      state.get("our_key", ""),
        "key_short":    state.get("key_short", ""),
        "shard_id":     state.get("shard_id"),
        "ens_name":     state.get("ens_name"),
        "capabilities": state.get("capabilities", []),
        "price_axl":    state.get("price_axl", 0.01),
        "metrics":      state.get("metrics", {}),
        "tasks":        state.get("tasks", {}),
        "peers":        state.get("peers", {}),
        "axl_mesh":     state.get("axl_mesh", {}),
        "balance":      round(_provider_balances.get(state.get("our_key", ""), 0.0), 3),
    })


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
                "shard_id": 0, "ens_name": None, "port": port, "status": "offline",
                "up_peers": 0, "total_peers": 0, "metrics": {},
            })
            continue

        our_key  = state.get("our_key", "")
        axl_mesh = state.get("axl_mesh", {})
        metrics  = state.get("metrics", {})
        peers    = state.get("peers", {})

        nodes.append({
            "id":           our_key,
            "short":        state.get("key_short", our_key[:8]),
            "shard_id":     state.get("shard_id", 0),
            "ens_name":     state.get("ens_name"),
            "port":         port,
            "status":       "alive",
            "up_peers":     axl_mesh.get("up_peers", 0),
            "total_peers":  axl_mesh.get("total_peers", 0),
            "metrics":      metrics,
            "tasks_held":   len([t for t in state.get("tasks", {}).values()
                                 if t.get("status") == "in_progress"
                                 and t.get("leased_by", "")[:8] == our_key[:8]]),
            "capabilities": state.get("capabilities", []),
            "price_axl":    state.get("price_axl", 0.01),
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
        "throughput": list(_throughput_history),
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
    base_url = _node_urls.get(port, f"http://127.0.0.1:{port}")
    try:
        resp = requests.get(f"{base_url}/state", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _poller():
    while True:
        changed = False
        for port in list(_node_states.keys()):
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
                completed_now = payload["cluster"]["completed"]
                _throughput_history.append({"ts": round(time.time(), 1), "c": completed_now})
                if len(_throughput_history) > _THROUGHPUT_MAXLEN:
                    _throughput_history.pop(0)
                payload["throughput"] = list(_throughput_history)
            socketio.emit("update", payload)

        time.sleep(2)


@app.route("/api/ens/<subname>")
def api_ens_records(subname):
    api_key = os.environ.get("JUSTANAME_API_KEY", "").strip()
    if not api_key:
        return json.dumps({"error": "no ENS records — start webui with JUSTANAME_API_KEY set"}), 200
    try:
        resp = requests.get(
            "https://api.justaname.id/ens/v1/subname/subname",
            params={"subname": subname, "chainId": 11155111},
            headers={"x-api-key": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        # Raw envelope: {"result": {"data": SubnameResponse}}
        # SubnameResponse.records.texts = [{key, value}, ...]
        texts = resp.json().get("result", {}).get("data", {}).get("records", {}).get("texts", [])
        records = {r["key"]: r["value"] for r in texts if "key" in r and "value" in r}
        return json.dumps(records)
    except Exception as exc:
        return json.dumps({"error": str(exc)}), 200


@app.route("/api/debug/ens")
def api_debug_ens():
    """Return ens_name for each known node — useful for confirming ENS data flow."""
    with _lock:
        result = {}
        for port, state in _node_states.items():
            result[str(port)] = {
                "ens_name": state.get("ens_name") if state else None,
                "key_short": state.get("key_short") if state else None,
                "status": "alive" if state else "offline",
            }
    return json.dumps(result)


@app.route("/api/nodes/add", methods=["POST"])
def api_nodes_add():
    with _lock:
        existing_ports = sorted(_node_states.keys())
    if not existing_ports:
        return json.dumps({"error": "no base nodes registered"}), 400

    # new node is one beyond the highest existing port
    new_port = max(existing_ports) + 1
    new_num  = new_port - 8887       # node number (1-based), matches run_local.sh
    shard_id = new_num

    caps_cycle = ["search", "summarize", "reason"]
    caps = caps_cycle[(new_num - 1) % 3]
    price = 0.010

    axl_cfg_dir = _spawn_cfg.get("axl_cfg_dir", "axl-local")
    keys_dir    = _spawn_cfg.get("keys_dir",    "keys")
    logs_dir    = _spawn_cfg.get("logs_dir",    "logs")
    data_dir    = _spawn_cfg.get("data_dir",    "data")
    shards_dir  = _spawn_cfg.get("shards_dir",  "demo/shards")
    axl_bin     = _spawn_cfg.get("axl_bin",     "./axl/node")

    for d in [axl_cfg_dir, keys_dir, logs_dir, data_dir]:
        os.makedirs(d, exist_ok=True)

    # Generate AXL config matching run_local.sh format (api_port = 9001 + i)
    axl_api_port = 9001 + new_num
    axl_cfg_path = os.path.join(axl_cfg_dir, f"node-config-{new_num}.json")
    if not os.path.exists(axl_cfg_path):
        cfg = {
            "PrivateKeyPath": os.path.join(keys_dir, f"private-{new_num}.pem"),
            "Peers":          [f"tls://127.0.0.1:9001"],
            "Listen":         [],
            "api_port":       axl_api_port,
            "tcp_port":       7000,
            "bridge_addr":    "127.0.0.1",
        }
        with open(axl_cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)

    key_file   = os.path.join(keys_dir, f"private-{new_num}.pem")
    log_file   = os.path.join(logs_dir, f"node-{new_num}.log")
    ledger     = os.path.join(data_dir, f"ledger-{new_num}.json")
    shard_idx  = ((shard_id - 1) % 6) + 1
    shard_file = os.path.join(shards_dir, f"shard-{shard_idx}.txt")
    if not os.path.exists(shard_file):
        shard_file = os.path.join(shards_dir, "shard-1.txt")

    cluster_size = len(existing_ports) + 1

    if not os.path.exists(key_file):
        try:
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ed25519", "-out", key_file],
                check=True, capture_output=True,
            )
        except Exception as exc:
            return json.dumps({"error": f"key gen failed: {exc}"}), 500

    try:
        with open(log_file, "a") as lf:
            axl_proc = subprocess.Popen(
                [axl_bin, "-config", axl_cfg_path],
                stdout=lf, stderr=lf,
            )
    except Exception as exc:
        return json.dumps({"error": f"AXL start failed: {exc}"}), 500

    time.sleep(1.5)

    python_bin = _spawn_cfg.get("python_bin", "python")
    try:
        with open(log_file, "a") as lf:
            whisper_proc = subprocess.Popen(
                [
                    python_bin, "-m", "whisper.node",
                    "--api-base",     f"http://127.0.0.1:{axl_api_port}",
                    "--shard-id",     str(shard_id),
                    "--shard-file",   shard_file,
                    "--ledger-file",  ledger,
                    "--debug-port",   str(new_port),
                    "--key-file",     key_file,
                    "--capabilities", caps,
                    "--price-axl",    str(price),
                    "--cluster-size", str(cluster_size),
                ],
                stdout=lf, stderr=lf,
            )
    except Exception as exc:
        axl_proc.terminate()
        return json.dumps({"error": f"whisper start failed: {exc}"}), 500

    with _lock:
        _node_states[new_port] = None
        _dynamic_procs[new_port] = (axl_proc, whisper_proc)
        total = len(_node_states)

    ts = time.strftime("%H:%M:%S")
    with _lock:
        _global_events.appendleft(f"[{ts}] + node :{new_port} added (shard-{shard_id}, {caps})")

    return json.dumps({"ok": True, "port": new_port, "total": total})


@app.route("/api/nodes/remove", methods=["POST"])
def api_nodes_remove():
    with _lock:
        if not _dynamic_procs:
            return json.dumps({"error": "no dynamic nodes to remove"}), 400
        remove_port = max(_dynamic_procs.keys())
        procs = _dynamic_procs.pop(remove_port)
        _node_states.pop(remove_port, None)
        _node_urls.pop(remove_port, None)
        total = len(_node_states)

    axl_proc, whisper_proc = procs
    for proc in (whisper_proc, axl_proc):
        try:
            proc.terminate()
        except Exception:
            pass

    ts = time.strftime("%H:%M:%S")
    with _lock:
        _global_events.appendleft(f"[{ts}] - node :{remove_port} removed")

    return json.dumps({"ok": True, "removed_port": remove_port, "total": total})


# ── Entry point ────────────────────────────────────────────────────────────────

def _load_dotenv(path: str = ".env") -> None:
    """Load key=value pairs from .env into os.environ (existing vars win)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


def main():
    global _axl_base, _spawn_cfg
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Whisper Network Web UI")
    parser.add_argument("--host",       default="0.0.0.0")
    parser.add_argument("--port",       type=int, default=5000)
    parser.add_argument("--count",       type=int, default=None,
                        help="Number of nodes (generates ports 8888..8888+count-1)")
    parser.add_argument("--nodes",      default=None,
                        help="Explicit port range (e.g. 8888-8893) or comma list; overrides --count")
    parser.add_argument("--node-urls",  default="",
                        help="Comma-separated http://host:port URLs for Docker mode (overrides localhost)")
    parser.add_argument("--axl-base",   default="http://127.0.0.1:9002",
                        help="AXL HTTP API of gateway node (for sending tasks via AXL mesh)")
    parser.add_argument("--axl-bin",    default="./axl/node",
                        help="Path to AXL binary for spawning new nodes")
    parser.add_argument("--axl-cfg-dir",default="axl-local",
                        help="Directory for AXL per-node config files")
    parser.add_argument("--keys-dir",   default="keys",
                        help="Directory for persistent ed25519 key files")
    parser.add_argument("--logs-dir",   default="logs",
                        help="Directory for spawned node log files")
    parser.add_argument("--data-dir",   default="data",
                        help="Directory for ledger files")
    parser.add_argument("--shards-dir", default="demo/shards",
                        help="Directory for shard text files")
    parser.add_argument("--python-bin", default="python",
                        help="Python interpreter for spawning whisper nodes")
    args = parser.parse_args()
    _axl_base = args.axl_base

    _spawn_cfg.update({
        "axl_bin":    args.axl_bin,
        "axl_cfg_dir":args.axl_cfg_dir,
        "keys_dir":   args.keys_dir,
        "logs_dir":   args.logs_dir,
        "data_dir":   args.data_dir,
        "shards_dir": args.shards_dir,
        "python_bin": args.python_bin,
    })

    if args.nodes:
        if "-" in args.nodes:
            lo, hi = args.nodes.split("-")
            ports = list(range(int(lo), int(hi) + 1))
        else:
            ports = [int(p) for p in args.nodes.split(",")]
    elif args.count:
        ports = list(range(8888, 8888 + args.count))
    else:
        ports = list(range(8888, 8894))  # default: 6 nodes

    node_url_list = [u.strip() for u in args.node_urls.split(",") if u.strip()] if args.node_urls else []

    for i, p in enumerate(ports):
        _node_states[p] = None
        if i < len(node_url_list):
            _node_urls[p] = node_url_list[i]

    print(f"Whisper Web UI  →  http://{args.host}:{args.port}")
    print(f"Polling nodes on ports: {ports}")

    threading.Thread(target=_poller, daemon=True, name="poller").start()
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
