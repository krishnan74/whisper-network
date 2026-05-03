"""Registry setSubnodeRecord — same as ensjs createSubname(contract='registry')."""

from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from web3.contract import Contract

from .constants import CHAIN_ID, ENS_PUBLIC_RESOLVER_SEPOLIA, ENS_REGISTRY
from .ens_hash import labelhash_bytes, namehash_bytes

_FN = "setSubnodeRecord(bytes32,bytes32,address,address,uint64)"
_SELECTOR = keccak(text=_FN)[:4]

_REGISTRY_ABI: list[dict[str, Any]] = [
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


def registry_contract(w3: Web3) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(ENS_REGISTRY), abi=_REGISTRY_ABI)


def read_root_owner_resolver(w3: Web3, root_name: str) -> tuple[str, str]:
    reg = registry_contract(w3)
    node = namehash_bytes(root_name)
    owner = reg.functions.owner(node).call()
    resolver = reg.functions.resolver(node).call()
    return to_checksum_address(owner), to_checksum_address(resolver)


def read_owner(w3: Web3, fqdn: str) -> str:
    reg = registry_contract(w3)
    node = namehash_bytes(fqdn)
    owner = reg.functions.owner(node).call()
    return to_checksum_address(owner)


def encode_set_subnode_record_calldata(
    fqdn: str,
    owner: str,
    resolver: str | None = None,
) -> bytes:
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
    body = abi_encode(
        ["bytes32", "bytes32", "address", "address", "uint64"],
        [parent_node, label_h, owner_cs, resolver_cs, 0],
    )
    return _SELECTOR + body


def send_registry_create_subname(
    w3: Web3,
    account: Account,
    fqdn: str,
    owner_address: str,
    resolver: str | None = None,
    gas_buffer: float = 1.2,
) -> str:
    """Send setSubnodeRecord from `account`; return 0x-prefixed tx hash."""
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


def wait_receipt(w3: Web3, tx_hash_hex: str, poll_latency: float = 2.0):
    return w3.eth.wait_for_transaction_receipt(tx_hash_hex, poll_latency=poll_latency)
