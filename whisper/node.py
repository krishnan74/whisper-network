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
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

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
        api_base:    str   = "http://127.0.0.1:9002",
        shard_id:    int   = 1,
        shard_file:  str   = "demo/shards/shard-1.txt",
        ledger_file: str   = "ledger.json",
        debug_port:  int   = 8888,
    ):
        self.debug_port  = debug_port

        self.transport   = AXLTransport(api_base)
        self.our_key     = self._wait_for_axl()
        logger.info("our key: %s...", self.our_key[:16])

        self.membership  = MembershipLayer(
            transport    = self.transport,
            our_key      = self.our_key,
            on_peer_dead = self._on_peer_dead,
        )

        self.ledger      = TaskLedger(
            transport    = self.transport,
            our_key      = self.our_key,
            ledger_file  = ledger_file,
        )
        self.ledger.set_peers_fn(self.membership.get_alive_peers)

        self.runtime     = AgentRuntime(
            ledger    = self.ledger,
            our_key   = self.our_key,
            shard_id  = shard_id,
            shard_dir = os.path.dirname(os.path.abspath(shard_file)),
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

        self.membership.start()
        self.runtime.start()
        self._start_debug_server()
        self._start_recv_loop()
        logger.info(
            "whisper node running (shard-%d, debug :%d)",
            self.runtime.shard_id, self.debug_port,
        )

    # ── Background threads ────────────────────────────────────────────────────

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
                    if mtype in ("heartbeat", "suspicion"):
                        self.membership.handle_message(from_peer, msg)
                    elif mtype == "ledger_update":
                        self.ledger.handle_ledger_update(from_peer, msg)
                    else:
                        logger.debug("unknown message type: %s", mtype)
                except Exception as e:
                    logger.debug("recv loop error: %s", e)
                    time.sleep(0.1)

        threading.Thread(target=loop, daemon=True, name="recv").start()

    def _start_debug_server(self):
        _Handler.node = self
        server = HTTPServer(("0.0.0.0", self.debug_port), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="debug-http").start()
        logger.info("debug API on :%d", self.debug_port)

    # ── Event handlers ────────────────────────────────────────────────────────

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
            }

        now   = time.time()
        tasks = {}
        for task in self.ledger.get_all_tasks():
            tasks[task.task_id] = {
                "task_id":         task.task_id,
                "shard_id":        task.shard_id,
                "status":          task.status,
                "leased_by":       (task.leased_by or "")[:8] or None,
                "lease_expires_in": max(0.0, task.lease_expires - now)
                                    if task.status == "in_progress" else 0.0,
                "result":          task.result,
                "version":         task.version,
            }

        m_events = self.membership.get_events(15)
        l_events = self.ledger.get_events(15)
        events   = sorted(set(m_events + l_events), reverse=True)[:20]

        return {
            "our_key":   self.our_key,
            "key_short": self.our_key[:8],
            "shard_id":  self.runtime.shard_id,
            "peers":     peers,
            "tasks":     tasks,
            "events":    events,
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
    parser.add_argument("--ledger-file", default="ledger.json")
    parser.add_argument("--debug-port", type=int, default=8888,
                        help="Port for the debug / dashboard HTTP API")
    parser.add_argument("--log-level",  default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level   = getattr(logging, args.log_level.upper()),
        format  = "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
    )

    node = WhisperNode(
        api_base    = args.api_base,
        shard_id    = args.shard_id,
        shard_file  = args.shard_file,
        ledger_file = args.ledger_file,
        debug_port  = args.debug_port,
    )
    node.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main()
