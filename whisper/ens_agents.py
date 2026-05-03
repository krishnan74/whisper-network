"""
ENS registration for nodes and agents using pyens.

Registers:
- Node: node<n>.axl.eth
- Agents: ml-agent.node<n>.axl.eth, web3-agent.node<n>.axl.eth, devops-agent.node<n>.axl.eth
"""

import logging
import os
from typing import Optional

from whisper.ens_registry import (
    DEFAULT_SEPOLIA_RPC,
    PK_ENV_KEYS,
    ROOT_NAME,
    send_registry_create_subname,
    wait_receipt,
)

logger = logging.getLogger(__name__)

# Initialize optional dependencies to None
Account = None
Web3 = None

try:
    from eth_account import Account
    from web3 import Web3
except ImportError as e:
    logger.warning(f"Web3 dependencies not available: {e}. ENS registration disabled.")
    Account = None
    Web3 = None


def get_private_key() -> Optional[str]:
    """Load private key from environment variables."""
    for env_key in (PK_ENV_KEYS if registry_tx else ["ENS_PRIVATE_KEY"]):
        pk = os.environ.get(env_key)
        if pk and pk.strip():
            return pk.strip()
    return None


def get_account(private_key: Optional[str] = None):
    """Get eth_account Account from private key."""
    if not Account:
        logger.warning("eth_account not available - ENS registration disabled")
        return None
    if not private_key:
        private_key = get_private_key()
    if not private_key:
        logger.warning("No private key found for ENS registration")
        return None

    try:
        if not private_key.startswith("0x"):
            private_key = f"0x{private_key}"
        return Account.from_key(private_key)
    except Exception as e:
        logger.error(f"Failed to load account from private key: {e}")
        return None


def get_w3(rpc_url: Optional[str] = None):
    """Get Web3 instance connected to Sepolia."""
    if not Web3:
        logger.warning("web3 not available - ENS registration disabled")
        return None
    if not rpc_url:
        rpc_url = DEFAULT_SEPOLIA_RPC if DEFAULT_SEPOLIA_RPC else "https://sepolia.infura.io/v3/"

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            logger.error(f"Failed to connect to RPC: {rpc_url}")
            return None
        return w3
    except Exception as e:
        logger.error(f"Web3 connection failed: {e}")
        return None


def register_node_ens(
    node_id: int,
    owner_address: Optional[str] = None,
    wait_confirmation: bool = False,
    private_key: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> Optional[str]:
    """
    Register node ENS name (e.g., node1.axl.eth).

    Args:
        node_id: Shard ID (1-6)
        owner_address: Address to own the ENS name (defaults to account address)
        wait_confirmation: Whether to wait for tx confirmation
        private_key: Private key (or read from env)
        rpc_url: RPC URL (or use default Sepolia)

    Returns:
        Transaction hash if successful, None otherwise
    """
    if not Account or not Web3:
        logger.warning("Web3 dependencies not available, skipping ENS registration")
        return None

    account = get_account(private_key)
    if not account:
        return None

    w3 = get_w3(rpc_url)
    if not w3:
        return None

    node_label = f"node{node_id}"
    owner = owner_address or account.address

    try:
        logger.info(f"Registering node ENS: {node_label}.{ROOT_NAME}")
        tx_hash = send_registry_create_subname(
            w3=w3,
            account=account,
            fqdn=f"{node_label}.{ROOT_NAME}",
            owner_address=owner,
            resolver=None,
        )
        logger.info(
            f"Node ENS registration submitted: {node_label}.{ROOT_NAME} (tx: {tx_hash})"
        )

        if wait_confirmation:
            try:
                receipt = wait_receipt(w3, tx_hash)
                if getattr(receipt, "status", 1) == 0:
                    logger.error(f"Node ENS registration failed: {tx_hash}")
                    return None
                logger.info(f"Node ENS confirmed: {node_label}.{ROOT_NAME}")
            except Exception as e:
                logger.warning(f"Failed to wait for node ENS confirmation: {e}")

        return tx_hash
    except Exception as e:
        logger.error(f"Failed to register node ENS: {e}")
        return None


def register_agent_ens(
    node_id: int,
    agent_id: str,  # "ml", "web3", "devops"
    owner_address: Optional[str] = None,
    wait_confirmation: bool = False,
    private_key: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> Optional[str]:
    """
    Register agent ENS name (e.g., ml-agent.node1.axl.eth).

    Args:
        node_id: Shard ID (1-6)
        agent_id: Agent type ("ml", "web3", "devops")
        owner_address: Address to own the ENS name
        wait_confirmation: Whether to wait for tx confirmation
        private_key: Private key (or read from env)
        rpc_url: RPC URL (or use default Sepolia)

    Returns:
        Transaction hash if successful, None otherwise
    """
    if not Account or not Web3:
        logger.warning("Web3 dependencies not available, skipping ENS registration")
        return None

    account = get_account(private_key)
    if not account:
        return None

    w3 = get_w3(rpc_url)
    if not w3:
        return None

    agent_label = f"{agent_id}-agent.node{node_id}"
    owner = owner_address or account.address

    try:
        logger.info(f"Registering agent ENS: {agent_label}.{ROOT_NAME}")
        tx_hash = send_registry_create_subname(
            w3=w3,
            account=account,
            fqdn=f"{agent_label}.{ROOT_NAME}",
            owner_address=owner,
            resolver=None,
        )
        logger.info(
            f"Agent ENS registration submitted: {agent_label}.{ROOT_NAME} (tx: {tx_hash})"
        )

        if wait_confirmation:
            try:
                receipt = wait_receipt(w3, tx_hash)
                if getattr(receipt, "status", 1) == 0:
                    logger.error(f"Agent ENS registration failed: {tx_hash}")
                    return None
                logger.info(f"Agent ENS confirmed: {agent_label}.{ROOT_NAME}")
            except Exception as e:
                logger.warning(f"Failed to wait for agent ENS confirmation: {e}")

        return tx_hash
    except Exception as e:
        logger.error(f"Failed to register agent ENS: {e}")
        return None


def register_all_agents_ens(
    node_id: int,
    owner_address: Optional[str] = None,
    wait_confirmation: bool = False,
    private_key: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """
    Register all 3 agents for a node.

    Args:
        node_id: Shard ID (1-6)
        owner_address: Address to own the ENS names
        wait_confirmation: Whether to wait for tx confirmations
        private_key: Private key (or read from env)
        rpc_url: RPC URL (or use default Sepolia)

    Returns:
        Dict mapping agent_id to tx_hash (or None if registration failed)
    """
    results = {}
    for agent_id in ["ml", "web3", "devops"]:
        tx_hash = register_agent_ens(
            node_id=node_id,
            agent_id=agent_id,
            owner_address=owner_address,
            wait_confirmation=wait_confirmation,
            private_key=private_key,
            rpc_url=rpc_url,
        )
        results[agent_id] = tx_hash
    return results


if __name__ == "__main__":
    # Test script
    import argparse

    parser = argparse.ArgumentParser(description="Register node and agent ENS names")
    parser.add_argument("--node-id", type=int, required=True, help="Node ID (1-6)")
    parser.add_argument(
        "--owner", help="Owner address (defaults to account address)"
    )
    parser.add_argument("--wait", action="store_true", help="Wait for confirmations")
    parser.add_argument("--private-key", help="Private key (or use env)")
    parser.add_argument("--rpc", help="RPC URL (or use default Sepolia)")

    args = parser.parse_args()

    # Register node
    print(f"\n=== Registering Node {args.node_id} ===")
    node_tx = register_node_ens(
        node_id=args.node_id,
        owner_address=args.owner,
        wait_confirmation=args.wait,
        private_key=args.private_key,
        rpc_url=args.rpc,
    )
    if node_tx:
        print(f"✓ Node registered: {node_tx}")
    else:
        print("✗ Node registration failed")

    # Register agents
    print(f"\n=== Registering Agents for Node {args.node_id} ===")
    agent_txs = register_all_agents_ens(
        node_id=args.node_id,
        owner_address=args.owner,
        wait_confirmation=args.wait,
        private_key=args.private_key,
        rpc_url=args.rpc,
    )
    for agent_id, tx_hash in agent_txs.items():
        if tx_hash:
            print(f"✓ {agent_id}-agent registered: {tx_hash}")
        else:
            print(f"✗ {agent_id}-agent registration failed")
