#!/usr/bin/env bash
# Simulation (no radio): 2x2 Rayleigh channel, 20 dB SNR, 300 Hz CFO.
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_n321.py --mode sim --channel rayleigh --snr 20 --cfo 300 "$@"
