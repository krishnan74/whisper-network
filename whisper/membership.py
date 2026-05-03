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
    peer_key:    str
    status:      PeerStatus     = PeerStatus.ALIVE
    last_seen:   float          = field(default_factory=time.time)
    tasks_held:  list           = field(default_factory=list)
    shard_id:    Optional[int]  = None  # home shard, learned from heartbeats
    enc_pubkey:          Optional[str]   = None   # X25519 pubkey for payload encryption
    reported_lease_duration: Optional[float] = None  # lease duration this peer advertises
    capabilities: list          = field(default_factory=list)  # e.g. ["search","summarize"]
    price_axl:   float          = 0.01  # AXL per job this peer charges


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
        self._prev_axl_connected: set[str] = set()  # previous snapshot for drop detection

        # Injected by node.py so heartbeats can include current tasks
        self._tasks_held_fn: Callable[[], list[str]] = lambda: []

        # Our own X25519 pubkey to advertise in heartbeats (set by node.py)
        self.our_enc_pubkey: Optional[str] = None
        # Our current lease duration, advertised in heartbeats for convergence
        self.our_lease_duration: float = 30.0
        # Capabilities and price advertised in every heartbeat
        self.our_capabilities: list[str] = []
        self.our_price_axl:    float     = 0.01

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

    def get_effective_cluster_size(self) -> int:
        """
        Return the cluster size to use for quorum calculations.

        If cluster_size was set explicitly (> 0), use it.
        Otherwise, infer from known peers: alive + dead + self.
        This lets new nodes join and be counted without any manual config.
        """
        if self.cluster_size > 0:
            return self.cluster_size
        with self._lock:
            return len(self._peers) + 1  # peers + self

    def has_quorum(self) -> bool:
        """
        Returns True if this node can see a strict majority (>50%) of the cluster.

        Confirmed-DEAD nodes are subtracted from the effective cluster size — they
        have been independently verified gone by 2+ reports and will not produce
        conflicting state.  This lets 3 survivors act after killing 3 nodes in a
        6-node cluster (effective size drops to 3; 3 > 1.5 → quorum).  A symmetric
        network partition where both sides confirm the other dead still risks
        split-brain, but gossip version reconciliation makes that safe in practice.
        """
        with self._lock:
            alive = sum(1 for p in self._peers.values() if p.status == PeerStatus.ALIVE)
            dead  = sum(1 for p in self._peers.values() if p.status == PeerStatus.DEAD)
        cluster = self.get_effective_cluster_size()
        if cluster <= 1:
            return True  # single-node cluster always has quorum
        effective_size = cluster - dead
        return (alive + 1) > effective_size / 2

    def get_peer_for_shard(self, shard_id: int) -> Optional[str]:
        """Return the key of the alive peer that owns shard_id, or None if dead/unknown."""
        with self._lock:
            for key, peer in self._peers.items():
                if peer.shard_id == shard_id and peer.status == PeerStatus.ALIVE:
                    return key
        return None

    def get_enc_pubkey(self, peer_key: str) -> Optional[str]:
        """Return the X25519 encryption pubkey advertised by peer_key, or None."""
        with self._lock:
            peer = self._peers.get(peer_key)
            return peer.enc_pubkey if peer else None

    def get_consensus_lease_duration(self) -> float:
        """
        Return the minimum lease duration reported by any alive peer (including self).

        Using the minimum ensures the whole cluster converges on the most conservative
        value — preventing any node from holding leases longer than its peers expect,
        which would block recovery after failures.
        """
        with self._lock:
            durations = [
                p.reported_lease_duration
                for p in self._peers.values()
                if p.status != PeerStatus.DEAD and p.reported_lease_duration is not None
            ]
        durations.append(self.our_lease_duration)
        return min(durations)

    def get_axl_connected(self) -> set[str]:
        with self._lock:
            return set(self._axl_connected)

    def axl_sync(self, connected_keys: set[str]):
        """
        Called by the node's topology-sync loop every 5s.
        - Adds newly appeared AXL peers to membership and sends them a direct node_join
          so they discover us immediately (AXL-native discovery — no bootstrap seed needed).
        - Fast-tracks peers absent from the AXL mesh AND silent too long to SUSPECTED,
          cutting failure detection from SUSPECT_AFTER down to SUSPECT_AFTER/2.
        """
        now = time.time()
        fast_suspect_after = self.suspect_after / 2
        newly_suspected: list[str] = []
        newly_discovered: list[str] = []

        with self._lock:
            prev_connected       = self._prev_axl_connected
            self._prev_axl_connected = set(connected_keys)
            self._axl_connected  = set(connected_keys)

            for key in connected_keys:
                if key != self.our_key and key not in self._peers:
                    self._peers[key] = PeerInfo(peer_key=key)
                    newly_discovered.append(key)
                    self._log(f"discovered peer via AXL topology: {key[:8]}")

            # Fast-suspect ONLY peers that were previously direct AXL peers and
            # have now dropped out.  Do NOT fast-suspect routing-only peers (nodes
            # that were never in connected_keys) — in a star topology those peers
            # are reachable via the hub and their absence from connected_keys is
            # expected, not a sign of failure.
            dropped = prev_connected - set(connected_keys)
            for key in dropped:
                peer = self._peers.get(key)
                if not peer or peer.status != PeerStatus.ALIVE:
                    continue
                if (now - peer.last_seen) > fast_suspect_after:
                    peer.status = PeerStatus.SUSPECTED
                    newly_suspected.append(key)
                    self._log(
                        f"node-{key[:8]} SUSPECTED "
                        f"(dropped from AXL mesh + {now - peer.last_seen:.0f}s silence)"
                    )

        # Send node_join directly to newly-seen AXL peers so they discover us without
        # waiting for the next heartbeat cycle — makes peer discovery purely AXL-driven.
        for key in newly_discovered:
            self._send_join_to(key)

        for key in newly_suspected:
            self._gossip_suspicion(key)

    def get_events(self, n: int = 20) -> list[str]:
        return list(self._events)[:n]

    def broadcast_join(self):
        """
        Announce this node's arrival to all AXL-connected peers immediately.
        Called once on startup so existing nodes add us to membership without
        waiting for the next heartbeat + topology-sync cycle.
        """
        with self._lock:
            targets = list(self._axl_connected)
        if not targets:
            return
        for peer_key in targets:
            self._send_join_to(peer_key, hops=GOSSIP_HOPS)
        logger.info("broadcast node_join to %d AXL peer(s)", len(targets))

    def _send_join_to(self, peer_key: str, hops: int = 1):
        """Send a node_join directly to one peer, carrying enc_pubkey and capabilities."""
        msg: dict = {
            "type":         "node_join",
            "msg_id":       str(uuid.uuid4()),
            "from":         self.our_key,
            "shard_id":     self.our_shard_id,
            "hops":         hops,
            "capabilities": self.our_capabilities,
            "price_axl":    self.our_price_axl,
        }
        if self.our_enc_pubkey:
            msg["enc_pubkey"] = self.our_enc_pubkey
        self.transport.send(peer_key, msg)

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
        elif mtype == "node_join":
            self._on_node_join(from_peer, msg)

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
            "type":           "heartbeat",
            "msg_id":         str(uuid.uuid4()),
            "from":           self.our_key,
            "timestamp":      time.time(),
            "shard_id":       self.our_shard_id,
            "tasks_held":     self._tasks_held_fn(),
            "known_peers":    known,
            "hops":           GOSSIP_HOPS,
            "lease_duration": self.our_lease_duration,
            "capabilities":   self.our_capabilities,
            "price_axl":      self.our_price_axl,
        }
        if self.our_enc_pubkey:
            msg["enc_pubkey"] = self.our_enc_pubkey
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

    def _on_node_join(self, from_peer: str, msg: dict):
        sender = msg.get("from")
        if not sender or sender == self.our_key:
            return
        with self._lock:
            if sender not in self._peers:
                self._peers[sender] = PeerInfo(peer_key=sender)
                self._log(f"node {sender[:8]} joined cluster dynamically")
            peer = self._peers[sender]
            peer.last_seen = time.time()
            if peer.status != PeerStatus.ALIVE:
                prev = peer.status.value
                peer.status = PeerStatus.ALIVE
                self._suspicions.pop(sender, None)
                self._log(f"node {sender[:8]} rejoined (was {prev})")
            if msg.get("shard_id") is not None:
                peer.shard_id = int(msg["shard_id"])
            if msg.get("enc_pubkey"):
                peer.enc_pubkey = msg["enc_pubkey"]
            if msg.get("capabilities") is not None:
                peer.capabilities = list(msg["capabilities"])
            if msg.get("price_axl") is not None:
                peer.price_axl = float(msg["price_axl"])
        hops = msg.get("hops", 0) - 1
        if hops > 0:
            self._fanout({**msg, "hops": hops})

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
            if msg.get("enc_pubkey"):
                peer.enc_pubkey = msg["enc_pubkey"]
            if msg.get("lease_duration"):
                peer.reported_lease_duration = float(msg["lease_duration"])
            if msg.get("capabilities") is not None:
                peer.capabilities = list(msg["capabilities"])
            if msg.get("price_axl") is not None:
                peer.price_axl = float(msg["price_axl"])

            if peer.status == PeerStatus.SUSPECTED:
                peer.status = PeerStatus.ALIVE
                self._suspicions.pop(sender, None)   # reset so stale reports can't re-kill
                self._log(f"node-{sender[:8]} recovered (was suspected)")
            elif peer.status == PeerStatus.DEAD:
                peer.status = PeerStatus.ALIVE
                self._suspicions.pop(sender, None)   # reset so stale reports can't re-kill
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
            peer = self._peers.get(suspect)
            # Discard suspicion if the peer has already sent a heartbeat more
            # recently than this suspicion was generated — it's a stale report
            # from before the node recovered.
            sus_ts = msg.get("timestamp", 0)
            if peer and peer.last_seen > sus_ts:
                return

            reporters = self._suspicions.setdefault(suspect, set())
            reporters.add(reporter)

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
