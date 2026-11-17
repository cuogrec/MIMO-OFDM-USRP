#!/usr/bin/env bash
# One N321, four antennas (TX/RX A,B transmit; RX2 A,B receive). Check local band rules first.
cd "$(dirname "$0")/.." && exec /usr/bin/python3 run_n321.py --mode rf --rf-link antenna --tx-gain 20 --rx-gain 35 "$@"
