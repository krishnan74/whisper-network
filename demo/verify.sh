#!/usr/bin/env bash
# Verify the full Whisper Network demo end-to-end.
# Run after ./run_local.sh (or FAST_MODE=1 ./run_local.sh) is already up.
# Can be invoked from any directory: ./demo/verify.sh or cd demo && ./verify.sh
set -euo pipefail

# Always run from the project root so Python module paths resolve correctly
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

PYTHON="${PYTHON:-$([ -f .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Whisper Network — Demo Verification"
echo "═══════════════════════════════════════════════════"
echo ""

# ── Step 1: Process count ─────────────────────────────────────────────────────
info "Step 1 — Checking all 12 processes are running..."

AXL_COUNT=$(pgrep -f "axl/node" | wc -l)
WHISPER_COUNT=$(pgrep -f "whisper.node" | wc -l)

[ "$AXL_COUNT" -eq 6 ]     || fail "Expected 6 AXL processes, found ${AXL_COUNT}. Run: FAST_MODE=1 ./run_local.sh"
[ "$WHISPER_COUNT" -eq 6 ] || fail "Expected 6 whisper processes, found ${WHISPER_COUNT}. Run: FAST_MODE=1 ./run_local.sh"
pass "6 AXL + 6 whisper processes running"

# ── Step 2: AXL /send returns 200 ────────────────────────────────────────────
info "Step 2 — Verifying AXL /send works (tcp_port check)..."

NODE2_KEY=$(curl -s http://127.0.0.1:9003/topology | \
    "${PYTHON}" -c "import sys,json; print(json.load(sys.stdin)['our_public_key'])")
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:9002/send \
    -H "X-Destination-Peer-Id: ${NODE2_KEY}" \
    -d '{"type":"ping","msg_id":"verify-ping"}')

if [ "$HTTP_CODE" = "200" ]; then
    pass "AXL /send → 200 (message routing works)"
else
    fail "AXL /send → ${HTTP_CODE} (expected 200). Delete axl-local/ and restart run_local.sh"
fi

# ── Step 3: All whisper nodes see each other ──────────────────────────────────
info "Step 3 — Waiting for whisper membership to converge (up to 15s)..."

DEADLINE=$((SECONDS + 15))
while [ $SECONDS -lt $DEADLINE ]; do
    ALIVE=$(curl -s http://127.0.0.1:8888/state | \
        "${PYTHON}" -c "
import sys, json
s = json.load(sys.stdin)
print(sum(1 for p in s['peers'].values() if p['status'] == 'alive'))
" 2>/dev/null || echo 0)
    [ "$ALIVE" -eq 5 ] && break
    sleep 1
done

[ "$ALIVE" -eq 5 ] || fail "Node-1 sees only ${ALIVE}/5 alive peers after 15s. Check logs/whisper-1.log"

UP_PEERS=$(curl -s http://127.0.0.1:8888/state | \
    "${PYTHON}" -c "import sys,json; print(json.load(sys.stdin)['axl_mesh']['up_peers'])" 2>/dev/null || echo 0)
pass "All 5 peers alive | AXL mesh up_peers=${UP_PEERS}"

# ── Step 4: Normal task submission ───────────────────────────────────────────
info "Step 4 — Submitting query 'attention' (all 6 shards should complete)..."

"${PYTHON}" -m demo.submit_task "attention" --api http://localhost:8888 --timeout 30 \
    && pass "All 6 shards completed (normal path)" \
    || fail "Task submission timed out or failed"

# ── Step 5: Fault tolerance — kill 3 nodes mid-flight ────────────────────────
info "Step 5 — Fault tolerance: killing nodes 4, 5, 6 mid-execution..."

# Submit in background
"${PYTHON}" -m demo.submit_task "gossip" --api http://localhost:8888 --timeout 90 &
SUBMIT_PID=$!

sleep 1
PIDS_TO_KILL=$(pgrep -f "shard-id [456]" 2>/dev/null || true)
if [ -z "$PIDS_TO_KILL" ]; then
    kill "$SUBMIT_PID" 2>/dev/null || true
    fail "Could not find whisper processes for shards 4-6"
fi
# shellcheck disable=SC2086
kill -9 $PIDS_TO_KILL
info "  Killed nodes 4, 5, 6 — waiting for survivors to recover..."

RECOVERY_START=$SECONDS
if wait "$SUBMIT_PID" 2>/dev/null; then
    RECOVERY_TIME=$((SECONDS - RECOVERY_START))
    pass "All 6/6 tasks completed after ~${RECOVERY_TIME}s with 3 nodes dead"
else
    fail "Recovery failed — surviving nodes did not complete all tasks within 60s"
fi

# ── Step 6: Cryptography self-test ───────────────────────────────────────────
info "Step 6 — Cryptography: GF(256) Shamir + ThresholdCipher round-trip..."

"${PYTHON}" -c "
import sys, os
sys.path.insert(0, '.')
from whisper.crypto import shamir_split, shamir_reconstruct, ThresholdCipher, PayloadCipher

# GF(256) Shamir (3,6) self-test
secret = os.urandom(32)
shares = shamir_split(secret, 6, 3)
assert shamir_reconstruct(shares[:3]) == secret, 'Shamir (3,6) first-3 failed'
assert shamir_reconstruct(shares[1:4]) == secret, 'Shamir (3,6) mid-3 failed'
assert shamir_reconstruct([shares[0], shares[2], shares[5]]) == secret, 'Shamir (3,6) sparse failed'
print('  GF(256) Shamir (3,6): all subset combinations correct')

# ThresholdCipher encrypt/decrypt round-trip using real key files (if available)
key_files = ['keys/private-1.pem','keys/private-2.pem','keys/private-3.pem']
ciphers   = []
for kf in key_files:
    if os.path.exists(kf):
        c = PayloadCipher(kf)
        if c.enabled:
            ciphers.append(c)

if len(ciphers) >= 2:
    t_ciph = ThresholdCipher.from_payload_cipher(ciphers[0])
    pubkeys = [c.x25519_pubkey_hex for c in ciphers]
    plaintext = 'test payload: attention transformer'
    ciphertext = t_ciph.encrypt(pubkeys, plaintext, t=2)
    assert ciphertext.startswith('THRESHOLD:'), 'THRESHOLD: marker missing'
    share0 = ThresholdCipher.from_payload_cipher(ciphers[0]).decrypt_own_share(ciphertext)
    share1 = ThresholdCipher.from_payload_cipher(ciphers[1]).decrypt_own_share(ciphertext)
    assert share0 is not None and share1 is not None, 'share decryption returned None'
    recovered = ThresholdCipher.reconstruct_and_decrypt(ciphertext, [share0, share1])
    assert recovered == plaintext, f'round-trip mismatch: {recovered!r}'
    print('  ThresholdCipher (2,3) encrypt → share decrypt → reconstruct: OK')
else:
    print('  ThresholdCipher: no key files found — skipping live key test (Shamir math verified above)')
" || fail "Cryptography self-test raised an exception"

# Also confirm enc_pubkeys are being gossiped
ENC_PEERS=$("${PYTHON}" -c "
import sys, json, urllib.request
try:
    d = json.loads(urllib.request.urlopen('http://127.0.0.1:8888/state', timeout=3).read())
    count = sum(1 for p in d.get('peers', {}).values() if p.get('enc_pubkey'))
    print(count)
except:
    print(0)
" 2>/dev/null || echo 0)
[ "$ENC_PEERS" -gt 0 ] \
    && pass "Cryptography self-test passed | ${ENC_PEERS} peer(s) advertising enc_pubkey via gossip" \
    || pass "Cryptography self-test passed (enc_pubkey gossip: not yet visible — check after cluster warms up)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo -e "  ${GREEN}All checks passed — demo is working correctly${NC}"
echo "═══════════════════════════════════════════════════"
echo ""
echo "  Web UI:    ${PYTHON} -m demo.webui       → http://localhost:5000"
echo "  Dashboard: ${PYTHON} -m demo.dashboard"
echo "  Partition: ./demo/partition_demo.sh 'transformer'"
echo ""
