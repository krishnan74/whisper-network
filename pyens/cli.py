#!/usr/bin/env python3
"""
Minimal CLI parity with main/app/ens + OnchainEnsFlow (variant=envPrivateKey).

- Sepolia · axl.eth · private key via env (no browser wallet).
- Writes: ENS Registry setSubnodeRecord (same default resolver as ensjs).
- Optional: list indexed *.axl.eth names for your address via ENS subgraph.

Does not implement Pimlico / EIP-7702 gasless flow (browser-only in TS).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from eth_account import Account
from eth_utils import is_address, to_checksum_address
from web3 import Web3

from . import __version__
from .constants import (
    CHAIN_ID,
    DEFAULT_SEPOLIA_RPC,
    PK_ENV_KEYS,
    ROOT_NAME,
)
from . import registry_tx
from . import state as state_mod
from . import subgraph


def _sep_tx_url(tx_hash: str) -> str:
    h = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    return f"https://sepolia.etherscan.io/tx/{h}"


def _sep_ens_app_url(ens_name: str) -> str:
    return f"https://sepolia.app.ens.domains/{ens_name.strip().lower()}"


def _parse_child_labels(raw: str, parent_fqdn: str) -> list[str]:
    suffix = f".{parent_fqdn.strip().lower()}"
    segments = re.split(r"[\n,]+", raw)
    seen: set[str] = set()
    out: list[str] = []
    for seg in segments:
        s = seg.strip().lower().replace(" ", "")
        if not s:
            continue
        if s.endswith(suffix):
            s = s[: -len(suffix)]
        if not s or "." in s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _load_pk(explicit: str | None = None) -> Account:
    raw = explicit
    if not raw:
        for k in PK_ENV_KEYS:
            v = os.environ.get(k)
            if v and v.strip():
                raw = v.strip()
                break
    if not raw:
        raise SystemExit(
            "Set one of "
            + ", ".join(PK_ENV_KEYS)
            + " to a Sepolia-funded key that owns axl.eth (or nested parents)."
        )
    if raw.startswith("0x"):
        key = raw
    else:
        key = "0x" + raw
    try:
        return Account.from_key(key)
    except Exception as e:
        raise SystemExit(f"Invalid private key: {e}") from e


def _w3_from_rpc(url: str) -> Web3:
    w = Web3(Web3.HTTPProvider(url))
    if not w.is_connected():
        raise SystemExit(f"RPC not reachable: {url}")
    if w.eth.chain_id != CHAIN_ID:
        raise SystemExit(
            f"Wrong chain id {w.eth.chain_id}; expected Sepolia ({CHAIN_ID})."
        )
    return w


def _norm_owner(addr: str | None, fallback: str) -> str:
    if not addr or not addr.strip():
        return to_checksum_address(fallback)
    a = addr.strip()
    if not is_address(a):
        raise SystemExit(f"Bad owner address: {a}")
    return to_checksum_address(a)


def cmd_info(w3: Web3) -> None:
    owner, resolver = registry_tx.read_root_owner_resolver(w3, ROOT_NAME)
    print(f"Root:           {ROOT_NAME}")
    print(f"Registry owner: {owner}")
    print(f"Registry resolver on root node: {resolver}")


def cmd_indexed(acct: Account, subgraph_url: str) -> None:
    try:
        rows = subgraph.fetch_names_for_address(acct.address, subgraph_url=subgraph_url)
    except Exception as e:
        print(f"Subgraph error: {e}")
        return
    names = subgraph.names_under_axl(rows, ROOT_NAME)
    print(f"Indexed names (*.axl.eth) for {acct.address}: {len(names)}")
    for n in names:
        print(f"  {n}")


def cmd_create_sub(acct: Account, w3: Web3, label: str, owner_str: str | None, wait: bool) -> None:
    st = state_mod.load_state()
    root_owner, _ = registry_tx.read_root_owner_resolver(w3, ROOT_NAME)
    if root_owner.lower() != acct.address.lower():
        raise SystemExit(
            "Private key must be the on-chain owner of "
            f"{ROOT_NAME} to mint direct subnames (owner is {root_owner})."
        )
    label_clean = label.strip().lower().replace(" ", "")
    if not label_clean or "." in label_clean:
        raise SystemExit("Enter a single label (e.g. hehe).")
    full = f"{label_clean}.{ROOT_NAME}"
    owner_cs = _norm_owner(owner_str, acct.address)
    txh = registry_tx.send_registry_create_subname(
        w3, acct, full, owner_cs, resolver=None
    )
    print(f"Submitted: {full}")
    print(_sep_tx_url(txh))
    print(_sep_ens_app_url(full))
    if wait:
        rcpt = registry_tx.wait_receipt(w3, txh)
        if getattr(rcpt, "status", 1) == 0:
            raise SystemExit("Transaction reverted.")
        print("Confirmed.")
    tx_log = list(st.get("txLog") or [])
    tx_log.insert(0, {"kind": "sub", "name": full, "hash": txh})
    subs = list(st.get("subnames") or [])
    if full not in subs:
        subs.insert(0, full)
    st.update(
        {
            "subnames": subs,
            "prefillParent": full,
            "txLog": tx_log,
        }
    )
    state_mod.save_state(st)


def cmd_create_nested(
    acct: Account,
    w3: Web3,
    parent: str,
    labels_raw: str,
    owner_str: str | None,
    wait: bool,
) -> None:
    parent_name = parent.strip().lower().replace(" ", "")
    if not parent_name:
        raise SystemExit("Set parent under axl.eth (e.g. hehe.axl.eth).")
    if parent_name != ROOT_NAME and not parent_name.endswith(f".{ROOT_NAME}"):
        raise SystemExit(
            f"Parent must be {ROOT_NAME} or a name ending .{ROOT_NAME}"
        )

    labels = _parse_child_labels(labels_raw, parent_name)
    if not labels:
        raise SystemExit(
            "Provide child labels (one per line or comma-separated). "
            "Paste goat or goat.hehe.axl.eth style lines."
        )
    parent_owner = registry_tx.read_owner(w3, parent_name)
    if parent_owner.lower() != acct.address.lower():
        raise SystemExit(
            "Private key must be the on-chain owner of the parent name. "
            f"Parent owner: {parent_owner}"
        )
    nested_owner = _norm_owner(owner_str, acct.address)

    successes: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    st = state_mod.load_state()
    tx_log = list(st.get("txLog") or [])
    nested_map = dict(st.get("nestedSubnames") or {})

    for i, lb in enumerate(labels, 1):
        full = f"{lb}.{parent_name}"
        print(f"[{i}/{len(labels)}] {full}")
        try:
            txh = registry_tx.send_registry_create_subname(
                w3, acct, full, nested_owner, resolver=None
            )
            print(f"  tx: {_sep_tx_url(txh)}")
            if wait:
                rcpt = registry_tx.wait_receipt(w3, txh)
                if getattr(rcpt, "status", 1) == 0:
                    raise RuntimeError("reverted")
            successes.append((full, txh))
            tx_log.insert(0, {"kind": "nested", "name": full, "hash": txh})
            lst = nested_map.get(parent_name, [])
            merged = list(dict.fromkeys([full] + lst))
            nested_map[parent_name] = merged
        except Exception as e:
            failures.append((lb, str(e)))
            print(f"  FAILED: {e}")

    if successes:
        st["nestedSubnames"] = nested_map
        st["txLog"] = tx_log
        state_mod.save_state(st)

    print("---")
    print(f"OK: {len(successes)} · Failed: {len(failures)}")
    for full, txh in successes:
        print(f"  {full} {_sep_tx_url(txh)}")
        print(f"       {_sep_ens_app_url(full)}")
    for lb, msg in failures:
        print(f"  FAIL {lb}: {msg}")


def cmd_show_state() -> None:
    st = state_mod.load_state()
    print("(persisted locally like the browser)")
    print("subnames:", st.get("subnames") or [])
    print("nestedSubnames:")
    nest = st.get("nestedSubnames") or {}
    for p, lst in sorted(nest.items()):
        print(f"  {p}: {lst}")
    print("txLog (recent first, max shown 20):")
    for row in (st.get("txLog") or [])[:20]:
        print(f"  {row}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sepolia axl.eth subnames (private key)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
`No module named pyens`: the package folder is ens-test/pyens/. From openagents/, use `./run_pyens.sh ... -m pyens` (it sets PYTHONPATH), or `cd ens-test` then `./pyens/run.sh .venv/bin/python -m pyens ...`.

macOS pip/pyexpat: `brew install expat`; `run_pyens.sh` / `pyens/run.sh` add Homebrew libexpat to DYLD_LIBRARY_PATH.

Examples (openagents repo root, venv at ens-test/.venv):

  ./run_pyens.sh ens-test/.venv/bin/python -m pip install -r ens-test/pyens/requirements.txt
  export PYENS_PRIVATE_KEY=0x...
  ./run_pyens.sh ens-test/.venv/bin/python -m pyens info

From inside `ens-test/`, `./pyens/run.sh ...` replaces `./run_pyens.sh ens-test/.venv/bin/python`.

If your cwd is the `pyens` folder: `pip install -r requirements.txt` (not `./pyens/requirements.txt`).

Signer must own axl.eth on-chain for *.axl.eth, or own the parent for nested. Mirrors main/app/ens (privkey); no Pimlico/EIP-7702 in Python.
""",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--rpc",
        default=DEFAULT_SEPOLIA_RPC,
        help="Sepolia JSON-RPC URL (default matches Next app PUBLIC fallback)",
    )
    p.add_argument(
        "--pk",
        default=None,
        help="Explicit private key (else read from env PYENS_PRIVATE_KEY / ENS_TEST_* )",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Print axl.eth registry owner/resolver")

    sp_i = sub.add_parser("indexed", help="Subgraph: *.axl.eth for this key’s address")

    sub.add_parser("status", help="Print saved subname / nested / tx history (local JSON)")

    cs = sub.add_parser("mint-sub", help="Create label.axl.eth (key must own axl.eth)")
    cs.add_argument("label", help="single label e.g. hehe")
    cs.add_argument(
        "--owner",
        default=None,
        help="Owner address for new subname (default: signer address)",
    )
    cs.add_argument("--wait", action="store_true", help="Wait for receipts")

    cn = sub.add_parser(
        "mint-nested",
        help="Batch child.parent under *.axl.eth (signer must own parent)",
    )
    cn.add_argument("--parent", required=True, help="e.g. hehe.axl.eth")
    cn.add_argument(
        "--labels",
        required=True,
        help='Labels: "a,b" or stdin-style string with newlines',
    )
    cn.add_argument(
        "--owner",
        default=None,
        help="Owner for nested names (default: signer address)",
    )
    cn.add_argument("--wait", action="store_true")

    sf = sub.add_parser(
        "labels-from-file",
        help="Mint nested from file (same as mint-nested labels from file)",
    )
    sf.add_argument("--parent", required=True)
    sf.add_argument("path", type=Path)
    sf.add_argument("--owner", default=None)
    sf.add_argument("--wait", action="store_true")

    return p


def main(argv: Iterable[str] | None = None) -> None:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    w3 = _w3_from_rpc(args.rpc)
    acct = _load_pk(args.pk)

    if args.cmd == "info":
        cmd_info(w3)
    elif args.cmd == "indexed":
        cmd_indexed(acct, subgraph.ENS_SEPOLIA_SUBGRAPH_URL)
    elif args.cmd == "status":
        cmd_show_state()
    elif args.cmd == "mint-sub":
        cmd_create_sub(acct, w3, args.label, args.owner, args.wait)
    elif args.cmd == "mint-nested":
        cmd_create_nested(
            acct, w3, args.parent, args.labels, args.owner, args.wait
        )
    elif args.cmd == "labels-from-file":
        raw = Path(args.path).read_text(encoding="utf-8")
        cmd_create_nested(acct, w3, args.parent, raw, args.owner, args.wait)
    else:
        raise SystemExit("unknown cmd")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
