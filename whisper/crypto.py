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


# ── GF(256) Shamir Secret Sharing ─────────────────────────────────────────────
# Finite-field arithmetic over GF(2^8) with AES reduction polynomial (0x11B).

def _gf_mul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8)."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B   # x^8 + x^4 + x^3 + x + 1
        b >>= 1
    return p


def _gf_inv(a: int) -> int:
    """Multiplicative inverse in GF(2^8): a^254 by Fermat's little theorem."""
    if a == 0:
        raise ValueError("GF(256) inverse of 0 is undefined")
    result, base, exp = 1, a, 254
    while exp:
        if exp & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        exp >>= 1
    return result


def shamir_split(secret: bytes, n: int, t: int) -> list[tuple[int, bytes]]:
    """
    Split `secret` into `n` shares where any `t` shares reconstruct it.
    Returns [(x, share_bytes), ...] with x ∈ {1..n}.
    """
    import secrets as _sec
    length   = len(secret)
    # For each secret byte, evaluate a random degree-(t-1) polynomial at x=1..n
    columns  = []  # columns[byte_idx] = [share_val_for_x1, ..., share_val_for_xN]
    for byte_val in secret:
        coeffs = [byte_val] + [_sec.randbelow(256) for _ in range(t - 1)]
        col = []
        for x in range(1, n + 1):
            # Horner's method in GF(256)
            val = 0
            for c in reversed(coeffs):
                val = _gf_mul(val, x) ^ c
            col.append(val)
        columns.append(col)
    # Transpose: share i = bytes(columns[b][i] for b in range(length))
    return [(i + 1, bytes(columns[b][i] for b in range(length))) for i in range(n)]


def shamir_reconstruct(shares: list[tuple[int, bytes]]) -> bytes:
    """
    Reconstruct secret from any t shares via Lagrange interpolation in GF(256).
    `shares` is [(x, share_bytes), ...].
    """
    length = len(shares[0][1])
    result = bytearray(length)
    for b_idx in range(length):
        s = 0
        for i, (xi, yi_bytes) in enumerate(shares):
            yi = yi_bytes[b_idx]
            # Lagrange basis at 0: L_i(0) = prod_{j≠i} xj / (xi XOR xj)
            Li = 1
            for j, (xj, _) in enumerate(shares):
                if i != j:
                    Li = _gf_mul(Li, _gf_mul(xj, _gf_inv(xi ^ xj)))
            s ^= _gf_mul(yi, Li)
        result[b_idx] = s
    return bytes(result)


class ThresholdCipher:
    """
    (t, n) threshold encryption keyed from the node's AXL X25519 identity.

    Payload is encrypted with a random AES-GCM key K.
    K is split into n Shamir shares (GF(256), t-of-n).
    Each share is individually encrypted to a different node's X25519 pubkey.

    Any t nodes can:
      1. Decrypt their own share.
      2. Exchange plain shares.
      3. Reconstruct K via Lagrange interpolation.
      4. Decrypt the payload.

    Ciphertext wire format embedded in Task.payload:
        "THRESHOLD:" + json.dumps({
            "t": int, "n": int,
            "ciphertext": base64(nonce||ct),
            "shares": [{"x": int, "node_pubkey": hex, "eph_pub": b64, "enc_share": b64}, ...]
        })
    """

    MARKER = "THRESHOLD:"

    def __init__(self, x25519_priv: Optional[X25519PrivateKey] = None,
                 x25519_pubkey_hex: Optional[str] = None):
        self._x25519_priv      = x25519_priv
        self.x25519_pubkey_hex = x25519_pubkey_hex

    @classmethod
    def from_payload_cipher(cls, cipher: "PayloadCipher") -> "ThresholdCipher":
        return cls(cipher._x25519_priv, cipher.x25519_pubkey_hex)

    @property
    def enabled(self) -> bool:
        return self._x25519_priv is not None

    def encrypt(self, target_pubkeys: list[str], plaintext: str, t: int) -> str:
        """
        Encrypt plaintext with (t, n) threshold where n = len(target_pubkeys).
        Returns THRESHOLD:<json blob>.
        """
        n       = len(target_pubkeys)
        aes_key = os.urandom(32)
        nonce   = os.urandom(12)
        ct      = AESGCM(aes_key).encrypt(nonce, plaintext.encode(), None)
        ct_b64  = base64.b64encode(nonce + ct).decode()

        raw_shares = shamir_split(aes_key, n, t)  # [(x, share_bytes), ...]

        enc_shares = []
        for (x, share_bytes), pubkey_hex in zip(raw_shares, target_pubkeys):
            target_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
            eph_priv   = X25519PrivateKey.generate()
            eph_pub    = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            shared     = eph_priv.exchange(target_pub)
            enc_key    = HKDF(SHA256(), 32, None, b"whisper-tshare").derive(shared)
            snonce     = os.urandom(12)
            enc_share  = AESGCM(enc_key).encrypt(snonce, share_bytes, None)
            enc_shares.append({
                "x":           x,
                "node_pubkey": pubkey_hex,
                "eph_pub":     base64.b64encode(eph_pub).decode(),
                "enc_share":   base64.b64encode(snonce + enc_share).decode(),
            })

        blob = {"t": t, "n": n, "ciphertext": ct_b64, "shares": enc_shares}
        return self.MARKER + json.dumps(blob)

    def decrypt_own_share(self, payload: str) -> Optional[tuple[int, bytes]]:
        """
        Find and decrypt this node's share in a THRESHOLD: payload.
        Returns (x, share_bytes) or None if our key is not in the share list.
        """
        if not self._x25519_priv or not self.x25519_pubkey_hex:
            return None
        try:
            blob = json.loads(payload[len(self.MARKER):])
        except Exception:
            return None

        for entry in blob.get("shares", []):
            if entry["node_pubkey"] != self.x25519_pubkey_hex:
                continue
            try:
                eph_pub    = X25519PublicKey.from_public_bytes(base64.b64decode(entry["eph_pub"]))
                shared     = self._x25519_priv.exchange(eph_pub)
                enc_key    = HKDF(SHA256(), 32, None, b"whisper-tshare").derive(shared)
                raw        = base64.b64decode(entry["enc_share"])
                share_bytes = AESGCM(enc_key).decrypt(raw[:12], raw[12:], None)
                return (entry["x"], share_bytes)
            except Exception as e:
                logger.warning("threshold: decrypt_own_share failed: %s", e)
                return None
        return None

    @staticmethod
    def reconstruct_and_decrypt(payload: str, shares: list[tuple[int, bytes]]) -> str:
        """Given t plain shares and a THRESHOLD: payload, reconstruct and decrypt."""
        blob    = json.loads(payload[len(ThresholdCipher.MARKER):])
        aes_key = shamir_reconstruct(shares)
        raw     = base64.b64decode(blob["ciphertext"])
        return AESGCM(aes_key).decrypt(raw[:12], raw[12:], None).decode()
