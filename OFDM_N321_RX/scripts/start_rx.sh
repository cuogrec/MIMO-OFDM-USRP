#!/usr/bin/env bash
# N321 #2 thu. Chạy TRƯỚC máy TX.  bash scripts/start_rx.sh --play
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_rx.py --mode rf "$@"
