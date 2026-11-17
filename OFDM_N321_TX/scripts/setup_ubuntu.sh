#!/usr/bin/env bash
# Ubuntu 24.04: GNU Radio 3.10 + UHD + FFmpeg (same stack as the N210 project).
set -euo pipefail
sudo apt-get update
sudo apt-get install -y gnuradio uhd-host python3-uhd python3-numpy ffmpeg ethtool iproute2
# FPGA/firmware images for the host-side UHD version (N320/N321 = "n320"):
sudo uhd_images_downloader -t n320 || true
uhd_config_info --version
