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
    except requests.HTTPError as exc:
        body = exc.response.text[:200] if exc.response is not None else ""
        logger.warning("ENS registration failed for %s.%s: %s %s", username, _ENS_DOMAIN, exc, body)
        return False
    except Exception as exc:
        logger.warning("ENS registration failed for %s.%s: %s", username, _ENS_DOMAIN, exc)
        return False


def _list_subnames(api_key: str) -> list:
    """Return all subnames under _ENS_DOMAIN as a list of SubnameResponse dicts.

    Raw API envelope: {"result": {"data": {"data": [...], "pagination": {...}}}}
    The SDK unwraps result.data; we unwrap one level further to get the array.
    """
    try:
        resp = requests.get(
            f"{_API_BASE}/ens/v1/subname/ens",
            params={"ensDomain": _ENS_DOMAIN, "chainId": _CHAIN_ID},
            headers={"x-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("data", {}).get("data", [])
    except Exception as exc:
        logger.warning("ENS list subnames failed: %s", exc)
        return []


def _lookup_by_peer_id(peer_id: str, api_key: str) -> Optional[str]:
    """Return the full ENS name that has axl.peer_id == peer_id, or None."""
    for sub in _list_subnames(api_key):
        for record in sub.get("records", {}).get("texts", []):
            if record.get("key") == "axl.peer_id" and record.get("value", "").strip() == peer_id:
                return sub.get("ens", "")
    return None


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

    # Try preferred names — attempt registration regardless of availability.
    # With overrideSignatureCheck=True the domain owner's API key acts as an
    # upsert: if the name is already claimed it updates its text records.
    for username in [f"node{shard_id}", f"node{shard_id}-{short}"]:
        full_name = f"{username}.{_ENS_DOMAIN}"
        if _register(username, records, api_key):
            logger.info("ENS registered/updated: %s", full_name)
            if callback:
                callback(full_name)
            return

    # Both registrations rejected — scan for an existing record matching our
    # peer_id (handles edge cases where the API rejects duplicate upserts).
    existing = _lookup_by_peer_id(peer_id, api_key)
    if existing:
        logger.info("ENS: reusing existing name %s for shard %d", existing, shard_id)
    else:
        logger.warning("ENS: could not register or find a name for shard %d", shard_id)
    if callback:
        callback(existing)


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


def discover_peers(ens_domain: str = _ENS_DOMAIN) -> list[str]:
    """Return AXL peer_ids of all registered subnames under ens_domain."""
    api_key = os.environ.get("JUSTANAME_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        resp = requests.get(
            f"{_API_BASE}/ens/v1/subname/ens",
            params={"ensDomain": ens_domain, "chainId": _CHAIN_ID},
            headers={"x-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        subnames = resp.json().get("result", {}).get("data", {}).get("data", [])
        peers = []
        for sub in subnames:
            for record in sub.get("records", {}).get("texts", []):
                if record.get("key") == "axl.peer_id":
                    val = record.get("value", "").strip()
                    if val:
                        peers.append(val)
        return peers
    except Exception as exc:
        logger.debug("ENS peer discovery failed: %s", exc)
        return []
