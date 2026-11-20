#!/usr/bin/env bash
# Short two-process simulation (no radio): RX first, then TX, both over ZeroMQ. Exit 0 only if the RX
# prints SIM_RX_VERIFIED. Used by .github/workflows/ci.yml; runs the same on a lab PC.
#   bash tools/ci_sim.sh [seconds] [extra args for BOTH sides, e.g. --rate 400e3 --mimo siso --acm --fb-link radio]
set -uo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SECONDS_TX="${1:-10}"; shift || true
LOG="$(mktemp -d)"
cleanup() { kill "$RXP" 2>/dev/null || true; }
trap cleanup EXIT

/usr/bin/python3 "$REPO/OFDM_N321_RX/run_rx.py" --mode sim --channel rayleigh --snr 20 --cfo 2000 --sfo 5 \
    --seconds $((SECONDS_TX + 60)) --name ci "$@" > "$LOG/rx.log" 2>&1 &
RXP=$!
for _ in $(seq 120); do grep -q "RX listening" "$LOG/rx.log" && break; sleep 0.5; done
timeout $((SECONDS_TX + 120)) /usr/bin/python3 "$REPO/OFDM_N321_TX/run_tx.py" --mode sim --seconds "$SECONDS_TX" --name ci "$@" > "$LOG/tx.log" 2>&1
TXRC=$?
wait "$RXP"; RXRC=$?
echo "--- TX (exit $TXRC)"; tail -n 3 "$LOG/tx.log"
echo "--- RX (exit $RXRC)"; grep "RX ok" "$LOG/rx.log" | tail -n 2; tail -n 1 "$LOG/rx.log"
grep -q "SIM_RX_VERIFIED" "$LOG/rx.log" && [ "$TXRC" -eq 0 ] && echo "CI SIM PASS" && exit 0
echo "CI SIM FAIL (logs in $LOG)"; exit 1
