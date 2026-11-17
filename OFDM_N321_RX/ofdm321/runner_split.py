"""Two N321 (or two processes): role 'tx' or 'rx'. Each role opens only its own radio.

TX: FFmpeg -> DATA/RS8_PARITY/END -> MIMO OFDM (2 channels) -> N321 #1 (TX/RX A:0, B:0)
RX: N321 #2 (RX2 A:0, B:0) -> MIMO OFDM RX -> verified RS8+8 -> video_rx.ts + UDP -> ffplay
--mode sim replaces the air link with ZeroMQ (TX PUSH -> RX PULL) plus, at the RX,
a sample-clock offset, a 2x2 channel, noise and CFO: the same impairments as two
separate radios with independent clocks.
"""
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

from gnuradio import blocks, gr

from . import __version__
from .blocks import MimoChannelSim, MimoRxSink, MimoTxSource, SfoResampler, VideoReceiver
from .config import ROOT, load_settings
from .media import encoder_command, require_ffmpeg, sha256_file
from .runner import Encoder, capacity

LOG = logging.getLogger("ofdm321")


def common_args(p):
    p.add_argument("--mode", choices=("rf", "sim"), default="rf")
    p.add_argument("--config", default=str(ROOT / "config/default.json"))
    p.add_argument("--mimo", dest="mimo_mode", choices=("siso", "simo", "alamouti", "smux"))
    p.add_argument("--mod", dest="modulation", choices=("qpsk", "16qam"))
    p.add_argument("--rate", dest="sample_rate", type=float)
    p.add_argument("--freq", dest="frequency", type=float)
    p.add_argument("--args", dest="device_args")
    p.add_argument("--sync", choices=("internal", "gpsdo", "external"),
                   help="clock+time source of THIS N321 (gpsdo: lock to GPS, needs antenna on GPS port)")
    p.add_argument("--zmq", dest="zmq_addr")
    p.add_argument("--name", default="")


def settings_from(a, extra):
    keys = ["mimo_mode", "modulation", "sample_rate", "frequency", "device_args", "zmq_addr"] + extra
    over = {k: getattr(a, k) for k in keys}
    if a.sync:
        over.update(clock_source=a.sync, time_source=a.sync)
    return load_settings(a.config, **over)


def new_run(role, s, mode, name):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ROOT / "runs" / f"{stamp}_{role}_{mode}_{s.mimo_mode}_{s.modulation}{('_' + name) if name else ''}"
    run_dir.mkdir(parents=True)
    (ROOT / "runs/latest_run.txt").write_text(str(run_dir) + "\n")
    return run_dir


def ffprobe_frames(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames,width,height", "-of", "json",
                              str(path)], capture_output=True, text=True, timeout=60)
        st = json.loads(out.stdout).get("streams", [])
        return st[0] if st else None
    except Exception:          # noqa: BLE001
        return None


# ===================================================================== TX
def main_tx(argv=None):
    p = argparse.ArgumentParser(description="N321 #1: MIMO OFDM video transmitter")
    common_args(p)
    p.add_argument("--tx-gain", dest="tx_gain", type=float)
    p.add_argument("--tx-rms", dest="tx_rms", type=float)
    p.add_argument("--interleaver", choices=("none", "block", "carrier"),
                   help="how RS bytes are spread over carriers/symbols (default block; RX follows)")
    p.add_argument("--rf-link", choices=("antenna", "cable"), default="antenna")
    p.add_argument("--attenuation-db", type=float)
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--warmup", type=float, default=5, help="IDLE seconds before video (RX locks)")
    p.add_argument("--source", choices=("testpattern", "webcam", "file"), default="testpattern")
    p.add_argument("--input")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = settings_from(a, ["tx_gain", "tx_rms", "interleaver"])
    require_ffmpeg()
    run_dir = new_run("tx", s, a.mode, a.name)
    session = secrets.randbits(32)
    cap = capacity(s)
    report = {"version": __version__, "role": "tx", "mode": a.mode, "session": session,
              "settings": s.to_dict(), "capacity": cap, "started": datetime.now().isoformat(),
              "status": "FAIL"}
    LOG.info("TX session=%d mimo=%s mod=%s interleaver=%s Fs=%.3g f=%.6g MHz, TS capacity %.0f kb/s",
             session, s.mimo_mode, s.modulation, s.interleaver, s.sample_rate, s.frequency / 1e6,
             cap["ts_capacity_bps_after_rs8_8"] / 1e3)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        tb = gr.top_block("N321 MIMO TX", catch_exceptions=True)
        src = MimoTxSource(s, session, throttle=(a.mode == "sim"))
        keep = [src]
        if a.mode == "rf":
            from .hardware import configure_direction, make_radio, validate_rf
            report["notes"] = validate_rf(s, a.rf_link, a.attenuation_db)
            usrp = make_radio(s, "tx")
            report["hardware"] = configure_direction(usrp, s, "tx")
            tb.connect((src, 0), (usrp, 0))
            tb.connect((src, 1), (usrp, 1))
            keep.append(usrp)
        else:
            from gnuradio import zeromq
            il = blocks.interleave(gr.sizeof_gr_complex, 1)
            sink = zeromq.push_sink(gr.sizeof_gr_complex, 1, s.zmq_addr, 100, False, -1)
            tb.connect((src, 0), (il, 0))
            tb.connect((src, 1), (il, 1))
            tb.connect(il, sink)
            keep += [il, sink]
            report["hardware"] = {"zmq": s.zmq_addr}
        tb._keep = keep
        tb.start()
        LOG.info("TX running: %0.f s IDLE so the RX can lock, then video", a.warmup)
        t_end = time.monotonic() + a.warmup
        while time.monotonic() < t_end and not stop.is_set():
            time.sleep(0.2)
        enc = Encoder(encoder_command(s, a.source, a.input, a.seconds), src, session, run_dir, stop)
        enc.start()
        while not stop.is_set() and not enc.done.is_set():
            time.sleep(1)
            t = src.snapshot()
            LOG.info("TX data %d | queue %d | frames %d", t["tx_enqueued_data"], t["tx_queue"], t["tx_frames"])
        while not stop.is_set() and src.snapshot()["tx_queue"] > 0:
            time.sleep(0.2)
        time.sleep(2)                        # trailing IDLE: RX closes the last group
        tb.stop()
        tb.wait()
        ts = run_dir / "video_tx.ts"
        report.update(tx=src.snapshot(), encoder_error=enc.error, video_tx_bytes=ts.stat().st_size,
                      video_tx_sha256=sha256_file(ts), video_tx_frames=ffprobe_frames(ts))
        if not enc.error and ts.stat().st_size > 0 and not stop.is_set():
            report["status"] = "TX_COMPLETE"
    except Exception as exc:          # noqa: BLE001
        report["error"] = str(exc)
        LOG.error("%s", exc)
    finally:
        report["completed"] = datetime.now().isoformat()
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    LOG.info("%s | session %d | sha256 %s | report %s", report["status"], session,
             report.get("video_tx_sha256", "-")[:16], run_dir / "report.json")
    return 0 if report["status"] == "TX_COMPLETE" else 1


# ===================================================================== RX
def main_rx(argv=None):
    p = argparse.ArgumentParser(description="N321 #2: MIMO OFDM video receiver")
    common_args(p)
    p.add_argument("--rx-gain", dest="rx_gain", type=float)
    p.add_argument("--session", type=int, help="accept only this TX session (default: first seen)")
    p.add_argument("--seconds", type=float, default=600, help="maximum listening time")
    p.add_argument("--play", action="store_true")
    p.add_argument("--no-soft", dest="soft_decoding", action="store_const", const=False,
                   help="hard-decision RS only (for comparison)")
    p.add_argument("--compare", help="TX report.json or video_tx.ts to compare (same PC or copied)")
    p.add_argument("--snr", dest="sim_snr_db", type=float)
    p.add_argument("--cfo", dest="sim_cfo_hz", type=float)
    p.add_argument("--sfo", dest="sim_sfo_ppm", type=float)
    p.add_argument("--channel", dest="sim_channel", choices=("identity", "rayleigh", "fixed"))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = settings_from(a, ["rx_gain", "sim_snr_db", "sim_cfo_hz", "sim_sfo_ppm", "sim_channel", "soft_decoding"])
    run_dir = new_run("rx", s, a.mode, a.name)
    report = {"version": __version__, "role": "rx", "mode": a.mode, "settings": s.to_dict(),
              "capacity": capacity(s), "started": datetime.now().isoformat(), "status": "FAIL"}
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    player = None
    try:
        tb = gr.top_block("N321 MIMO RX", catch_exceptions=True)
        rx = MimoRxSink(s)
        vr = VideoReceiver(a.session, run_dir / "video_rx.ts", s.udp_port)
        keep = [rx, vr]
        if a.mode == "rf":
            from .hardware import configure_direction, make_radio
            usrp = make_radio(s, "rx")
            report["hardware"] = configure_direction(usrp, s, "rx")
            tb.connect((usrp, 0), (rx, 0))
            tb.connect((usrp, 1), (rx, 1))
            keep.append(usrp)
        else:
            from gnuradio import zeromq
            srcz = zeromq.pull_source(gr.sizeof_gr_complex, 1, s.zmq_addr, 100, False, -1)
            di = blocks.deinterleave(gr.sizeof_gr_complex, 1)
            sfo = SfoResampler(s.sim_sfo_ppm)
            ch = MimoChannelSim(s)
            tb.connect(srcz, di)
            tb.connect((di, 0), (sfo, 0))
            tb.connect((di, 1), (sfo, 1))
            for i in (0, 1):
                tb.connect((sfo, i), (ch, i))
                tb.connect((ch, i), (rx, i))
            keep += [srcz, di, sfo, ch]
            report["hardware"] = {"zmq": s.zmq_addr, "sfo_ppm": s.sim_sfo_ppm, "cfo_hz": s.sim_cfo_hz,
                                  "snr_db": s.sim_snr_db, "channel": s.sim_channel}
        tb.msg_connect((rx, "pdus"), (vr, "pdus"))
        tb._keep = keep
        tb.start()
        if a.play:
            player = subprocess.Popen(["ffplay", "-hide_banner", "-loglevel", "warning", "-fflags",
                                       "nobuffer", "-flags", "low_delay", "-framedrop",
                                       f"udp://127.0.0.1:{s.udp_port}?fifo_size=100000&overrun_nonfatal=1"])
        LOG.info("RX listening (start the TX now). mimo=%s mod=%s f=%.6g MHz, Ctrl+C to stop",
                 s.mimo_mode, s.modulation, s.frequency / 1e6)
        limit = time.monotonic() + a.seconds
        ended_at = None
        while not stop.is_set() and time.monotonic() < limit:
            time.sleep(1)
            r, v = rx.snapshot(), vr.snapshot()
            LOG.info("RX ok %d (soft %d) sig %d rs %d crc %d lost %d | session %s DATA %d missing %d END %s | "
                     "SNR %s EVM %s cond %s dB | rx_rms %.4f",
                     r["ok"], r["soft_decoded"], r["sig_fail"], r["rs_fail"], r["crc_fail"], r["lost_sync"], v["session"],
                     v["data_packets"], v["missing_data_packets"], v["end_received"],
                     None if r["snr_db"] is None else round(r["snr_db"], 1),
                     None if r["evm_db"] is None else round(r["evm_db"], 1),
                     None if r["cond_db"] is None else round(r["cond_db"], 1), r["rx_rms"])
            if vr.ended.is_set():
                ended_at = ended_at or time.monotonic()
                if time.monotonic() - ended_at > 3:
                    break
        tb.stop()
        tb.wait()
        vr.close()
        r, v = rx.snapshot(), vr.snapshot()
        ts = run_dir / "video_rx.ts"
        info = ffprobe_frames(ts) if ts.stat().st_size else None
        report.update(rx=r, video=v, video_rx_bytes=ts.stat().st_size,
                      video_rx_sha256=sha256_file(ts) if ts.stat().st_size else None, video_rx_frames=info)
        if a.compare:
            from pathlib import Path
            ref = Path(a.compare)
            tx_hash = (json.loads(ref.read_text()).get("video_tx_sha256") if ref.suffix == ".json"
                       else sha256_file(ref))
            report["tx_sha256"] = tx_hash
            report["ts_identical"] = tx_hash == report["video_rx_sha256"]
        ok = v["end_received"] and v["missing_data_packets"] == 0 and info and int(info.get("nb_read_frames", 0)) > 0
        if ok and report.get("ts_identical", True):
            report["status"] = "RF_RX_VERIFIED" if a.mode == "rf" else "SIM_RX_VERIFIED"
    except Exception as exc:          # noqa: BLE001
        report["error"] = str(exc)
        LOG.error("%s", exc)
    finally:
        if player:
            player.terminate()
        report["completed"] = datetime.now().isoformat()
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    LOG.info("%s | sha256 %s | report %s", report["status"], (report.get("video_rx_sha256") or "-")[:16],
             run_dir / "report.json")
    return 0 if report["status"].endswith("VERIFIED") else 1
