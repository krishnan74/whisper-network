#!/usr/bin/env python3
"""
Submit a task via the AXL encrypted overlay mesh (no debug HTTP required).

Instead of posting to a whisper node's /submit debug endpoint, this script
reads the AXL topology to find a live peer and injects tasks directly as
'task_submit' AXL messages.  When each task completes the executing node
sends a 'task_result' push notification back to us via AXL — demonstrating
AXL as a true bidirectional, encrypted application bus.

Usage:
    python -m demo.submit_p2p "neural network"
    python -m demo.submit_p2p "gossip" --axl http://localhost:9002
"""
import argparse
import json
import sys
import time
import uuid

import requests


def _drain_recv(axl_base: str) -> list[dict]:
    """Pull all pending messages from the AXL recv queue."""
    msgs = []
    while True:
        try:
            resp = requests.get(f"{axl_base}/recv", timeout=3)
            if resp.status_code == 204 or not resp.content:
                break
            data = resp.json()
            if not data:
                break
            msgs.append(data)
        except Exception:
            break
    return msgs


def submit_via_axl(axl_base: str, query: str, num_shards: int = 6):
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
    task_ids = []  # list of (shard_id, task_id)

    for shard_id in range(1, num_shards + 1):
        task_id = f"p2p-{uuid.uuid4().hex[:6]}-s{shard_id}"
        msg = {
            "type":     "task_submit",
            "msg_id":   str(uuid.uuid4()),
            "from":     our_key,          # nodes use this to push task_result back to us
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
        print(f"  shard-{shard_id}: {task_id}  {'sent via AXL' if ok else 'send failed'}")
        task_ids.append((shard_id, task_id))

    task_id_set = {tid for _, tid in task_ids}
    done: dict[int, str] = {}  # shard_id -> result

    print(f"\nWaiting for push notifications via AXL /recv ...\n")
    deadline = time.time() + 120
    while time.time() < deadline and len(done) < len(task_ids):
        for msg in _drain_recv(axl_base):
            if msg.get("type") == "task_result" and msg.get("task_id") in task_id_set:
                sid    = int(msg.get("shard_id", 0))
                result = msg.get("result", "")
                if sid not in done:
                    done[sid] = result

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
        print(f"\nTimeout — {len(done)}/{len(task_ids)} tasks received push notifications.")
        print("(Results may still be completing — check dashboard or whisper /state)")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Submit task via AXL P2P message")
    parser.add_argument("query",    help="Keyword to search across all shards")
    parser.add_argument("--axl",    default="http://localhost:9002",
                        help="AXL node HTTP API base (default: http://localhost:9002)")
    parser.add_argument("--shards", type=int, default=6)
    args = parser.parse_args()
    submit_via_axl(args.axl, args.query, args.shards)


if __name__ == "__main__":
    main()
