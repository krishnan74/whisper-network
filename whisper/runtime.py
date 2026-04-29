"""
Layer 3: Agent Runtime

Background loop that:
  1. Renews leases we hold before they expire
  2. Claims tasks matching our shard_id (pending or expired leases)
  3. Executes claimed tasks and writes results back to the ledger

For the demo, "execution" is a keyword search over the node's local document
shard. The execute() method can be replaced for any other workload.
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 5.0   # seconds between ledger scans


class AgentRuntime:
    def __init__(
        self,
        ledger,
        our_key: str,
        shard_id: int,
        shard_file: str,
    ):
        self.ledger    = ledger
        self.our_key   = our_key
        self.shard_id  = shard_id
        self.shard_file = shard_file

        self._shard_lines: list[str] = []
        self._load_shard()

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _load_shard(self):
        if os.path.exists(self.shard_file):
            with open(self.shard_file) as f:
                self._shard_lines = [l.rstrip() for l in f if l.strip()]
            logger.info(
                "loaded shard-%d: %d lines from %s",
                self.shard_id, len(self._shard_lines), self.shard_file,
            )
        else:
            logger.warning("shard file not found: %s", self.shard_file)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="runtime"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._scan()
            except Exception as e:
                logger.error("runtime scan error: %s", e, exc_info=True)
            time.sleep(SCAN_INTERVAL)

    def _scan(self):
        # 1. Renew any leases that are about to expire
        for task in self.ledger.get_tasks_needing_renewal():
            self.ledger.renew_lease(task.task_id)

        # 2. Find tasks for our shard that need claiming
        claimable = [
            t for t in self.ledger.get_claimable_tasks()
            if t.shard_id == self.shard_id
        ]

        for task in claimable:
            if not self.ledger.claim_task(task.task_id):
                continue

            # Brief pause to let gossip propagate our claim before executing;
            # if another node won the race we'll see it on next scan.
            time.sleep(0.3)

            # Re-check we still hold the lease after gossip settle
            active = [t for t in self.ledger.get_my_active_tasks()
                      if t.task_id == task.task_id]
            if not active:
                logger.info("lost lease race for %s, skipping", task.task_id[:12])
                continue

            logger.info("executing task %s (shard-%d)", task.task_id[:12], self.shard_id)
            result = self.execute(task.payload)
            self.ledger.complete_task(task.task_id, result)
            break  # process one task per scan cycle

    def execute(self, payload: str) -> str:
        """
        Search the local document shard for lines matching the query.
        Override this method to plug in any other task execution logic.
        """
        query = payload.strip()
        if query.lower().startswith("query:"):
            query = query[6:].strip()
        query_lower = query.lower()

        matches = [line for line in self._shard_lines if query_lower in line.lower()]

        if matches:
            preview = " | ".join(matches[:3])
            if len(preview) > 120:
                preview = preview[:117] + "..."
            return f"shard-{self.shard_id}: {len(matches)} match(es): {preview}"
        return f"shard-{self.shard_id}: no matches for '{query}'"
