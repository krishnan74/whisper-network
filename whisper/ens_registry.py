"""
Consolidated ENS registry module (merged from pyens).
Provides ENS name registration via setSubnodeRecord transactions.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# ── Constants ──────────────────────────────────────────────────────────────

CHAIN_ID = 11155111
ROOT_NAME = "axl.eth"

ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"
ENS_PUBLIC_RESOLVER_SEPOLIA = "0xE99638b40E4Fff0129D56f03b55b6bbC4BBE49b5"

DEFAULT_SEPOLIA_RPC = os.environ.get(
    "PYENS_SEPOLIA_RPC_URL",
    "https://ethereum-sepolia-rpc.publicnode.com",
)

PK_ENV_KEYS = ("PYENS_PRIVATE_KEY", "ENS_TEST_PRIVATE_KEY", "NEXT_PUBLIC_ENS_TEST_PRIVATE_KEY")

# ── ENS Hash Functions ─────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Normalize ENS name."""
    n = name.strip().lower().replace(" ", "")
    try:
        import ens_normalize
        return ens_normalize.ens_normalize(n)
    except Exception:
        return n


def namehash_bytes(name: str) -> bytes:
    """EIP-137 namehash."""
    from eth_utils import keccak

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
    """Return namehash as hex string."""
    return "0x" + namehash_bytes(name).hex()


def labelhash_bytes(label: str) -> bytes:
    """Single label hash; normalize like viem labelhash."""
    from eth_utils import keccak

    normalized = _norm_name(label)
    if "." in normalized:
        raise ValueError("labelhash expects a single label, not a fqdn")
    return keccak(text=normalized)


def labelhash_hex(label: str) -> str:
    """Return labelhash as hex string."""
    return "0x" + labelhash_bytes(label).hex()


# ── Registry Transaction Functions ────────────────────────────────────────

def _get_registry_abi():
    """ENS registry contract ABI."""
    return [
        {
            "inputs": [{"name": "node", "type": "bytes32"}],
            "name": "owner",
            "outputs": [{"name": "", "type": "address"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"name": "node", "type": "bytes32"}],
            "name": "resolver",
            "outputs": [{"name": "", "type": "address"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"name": "node", "type": "bytes32"},
                {"name": "label", "type": "bytes32"},
                {"name": "owner", "type": "address"},
                {"name": "resolver", "type": "address"},
                {"name": "ttl", "type": "uint64"},
            ],
            "name": "setSubnodeRecord",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
    ]


def registry_contract(w3):
    """Get ENS registry contract instance."""
    return w3.eth.contract(
        address=w3.to_checksum_address(ENS_REGISTRY),
        abi=_get_registry_abi(),
    )


def read_root_owner_resolver(w3, root_name: str) -> tuple[str, str]:
    """Read root owner and resolver from registry."""
    from eth_utils import to_checksum_address

    reg = registry_contract(w3)
    node = namehash_bytes(root_name)
    owner = reg.functions.owner(node).call()
    resolver = reg.functions.resolver(node).call()
    return to_checksum_address(owner), to_checksum_address(resolver)


def read_owner(w3, fqdn: str) -> str:
    """Read owner of ENS name from registry."""
    from eth_utils import to_checksum_address

    reg = registry_contract(w3)
    node = namehash_bytes(fqdn)
    owner = reg.functions.owner(node).call()
    return to_checksum_address(owner)


def encode_set_subnode_record_calldata(
    fqdn: str,
    owner: str,
    resolver: Optional[str] = None,
) -> bytes:
    """Encode setSubnodeRecord calldata."""
    from eth_abi import encode as abi_encode
    from eth_utils import keccak
    from web3 import Web3

    parts = fqdn.strip().lower().split(".")
    if len(parts) < 2:
        raise ValueError("Need label.parent…, e.g. goat.axl.eth")

    label = parts[0]
    parent = ".".join(parts[1:])
    parent_node = namehash_bytes(parent)
    label_h = labelhash_bytes(label)
    resolver_cs = Web3.to_checksum_address(
        resolver if resolver else ENS_PUBLIC_RESOLVER_SEPOLIA
    )
    owner_cs = Web3.to_checksum_address(owner)

    fn_sig = "setSubnodeRecord(bytes32,bytes32,address,address,uint64)"
    selector = keccak(text=fn_sig)[:4]

    body = abi_encode(
        ["bytes32", "bytes32", "address", "address", "uint64"],
        [parent_node, label_h, owner_cs, resolver_cs, 0],
    )
    return selector + body


def send_registry_create_subname(
    w3,
    account,
    fqdn: str,
    owner_address: str,
    resolver: Optional[str] = None,
    gas_buffer: float = 1.2,
) -> str:
    """Send setSubnodeRecord transaction; return 0x-prefixed tx hash."""
    from web3 import Web3

    data = encode_set_subnode_record_calldata(fqdn, owner_address, resolver)
    reg_addr = Web3.to_checksum_address(ENS_REGISTRY)
    signer = account.address

    tx: dict[str, Any] = {
        "from": signer,
        "to": reg_addr,
        "data": data,
        "chainId": CHAIN_ID,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(signer),
    }

    gas = w3.eth.estimate_gas(tx)
    tx["gas"] = int(gas * gas_buffer)

    base_fee = w3.eth.get_block("latest").get("baseFeePerGas")
    if base_fee is not None:
        try:
            priority = int(w3.eth.max_priority_fee)
        except Exception:
            priority = Web3.to_wei(1, "gwei")
        max_fee = int(base_fee * 2 + priority)
        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = priority
    else:
        tx["gasPrice"] = int(w3.eth.gas_price)

    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    if raw is None:
        raise RuntimeError("SignedTransactionSerialized missing raw_transaction")
    tx_hash = w3.eth.send_raw_transaction(raw)
    return Web3.to_hex(tx_hash)


def wait_receipt(w3, tx_hash_hex: str, poll_latency: float = 2.0):
    """Wait for transaction receipt."""
    return w3.eth.wait_for_transaction_receipt(tx_hash_hex, poll_latency=poll_latency)
