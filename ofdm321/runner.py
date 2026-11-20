"""Run one finite video session (sim or N321 RF) and write runs/<id>/report.json."""
import argparse
from datetime import datetime
import json
import logging
import secrets
import signal
import subprocess
import sys
import threading
import time

from gnuradio import gr

from . import __version__
from .blocks import MimoChannelSim, MimoRxSink, MimoTxSource, VideoReceiver
from .config import app_root, TS_CHUNK_BYTES, load_settings
from .media import encoder_command, require_ffmpeg, sha256_file
from .modem import codewords_per_frame, frame_samples
from .protocol import DATA, END, RS8_PARITY, pack_packet, rs_parity_stamp

LOG = logging.getLogger("ofdm321")


def capacity(s):
    acm = bool(getattr(s, "acm", False))
    fec, rate = getattr(s, "fec", "rs"), getattr(s, "ldpc_rate", "3/4")
    fl = frame_samples(s.mimo_mode, s.modulation, s.frame_gap_samples, acm, fec, rate)
    fps = s.sample_rate / fl
    per_packet = TS_CHUNK_BYTES * 8 * 8 / 16                 # TS bits per packet after RS8+8
    out = {"frame_samples": fl, "frames_per_s": fps, "fec": fec if fec == "rs" else
           f"ldpc {'5/6 punctured to 2016 bits' if acm else rate}"}
    if acm:
        from .acm import parse_modcods
        out["acm_ts_capacity_bps"] = {m: fps * codewords_per_frame(s.mimo_mode, m) * per_packet
                                      for m in parse_modcods(s.acm_modcods)}
        out["ts_capacity_bps_after_rs8_8"] = min(out["acm_ts_capacity_bps"].values())
    else:
        out["ts_capacity_bps_after_rs8_8"] = fps * per_packet
    return out


class Encoder(threading.Thread):
    def __init__(self, cmd, src, session, run_dir, stop):
        super().__init__(daemon=True, name="FFmpeg to OFDM")
        self.cmd, self.src, self.session, self.run_dir, self.stop = cmd, src, session, run_dir, stop
        self.count = 0
        self.error = None
        self.done = threading.Event()

    def _parity(self, group):
        from .fec_rs import encode_parity
        base = self.count - len(group)
        for i, payload in enumerate(encode_parity(group, 8)):
            raw = pack_packet(RS8_PARITY, self.session, base, payload, rs_parity_stamp(len(group), i, 8))
            if not self.src.enqueue(raw, self.stop):
                return False
        return True

    def run(self):
        try:
            with open(self.run_dir / "ffmpeg_tx.log", "wb") as err, \
                    open(self.run_dir / "video_tx.ts", "wb") as ref:
                proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=err)
                group = []
                while not self.stop.is_set():
                    payload = proc.stdout.read(TS_CHUNK_BYTES)
                    if len(payload) < TS_CHUNK_BYTES:
                        break
                    if not self.src.enqueue(pack_packet(DATA, self.session, self.count, payload,
                                                        time.monotonic_ns()), self.stop):
                        break
                    ref.write(payload)
                    self.count += 1
                    group.append(payload)
                    if len(group) == 8:
                        if not self._parity(group):
                            break
                        group = []
                if self.stop.is_set():
                    proc.terminate()
                proc.wait(timeout=10)
                ref.flush()
                if not self.stop.is_set():
                    if group:
                        self._parity(group)
                    for _ in range(12):
                        self.src.enqueue(pack_packet(END, self.session, self.count), self.stop)
        except Exception as exc:     # noqa: BLE001
            self.error = str(exc)
            LOG.exception("encoder failed")
        finally:
            self.done.set()


def build(s, mode, session, run_dir, rf_link, attenuation_db):
    tb = gr.top_block("OFDM N321 MIMO", catch_exceptions=True)
    src = MimoTxSource(s, session, throttle=(mode == "sim"))
    rx = MimoRxSink(s)
    vr = VideoReceiver(session, run_dir / "video_rx.ts", s.udp_port)
    hw = {"mode": mode}
    if mode == "sim":
        ch = MimoChannelSim(s)
        tb.connect((src, 0), (ch, 0))
        tb.connect((src, 1), (ch, 1))
        tb.connect((ch, 0), (rx, 0))
        tb.connect((ch, 1), (rx, 1))
        hw["sim_H"] = [[str(complex(v)) for v in row] for row in ch.H]
        tb._keep = [ch]          # Python blocks must outlive start(): avoid GC segfault
    else:
        from .hardware import configure, make_radios, validate_rf
        hw["notes"] = validate_rf(s, rf_link, attenuation_db)
        hw["rf_link"], hw["attenuation_db"] = rf_link, attenuation_db
        usrp_rx, usrp_tx = make_radios(s)
        hw.update(configure(usrp_rx, usrp_tx, s))
        tb.connect((src, 0), (usrp_tx, 0))
        tb.connect((src, 1), (usrp_tx, 1))
        tb.connect((usrp_rx, 0), (rx, 0))
        tb.connect((usrp_rx, 1), (rx, 1))
        tb._keep = [usrp_rx, usrp_tx]
    tb.msg_connect((rx, "pdus"), (vr, "pdus"))
    tb._keep += [src, rx, vr]
    return tb, src, rx, vr, hw


def parse(argv):
    p = argparse.ArgumentParser(description="OFDM video over USRP N321 (2x2 MIMO loopback)")
    p.add_argument("--mode", choices=("sim", "rf"), default="sim")
    p.add_argument("--config", default=str(app_root() / "config/default.json"))
    p.add_argument("--mimo", dest="mimo_mode", choices=("siso", "simo", "alamouti", "smux"))
    p.add_argument("--mod", dest="modulation", choices=("qpsk", "16qam"))
    p.add_argument("--rate", dest="sample_rate", type=float)
    p.add_argument("--freq", dest="frequency", type=float)
    p.add_argument("--tx-gain", dest="tx_gain", type=float)
    p.add_argument("--rx-gain", dest="rx_gain", type=float)
    p.add_argument("--tx-rms", dest="tx_rms", type=float)
    p.add_argument("--interleaver", choices=("none", "block", "carrier"))
    p.add_argument("--no-soft", dest="soft_decoding", action="store_const", const=False)
    p.add_argument("--fec", choices=("rs", "ldpc"), help="inner code: rs (default) or ldpc (802.11n)")
    p.add_argument("--ldpc-rate", dest="ldpc_rate", choices=("1/2", "2/3", "3/4", "5/6"))
    p.add_argument("--args", dest="device_args")
    p.add_argument("--snr", dest="sim_snr_db", type=float)
    p.add_argument("--cfo", dest="sim_cfo_hz", type=float)
    p.add_argument("--channel", dest="sim_channel", choices=("identity", "rayleigh", "fixed"))
    p.add_argument("--rf-link", choices=("cable", "antenna"), default="cable")
    p.add_argument("--attenuation-db", type=float)
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--source", choices=("testpattern", "webcam", "file"), default="testpattern")
    p.add_argument("--input")
    p.add_argument("--play", action="store_true")
    p.add_argument("--name", default="")
    return p.parse_args(argv)


def main(argv=None):
    a = parse(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    keys = ("mimo_mode", "modulation", "sample_rate", "frequency", "tx_gain", "rx_gain", "tx_rms",
            "device_args", "sim_snr_db", "sim_cfo_hz", "sim_channel", "interleaver", "soft_decoding",
            "fec", "ldpc_rate")
    s = load_settings(a.config, **{k: getattr(a, k) for k in keys})
    require_ffmpeg()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = app_root() / "runs" / f"{stamp}_{a.mode}_{s.mimo_mode}_{s.modulation}{('_' + a.name) if a.name else ''}"
    run_dir.mkdir(parents=True)
    (app_root() / "runs/latest_run.txt").write_text(str(run_dir) + "\n")
    session = secrets.randbits(32)
    cap = capacity(s)
    report = {"version": __version__, "mode": a.mode, "settings": s.to_dict(), "capacity": cap,
              "session": session, "started": datetime.now().isoformat(), "status": "FAIL"}
    if s.ts_muxrate > 0.85 * cap["ts_capacity_bps_after_rs8_8"]:
        LOG.warning("ts_muxrate %d > 85%% of link TS capacity %.0f b/s: video will lag",
                    s.ts_muxrate, cap["ts_capacity_bps_after_rs8_8"])
    from .hardware import antenna_summary
    report["antennas"] = antenna_summary(s, "loopback")
    LOG.info("Cắm anten/cáp (loopback%s): %s", "" if a.mode == "rf" else ", mô phỏng", report["antennas"])
    LOG.info("mode=%s mimo=%s mod=%s Fs=%.3g frame=%d samples, TS capacity %.0f kb/s",
             a.mode, s.mimo_mode, s.modulation, s.sample_rate, cap["frame_samples"],
             cap["ts_capacity_bps_after_rs8_8"] / 1e3)
    stop = threading.Event()
    player = None
    try:
        tb, src, rx, vr, hw = build(s, a.mode, session, run_dir, a.rf_link, a.attenuation_db)
        report["hardware"] = hw
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        tb.start()
        deadline = time.monotonic() + 15
        while rx.snapshot()["ok"] < 3:
            if time.monotonic() > deadline or stop.is_set():
                raise RuntimeError("No OFDM link (no IDLE frame decoded in 15 s): check cabling/gain")
            time.sleep(0.2)
        LOG.info("OFDM link ready: %s", rx.snapshot())
        if a.play:
            player = subprocess.Popen(["ffplay", "-hide_banner", "-loglevel", "warning", "-fflags",
                                       "nobuffer", "-flags", "low_delay", "-framedrop",
                                       # small probe: ffplay starts at the first keyframe instead of
                                       # analysing ~5 s of stream first (that video was never shown)
                                       "-probesize", "32768", "-analyzeduration", "500000",
                                       f"udp://127.0.0.1:{s.udp_port}?fifo_size=100000&overrun_nonfatal=1"])
        enc = Encoder(encoder_command(s, a.source, a.input, a.seconds), src, session, run_dir, stop)
        enc.start()
        limit = time.monotonic() + a.seconds + 60
        while not stop.is_set() and time.monotonic() < limit:
            time.sleep(1)
            r, v, t = rx.snapshot(), vr.snapshot(), src.snapshot()
            LOG.info("TX data %d q %d | RX ok %d (soft %d) sig %d rs %d crc %d | DATA %d missing %d | SNR %s dB EVM %s dB cond %s dB",
                     t["tx_enqueued_data"], t["tx_queue"], r["ok"], r["soft_decoded"], r["sig_fail"], r["rs_fail"], r["crc_fail"],
                     v["data_packets"], v["missing_data_packets"],
                     None if r["snr_db"] is None else round(r["snr_db"], 1),
                     None if r["evm_db"] is None else round(r["evm_db"], 1),
                     None if r["cond_db"] is None else round(r["cond_db"], 1))
            if enc.done.is_set() and vr.ended.is_set():
                time.sleep(1)
                break
        stop.set()
        tb.stop()
        tb.wait()
        vr.close()
        r, v, t = rx.snapshot(), vr.snapshot(), src.snapshot()
        report.update(rx=r, video=v, tx=t, encoder_error=enc.error)
        tx_ts, rx_ts = run_dir / "video_tx.ts", run_dir / "video_rx.ts"
        same = tx_ts.stat().st_size > 0 and sha256_file(tx_ts) == sha256_file(rx_ts)
        report["ts_identical"] = same
        if same and v["missing_data_packets"] == 0 and v["end_received"]:
            report["status"] = "PASS"
    except Exception as exc:        # noqa: BLE001
        report["error"] = str(exc)
        LOG.error("%s", exc)
    finally:
        stop.set()
        if player:
            player.terminate()
        report["completed"] = datetime.now().isoformat()
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    LOG.info("%s | report: %s", report["status"], run_dir / "report.json")
    return 0 if report["status"] == "PASS" else 1
