"""ENS namehash / labelhash aligned with viem + ens-normalize (ASCII-safe for axl.eth labels)."""

from __future__ import annotations

import ens_normalize
from eth_utils import keccak

HEX_PREFIX = "0x"


def _norm_name(name: str) -> str:
    n = name.strip().lower().replace(" ", "")
    try:
        return ens_normalize.ens_normalize(n)
    except Exception:
        return n


def namehash_bytes(name: str) -> bytes:
    """EIP-137 namehash."""
    node = b"\x00" * 32
    if not name:
        return node
    normalized = _norm_name(name)
    if not normalized:
        return node
    for label in reversed(normalized.split(".")):
        node = keccak(node + keccak(text=label))
    return node


def namehash_hex(name: str) -> str:
    return HEX_PREFIX + namehash_bytes(name).hex()


def labelhash_bytes(label: str) -> bytes:
    """Single label; normalize like viem labelhash."""
    normalized = _norm_name(label)
    if "." in normalized:
        raise ValueError("labelhash expects a single label, not a fqdn")
    return keccak(text=normalized)


def labelhash_hex(label: str) -> str:
    return HEX_PREFIX + labelhash_bytes(label).hex()
