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

SCAN_INTERVAL = 5.0   # seconds between ledger scans (also max latency for non-auction tasks)


class AgentRuntime:
    def __init__(
        self,
        ledger,
        our_key:    str,
        shard_id:   int,
        shard_dir:  str,
        membership      = None,
        num_shards:  int = 6,
        payload_cipher   = None,
        collect_shares_fn = None,
        capabilities: list = None,
        exec_delay: float = 0.0,
    ):
        self.ledger            = ledger
        self.our_key           = our_key
        self.shard_id          = shard_id   # this node's home shard
        self.shard_dir         = shard_dir
        self.membership        = membership  # used for shard-affinity routing
        self.num_shards        = num_shards
        self.payload_cipher    = payload_cipher
        self.collect_shares_fn = collect_shares_fn
        self.capabilities      = set(capabilities) if capabilities else set()
        self.exec_delay        = exec_delay  # artificial delay before completing a task (demo kill window)

        # Load ALL shards — any surviving node can execute any task
        self._shards: dict[int, list[str]] = {}
        self._load_all_shards()

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Track in-flight task_ids to prevent double-execution across
        # the auction path and the scan-loop fallback path.
        self._executing: set = set()
        self._exec_lock = threading.Lock()

        # Event to wake the scan loop early (e.g. when a new task arrives).
        self._wake_event = threading.Event()

    def _load_all_shards(self):
        for i in range(1, self.num_shards + 1):
            path = os.path.join(self.shard_dir, f"shard-{i}.txt")
            if os.path.exists(path):
                with open(path) as f:
                    lines = [l.rstrip() for l in f if l.strip()]
                self._shards[i] = lines
                logger.info("loaded shard-%d: %d lines", i, len(lines))
            else:
                self._shards[i] = []
                logger.warning("shard-%d not found at %s", i, path)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="runtime"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake_event.set()  # unblock the wait so the thread exits cleanly

    def wake(self):
        """Signal the scan loop to run immediately instead of waiting SCAN_INTERVAL."""
        self._wake_event.set()

    def _loop(self):
        while self._running:
            try:
                self._scan()
            except Exception as e:
                logger.error("runtime scan error: %s", e, exc_info=True)
            self._wake_event.wait(timeout=SCAN_INTERVAL)
            self._wake_event.clear()

    def _replica_shard_for(self) -> int:
        """Return the shard ID this node is the designated replica for (circular: n→n-1)."""
        return (self.shard_id - 2) % self.num_shards + 1

    def _capable_peer_alive(self, required_caps: set) -> bool:
        """Return True if any alive peer (other than us) advertises all required capabilities."""
        if not required_caps or self.membership is None:
            return False
        for peer in self.membership.get_all_peers().values():
            from whisper.membership import PeerStatus
            if peer.status == PeerStatus.ALIVE and required_caps.issubset(set(peer.capabilities or [])):
                return True
        return False

    def _scan(self):
        # 1. Renew any leases that are about to expire
        for task in self.ledger.get_tasks_needing_renewal():
            self.ledger.renew_lease(task.task_id)

        # 2. Find claimable tasks with shard-affinity ordering:
        #    - Priority A: tasks for our own shard (home node)
        #    - Priority B: tasks for our replica shard (home dead — we're designated backup)
        #    - Priority C: any other orphaned task with quorum (general survivor rescue)
        #    Skip tasks whose home node is alive — let it claim its own work.
        claimable  = self.ledger.get_claimable_tasks()
        replica_id = self._replica_shard_for()

        mine = [t for t in claimable if t.shard_id == self.shard_id]

        # Only claim non-home tasks if we have a majority of the cluster visible —
        # prevents both sides of a partition doing duplicate work.
        has_quorum = self.membership is None or self.membership.has_quorum()

        def _home_dead(t):
            return (self.membership is None
                    or self.membership.get_peer_for_shard(t.shard_id) is None)

        def _no_capable_peer(t):
            """True when no alive peer advertises our capabilities for this task's shard."""
            if not self.capabilities:
                return True  # no capability filtering — claim anything
            return not self._capable_peer_alive(self.capabilities)

        replica_orphaned = [
            t for t in claimable
            if t.shard_id == replica_id and t.shard_id != self.shard_id
            and has_quorum and _home_dead(t)
        ]
        general_orphaned = [
            t for t in claimable
            if t.shard_id != self.shard_id and t.shard_id != replica_id
            and has_quorum and _home_dead(t) and _no_capable_peer(t)
        ]

        if not has_quorum and any(t.shard_id != self.shard_id for t in claimable):
            logger.info("no quorum — skipping %d orphaned task(s) to avoid split-brain",
                        sum(1 for t in claimable if t.shard_id != self.shard_id))

        # Dispatch each claimable task in its own thread so threshold share
        # collection (up to 4s) doesn't block the whole scan cycle.
        for task in mine + replica_orphaned + general_orphaned:
            with self._exec_lock:
                if task.task_id in self._executing:
                    continue  # already running via auction path

            if not self.ledger.claim_task(task.task_id):
                continue

            with self._exec_lock:
                self._executing.add(task.task_id)

            threading.Thread(
                target=self._run_exec_thread,
                args=(task.task_id,),
                daemon=True,
                name=f"exec-{task.task_id[:8]}",
            ).start()

    def _run_exec_thread(self, task_id: str):
        """Thread body: brief gossip-settle pause, verify lease, then execute."""
        try:
            # Brief pause to let gossip propagate our claim before executing
            time.sleep(0.3)
            active = [t for t in self.ledger.get_my_active_tasks()
                      if t.task_id == task_id]
            if not active:
                logger.info("lost lease race for %s, skipping", task_id[:12])
                return
            self._execute_one(active[0])
        finally:
            with self._exec_lock:
                self._executing.discard(task_id)

    def execute_awarded_task(self, task_id: str):
        """
        Called by node.py immediately after winning an auction.
        Runs in the caller's thread (already a daemon thread spawned by node.py).
        Skips if the scan loop already picked up the same task.
        """
        with self._exec_lock:
            if task_id in self._executing:
                return
            self._executing.add(task_id)
        try:
            time.sleep(0.3)  # let gossip propagate our claim
            active = [t for t in self.ledger.get_my_active_tasks()
                      if t.task_id == task_id]
            if not active:
                logger.info("auction claim for %s lost in gossip, skipping", task_id[:12])
                return
            self._execute_one(active[0])
        finally:
            with self._exec_lock:
                self._executing.discard(task_id)

    def _execute_one(self, task):
        """Execute a single task whose lease we already hold."""
        from whisper.crypto import ThresholdCipher as _TC
        origin  = "home" if task.shard_id == self.shard_id else "survivor"
        payload = task.payload

        # ── Threshold decryption (t-of-n Shamir) ──────────────────────────
        if task.threshold_t > 0 and payload.startswith(_TC.MARKER):
            if not self.collect_shares_fn:
                result = f"shard-{task.shard_id}: [threshold encrypted — no share collection fn]"
                self.ledger.complete_task(task.task_id, result)
                return
            shares = self.collect_shares_fn(task)
            if len(shares) < task.threshold_t:
                logger.warning(
                    "task %s: only %d/%d shares collected — releasing lease to retry",
                    task.task_id[:12], len(shares), task.threshold_t,
                )
                self.ledger.release_lease(task.task_id)
                return
            try:
                payload = _TC.reconstruct_and_decrypt(payload, shares)
                logger.info(
                    "task %s: threshold decrypted (%d shares) [%s]",
                    task.task_id[:12], len(shares), origin,
                )
            except Exception as e:
                result = f"shard-{task.shard_id}: [threshold decryption failed: {e}]"
                self.ledger.complete_task(task.task_id, result)
                return

        # ── Per-shard ECDH decryption ──────────────────────────────────────
        elif task.encrypted and payload.startswith("ENC:"):
            if self.payload_cipher and self.payload_cipher.enabled:
                try:
                    payload = self.payload_cipher.decrypt(payload)
                    logger.info("task %s: payload decrypted [%s]", task.task_id[:12], origin)
                except Exception:
                    result = f"shard-{task.shard_id}: [encrypted payload — home node offline]"
                    self.ledger.complete_task(task.task_id, result)
                    return
            else:
                result = f"shard-{task.shard_id}: [encrypted payload — no key configured]"
                self.ledger.complete_task(task.task_id, result)
                return

        logger.info(
            "executing task %s (shard-%d) [%s]",
            task.task_id[:12], task.shard_id, origin,
        )
        if self.exec_delay > 0:
            logger.info("task %s: simulating %gs work — kill this node now to trigger rescue",
                        task.task_id[:12], self.exec_delay)
            time.sleep(self.exec_delay)
        result = self.execute(payload, task.shard_id)
        self.ledger.complete_task(task.task_id, result)

    def execute(self, payload: str, shard_id: int) -> str:
        """
        Search the specified document shard for lines matching the query.
        Any node can execute tasks for any shard — survivors pick up dead nodes' work.
        """
        query = payload.strip()
        if query.lower().startswith("query:"):
            query = query[6:].strip()
        query_lower = query.lower()

        lines   = self._shards.get(shard_id, [])
        matches = [line for line in lines if query_lower in line.lower()]

        if matches:
            preview = " | ".join(matches[:3])
            if len(preview) > 120:
                preview = preview[:117] + "..."
            return f"shard-{shard_id}: {len(matches)} match(es): {preview}"
        return f"shard-{shard_id}: no matches for '{query}'"
