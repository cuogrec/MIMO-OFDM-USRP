#!/usr/bin/env bash
# Mô phỏng ACM với phản hồi qua sóng radio (SISO + kênh B). Chạy trước sim_tx_fbradio.sh (folder TX).
# Máy yếu: giữ --rate 400e3 --fb-rate 250e3 như dưới; máy lab có thể bỏ để chạy 1 MS/s / 500 kS/s.
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_rx.py --mode sim --mimo siso --acm --fb-link radio \
    --rate 400e3 --fb-rate 250e3 --channel rayleigh --snr 30 --cfo 2000 --sfo 5 \
    --snr-profile steps:30,22,14,11,20,28:6 "$@"
