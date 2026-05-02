"""
ENS subname self-registration for Whisper Network nodes.

Each node attempts to claim:
  node{shard_id}.notdocker.eth            (preferred)
  node{shard_id}-{short_key}.notdocker.eth  (fallback if taken)

Text records set at registration time:
  axl.peer_id    — full AXL public key
  capabilities   — comma-separated capability list
  price_axl      — bid price
  shard_id       — numeric shard assignment

Set JUSTANAME_API_KEY in the environment to enable; omit to skip silently.
"""
import logging
import os
import threading
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_API_BASE   = "https://api.justaname.id"
_ENS_DOMAIN = "notdocker.eth"
_CHAIN_ID   = 11155111  # Sepolia


def _check_available(subname: str, api_key: str) -> bool:
    try:
        resp = requests.get(
            f"{_API_BASE}/ens/v1/subname/available",
            params={"subname": subname, "chainId": _CHAIN_ID},
            headers={"x-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("data", {}).get("isAvailable", False)
    except Exception as exc:
        logger.warning("ENS availability check failed for %s: %s", subname, exc)
        return False


def _register(username: str, text_records: list, api_key: str) -> bool:
    try:
        resp = requests.post(
            f"{_API_BASE}/ens/v1/subname/add",
            json={
                "username":               username,
                "ensDomain":              _ENS_DOMAIN,
                "chainId":                _CHAIN_ID,
                "overrideSignatureCheck": True,
                # API requires at least one address (coinType 60 = ETH).
                # Nodes have no wallet; zero address satisfies the constraint.
                "addresses": [{"coinType": 60, "address": "0x0000000000000000000000000000000000000000"}],
                "text":      text_records,
            },
            headers={"x-api-key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("ENS registration failed for %s.%s: %s", username, _ENS_DOMAIN, exc)
        return False


def _run_registration(
    shard_id:     int,
    peer_id:      str,
    capabilities: list,
    price_axl:    float,
    api_key:      str,
    callback:     Optional[Callable],
):
    short   = peer_id[:8]
    records = [
        {"key": "axl.peer_id",  "value": peer_id},
        {"key": "capabilities", "value": ",".join(capabilities)},
        {"key": "price_axl",    "value": str(price_axl)},
        {"key": "shard_id",     "value": str(shard_id)},
    ]

    for username in [f"node{shard_id}", f"node{shard_id}-{short}"]:
        full_name = f"{username}.{_ENS_DOMAIN}"
        if _check_available(full_name, api_key) and _register(username, records, api_key):
            logger.info("ENS registered: %s", full_name)
            if callback:
                callback(full_name)
            return

    logger.warning("ENS: all candidate names taken or registration failed for shard %d", shard_id)
    if callback:
        callback(None)


def start_registration(
    shard_id:     int,
    peer_id:      str,
    capabilities: list,
    price_axl:    float,
    callback:     Optional[Callable] = None,
) -> None:
    """Launch ENS registration in a daemon thread. No-ops if JUSTANAME_API_KEY is unset."""
    api_key = os.environ.get("JUSTANAME_API_KEY", "").strip()
    if not api_key:
        logger.info("ENS registration skipped: JUSTANAME_API_KEY not set")
        if callback:
            callback(None)
        return

    threading.Thread(
        target=_run_registration,
        args=(shard_id, peer_id, capabilities, price_axl, api_key, callback),
        daemon=True,
        name="ens-register",
    ).start()
