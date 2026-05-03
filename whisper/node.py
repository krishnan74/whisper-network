"""
Whisper Network node entry point.

Wires together:
  - AXLTransport  (HTTP bridge to the AXL binary)
  - MembershipLayer (Layer 1 — gossip heartbeats + failure detection)
  - TaskLedger      (Layer 2 — distributed, lease-based task log)
  - AgentRuntime    (Layer 3 — task execution loop)

Also exposes a tiny debug HTTP server (default :8888) with:
  GET  /state   — full JSON snapshot for the dashboard
  POST /submit  — inject a new task into the ledger (used by submit_task.py)
"""
import argparse
import base64
import json
import logging
import os
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from whisper.crypto import Signer, PayloadCipher, ThresholdCipher
from whisper.ens_agents import register_node_ens, register_all_agents_ens
from whisper.ledger import TaskLedger
from whisper.membership import MembershipLayer
from whisper.runtime import AgentRuntime
from whisper.transport import AXLTransport

logger = logging.getLogger(__name__)


# ── Debug HTTP server ─────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    node: "WhisperNode"  # set by WhisperNode.start()

    def do_GET(self):
        if self.path == "/state":
            body = json.dumps(self.node.get_state()).encode()
            self._respond(200, "application/json", body)
        elif self.path == "/health":
            self._respond(200, "text/plain", b"ok")
        elif self.path == "/results":
            # Drain buffered task_result push notifications received via AXL
            body = json.dumps(self.node.drain_results()).encode()
            self._respond(200, "application/json", body)
        else:
            self._respond(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/submit":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data    = json.loads(body)
                task_id = data["task_id"]
                payload = data["payload"]
                shard_id = int(data["shard_id"])
                self.node.ledger.submit_task(task_id, payload, shard_id)
                self._respond(200, "application/json", b'{"ok":true}')
            except Exception as e:
                self._respond(400, "application/json",
                              json.dumps({"error": str(e)}).encode())
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress default access log noise


# ── Main node ─────────────────────────────────────────────────────────────────

class WhisperNode:
    def __init__(
        self,
        api_base:           str   = "http://127.0.0.1:9002",
        shard_id:           int   = 1,
        shard_file:         str   = "demo/shards/shard-1.txt",
        ledger_file:        str   = "ledger.json",
        debug_port:         int   = 8888,
        cluster_size:       int   = 0,
        lease_duration:     float = 30.0,
        renew_threshold:    float = 15.0,
        heartbeat_interval: float = 2.0,
        suspect_after:      float = 10.0,
        key_file:           Optional[str] = None,
        capabilities:       Optional[list] = None,
        price_axl:          float = 0.01,
        exec_delay:         float = 0.0,
    ):
        self.debug_port  = debug_port

        self.transport   = AXLTransport(api_base)
        self.our_key     = self._wait_for_axl()
        self._signer     = Signer(key_file)
        self._cipher     = PayloadCipher(key_file)
        logger.info("our key: %s...", self.our_key[:16])

        self.ens_name: Optional[str]  = None
        self._axl_mesh_stats: dict    = {"total_peers": 0, "up_peers": 0}
        self._recovered_task_count: int = 0
        self._task_results: list      = []   # buffered task_result AXL push notifications
        self._results_lock            = threading.Lock()

        # Auction state: task_id → {"event": Event, "bids": list}
        self._bid_collections: dict   = {}
        self._bids_lock               = threading.Lock()

        # Threshold share collection: task_id → (event, needed, [shares])
        self._share_collections: dict = {}
        self._threshold_cipher        = ThresholdCipher.from_payload_cipher(self._cipher)

        self.membership  = MembershipLayer(
            transport           = self.transport,
            our_key             = self.our_key,
            our_shard_id        = shard_id,
            cluster_size        = cluster_size,
            heartbeat_interval  = heartbeat_interval,
            suspect_after       = suspect_after,
            on_peer_dead        = self._on_peer_dead,
        )
        if self._cipher.enabled:
            self.membership.our_enc_pubkey = self._cipher.x25519_pubkey_hex
        self.membership.our_lease_duration = lease_duration
        self.membership.our_capabilities   = list(capabilities) if capabilities else []
        self.membership.our_price_axl      = price_axl

        self.ledger      = TaskLedger(
            transport        = self.transport,
            our_key          = self.our_key,
            ledger_file      = ledger_file,
            lease_duration   = lease_duration,
            renew_threshold  = renew_threshold,
            signer           = self._signer,
        )
        self.ledger.set_peers_fn(self.membership.get_alive_peers)
        self.ledger.set_axl_connected_fn(self.membership.get_axl_connected)
        self.ledger.set_local_result_fn(self._buffer_result)
        self.ledger.set_enc_pubkey_fn(self._get_enc_pubkey_for_shard)
        self.ledger.set_payload_cipher(self._cipher)
        self.ledger.set_threshold_cipher(self._threshold_cipher)
        self.ledger.set_threshold_fn(self._get_threshold_params)

        self.runtime     = AgentRuntime(
            ledger             = self.ledger,
            our_key            = self.our_key,
            shard_id           = shard_id,
            shard_dir          = os.path.dirname(os.path.abspath(shard_file)),
            membership         = self.membership,
            payload_cipher     = self._cipher,
            collect_shares_fn  = self._collect_threshold_shares,
            capabilities       = list(capabilities) if capabilities else [],
            exec_delay         = exec_delay,
        )

        self.membership.set_tasks_held_fn(self.ledger.get_my_task_ids)

    # ── Startup ───────────────────────────────────────────────────────────────

    def _wait_for_axl(self, timeout: float = 60.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return self.transport.our_public_key()
            except Exception:
                time.sleep(1)
        raise RuntimeError("AXL not reachable after 60s — is the node running?")

    def start(self):
        # Seed membership from AXL topology (Yggdrasil-level direct peers)
        try:
            for key in self.transport.known_peer_keys():
                self.membership.add_peer(key)
            logger.info("seeded %d peers from /topology", len(self.transport.known_peer_keys()))
        except Exception as e:
            logger.warning("could not seed peers from topology: %s", e)

        # Recover any tasks this node owned before a crash.
        # Must run after AXL is ready (so our_key is confirmed) but before the
        # runtime loop starts (so we don't race against our own recovery gossip).
        self._recovered_task_count = self.ledger.recover_identity()
        if self._recovered_task_count:
            logger.info(
                "identity recovery: re-adopted %d task(s) — "
                "peers will see updated leases within one gossip round",
                self._recovered_task_count,
            )

        self.membership.start()
        self.membership.broadcast_join()
        self.runtime.start()
        self._start_debug_server()
        self._start_recv_loop()
        threading.Thread(target=self._axl_sync_loop,        daemon=True, name="axl-sync").start()
        threading.Thread(target=self._lease_convergence_loop, daemon=True, name="lease-conv").start()

        # Register ENS names via pyens (background thread)
        threading.Thread(
            target=self._register_ens_names,
            daemon=True,
            name="ens-register",
        ).start()

        logger.info(
            "whisper node running (shard-%d, debug :%d)",
            self.runtime.shard_id, self.debug_port,
        )

    # ── Background threads ────────────────────────────────────────────────────

    def _register_ens_names(self):
        """Register node and agent ENS names via pyens (optional, non-blocking)."""
        try:
            time.sleep(2)  # Wait for the node to stabilize
            node_id = self.runtime.shard_id
            node_ens_name = self.runtime.node_ens_name

            logger.info(f"Registering ENS names for node {node_id}...")

            # Register node
            node_tx = register_node_ens(node_id=node_id)
            if node_tx:
                logger.info(f"Node ENS registration submitted: {node_ens_name} (tx: {node_tx})")
            else:
                logger.warning(f"Node ENS registration skipped (pyens not available or misconfigured)")

            # Register agents
            agent_txs = register_all_agents_ens(node_id=node_id)
            for agent_id, tx_hash in agent_txs.items():
                if tx_hash:
                    logger.info(f"Agent ENS registration submitted: {agent_id}-agent.{node_ens_name} (tx: {tx_hash})")
                else:
                    logger.debug(f"Agent {agent_id}-agent ENS registration skipped")

        except Exception as e:
            logger.debug(f"ENS registration background task failed (non-blocking): {e}")

    def _start_recv_loop(self):
        def loop():
            while True:
                try:
                    from_peer, msg = self.transport.recv()
                    if msg is None:
                        time.sleep(0.02)
                        continue

                    # X-From-Peer-Id is a partial Yggdrasil address, not the full
                    # ed25519 key. Peer discovery happens via msg["from"] in heartbeats.

                    mtype = msg.get("type")
                    if mtype in ("heartbeat", "suspicion", "node_join"):
                        self.membership.handle_message(from_peer, msg)
                    elif mtype == "ledger_update":
                        self.ledger.handle_ledger_update(from_peer, msg)
                    elif mtype == "task_submit":
                        self._handle_p2p_task_submit(msg)
                    elif mtype == "task_bid_request":
                        self._handle_task_bid_request(msg)
                    elif mtype == "task_bid":
                        self._handle_task_bid(msg)
                    elif mtype == "task_award":
                        self._handle_task_award(msg)
                    elif mtype == "task_result":
                        logger.info(
                            "task_result for %s shard-%s via AXL: %s",
                            msg.get("task_id", "?")[:12],
                            msg.get("shard_id", "?"),
                            (msg.get("result") or "")[:80],
                        )
                        self._buffer_result(msg)
                    elif mtype == "share_request":
                        self._handle_share_request(msg)
                    elif mtype == "share_response":
                        self._handle_share_response(msg)
                    else:
                        logger.debug("unknown message type: %s", mtype)
                except Exception as e:
                    logger.debug("recv loop error: %s", e)
                    time.sleep(0.1)

        threading.Thread(target=loop, daemon=True, name="recv").start()

    def _axl_sync_loop(self):
        """Poll AXL /topology every 5s — keeps peer membership in sync with the overlay mesh."""
        while True:
            try:
                connected = self.transport.axl_connected_keys()
                self.membership.axl_sync(connected)
                self._axl_mesh_stats = self.transport.axl_mesh_stats()
            except Exception as e:
                logger.debug("AXL topology sync error: %s", e)
            time.sleep(5)

    def _lease_convergence_loop(self):
        """
        Every 15s, compute the cluster-consensus lease duration (minimum reported
        by any alive peer) and adopt it if it differs from our current setting by
        more than 10%.  Keeps the whole cluster converged on one value without
        manual reconfiguration.
        """
        while True:
            time.sleep(15)
            try:
                consensus = self.membership.get_consensus_lease_duration()
                current   = self.ledger.lease_duration
                if abs(consensus - current) / max(current, 0.1) > 0.10:
                    logger.info(
                        "lease convergence: %.1fs → %.1fs (cluster consensus)",
                        current, consensus,
                    )
                    self.ledger.lease_duration   = consensus
                    self.ledger.renew_threshold  = consensus * 0.4
                    self.membership.our_lease_duration = consensus
            except Exception as e:
                logger.debug("lease convergence error: %s", e)

    def _handle_p2p_task_submit(self, msg: dict):
        """Accept a task via AXL, enter it into the ledger, then run a price auction."""
        try:
            task_id       = msg["task_id"]
            payload       = msg["payload"]
            shard_id      = int(msg["shard_id"])
            submitter_key = msg.get("from") or None
            self.ledger.submit_task(task_id, payload, shard_id, submitter_key=submitter_key)
            logger.info("P2P task %s (shard-%d) received via AXL from %s",
                        task_id[:12], shard_id, (submitter_key or "?")[:8])
            # Wake scan loop immediately as fallback, then run the price auction.
            # The auction winner bypasses the scan cycle entirely via execute_awarded_task.
            self.runtime.wake()
            threading.Thread(
                target=self._run_auction, args=(task_id, shard_id),
                daemon=True, name=f"auction-{task_id[:8]}",
            ).start()
        except Exception as e:
            logger.warning("malformed task_submit message: %s", e)

    def _run_auction(self, task_id: str, shard_id: int):
        """
        Broadcast a bid request to all alive peers, collect bids for 400ms,
        award the task to the lowest-price bidder. The winner skips the normal
        scan-cycle delay and claims immediately, while the ledger's lease
        mechanism still guards against split-brain races.
        """
        event = threading.Event()
        with self._bids_lock:
            self._bid_collections[task_id] = {"event": event, "bids": []}

        peers = self.membership.get_alive_peers()
        request = {
            "type":     "task_bid_request",
            "msg_id":   str(uuid.uuid4()),
            "from":     self.our_key,
            "task_id":  task_id,
            "shard_id": shard_id,
        }
        for peer in peers:
            self.transport.send(peer, request)

        # Wait up to 400ms for bids; signal early once ≥3 arrive
        event.wait(timeout=0.4)

        with self._bids_lock:
            col = self._bid_collections.pop(task_id, {})
        bids = col.get("bids", [])

        # Include self as a candidate
        bids.append({
            "from":         self.our_key,
            "price_axl":    self.membership.our_price_axl,
            "capabilities": self.membership.our_capabilities,
            "shard_id":     self.runtime.shard_id,
        })

        # Prefer shard home node (best locality), then lowest price
        def _bid_key(b):
            home_bonus = 0.0 if b["shard_id"] == shard_id else 0.005
            return b["price_axl"] + home_bonus

        bids.sort(key=_bid_key)
        winner = bids[0]
        winner_key   = winner["from"]
        winner_price = winner["price_axl"]

        logger.info(
            "auction task %s shard-%d: %d bid(s) → %s @ %.3f AXL",
            task_id[:12], shard_id, len(bids), winner_key[:8], winner_price,
        )

        award_msg = {
            "type":      "task_award",
            "msg_id":    str(uuid.uuid4()),
            "from":      self.our_key,
            "task_id":   task_id,
            "winner":    winner_key,
            "price_axl": winner_price,
            "shard_id":  shard_id,
        }
        if winner_key == self.our_key:
            # Handle self-award directly — AXL may not loopback to self
            self._handle_task_award(award_msg)
        else:
            self.transport.send(winner_key, award_msg)

        all_prices = ", ".join(f"{b['from'][:8]}@{b['price_axl']:.3f}" for b in bids[:4])
        self.ledger._log(
            f"auction shard-{shard_id}: {len(bids)} bid(s) [{all_prices}] "
            f"→ {winner_key[:8]} @ {winner_price:.3f} AXL"
        )

    def _handle_task_bid_request(self, msg: dict):
        """Peer opened an auction — respond with our price and capabilities."""
        task_id   = msg.get("task_id")
        requester = msg.get("from")
        if not task_id or not requester or requester == self.our_key:
            return
        self.transport.send(requester, {
            "type":         "task_bid",
            "msg_id":       str(uuid.uuid4()),
            "from":         self.our_key,
            "task_id":      task_id,
            "shard_id":     self.runtime.shard_id,
            "price_axl":    self.membership.our_price_axl,
            "capabilities": self.membership.our_capabilities,
        })

    def _handle_task_bid(self, msg: dict):
        """Inbound bid from a peer — store it, signal when we have enough."""
        task_id = msg.get("task_id")
        if not task_id:
            return
        with self._bids_lock:
            col = self._bid_collections.get(task_id)
            if col is None:
                return
            col["bids"].append({
                "from":         msg.get("from"),
                "price_axl":    float(msg.get("price_axl", 0.01)),
                "capabilities": msg.get("capabilities", []),
                "shard_id":     msg.get("shard_id"),
            })
            if len(col["bids"]) >= 3:
                col["event"].set()

    def _handle_task_award(self, msg: dict):
        """We won the auction — claim and immediately execute to bypass the scan-cycle delay."""
        task_id    = msg.get("task_id")
        winner_key = msg.get("winner")
        price      = msg.get("price_axl", 0.01)
        if not task_id or winner_key != self.our_key:
            return
        logger.info("won auction for task %s (%.3f AXL) — claiming immediately", task_id[:12], price)
        if self.ledger.claim_task(task_id):
            threading.Thread(
                target=self.runtime.execute_awarded_task,
                args=(task_id,),
                daemon=True,
                name=f"exec-{task_id[:8]}",
            ).start()

    def _start_debug_server(self):
        _Handler.node = self
        server = HTTPServer(("0.0.0.0", self.debug_port), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="debug-http").start()
        logger.info("debug API on :%d", self.debug_port)

    # ── Event handlers ────────────────────────────────────────────────────────

    def shutdown(self):
        """
        Graceful shutdown: release all held leases so survivors can claim
        them immediately (< 1s) rather than waiting for lease expiry (30s).
        """
        logger.info("graceful shutdown initiated — releasing leases...")
        self.runtime.stop()
        self.membership.stop()
        released = self.ledger.release_all_leases()
        # Brief pause so gossip can propagate the lease releases to peers
        if released:
            time.sleep(1.5)
        logger.info("shutdown complete (%d lease(s) released)", released)

    def _get_threshold_params(self):
        """
        Return (t, [enc_pubkeys]) if all cluster nodes' encryption keys are known,
        enabling (ceil(n/2))-of-n threshold encryption. Returns None otherwise.
        """
        if not self._threshold_cipher.enabled:
            return None
        own_pubkey   = self._cipher.x25519_pubkey_hex
        peers_info   = self.membership.get_all_peers()
        peer_pubkeys = [p.enc_pubkey for p in peers_info.values() if p.enc_pubkey]

        all_pubkeys = list(dict.fromkeys([own_pubkey] + peer_pubkeys))  # deduplicate, keep order
        n = len(all_pubkeys)
        if n < 3:
            return None  # need at least 3 nodes for meaningful threshold

        t = (n + 1) // 2  # majority: ceil(n/2)  → 3-of-6, 2-of-3, etc.
        return (t, all_pubkeys)

    def _collect_threshold_shares(self, task) -> list:
        """
        Collect t Shamir shares from alive peers for a THRESHOLD: task.
        Returns list of (x, share_bytes) with at least task.threshold_t entries,
        or fewer if not enough peers respond within the timeout.
        """
        payload = task.payload
        if not payload.startswith(ThresholdCipher.MARKER):
            return []

        own_share = self._threshold_cipher.decrypt_own_share(payload)
        if own_share is None:
            return []

        t = task.threshold_t or 1
        if t <= 1:
            return [own_share]

        # Set up collection slot
        event = threading.Event()
        with self._results_lock:
            self._share_collections[task.task_id] = {
                "event":  event,
                "needed": t,
                "shares": [own_share],
            }

        # Broadcast share_request to all alive peers
        peers = self.membership.get_alive_peers()
        msg = {
            "type":    "share_request",
            "msg_id":  str(uuid.uuid4()),
            "from":    self.our_key,
            "task_id": task.task_id,
        }
        for peer in peers:
            self.transport.send(peer, msg)

        # Wait up to 4s for enough shares
        event.wait(timeout=4.0)

        with self._results_lock:
            col = self._share_collections.pop(task.task_id, {})
        return col.get("shares", [own_share])

    def _handle_share_request(self, msg: dict):
        """Peer wants our Shamir share for a threshold task — decrypt and send it back."""
        task_id   = msg.get("task_id")
        requester = msg.get("from")
        if not task_id or not requester:
            return
        task = next((t for t in self.ledger.get_all_tasks() if t.task_id == task_id), None)
        if not task or not task.payload.startswith(ThresholdCipher.MARKER):
            return
        share = self._threshold_cipher.decrypt_own_share(task.payload)
        if share is None:
            return
        x, share_bytes = share
        try:
            self.transport.send(requester, {
                "type":    "share_response",
                "msg_id":  str(uuid.uuid4()),
                "from":    self.our_key,
                "task_id": task_id,
                "x":       x,
                "share":   base64.b64encode(share_bytes).decode(),
            })
        except Exception as e:
            logger.debug("share_response send failed: %s", e)

    def _handle_share_response(self, msg: dict):
        """Inbound share from a peer — store it and signal if we now have enough."""
        task_id = msg.get("task_id")
        if not task_id:
            return
        try:
            x           = int(msg["x"])
            share_bytes = base64.b64decode(msg["share"])
        except Exception:
            return

        with self._results_lock:
            col = self._share_collections.get(task_id)
            if col is None:
                return
            # Deduplicate by x
            if any(s[0] == x for s in col["shares"]):
                return
            col["shares"].append((x, share_bytes))
            if len(col["shares"]) >= col["needed"]:
                col["event"].set()

    def _get_enc_pubkey_for_shard(self, shard_id: int) -> Optional[str]:
        """Return the X25519 encryption pubkey of the home node for shard_id."""
        if shard_id == self.runtime.shard_id:
            return self._cipher.x25519_pubkey_hex  # self
        peer_key = self.membership.get_peer_for_shard(shard_id)
        if peer_key:
            return self.membership.get_enc_pubkey(peer_key)
        return None

    def _buffer_result(self, result: dict):
        with self._results_lock:
            self._task_results.append(result)

    def drain_results(self) -> list:
        """Return and clear all buffered task_result push notifications."""
        with self._results_lock:
            results = list(self._task_results)
            self._task_results.clear()
        return results

    def _on_peer_dead(self, dead_key: str):
        """Called when a peer is confirmed dead. The runtime will reclaim its tasks."""
        logger.info("peer confirmed dead: %s..., lease scanner will reclaim tasks", dead_key[:8])

    # ── State snapshot (served at GET /state) ─────────────────────────────────

    def get_state(self) -> dict:
        peers = {}
        for key, info in self.membership.get_all_peers().items():
            peers[key[:8]] = {
                "full_key":   key,
                "status":     info.status.value,
                "last_seen":  info.last_seen,
                "tasks_held": info.tasks_held,
                "shard_id":   info.shard_id,
            }

        now   = time.time()
        tasks = {}
        for task in self.ledger.get_all_tasks():
            tasks[task.task_id] = {
                "task_id":          task.task_id,
                "shard_id":         task.shard_id,
                "status":           task.status,
                "leased_by":        (task.leased_by or "")[:8] or None,
                "lease_expires_in": max(0.0, task.lease_expires - now)
                                    if task.status == "in_progress" else 0.0,
                "result":           task.result,
                "version":          task.version,
                "encrypted":        task.encrypted,
                "threshold_t":      task.threshold_t,
                "created_at":       task.created_at,
                "claimed_at":       task.claimed_at,
                "completed_at":     task.completed_at,
                "commitment":       task.commitment,
                "result_hash":      task.result_hash,
            }

        m_events = self.membership.get_events(15)
        l_events = self.ledger.get_events(15)
        events   = sorted(set(m_events + l_events), reverse=True)[:20]

        return {
            "our_key":         self.our_key,
            "key_short":       self.our_key[:8],
            "shard_id":        self.runtime.shard_id,
            "ens_name":        self.ens_name,
            "axl_mesh":        self._axl_mesh_stats,
            "recovered_tasks": self._recovered_task_count,
            "metrics":         self.ledger.get_metrics(),
            "peers":           peers,
            "tasks":           tasks,
            "events":          events,
            "capabilities":    self.membership.our_capabilities,
            "price_axl":       self.membership.our_price_axl,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Whisper Network Node")
    parser.add_argument("--api-base",    default="http://127.0.0.1:9002",
                        help="AXL HTTP API base URL")
    parser.add_argument("--shard-id",   type=int, required=True,
                        help="Document shard this node is responsible for")
    parser.add_argument("--shard-file", required=True,
                        help="Path to this node's document shard text file")
    parser.add_argument("--ledger-file",         default="ledger.json")
    parser.add_argument("--debug-port",          type=int,   default=8888)
    parser.add_argument("--cluster-size",        type=int,   default=6,
                        help="Total expected nodes for quorum check (0=disable)")
    parser.add_argument("--lease-duration",      type=float, default=30.0,
                        help="Lease validity in seconds (default 30; try 5 for fast demo)")
    parser.add_argument("--renew-threshold",     type=float, default=15.0,
                        help="Renew lease when less than N seconds remain")
    parser.add_argument("--heartbeat-interval",  type=float, default=2.0,
                        help="Heartbeat broadcast interval in seconds")
    parser.add_argument("--suspect-after",       type=float, default=10.0,
                        help="Silence threshold before marking peer SUSPECTED")
    parser.add_argument("--key-file",            default=None,
                        help="Path to ed25519 PEM private key for ledger_update signing")
    parser.add_argument("--capabilities",        default="",
                        help="Comma-separated agent capabilities e.g. search,summarize,reason")
    parser.add_argument("--price-axl",           type=float, default=0.01,
                        help="AXL price per completed job advertised to the market")
    parser.add_argument("--exec-delay",          type=float, default=0.0,
                        help="Seconds to sleep before completing a task (0=off; use 10-20 to demo kill/rescue)")
    parser.add_argument("--log-level",           default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level   = getattr(logging, args.log_level.upper()),
        format  = "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
    )

    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    node = WhisperNode(
        api_base            = args.api_base,
        shard_id            = args.shard_id,
        shard_file          = args.shard_file,
        ledger_file         = args.ledger_file,
        debug_port          = args.debug_port,
        cluster_size        = args.cluster_size,
        lease_duration      = args.lease_duration,
        renew_threshold     = args.renew_threshold,
        heartbeat_interval  = args.heartbeat_interval,
        suspect_after       = args.suspect_after,
        key_file            = args.key_file,
        capabilities        = caps,
        price_axl           = args.price_axl,
        exec_delay          = args.exec_delay,
    )
    node.start()

    _stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s — starting graceful shutdown", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _stop.wait()
    node.shutdown()


if __name__ == "__main__":
    main()
