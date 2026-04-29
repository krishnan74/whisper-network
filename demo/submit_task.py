#!/usr/bin/env python3
"""
Submit a distributed search query across all 6 shards and collect results.

Usage:
    python -m demo.submit_task "neural network" --api http://localhost:8888
    python -m demo.submit_task "protein folding"
"""
import argparse
import json
import sys
import time
import uuid

import requests


def submit_query(api_base: str, query: str, num_shards: int = 6, timeout: int = 120):
    print(f"\nSubmitting query '{query}' across {num_shards} shards...\n")

    task_ids = []
    for shard_id in range(1, num_shards + 1):
        task_id = f"q-{uuid.uuid4().hex[:6]}-s{shard_id}"
        resp = requests.post(
            f"{api_base}/submit",
            json={"task_id": task_id, "payload": f"query: {query}", "shard_id": shard_id},
            timeout=5,
        )
        resp.raise_for_status()
        task_ids.append((shard_id, task_id))
        print(f"  submitted shard-{shard_id}: {task_id}")

    print(f"\nWaiting for {len(task_ids)} tasks to complete (timeout {timeout}s)...\n")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp  = requests.get(f"{api_base}/state", timeout=5)
            state = resp.json()
        except Exception as e:
            print(f"  (retrying — {e})")
            time.sleep(2)
            continue

        tasks    = state.get("tasks", {})
        done     = {sid: tasks[tid]["result"]
                    for sid, tid in task_ids
                    if tid in tasks and tasks[tid]["status"] == "completed"}
        pending  = len(task_ids) - len(done)

        bar = "".join("█" if i + 1 in done else "░" for i in range(num_shards))
        print(f"\r  [{bar}] {len(done)}/{len(task_ids)} complete", end="", flush=True)

        if pending == 0:
            print("\n\n═══════════════════════════════════════")
            print(f"  QUERY: {query!r}")
            print("═══════════════════════════════════════")
            for shard_id in sorted(done):
                print(f"  {done[shard_id]}")
            print("═══════════════════════════════════════\n")
            return done

        time.sleep(2)

    print(f"\n\nTimeout after {timeout}s — {len(done)}/{len(task_ids)} tasks completed.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Submit a whisper network query")
    parser.add_argument("query",  help="Keyword to search across all shards")
    parser.add_argument("--api",  default="http://localhost:8888",
                        help="Whisper debug API base (default: http://localhost:8888)")
    parser.add_argument("--shards",   type=int, default=6)
    parser.add_argument("--timeout",  type=int, default=120)
    args = parser.parse_args()
    submit_query(args.api, args.query, args.shards, args.timeout)


if __name__ == "__main__":
    main()
