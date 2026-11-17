#!/usr/bin/env bash
# Mô phỏng không cần radio: TX gửi mẫu qua ZeroMQ tới RX (chạy RX sim_rx.sh trước, cùng PC).
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_tx.py --mode sim --seconds 15 "$@"
