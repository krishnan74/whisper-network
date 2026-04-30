"""Ed25519 signing/verification and X25519 ECDH + AES-GCM payload encryption."""
import base64
import json
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption, load_pem_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
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


class PayloadCipher:
    """
    X25519 ECDH + AES-GCM payload encryption keyed from the node's ed25519 identity.

    Encryption is ephemeral: a fresh X25519 keypair is generated per message so
    the encrypted blob is self-contained and the sender's identity is not leaked.

    Wire format:  base64( eph_pub[32] || nonce[12] || ciphertext )
    """

    MARKER = "ENC:"  # prefix in task.payload that signals encryption

    def __init__(self, key_path: Optional[str] = None):
        self._x25519_priv: Optional[X25519PrivateKey] = None
        self.x25519_pubkey_hex: Optional[str]         = None

        if not key_path:
            return
        try:
            with open(key_path, "rb") as f:
                pem = f.read()
            ed_priv  = load_pem_private_key(pem, password=None)
            ed_raw   = ed_priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            x25519_seed = HKDF(SHA256(), 32, None, b"whisper-x25519").derive(ed_raw)
            self._x25519_priv   = X25519PrivateKey.from_private_bytes(x25519_seed)
            pub_raw             = self._x25519_priv.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
            self.x25519_pubkey_hex = pub_raw.hex()
            logger.info("crypto: X25519 encryption key %s...", self.x25519_pubkey_hex[:16])
        except Exception as e:
            logger.warning("crypto: PayloadCipher init failed: %s", e)

    @property
    def enabled(self) -> bool:
        return self._x25519_priv is not None

    def encrypt(self, target_pubkey_hex: str, plaintext: str) -> str:
        """Encrypt plaintext for target. Returns ENC:<base64(eph_pub||nonce||ct)>."""
        target_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(target_pubkey_hex))
        eph_priv   = X25519PrivateKey.generate()
        eph_pub    = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        shared     = eph_priv.exchange(target_pub)
        key        = HKDF(SHA256(), 32, None, b"whisper-payload").derive(shared)
        nonce      = os.urandom(12)
        ct         = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        return self.MARKER + base64.b64encode(eph_pub + nonce + ct).decode()

    def decrypt(self, payload: str) -> str:
        """Decrypt a payload produced by encrypt(). Raises if not encrypted or invalid."""
        if not payload.startswith(self.MARKER):
            raise ValueError("payload is not encrypted")
        raw      = base64.b64decode(payload[len(self.MARKER):])
        eph_pub  = X25519PublicKey.from_public_bytes(raw[:32])
        nonce, ct = raw[32:44], raw[44:]
        shared   = self._x25519_priv.exchange(eph_pub)
        key      = HKDF(SHA256(), 32, None, b"whisper-payload").derive(shared)
        return AESGCM(key).decrypt(nonce, ct, None).decode()
