"""Ed25519 signing and verification for Whisper Network ledger_update messages."""
import base64
import json
import logging
from typing import Optional

from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, load_pem_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)


def _canonical(msg: dict) -> bytes:
    """Deterministic bytes to sign — covers the fields that matter for a lease claim."""
    task = msg.get("task") or {}
    fields = {
        "msg_id":    msg.get("msg_id", ""),
        "task_id":   task.get("task_id", ""),
        "status":    task.get("status", ""),
        "leased_by": task.get("leased_by") or "",
        "version":   task.get("version", 0),
    }
    return json.dumps(fields, sort_keys=True).encode()


class Signer:
    """
    Loads a node's ed25519 private key (same PEM used by AXL) and provides
    sign() / verify() for ledger_update gossip messages.

    Signing is opt-in: if no key_path is given, sign() is a no-op and
    verify() accepts all messages — old nodes without signing still interoperate.
    """

    def __init__(self, key_path: Optional[str] = None):
        self._private_key: Optional[Ed25519PrivateKey] = None
        self.public_key_hex: Optional[str] = None

        if not key_path:
            return
        try:
            with open(key_path, "rb") as f:
                pem = f.read()
            priv = load_pem_private_key(pem, password=None)
            pub  = priv.public_key()
            raw  = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
            self._private_key  = priv
            self.public_key_hex = raw.hex()
            logger.info("crypto: loaded signing key %s...", self.public_key_hex[:16])
        except Exception as e:
            logger.warning("crypto: could not load key from %s: %s — signing disabled", key_path, e)

    @property
    def enabled(self) -> bool:
        return self._private_key is not None

    def sign(self, msg: dict) -> dict:
        """Return msg with 'signature' and 'signing_key' fields added."""
        if not self.enabled:
            return msg
        data = _canonical(msg)
        sig  = self._private_key.sign(data)
        return {
            **msg,
            "signature":   base64.b64encode(sig).decode(),
            "signing_key": self.public_key_hex,
        }

    def verify(self, msg: dict) -> bool:
        """
        Verify msg signature. Returns True if valid or unsigned.
        Unsigned messages are accepted for backwards compatibility.
        """
        sig_b64  = msg.get("signature")
        key_hex  = msg.get("signing_key")

        if not sig_b64 or not key_hex:
            return True  # unsigned — accepted

        try:
            pub  = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
            sig  = base64.b64decode(sig_b64)
            data = _canonical(msg)
            pub.verify(sig, data)
            return True
        except InvalidSignature:
            logger.warning("crypto: INVALID signature from signing_key %s...", key_hex[:16])
            return False
        except Exception as e:
            logger.warning("crypto: verification error: %s", e)
            return False
