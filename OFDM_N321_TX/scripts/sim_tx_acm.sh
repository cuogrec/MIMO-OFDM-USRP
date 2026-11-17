#!/usr/bin/env bash
# Mô phỏng ACM phía phát (chạy sau scripts/sim_rx_acm.sh của folder RX, cùng PC).
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_tx.py --mode sim --acm --seconds 36 "$@"
