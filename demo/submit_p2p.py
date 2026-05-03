#!/usr/bin/env python3
"""
Submit a task via the AXL encrypted overlay mesh (no debug HTTP for submission).

Tasks are injected as 'task_submit' AXL messages. When each shard completes,
the executing node sends a 'task_result' push notification back via AXL to the
submitter's key. The receiving whisper node buffers these in memory and exposes
them via GET /results on its debug API — submit_p2p polls that to collect results.

This keeps the full submit→execute→result flow on AXL while avoiding the
/recv queue race with the whisper recv loop.

Usage:
    python -m demo.submit_p2p "neural network"
    python -m demo.submit_p2p "gossip" --axl http://localhost:9002 --api http://localhost:8888
"""
import argparse
import json
import sys
import time
import uuid

import requests


def submit_via_axl(axl_base: str, whisper_api: str, query: str, num_shards: int = 6):
    axl_base    = axl_base.rstrip("/")
    whisper_api = whisper_api.rstrip("/")

    print("Reading AXL topology...")
    try:
        topo    = requests.get(f"{axl_base}/topology", timeout=5).json()
        our_key = topo["our_public_key"]
        peers   = [
            p["public_key"]
            for p in topo.get("peers", [])
            if p.get("up") and p.get("public_key")
        ]
    except Exception as e:
        print(f"Cannot reach AXL at {axl_base}: {e}")
        sys.exit(1)

    if not peers:
        print("No AXL peers connected. Is the network up?")
        sys.exit(1)

    print(f"AXL identity : {our_key[:20]}...")
    print(f"AXL mesh     : {len(peers)} direct peer(s) up")
    print(f"\nSubmitting '{query}' across {num_shards} shards via AXL P2P...\n")

    target   = peers[0]
    task_ids = []

    for shard_id in range(1, num_shards + 1):
        task_id = f"p2p-{uuid.uuid4().hex[:6]}-s{shard_id}"
        msg = {
            "type":     "task_submit",
            "msg_id":   str(uuid.uuid4()),
            "from":     our_key,
            "task_id":  task_id,
            "payload":  f"query: {query}",
            "shard_id": shard_id,
        }
        try:
            resp = requests.post(
                f"{axl_base}/send",
                headers={"X-Destination-Peer-Id": target},
                data=json.dumps(msg).encode(),
                timeout=5,
            )
            ok = resp.status_code == 200
        except Exception:
            ok = False
        print(f"  shard-{shard_id}: {task_id}  {'sent via AXL' if ok else 'send FAILED'}")
        task_ids.append((shard_id, task_id))

    task_id_set = {tid for _, tid in task_ids}
    done: dict[int, str] = {}

    print(f"\nWaiting for push results via {whisper_api}/results ...\n")
    deadline = time.time() + 360
    while time.time() < deadline and len(done) < len(task_ids):
        try:
            results = requests.get(f"{whisper_api}/results", timeout=3).json()
        except Exception:
            results = []

        for r in results:
            if r.get("task_id") in task_id_set:
                sid = int(r.get("shard_id", 0))
                if sid not in done:
                    done[sid] = r.get("result", "")

        bar = "".join("█" if i + 1 in done else "░" for i in range(num_shards))
        print(f"\r  [{bar}] {len(done)}/{len(task_ids)} complete", end="", flush=True)

        if len(done) < len(task_ids):
            time.sleep(1)

    print()
    if len(done) == len(task_ids):
        print("\n═══════════════════════════════════════════")
        print(f"  QUERY : {query!r}  (submitted + results via AXL)")
        print("═══════════════════════════════════════════")
        for shard_id in sorted(done):
            print(f"  {done[shard_id]}")
        print("═══════════════════════════════════════════\n")
    else:
        print(f"\nTimeout — {len(done)}/{len(task_ids)} results received.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Submit task via AXL P2P")
    parser.add_argument("query",    help="Keyword to search across all shards")
    parser.add_argument("--axl",    default="http://localhost:9002",
                        help="AXL node HTTP API (default: node-1 at :9002)")
    parser.add_argument("--api",    default="http://localhost:8888",
                        help="Whisper debug API on the same node (for /results polling)")
    parser.add_argument("--shards", type=int, default=6)
    args = parser.parse_args()
    submit_via_axl(args.axl, args.api, args.query, args.shards)


if __name__ == "__main__":
    main()
