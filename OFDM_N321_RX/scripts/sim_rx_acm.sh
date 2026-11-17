#!/usr/bin/env bash
# Mô phỏng ACM: SNR đổi theo bậc 30/22/14/11/20/28 dB (mỗi bậc 6 s), kênh Rayleigh, CFO 2 kHz, lệch clock 5 ppm.
# Chạy trước, rồi chạy scripts/sim_tx_acm.sh ở folder TX. Máy yếu: thêm --rate 400e3 ở CẢ HAI bên.
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_rx.py --mode sim --acm --channel rayleigh --snr 30 --cfo 2000 --sfo 5 \
    --snr-profile steps:30,22,14,11,20,28:6 "$@"
