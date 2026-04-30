"""
Layer 2: Distributed Task Ledger

Each node holds a full copy of the task ledger, replicated via gossip.
Lease-based ownership prevents duplicate execution; expired leases are
reclaimed by any surviving node that scans the ledger.

Conflict resolution: higher `version` wins. Completed tasks are sticky
(cannot be downgraded). Two nodes racing to claim the same expired lease
will both gossip their claim; within 1-2 rounds the higher lease_expires
timestamp propagates and the loser backs off on its next scan.
"""
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Tuning ────────────────────────────────────────────────────────────────────
LEASE_DURATION  = 30.0   # seconds a claimed lease is valid (short for demo: fast recovery)
RENEW_THRESHOLD = 15.0   # renew when less than this many seconds remain
GOSSIP_FANOUT   = 3
GOSSIP_HOPS     = 8
SEEN_CACHE_SIZE = 1000


@dataclass
class Task:
    task_id:      str
    payload:      str
    shard_id:     int
    status:       str            # "pending" | "in_progress" | "completed"
    leased_by:    Optional[str]
    lease_expires: float
    result:       Optional[str]
    created_at:   float
    version:      int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class TaskLedger:
    def __init__(
        self,
        transport,
        our_key: str,
        ledger_file: str = "ledger.json",
    ):
        self.transport   = transport
        self.our_key     = our_key
        self.ledger_file = ledger_file

        self._tasks:    dict[str, Task] = {}
        self._seen_ids: deque           = deque(maxlen=SEEN_CACHE_SIZE)
        self._events:   deque           = deque(maxlen=200)
        self._lock      = threading.Lock()

        # Injected by node so we can gossip to alive peers
        self._peers_fn: Callable[[], list[str]] = lambda: []
        # Injected by node: returns keys currently up in the AXL overlay mesh
        self._axl_connected_fn: Callable[[], set[str]] = lambda: set()

        self._load()

    # ── Public interface ──────────────────────────────────────────────────────

    def set_peers_fn(self, fn: Callable[[], list[str]]):
        self._peers_fn = fn

    def set_axl_connected_fn(self, fn: Callable[[], set[str]]):
        self._axl_connected_fn = fn

    def recover_identity(self) -> int:
        """
        Called once at startup after AXL is ready.

        Scans the on-disk ledger for tasks we owned before a crash
        (leased_by == our_key, status == in_progress). Refreshes their
        lease expiry so they stay ours instead of being reclaimed by peers
        who saw the lease expire while we were down.

        Returns the number of tasks recovered.
        """
        now      = time.time()
        recovered: list[Task] = []

        with self._lock:
            for task in self._tasks.values():
                if task.leased_by == self.our_key and task.status == "in_progress":
                    task.lease_expires = now + LEASE_DURATION
                    task.version      += 1
                    recovered.append(task)
            if recovered:
                self._persist()

        for task in recovered:
            self._gossip_task(task)
            self._log(
                f"recovered task {task.task_id[:12]} shard-{task.shard_id} "
                f"(node restarted with same AXL identity)"
            )

        if recovered:
            logger.info(
                "identity recovery: re-adopted %d in-progress task(s) from previous run",
                len(recovered),
            )
        return len(recovered)

    def submit_task(self, task_id: str, payload: str, shard_id: int) -> Task:
        task = Task(
            task_id      = task_id,
            payload      = payload,
            shard_id     = shard_id,
            status       = "pending",
            leased_by    = None,
            lease_expires = 0.0,
            result       = None,
            created_at   = time.time(),
            version      = 1,
        )
        with self._lock:
            self._tasks[task_id] = task
            self._persist()
        self._gossip_task(task)
        self._log(f"submitted task {task_id[:12]} shard-{shard_id}")
        return task

    def claim_task(self, task_id: str) -> bool:
        """
        Attempt to claim a task. Returns True if we successfully wrote a lease.
        The caller should verify the claim survived gossip reconciliation before
        executing (the runtime does this by checking leased_by == our_key).
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status == "completed":
                return False
            now = time.time()
            if task.status == "in_progress" and task.lease_expires > now:
                return False  # valid lease held by someone else
            task.status       = "in_progress"
            task.leased_by    = self.our_key
            task.lease_expires = now + LEASE_DURATION
            task.version     += 1
            self._persist()

        self._gossip_task(task)
        self._log(f"claimed task {task_id[:12]}")
        return True

    def renew_lease(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if (not task
                    or task.leased_by != self.our_key
                    or task.status != "in_progress"):
                return False
            task.lease_expires = time.time() + LEASE_DURATION
            task.version      += 1
            self._persist()

        self._gossip_task(task)
        return True

    def complete_task(self, task_id: str, result: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.leased_by != self.our_key:
                return False
            task.status  = "completed"
            task.result  = result
            task.version += 1
            self._persist()

        self._gossip_task(task)
        self._log(f"completed task {task_id[:12]}: {result[:60]}")
        return True

    def get_claimable_tasks(self) -> list[Task]:
        """Tasks that are pending or whose lease has expired (and aren't ours)."""
        now = time.time()
        with self._lock:
            out = []
            for t in self._tasks.values():
                if t.status == "pending":
                    out.append(t)
                elif (t.status == "in_progress"
                      and t.lease_expires < now
                      and t.leased_by != self.our_key):
                    out.append(t)
            return out

    def get_my_active_tasks(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.leased_by == self.our_key and t.status == "in_progress"]

    def get_tasks_needing_renewal(self) -> list[Task]:
        now = time.time()
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.status == "in_progress"
                    and t.leased_by == self.our_key
                    and (t.lease_expires - now) < RENEW_THRESHOLD]

    def get_my_task_ids(self) -> list[str]:
        with self._lock:
            return [t.task_id for t in self._tasks.values()
                    if t.leased_by == self.our_key and t.status == "in_progress"]

    def get_all_tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def get_events(self, n: int = 20) -> list[str]:
        return list(self._events)[:n]

    # ── Inbound gossip handler ────────────────────────────────────────────────

    def handle_ledger_update(self, from_peer: str, msg: dict):
        msg_id = msg.get("msg_id")
        if not msg_id or msg_id in self._seen_ids:
            return
        self._seen_ids.append(msg_id)

        task_dict = msg.get("task")
        if not task_dict:
            return

        incoming = Task.from_dict(task_dict)
        changed  = False

        with self._lock:
            existing = self._tasks.get(incoming.task_id)

            if existing is None:
                self._tasks[incoming.task_id] = incoming
                changed = True
                self._log(f"learned task {incoming.task_id[:12]} via gossip (shard-{incoming.shard_id})")

            elif existing.status == "completed":
                pass  # completed tasks are immutable

            elif incoming.version > existing.version:
                self._tasks[incoming.task_id] = incoming
                changed = True
                if incoming.status == "completed":
                    self._log(
                        f"task {incoming.task_id[:12]} completed by "
                        f"{(incoming.leased_by or '?')[:8]} via gossip"
                    )
                elif (incoming.status == "in_progress"
                      and existing.status in ("pending", "in_progress")
                      and incoming.leased_by != self.our_key):
                    self._log(
                        f"task {incoming.task_id[:12]} claimed by "
                        f"{(incoming.leased_by or '?')[:8]} via gossip"
                    )

            elif (incoming.status == "completed"
                  and existing.status != "completed"):
                # Completed always wins regardless of version
                self._tasks[incoming.task_id] = incoming
                changed = True

            if changed:
                self._persist()

        # Re-gossip only if we updated local state
        if changed:
            hops = msg.get("hops", 0) - 1
            if hops > 0:
                self._fanout_raw({**msg, "hops": hops})

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._events.appendleft(f"[{ts}] {msg}")
        logger.info(msg)

    def _gossip_task(self, task: Task):
        msg = {
            "type":   "ledger_update",
            "msg_id": str(uuid.uuid4()),
            "from":   self.our_key,
            "hops":   GOSSIP_HOPS,
            "task":   task.to_dict(),
        }
        self._fanout_raw(msg)

    def _fanout_raw(self, msg: dict):
        peers = self._peers_fn()
        axl   = self._axl_connected_fn()
        # Prefer AXL-directly-connected peers; randomise within each group
        axl_peers   = [k for k in peers if k in axl]
        other_peers = [k for k in peers if k not in axl]
        random.shuffle(axl_peers)
        random.shuffle(other_peers)
        targets = (axl_peers + other_peers)[:GOSSIP_FANOUT]
        for peer_key in targets:
            self.transport.send(peer_key, msg)

    def _persist(self):
        try:
            data = {tid: t.to_dict() for tid, t in self._tasks.items()}
            with open(self.ledger_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("ledger persist failed: %s", e)

    def _load(self):
        if not os.path.exists(self.ledger_file):
            return
        try:
            with open(self.ledger_file) as f:
                data = json.load(f)
            for task_id, d in data.items():
                self._tasks[task_id] = Task.from_dict(d)
            logger.info("loaded %d tasks from %s", len(self._tasks), self.ledger_file)
        except Exception as e:
            logger.warning("failed to load ledger: %s", e)
