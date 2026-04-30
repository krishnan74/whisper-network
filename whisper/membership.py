"""
Layer 1: Gossip Membership (SWIM-lite)

Each node independently tracks which peers are ALIVE / SUSPECTED / DEAD by:
  - Broadcasting a heartbeat to all known peers every 2 seconds
  - Marking peers SUSPECTED after 6 s of silence
  - Marking peers DEAD after 2 independent suspicion reports
  - Gossiping both heartbeats and suspicion notices with hop-limited fanout
"""
import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 2.0   # seconds between heartbeat broadcasts
SUSPECT_AFTER      = 10.0  # seconds of silence before marking SUSPECTED (must be > 2x heartbeat interval + routing latency)
DEAD_REPORTS_NEEDED = 2    # independent suspicion reports to confirm DEAD
GOSSIP_FANOUT      = 3     # peers to forward each gossip message to
GOSSIP_HOPS        = 8     # max hop count for any gossip message
SEEN_CACHE_SIZE    = 1000  # rolling dedup cache for msg_ids


class PeerStatus(str, Enum):
    ALIVE     = "alive"
    SUSPECTED = "suspected"
    DEAD      = "dead"


@dataclass
class PeerInfo:
    peer_key:   str
    status:     PeerStatus     = PeerStatus.ALIVE
    last_seen:  float          = field(default_factory=time.time)
    tasks_held: list           = field(default_factory=list)
    shard_id:   Optional[int]  = None  # home shard, learned from heartbeats


class MembershipLayer:
    def __init__(
        self,
        transport,
        our_key:            str   = "",
        our_shard_id:       int   = 0,
        cluster_size:       int   = 0,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        suspect_after:      float = SUSPECT_AFTER,
        on_peer_dead:       Optional[Callable[[str], None]] = None,
    ):
        self.transport          = transport
        self.our_key            = our_key
        self.our_shard_id       = our_shard_id
        self.cluster_size       = cluster_size
        self.heartbeat_interval = heartbeat_interval
        self.suspect_after      = suspect_after
        self.on_peer_dead       = on_peer_dead

        self._peers:      dict[str, PeerInfo] = {}
        self._suspicions: dict[str, set]     = {}  # suspect -> set of reporters
        self._seen_ids:   deque              = deque(maxlen=SEEN_CACHE_SIZE)
        self._events:     deque              = deque(maxlen=200)
        self._lock        = threading.Lock()

        # Keys currently up in the AXL overlay mesh (updated by topology sync loop)
        self._axl_connected: set[str] = set()

        # Injected by node.py so heartbeats can include current tasks
        self._tasks_held_fn: Callable[[], list[str]] = lambda: []

        self._running = False

    # ── Public interface ──────────────────────────────────────────────────────

    def add_peer(self, peer_key: str):
        if peer_key == self.our_key:
            return
        with self._lock:
            if peer_key not in self._peers:
                self._peers[peer_key] = PeerInfo(peer_key=peer_key)
                self._log(f"discovered peer {peer_key[:8]}")

    def get_alive_peers(self) -> list[str]:
        with self._lock:
            return [k for k, p in self._peers.items() if p.status != PeerStatus.DEAD]

    def get_all_peers(self) -> dict[str, PeerInfo]:
        with self._lock:
            return dict(self._peers)

    def set_tasks_held_fn(self, fn: Callable[[], list[str]]):
        self._tasks_held_fn = fn

    def has_quorum(self) -> bool:
        """
        Returns True if this node can see a strict majority (>50%) of the cluster.
        Always True when cluster_size is 0 (quorum check disabled).
        Counts self + alive peers.
        """
        if self.cluster_size <= 0:
            return True
        with self._lock:
            alive_peers = sum(1 for p in self._peers.values() if p.status == PeerStatus.ALIVE)
        # +1 for ourselves
        return (alive_peers + 1) > self.cluster_size / 2

    def get_peer_for_shard(self, shard_id: int) -> Optional[str]:
        """Return the key of the alive peer that owns shard_id, or None if dead/unknown."""
        with self._lock:
            for key, peer in self._peers.items():
                if peer.shard_id == shard_id and peer.status == PeerStatus.ALIVE:
                    return key
        return None

    def get_axl_connected(self) -> set[str]:
        with self._lock:
            return set(self._axl_connected)

    def axl_sync(self, connected_keys: set[str]):
        """
        Called by the node's topology-sync loop every 5s.
        - Adds newly appeared AXL peers to membership.
        - Fast-tracks peers absent from the AXL mesh AND silent too long to SUSPECTED,
          cutting failure detection from SUSPECT_AFTER down to SUSPECT_AFTER/2.
        """
        now = time.time()
        fast_suspect_after = self.suspect_after / 2
        newly_suspected: list[str] = []

        with self._lock:
            self._axl_connected = set(connected_keys)

            for key in connected_keys:
                if key != self.our_key and key not in self._peers:
                    self._peers[key] = PeerInfo(peer_key=key)
                    self._log(f"discovered peer via AXL topology: {key[:8]}")

            for key, peer in list(self._peers.items()):
                if peer.status == PeerStatus.DEAD:
                    continue
                if (peer.status == PeerStatus.ALIVE
                        and key not in connected_keys
                        and (now - peer.last_seen) > fast_suspect_after):
                    peer.status = PeerStatus.SUSPECTED
                    newly_suspected.append(key)
                    self._log(
                        f"node-{key[:8]} SUSPECTED "
                        f"(dropped from AXL mesh + {now - peer.last_seen:.0f}s silence)"
                    )

        for key in newly_suspected:
            self._gossip_suspicion(key)

    def get_events(self, n: int = 20) -> list[str]:
        return list(self._events)[:n]

    def start(self):
        self._running = True
        threading.Thread(target=self._heartbeat_loop,  daemon=True, name="hb").start()
        threading.Thread(target=self._detect_failures, daemon=True, name="fd").start()

    def stop(self):
        self._running = False

    # ── Message handler (called by node's recv loop) ──────────────────────────

    def handle_message(self, from_peer: str, msg: dict):
        msg_id = msg.get("msg_id")
        if not msg_id or msg_id in self._seen_ids:
            return
        self._seen_ids.append(msg_id)

        mtype = msg.get("type")
        if mtype == "heartbeat":
            self._on_heartbeat(from_peer, msg)
        elif mtype == "suspicion":
            self._on_suspicion(from_peer, msg)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._events.appendleft(f"[{ts}] {msg}")
        logger.info(msg)

    def _heartbeat_loop(self):
        while self._running:
            self._broadcast_heartbeat()
            time.sleep(self.heartbeat_interval)

    def _broadcast_heartbeat(self):
        with self._lock:
            alive = [k for k, p in self._peers.items() if p.status != PeerStatus.DEAD]
            known = [k for k, p in self._peers.items() if p.status == PeerStatus.ALIVE]

        msg = {
            "type":        "heartbeat",
            "msg_id":      str(uuid.uuid4()),
            "from":        self.our_key,
            "timestamp":   time.time(),
            "shard_id":    self.our_shard_id,
            "tasks_held":  self._tasks_held_fn(),
            "known_peers": known,
            "hops":        GOSSIP_HOPS,
        }
        for peer_key in alive:
            self.transport.send(peer_key, msg)

    def _detect_failures(self):
        while self._running:
            time.sleep(1.0)
            now = time.time()
            suspected_now = []
            with self._lock:
                for key, peer in list(self._peers.items()):
                    if peer.status == PeerStatus.DEAD:
                        continue
                    if now - peer.last_seen > self.suspect_after and peer.status == PeerStatus.ALIVE:
                        peer.status = PeerStatus.SUSPECTED
                        suspected_now.append(key)
                        self._log(
                            f"node-{key[:8]} SUSPECTED dead "
                            f"(silent {now - peer.last_seen:.0f}s)"
                        )
            for key in suspected_now:
                self._gossip_suspicion(key)

    def _gossip_suspicion(self, suspect: str):
        msg = {
            "type":      "suspicion",
            "msg_id":    str(uuid.uuid4()),
            "from":      self.our_key,
            "suspect":   suspect,
            "timestamp": time.time(),
            "hops":      GOSSIP_HOPS,
        }
        self._fanout(msg)

    def _fanout(self, msg: dict):
        with self._lock:
            alive = [k for k, p in self._peers.items() if p.status != PeerStatus.DEAD]
            axl   = self._axl_connected
        # Prefer AXL-directly-connected peers (lower latency); randomise within each group
        axl_alive   = [k for k in alive if k in axl]
        other_alive = [k for k in alive if k not in axl]
        random.shuffle(axl_alive)
        random.shuffle(other_alive)
        targets = (axl_alive + other_alive)[:GOSSIP_FANOUT]
        for peer_key in targets:
            self.transport.send(peer_key, msg)

    def _on_heartbeat(self, from_peer: str, msg: dict):
        # Always use the key from the message body — X-From-Peer-Id is a
        # partial Yggdrasil-derived address, not the full ed25519 public key.
        sender = msg.get("from")
        if not sender or sender == self.our_key:
            return  # ignore self-heartbeats looped back via gossip relay

        with self._lock:
            prev_status = None
            if sender not in self._peers:
                self._peers[sender] = PeerInfo(peer_key=sender)
                self._log(f"new peer via heartbeat: {sender[:8]}")
            else:
                prev_status = self._peers[sender].status

            peer = self._peers[sender]
            peer.last_seen  = time.time()
            peer.tasks_held = msg.get("tasks_held", [])
            if msg.get("shard_id") is not None:
                peer.shard_id = int(msg["shard_id"])

            if peer.status == PeerStatus.SUSPECTED:
                peer.status = PeerStatus.ALIVE
                self._log(f"node-{sender[:8]} recovered (was suspected)")
            elif peer.status == PeerStatus.DEAD:
                # Allow revival — at startup nodes may be briefly wrongly confirmed dead
                peer.status = PeerStatus.ALIVE
                self._log(f"node-{sender[:8]} REVIVED (was confirmed dead)")

            # Absorb newly advertised peers
            for p in msg.get("known_peers", []):
                if p not in self._peers and p != self.our_key:
                    self._peers[p] = PeerInfo(peer_key=p)
                    self._log(f"discovered peer via gossip: {p[:8]}")

        # Forward with decremented hop count
        hops = msg.get("hops", 0) - 1
        if hops > 0:
            self._fanout({**msg, "hops": hops})

    def _on_suspicion(self, from_peer: str, msg: dict):
        suspect  = msg.get("suspect")
        reporter = msg.get("from", from_peer)

        if not suspect or suspect == self.our_key:
            return

        confirmed_dead = False
        with self._lock:
            reporters = self._suspicions.setdefault(suspect, set())
            reporters.add(reporter)

            peer = self._peers.get(suspect)
            if peer and peer.status != PeerStatus.DEAD and len(reporters) >= DEAD_REPORTS_NEEDED:
                peer.status  = PeerStatus.DEAD
                confirmed_dead = True
                self._log(
                    f"node-{suspect[:8]} CONFIRMED DEAD "
                    f"({len(reporters)} independent reports)"
                )

        if confirmed_dead and self.on_peer_dead:
            threading.Thread(
                target=self.on_peer_dead, args=(suspect,), daemon=True
            ).start()

        hops = msg.get("hops", 0) - 1
        if hops > 0:
            self._fanout({**msg, "hops": hops})
