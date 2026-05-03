"""getNamesForAddress-style query (simplified from @ensdomains/ensjs getNamesForAddress)."""

from __future__ import annotations

import time
from typing import Any

import requests

from .constants import (
    ADDR_REVERSE_NAMEHASH,
    EMPTY_ADDRESS,
    ENS_SEPOLIA_SUBGRAPH_URL,
    ROOT_NAME,
)

_NAMES_QUERY = """
query getNamesForAddress(
  $orderBy: Domain_orderBy
  $orderDirection: OrderDirection
  $first: Int
  $whereFilter: Domain_filter
) {
  domains(
    orderBy: $orderBy
    orderDirection: $orderDirection
    first: $first
    where: $whereFilter
  ) {
    name
    id
  }
}
"""


def _where_for_address(address: str) -> dict[str, Any]:
    addr = address.lower()
    owner_or = {
        "or": [
            {"owner": addr},
            {"registrant": addr},
            {"wrappedOwner": addr},
            {"resolvedAddress": addr},
        ]
    }
    not_expired = {
        "or": [
            {"expiryDate_gt": str(int(time.time()))},
            {"expiryDate": None},
        ]
    }
    not_deleted = {
        "or": [
            {"owner_not": EMPTY_ADDRESS},
            {"resolver_not": None},
            {
                "and": [
                    {"registrant_not": EMPTY_ADDRESS},
                    {"registrant_not": None},
                ]
            },
        ]
    }
    return {
        "and": [
            owner_or,
            {"parent_not": ADDR_REVERSE_NAMEHASH},
            not_expired,
            not_deleted,
        ]
    }


def fetch_names_for_address(
    address: str,
    subgraph_url: str = ENS_SEPOLIA_SUBGRAPH_URL,
    page_size: int = 500,
) -> list[dict[str, str]]:
    """Return raw domain rows `{name, id}` from the indexer."""
    variables = {
        "orderBy": "name",
        "orderDirection": "asc",
        "first": page_size,
        "whereFilter": _where_for_address(address),
    }
    r = requests.post(
        subgraph_url,
        json={"query": _NAMES_QUERY, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(body["errors"])
    domains = body.get("data", {}).get("domains") or []
    return domains


def names_under_axl(domains: list[dict[str, str]], root_name: str = ROOT_NAME) -> list[str]:
    suffix = f".{root_name}"
    seen: set[str] = set()
    out: list[str] = []
    for row in domains:
        n = (row.get("name") or "").strip().lower()
        if not n or n == root_name:
            continue
        if not n.endswith(suffix):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    out.sort()
    return out
