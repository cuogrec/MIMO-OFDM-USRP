#!/usr/bin/python3
"""Check the Ubuntu host, the 10 GbE link and the N321 WITHOUT transmitting RF.

    python3 tools/probe_n321.py [--addr 192.168.20.2] [--stream]

--stream also runs UHD benchmark_rate RX-only on both channels (RF0, RF1; no TX) to check
for overflows at the project sample rate. Everything is written to runs/probe_<time>/.
"""
import argparse
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root
from ofdm321.config import app_root  # noqa: E402
ROOT = app_root()   # runs/ of the folder you are in (OFDM_N321_TX / _RX)


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="192.168.20.2")
    ap.add_argument("--rate", default="1e6")
    ap.add_argument("--stream", action="store_true")
    a = ap.parse_args()
    out = ROOT / "runs" / f"probe_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True)
    res, warn = {}, []

    code, txt = sh("uhd_config_info --version")
    res["uhd_version"] = txt
    code, txt = sh("python3 -c 'import gnuradio.gr as g; print(g.version())'")
    res["gnuradio"] = txt
    # host interface on the same /24 as the N321
    net = ipaddress.ip_network(a.addr + "/24", strict=False)
    iface, mtu = None, None
    code, txt = sh("ip -o -4 addr show")
    for line in txt.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", line)
        if m and ipaddress.ip_address(m.group(2)) in net:
            iface = m.group(1)
    res["host_interface"] = iface
    if iface is None:
        warn.append(f"No host IPv4 address in {net}: set e.g. {net.network_address + 1}/24 on the SFP+ NIC")
    else:
        mtu = int(open(f"/sys/class/net/{iface}/mtu").read())
        speed = sh(f"cat /sys/class/net/{iface}/speed")[1]
        res.update(mtu=mtu, link_speed_mbps=speed)
        if mtu < 9000:
            warn.append(f"MTU {mtu} on {iface}: N3xx SFP defaults to 9000 -> run scripts/setup_network_10g.sh")
        if speed.isdigit() and int(speed) < 10000:
            warn.append(f"{iface} links at {speed} Mb/s, not 10 GbE (check SFP+ module/cable)")
        res["ethtool_ring"] = sh(f"ethtool -g {iface}")[1]
    for key in ("net.core.rmem_max", "net.core.wmem_max"):
        v = sh(f"sysctl -n {key}")[1]
        res[key] = v
        if v.isdigit() and int(v) < 33554432:
            warn.append(f"{key}={v} < 33554432 (UHD recommends 32 MB) -> scripts/setup_network_10g.sh")
    res["ping"] = sh(f"ping -c 3 -W 1 {a.addr}")[1]
    if "0 received" in res["ping"] or "100% packet loss" in res["ping"]:
        warn.append(f"N321 {a.addr} does not answer ping")
    res["find_devices"] = sh("uhd_find_devices", 30)[1]
    code, probe = sh(f"uhd_usrp_probe --args addr={a.addr}", 120)
    (out / "uhd_usrp_probe.txt").write_text(probe)
    res["probe_ok"] = code == 0
    if code != 0:
        if "No devices found" in probe or "No UHD Devices Found" in res["find_devices"]:
            warn.append("No UHD device on the network: check that the cable is in SFP0/SFP1 (not the "
                        "RJ45 management port), the N321 has finished booting, and the IP/subnet match")
        elif re.search(r"compat|version mismatch|MPM", probe, re.I):
            warn.append("UHD/MPM/FPGA version mismatch -> update the N321 filesystem/FPGA to the host UHD version")
        else:
            warn.append("uhd_usrp_probe failed: see uhd_usrp_probe.txt")
    for pat in ("Mboard: .*", "FPGA Version: .*", "MPM Version: .*", "RX Dboard: .*", "TX Dboard: .*",
                "Name: .*", "Freq range: .*"):
        found = re.findall(pat, probe)
        if found:
            res.setdefault("probe_summary", []).extend(sorted(set(found))[:6])
    if a.stream:
        import glob
        bench = shutil.which("benchmark_rate") or next(iter(sorted(
            glob.glob("/usr/libexec/uhd/examples/benchmark_rate") + glob.glob("/usr/lib/*/uhd/examples/benchmark_rate")
            + glob.glob("/usr/lib/uhd/examples/benchmark_rate")
            + glob.glob("/usr/local/lib/uhd/examples/benchmark_rate"))), "benchmark_rate")
        code, txt = sh(f"{bench} --args addr={a.addr} --rx_rate {a.rate} --channels 0,1 --duration 10", 60)
        (out / "benchmark_rate_rx.txt").write_text(txt)
        res["benchmark_rx"] = txt[-800:]
        m = re.search(r"Num overruns detected:\s+(\d+)", txt)
        if m and int(m.group(1)) > 0:
            warn.append(f"{m.group(1)} RX overruns at {a.rate} S/s x2: check NIC/CPU settings")
    res["warnings"] = warn
    (out / "probe.json").write_text(json.dumps(res, indent=2))
    print(json.dumps({k: res[k] for k in res if k not in ("ping", "find_devices", "ethtool_ring",
                                                          "benchmark_rx")}, indent=2))
    print("\nWARNINGS:" if warn else "\nNo warnings.")
    for w in warn:
        print(" -", w)
    print("Saved:", out)


if __name__ == "__main__":
    main()
