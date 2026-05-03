"""Match @ensdomains/ensjs constants for Sepolia (see ens-test/main node_modules/@ensdomains/ensjs/dist/contracts/consts.js)."""

import os

CHAIN_ID = 11155111
ROOT_NAME = "axl.eth"

ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"
ENS_PUBLIC_RESOLVER_SEPOLIA = "0xE99638b40E4Fff0129D56f03b55b6bbC4BBE49b5"

# Default public RPC (same idea as OnchainEnsFlow fallback)
DEFAULT_SEPOLIA_RPC = os.environ.get(
    "PYENS_SEPOLIA_RPC_URL",
    "https://ethereum-sepolia-rpc.publicnode.com",
)

# ENS Sepolia subgraph (ensjs default)
ENS_SEPOLIA_SUBGRAPH_URL = os.environ.get(
    "PYENS_ENS_SUBGRAPH_URL",
    "https://api.studio.thegraph.com/query/49574/enssepolia/version/latest",
)

ADDR_REVERSE_NAMEHASH = (
    "0x91d1777781884d03a6757a803996e38de2a42967fb37eeaca72729271025a9e2"
)
EMPTY_ADDRESS = "0x0000000000000000000000000000000000000000"

# Env var for hex private key (with or without 0x) — mirrors NEXT_PUBLIC_ENS_TEST_PRIVATE_KEY usage
PK_ENV_KEYS = ("PYENS_PRIVATE_KEY", "ENS_TEST_PRIVATE_KEY", "NEXT_PUBLIC_ENS_TEST_PRIVATE_KEY")

STATE_FILE = os.environ.get("PYENS_STATE_FILE", ".pyens_state.json")
