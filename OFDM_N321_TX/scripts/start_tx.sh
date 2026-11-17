#!/usr/bin/env bash
# N321 #1 phát. Chạy SAU khi máy RX đã chạy start_rx.sh.
#   bash scripts/start_tx.sh                          # testpattern 30 s, alamouti QPSK, 915 MHz
#   bash scripts/start_tx.sh --source file --input ~/Videos/clip.mp4 --seconds 60
#   bash scripts/start_tx.sh --source webcam --input /dev/video0
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_tx.py --mode rf "$@"
