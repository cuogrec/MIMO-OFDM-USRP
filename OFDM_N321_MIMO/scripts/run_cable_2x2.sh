#!/usr/bin/env bash
# One N321, cabled 2x2 loopback through >=30 dB attenuators (see docs/DAU_NOI_2x2.md).
# Pass the REAL installed attenuation:  bash scripts/run_cable_2x2.sh --attenuation-db 40 --play
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_n321.py --mode rf --rf-link cable --tx-gain 15 --rx-gain 30 "$@"
