#!/usr/bin/env python3
"""
Centralized comparison: identical task-distribution system using Redis pub/sub.

Run this alongside the Whisper Network demo to show the contrast:
  - Works fine while Redis is alive
  - Freezes completely when Redis is killed (SIGKILL docker stop redis)
  - No recovery, no reassignment, no fault tolerance

Usage:
    # Terminal 1 — start Redis (or use docker-compose redis service)
    docker run --rm -p 6379:6379 redis:7

    # Terminal 2 — run the broker demo
    python -m comparison.redis_broker --query "neural network"

    # Kill Redis mid-execution:
    docker kill <redis-container>
    # Watch the broker freeze with no recovery.
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid
from typing import Optional

try:
    import redis
except ImportError:
    print("Install redis-py:  pip install redis")
    sys.exit(1)


SHARD_FILES = [f"demo/shards/shard-{i}.txt" for i in range(1, 7)]
NUM_SHARDS  = 6


def load_shard(shard_id: int) -> list[str]:
    path = f"demo/shards/shard-{shard_id}.txt"
    if os.path.exists(path):
        with open(path) as f:
            return [l.rstrip() for l in f if l.strip()]
    return []


def execute_query(payload: str, shard_id: int, lines: list[str]) -> str:
    query = payload.replace("query:", "").strip().lower()
    matches = [l for l in lines if query in l.lower()]
    if matches:
        preview = " | ".join(matches[:3])[:120]
        return f"shard-{shard_id}: {len(matches)} match(es): {preview}"
    return f"shard-{shard_id}: no matches for '{query}'"


class RedisWorker(threading.Thread):
    def __init__(self, shard_id: int, redis_url: str):
        super().__init__(daemon=True, name=f"worker-shard-{shard_id}")
        self.shard_id  = shard_id
        self.redis_url = redis_url
        self.lines     = load_shard(shard_id)
        self.status    = "starting"
        self._r: Optional[redis.Redis] = None

    def run(self):
        try:
            self._r    = redis.from_url(self.redis_url, socket_timeout=5)
            self.status = "idle"
            print(f"  worker shard-{self.shard_id} ready ({len(self.lines)} lines loaded)")
        except Exception as e:
            self.status = f"failed: {e}"
            return

        queue = f"whisper:queue:shard:{self.shard_id}"
        while True:
            try:
                # Blocking pop — hangs forever if Redis dies
                item = self._r.blpop(queue, timeout=0)
                if item is None:
                    continue

                _, task_json = item
                task         = json.loads(task_json)
                task_id      = task["task_id"]

                self.status  = f"executing {task_id[:8]}"
                print(f"  [shard-{self.shard_id}] executing task {task_id[:8]}")

                # Simulate brief work
                time.sleep(0.5)

                result = execute_query(task["payload"], self.shard_id, self.lines)

                self._r.hset("whisper:results", task_id, json.dumps({
                    "task_id": task_id,
                    "result":  result,
                    "done_at": time.time(),
                }))
                self._r.rpush("whisper:completed", task_id)
                self.status = "idle"
                print(f"  [shard-{self.shard_id}] ✓ {result[:70]}")

            except redis.RedisError as e:
                # ── THIS IS WHERE IT FREEZES ──────────────────────────────
                self.status = f"BLOCKED — Redis down: {e}"
                print(
                    f"\n  !! [shard-{self.shard_id}] REDIS ERROR: {e}\n"
                    "  !! No recovery possible — this worker is stuck.\n"
                    "  !! (Whisper Network would reclaim the task in ~6s)\n",
                    flush=True,
                )
                # No retry logic — just freeze like a real system would
                time.sleep(9999)
                return


class RedisBrokerDemo:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.workers: list[RedisWorker] = []
        self._r: Optional[redis.Redis] = None

    def connect(self):
        self._r = redis.from_url(self.redis_url, socket_timeout=5)
        self._r.ping()

    def start_workers(self):
        print(f"\nStarting {NUM_SHARDS} shard workers...")
        for i in range(1, NUM_SHARDS + 1):
            w = RedisWorker(i, self.redis_url)
            w.start()
            self.workers.append(w)
        time.sleep(0.5)  # let workers connect

    def submit_query(self, query: str) -> list[str]:
        task_ids = []
        print(f"\nSubmitting query '{query}' across {NUM_SHARDS} shards...")
        for shard_id in range(1, NUM_SHARDS + 1):
            task_id = f"redis-{uuid.uuid4().hex[:6]}-s{shard_id}"
            task = {
                "task_id":  task_id,
                "payload":  f"query: {query}",
                "shard_id": shard_id,
            }
            self._r.rpush(f"whisper:queue:shard:{shard_id}", json.dumps(task))
            task_ids.append(task_id)
        return task_ids

    def wait_for_results(self, task_ids: list[str], timeout: int = 60) -> dict:
        print(f"\nWaiting for {len(task_ids)} tasks (timeout {timeout}s)...\n")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                done   = {}
                for tid in task_ids:
                    raw = self._r.hget("whisper:results", tid)
                    if raw:
                        done[tid] = json.loads(raw)["result"]

                bar    = "".join("█" if tid in done else "░" for tid in task_ids)
                status = " | ".join(
                    f"shard-{i+1}:{'done' if tid in done else 'wait'}"
                    for i, tid in enumerate(task_ids)
                )
                print(f"\r  [{bar}] {len(done)}/{len(task_ids)}  {status}", end="", flush=True)

                if len(done) == len(task_ids):
                    print("\n\n=== REDIS RESULTS ===")
                    for tid, result in done.items():
                        print(f"  {result}")
                    return done

            except redis.RedisError as e:
                print(f"\n\n  !! REDIS UNREACHABLE: {e}")
                print("  !! All pending tasks are LOST — no recovery possible.")
                print("  !! (Contrast: Whisper Network recovers in ~6s)\n")
                return {}

            time.sleep(1)

        print(f"\nTimeout after {timeout}s")
        return {}

    def cleanup(self):
        if self._r:
            try:
                self._r.delete("whisper:results", "whisper:completed")
                for i in range(1, NUM_SHARDS + 1):
                    self._r.delete(f"whisper:queue:shard:{i}")
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Redis centralized broker demo")
    parser.add_argument("--query",  default="neural network", help="search query")
    parser.add_argument("--redis",  default="redis://localhost:6379")
    parser.add_argument("--kill-at", type=float, default=0,
                        help="Kill Redis after N seconds (0=don't). "
                             "Manually kill with: docker kill redis")
    args = parser.parse_args()

    demo = RedisBrokerDemo(args.redis)

    print("=== Centralized Broker Demo (Redis) ===")
    print("Connecting to Redis...")
    try:
        demo.connect()
        print("Connected.\n")
    except Exception as e:
        print(f"Cannot connect to Redis: {e}")
        print("Start Redis with: docker run --rm -p 6379:6379 redis:7")
        sys.exit(1)

    demo.cleanup()
    demo.start_workers()
    task_ids = demo.submit_query(args.query)

    if args.kill_at > 0:
        def kill_redis():
            time.sleep(args.kill_at)
            print(f"\n\n  *** KILLING REDIS AFTER {args.kill_at}s ***\n")
            os.system("docker kill redis 2>/dev/null || pkill -f 'redis-server' 2>/dev/null || true")
        threading.Thread(target=kill_redis, daemon=True).start()

    demo.wait_for_results(task_ids)


if __name__ == "__main__":
    main()
