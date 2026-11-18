#!/usr/bin/env bash
# Mô phỏng ACM với phản hồi qua sóng radio, phía phát (chạy sau sim_rx_fbradio.sh của folder RX).
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_tx.py --mode sim --mimo siso --acm --fb-link radio \
    --rate 400e3 --fb-rate 250e3 --seconds 36 "$@"
