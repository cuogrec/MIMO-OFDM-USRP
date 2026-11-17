#!/usr/bin/env bash
# Mô phỏng hai radio độc lập: kênh 2x2 Rayleigh, SNR 20 dB, CFO 2 kHz, lệch clock 5 ppm.
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_rx.py --mode sim --channel rayleigh --snr 20 --cfo 2000 --sfo 5 "$@"
