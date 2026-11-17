#!/usr/bin/env bash
# Prepare the host SFP+ NIC for the N321.  Usage:
#   sudo bash scripts/setup_network_10g.sh <iface> [host_ip/24]
# Speed check: SFP0 (1 GbE, 192.168.10.2) is fine for <= a few MS/s; SFP1 10 GbE for more.
# Default N3xx addresses: SFP0 192.168.10.2, SFP1 192.168.20.2 (HG image: SFP1 = 10 GbE).
set -euo pipefail
IFACE=${1:?"interface name, e.g. enp1s0f0 (see: ip -br link)"}
HOSTIP=${2:-192.168.20.1/24}
echo "Before: $(ip -br addr show "$IFACE")"
ip addr replace "$HOSTIP" dev "$IFACE"
ip link set "$IFACE" mtu 9000 up
echo "Link speed: $(cat /sys/class/net/$IFACE/speed 2>/dev/null) Mb/s"
sysctl -w net.core.rmem_max=33554432 net.core.wmem_max=33554432
ethtool -G "$IFACE" rx 4096 tx 4096 2>/dev/null || echo "ethtool -G not supported on $IFACE (ok)"
cat <<MSG
Done for this boot.  To make it permanent:
  * NetworkManager:  nmcli con mod <connection> ipv4.method manual ipv4.addresses $HOSTIP 802-3-ethernet.mtu 9000
  * sysctl:          echo -e 'net.core.rmem_max=33554432\nnet.core.wmem_max=33554432' | sudo tee /etc/sysctl.d/90-uhd.conf
Then:  python3 tools/probe_n321.py --stream
MSG
