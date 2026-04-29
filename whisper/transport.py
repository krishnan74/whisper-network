"""Thin wrapper around the AXL HTTP API."""
import json
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AXLTransport:
    """Wraps AXL's three endpoints: /send, /recv, /topology."""

    def __init__(self, api_base: str = "http://127.0.0.1:9002"):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()

    def send(self, peer_id: str, data: dict) -> bool:
        """Fire-and-forget JSON message to a peer. Returns True on success."""
        try:
            resp = self.session.post(
                f"{self.api_base}/send",
                headers={"X-Destination-Peer-Id": peer_id},
                data=json.dumps(data).encode(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug("send to %s... failed: %s", peer_id[:8], e)
            return False

    def recv(self) -> tuple[Optional[str], Optional[dict]]:
        """
        Poll for one inbound message.
        Returns (from_peer_id, parsed_dict) or (None, None) if queue is empty.
        """
        try:
            resp = self.session.get(f"{self.api_base}/recv", timeout=2)
            if resp.status_code == 204:
                return None, None
            from_peer = resp.headers.get("X-From-Peer-Id")
            msg = json.loads(resp.content)
            return from_peer, msg
        except Exception as e:
            logger.debug("recv error: %s", e)
            return None, None

    def topology(self) -> dict:
        resp = self.session.get(f"{self.api_base}/topology", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def our_public_key(self) -> str:
        return self.topology()["our_public_key"]

    def known_peer_keys(self) -> list[str]:
        """Return public keys of currently connected Yggdrasil peers."""
        topo = self.topology()
        return [
            p["public_key"]
            for p in topo.get("peers", [])
            if p.get("up") and p.get("public_key")
        ]
