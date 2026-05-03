"""Persist like the browser localStorage key `ens-test:sepolia-onchain-state:axl:env-pk`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import ROOT_NAME, STATE_FILE


def default_state() -> dict[str, Any]:
    return {
        "name": ROOT_NAME,
        "lastOwner": None,
        "lastResolver": None,
        "subnames": [],
        "nestedSubnames": {},
        "prefillParent": "",
        "txLog": [],
    }


def load_state(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path or STATE_FILE)
    if not p.is_file():
        return default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_state()
    if data.get("name") != ROOT_NAME:
        return default_state()
    out = default_state()
    out.update({k: data.get(k, out[k]) for k in out})
    out.setdefault("nestedSubnames", {})
    out.setdefault("txLog", [])
    return out


def save_state(state: dict[str, Any], path: str | Path | None = None) -> None:
    p = Path(path or STATE_FILE)
    payload = dict(state)
    payload["name"] = ROOT_NAME
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
