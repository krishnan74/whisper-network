#!/usr/bin/env python3
"""
Submit a task via the AXL encrypted overlay mesh (no debug HTTP required).

Instead of posting to a whisper node's /submit debug endpoint, this script
reads the AXL topology to find a live peer and injects tasks directly as
'task_submit' AXL messages. The receiving node gossips them to the full mesh.

This demonstrates AXL as a first-class application bus — not just a transport
that whisper happens to run on top of.

Usage:
    python -m demo.submit_p2p "neural network"
    python -m demo.submit_p2p "gossip" --axl http://localhost:9002 --wait http://localhost:8888
"""
import argparse
import json
import sys
import time
import uuid

import requests


def submit_via_axl(axl_base: str, query: str, num_shards: int = 6, wait_api: str = None):
    axl_base = axl_base.rstrip("/")

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

    # Route all tasks through the first AXL-connected peer; gossip does the rest.
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
        print(f"  shard-{shard_id}: {task_id}  {'✓ sent via AXL' if ok else '✗ send failed'}")
        task_ids.append((shard_id, task_id))

    if not wait_api:
        print("\nDone. (Pass --wait <whisper-api> to poll for results.)")
        return

    print(f"\nWaiting for results via {wait_api}/state ...\n")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            state = requests.get(f"{wait_api}/state", timeout=5).json()
        except Exception as e:
            print(f"  (retrying — {e})")
            time.sleep(2)
            continue

        tasks = state.get("tasks", {})
        done  = {
            sid: tasks[tid]["result"]
            for sid, tid in task_ids
            if tid in tasks and tasks[tid]["status"] == "completed"
        }

        bar = "".join("█" if i + 1 in done else "░" for i in range(num_shards))
        print(f"\r  [{bar}] {len(done)}/{len(task_ids)} complete", end="", flush=True)

        if len(done) == len(task_ids):
            print("\n\n═══════════════════════════════════════════")
            print(f"  QUERY : {query!r}  (submitted via AXL P2P)")
            print("═══════════════════════════════════════════")
            for shard_id in sorted(done):
                print(f"  {done[shard_id]}")
            print("═══════════════════════════════════════════\n")
            return

        time.sleep(2)

    print(f"\nTimeout after 120s — {len(done)}/{len(task_ids)} tasks completed.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Submit task via AXL P2P message")
    parser.add_argument("query",   help="Keyword to search across all shards")
    parser.add_argument("--axl",   default="http://localhost:9002",
                        help="AXL node HTTP API base (default: http://localhost:9002)")
    parser.add_argument("--shards", type=int, default=6)
    parser.add_argument("--wait",  default="http://localhost:8888",
                        help="Whisper debug API to poll for results "
                             "(empty string = skip polling)")
    args = parser.parse_args()
    submit_via_axl(args.axl, args.query, args.shards, args.wait or None)


if __name__ == "__main__":
    main()
