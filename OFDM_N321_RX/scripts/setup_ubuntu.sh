#!/usr/bin/env bash
# Ubuntu 24.04: GNU Radio 3.10 + UHD + FFmpeg (same stack as the N210 project) + the shared ofdm321 package.
set -euo pipefail
sudo apt-get update
sudo apt-get install -y gnuradio uhd-host python3-uhd python3-numpy python3-matplotlib python3-pip \
    python3-setuptools ffmpeg ethtool iproute2
# FPGA/firmware images for the host-side UHD version (N320/N321 = "n320"):
sudo uhd_images_downloader -t n320 || true
uhd_config_info --version
# Shared Python package at the repository root, editable (a git pull is enough afterwards).
# --user: no sudo; --break-system-packages: Ubuntu 24.04 (PEP 668); --no-build-isolation: no download.
cd "$(dirname "$0")/../.."
/usr/bin/python3 -m pip install --user --break-system-packages --no-build-isolation -e .
/usr/bin/python3 -c "import ofdm321; print('ofdm321', ofdm321.__version__, 'from', ofdm321.__file__)"
