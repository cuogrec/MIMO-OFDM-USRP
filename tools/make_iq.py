"""PC side of the 'one Ethernet port' setup: pre-compute the whole TX waveform.

The PC encodes the video, packetises it (DATA + RS8+8 + END), modulates every frame
exactly like the live transmitter and writes a 2-channel IQ file. The file is then
copied once to N321 #1, which replays it on its own ARM (tools/n321_replay.py),
so the PC's single Ethernet port stays connected to N321 #2 (receiver).

    python3 tools/make_iq.py --source file --input ~/Videos/clip.mp4 --seconds 20
    python3 tools/make_iq.py --mimo smux --mod 16qam            # must match the RX

Output folder runs/<stamp>_iq_<mimo>_<mod>/:
    tx_iq_sc16.bin   int16 I,Q of ch A then ch B for every sample (8 bytes / sample)
    report.json      session, SHA-256 of the video (RX: --compare report.json)
    video_tx.ts      the transmitted MPEG-TS
    n321_replay.py   replay script (copy the whole folder to the N321)
The file does not depend on the sample rate: use the same --rate at replay and RX.
"""
import argparse
from datetime import datetime
import json
import os
import secrets
import shutil
import sys
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from ofdm321 import __version__                                   # noqa: E402
from ofdm321.config import app_root, load_settings               # noqa: E402
PROOT = app_root()   # run it inside OFDM_N321_TX: config/ and runs/ of that folder
from ofdm321.media import encoder_command, require_ffmpeg, sha256_file   # noqa: E402
from ofdm321.modem import Modulator                               # noqa: E402
from ofdm321.protocol import DATA, IDLE, pack_packet              # noqa: E402
from ofdm321.runner import Encoder, capacity                      # noqa: E402


class Collect:
    """Stands in for MimoTxSource: the encoder thread enqueues packets here."""

    def __init__(self):
        self.packets = []

    def enqueue(self, raw, stop_event):
        self.packets.append(raw)
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(PROOT / "config/default.json"))
    ap.add_argument("--mimo", dest="mimo_mode", choices=("siso", "simo", "alamouti", "smux"))
    ap.add_argument("--mod", dest="modulation", choices=("bpsk", "qpsk", "16qam", "64qam"))
    ap.add_argument("--interleaver", choices=("none", "block", "carrier"))
    ap.add_argument("--tx-rms", dest="tx_rms", type=float)
    ap.add_argument("--fec", choices=("rs", "ldpc"), help="inner code (the RX must use the same --fec)")
    ap.add_argument("--ldpc-rate", dest="ldpc_rate", choices=("1/2", "2/3", "3/4", "5/6"))
    ap.add_argument("--source", choices=("testpattern", "file"), default="testpattern")
    ap.add_argument("--input")
    ap.add_argument("--seconds", type=float, default=20, help="video length")
    ap.add_argument("--warmup", type=float, default=15,
                    help="IDLE seconds before the video (time to start the replay after the RX)")
    ap.add_argument("--tail", type=float, default=4, help="IDLE seconds after END")
    a = ap.parse_args()
    s = load_settings(a.config, mimo_mode=a.mimo_mode, modulation=a.modulation,
                      interleaver=a.interleaver, tx_rms=a.tx_rms, fec=a.fec, ldpc_rate=a.ldpc_rate)
    require_ffmpeg()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROOT / "runs" / f"{stamp}_iq_{s.mimo_mode}_{s.modulation}"
    out.mkdir(parents=True)
    session = secrets.randbits(32)

    # 1. video -> packets (same packetiser as the live TX, without real-time pacing)
    cmd = [c for c in encoder_command(s, a.source, a.input, a.seconds) if c != "-re"]
    col = Collect()
    enc = Encoder(cmd, col, session, out, threading.Event())
    enc.run()
    if enc.error:
        raise SystemExit(f"encoder failed: {enc.error}")
    data = sum(p[5] == DATA for p in col.packets)
    print(f"packets: {len(col.packets)} ({data} DATA)")

    # 2. packets -> frames -> int16 IQ file
    mod = Modulator(s.mimo_mode, s.modulation, s.frame_gap_samples, s.tx_rms, s.interleaver, fec=s.fec,
                    ldpc_rate=s.ldpc_rate)
    idle = mod.frame(pack_packet(IDLE, session))
    fl = idle.shape[1]
    n_warm = int(a.warmup * s.sample_rate / fl)
    n_tail = int(a.tail * s.sample_rate / fl)
    peak, clipped, total = 0.0, 0, 0
    path = out / "tx_iq_sc16.bin"
    with open(path, "wb") as f:
        def put(fr):
            nonlocal peak, clipped, total
            x = np.stack([fr.real, fr.imag], axis=-1).transpose(1, 0, 2).reshape(-1)   # (n, ch, iq)
            peak = max(peak, float(np.abs(x).max()))
            clipped += int(np.count_nonzero(np.abs(x) > 1.0))
            total += x.size
            f.write((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        for _ in range(n_warm):
            put(idle)
        for raw in col.packets:
            put(mod.frame(raw))
        for _ in range(n_tail):
            put(idle)
    n_samp = path.stat().st_size // 8
    cap = capacity(s)
    report = {"version": __version__, "role": "tx", "mode": "iq_replay", "session": session,
              "settings": s.to_dict(), "capacity": cap, "status": "TX_COMPLETE",
              "iq_file": path.name, "iq_samples": n_samp, "iq_bytes": path.stat().st_size,
              "duration_s_at_rate": {str(r): n_samp / r for r in (1e6, 500e3, 400e3)},
              "warmup_s": a.warmup, "packets": len(col.packets), "data_packets": data,
              "peak_abs": peak, "clipped_fraction": clipped / max(total, 1),
              "video_tx_bytes": (out / "video_tx.ts").stat().st_size,
              "video_tx_sha256": sha256_file(out / "video_tx.ts"), "created": datetime.now().isoformat()}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "n321_replay.py"), out)
    print(f"IQ file {path} : {n_samp / 1e6:.1f} M samples, {path.stat().st_size / 1e6:.0f} MB, "
          f"{n_samp / s.sample_rate:.1f} s at {s.sample_rate:g} S/s, peak {peak:.2f}, clipped {clipped}")
    print(f"session {session} | video sha256 {report['video_tx_sha256'][:16]}")
    print(f"folder: {out}")
    print("next: scp -r", out, "root@192.168.180.3:/home/root/   (see docs/MOT_CONG_MANG.md)")


if __name__ == "__main__":
    main()
