#!/usr/bin/python3
"""So sánh video phát/thu:  python3 tools/compare_tx_rx.py <TX report.json> [RX report.json]
(RX mặc định = lượt mới nhất trong runs/). Copy report.json của máy TX sang nếu là 2 PC."""
import json, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
tx = json.loads(Path(sys.argv[1]).read_text())
rxp = Path(sys.argv[2]) if len(sys.argv) > 2 else Path((root / "runs/latest_run.txt").read_text().strip()) / "report.json"
rx = json.loads(rxp.read_text())
v = rx.get("video", {})
print("TX session", tx.get("session"), "| RX session", v.get("session"))
print("TX DATA", tx.get("tx", {}).get("tx_enqueued_data"), "| RX DATA", v.get("data_packets"),
      "missing", v.get("missing_data_packets"), "END", v.get("end_received"))
print("TX sha256", tx.get("video_tx_sha256")); print("RX sha256", rx.get("video_rx_sha256"))
same = tx.get("video_tx_sha256") == rx.get("video_rx_sha256") and tx.get("session") == v.get("session")
print("=> VIDEO GIỐNG HỆT (PASS)" if same else "=> KHÁC NHAU (FAIL)")
sys.exit(0 if same else 1)
