"""
Multi-client concurrent inference demo.

Spawns N independent clients, each submitting a different query simultaneously
to a different compute node. Demonstrates true marketplace dynamics — multiple
buyers, multiple providers, no coordinator.

Usage:
    python -m demo.multi_client
    python -m demo.multi_client "attention mechanism" "gradient descent" "BERT"
"""
import sys
import threading
import time
import uuid

import requests

DEFAULT_QUERIES = [
    "attention mechanism",
    "transformer architecture",
    "gradient descent optimization",
    "positional encoding",
    "multi-head attention",
    "layer normalization",
]

PORTS = list(range(8888, 8894))  # debug API ports for nodes 1-6


def _submit_and_wait(query: str, port: int, shard_id: int, out: dict, idx: int):
    task_id = str(uuid.uuid4())
    t0 = time.time()
    try:
        r = requests.post(
            f"http://127.0.0.1:{port}/submit",
            json={"task_id": task_id, "payload": query, "shard_id": shard_id},
            timeout=3,
        )
        if r.status_code != 200:
            out[idx] = {"query": query, "error": f"submit HTTP {r.status_code}"}
            return
    except Exception as e:
        out[idx] = {"query": query, "error": str(e)}
        return

    # Poll /results on same node
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/results", timeout=2)
            for res in r.json():
                if res.get("task_id") == task_id:
                    out[idx] = {
                        "query":    query,
                        "task_id":  task_id,
                        "shard_id": shard_id,
                        "port":     port,
                        "result":   (res.get("result") or ""),
                        "elapsed":  round(time.time() - t0, 2),
                    }
                    return
        except Exception:
            pass
        time.sleep(0.4)

    out[idx] = {"query": query, "error": "timeout (>25s)", "elapsed": round(time.time() - t0, 1)}


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_QUERIES
    n       = len(queries)
    results = {}
    threads = []

    print(f"\n{'━'*64}")
    print(f"  Whisper Network  ·  {n} Concurrent Clients")
    print(f"{'━'*64}")
    for i, q in enumerate(queries):
        port     = PORTS[i % len(PORTS)]
        shard_id = (i % 6) + 1
        print(f"  client-{i+1:02d}  shard-{shard_id}  :{port}  '{q}'")

    print(f"\n  ▸ Launching all {n} clients simultaneously…\n")
    t_start = time.time()

    for i, q in enumerate(queries):
        port     = PORTS[i % len(PORTS)]
        shard_id = (i % 6) + 1
        t = threading.Thread(
            target=_submit_and_wait,
            args=(q, port, shard_id, results, i),
            daemon=True,
            name=f"client-{i+1}",
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    wall = round(time.time() - t_start, 2)
    ok   = sum(1 for r in results.values() if "result" in r)

    print(f"{'━'*64}")
    print(f"  Results  ({wall}s wall time)")
    print(f"{'━'*64}")
    for i in range(n):
        r = results.get(i)
        if r is None:
            print(f"  [{i+1:02d}] ✗  no response")
        elif "error" in r:
            elapsed = f"  +{r['elapsed']}s" if "elapsed" in r else ""
            print(f"  [{i+1:02d}] ✗  {r['error']}{elapsed}")
        else:
            snippet = r["result"][:72] + ("…" if len(r["result"]) > 72 else "")
            print(f"  [{i+1:02d}] ✓  +{r['elapsed']}s  {snippet}")

    print(f"{'━'*64}")
    print(f"  {ok}/{n} succeeded  ·  {wall}s total\n")


if __name__ == "__main__":
    main()
