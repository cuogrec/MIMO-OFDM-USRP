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
    p.add_argument("--mod", dest="modulation", choices=("bpsk", "qpsk", "16qam", "64qam"))
    p.add_argument("--acm", action="store_const", const=True,
                   help="adaptive modulation: TX picks BPSK..64QAM per frame from RX feedback (both sides)")
    p.add_argument("--acm-port", dest="acm_port", type=int, help="UDP port of the feedback channel (5003)")
    p.add_argument("--fb-link", dest="fb_link", choices=("udp", "radio"),
                   help="ACM feedback path: udp (network, default) or radio (channel B of both N321, --mimo siso)")
    p.add_argument("--fb-freq", dest="fb_freq", type=float, help="radio feedback carrier (default 922.7e6)")
    p.add_argument("--fb-rate", dest="fb_rate", type=float, help="radio feedback sample rate (default 500e3)")
    p.add_argument("--rate", dest="sample_rate", type=float)
    p.add_argument("--freq", dest="frequency", type=float)
    p.add_argument("--args", dest="device_args")
    p.add_argument("--sync", choices=("internal", "gpsdo", "external"),
                   help="clock+time source of THIS N321 (gpsdo: lock to GPS, needs antenna on GPS port)")
    p.add_argument("--zmq", dest="zmq_addr")
    p.add_argument("--name", default="")


def settings_from(a, extra):
    keys = ["mimo_mode", "modulation", "sample_rate", "frequency", "device_args", "zmq_addr",
            "acm", "acm_port", "fb_link", "fb_freq", "fb_rate"] + extra
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
    p.add_argument("--acm-modcods", dest="acm_modcods", help="ACM ladder, e.g. qpsk,16qam,64qam")
    p.add_argument("--tx-gain-profile", help="RF test of ACM: TX gain over time, e.g. steps:25,15,5,15:10 "
                   "(never above --tx-gain)")
    p.add_argument("--acm-margin", dest="acm_margin_db", type=float, help="ACM SNR margin dB (1)")
    p.add_argument("--fb-rx-gain", dest="fb_rx_gain", type=float, help="radio feedback: RX2 B (RF1) gain (60)")
    p.add_argument("--acm-resend", dest="acm_resend_s", type=float,
                   help="ACM: on a downshift resend the packets of the last max(S, 1.5 x loop) s (0.1, 0 = off)")
    p.add_argument("--adaptive-video", dest="adaptive_video", action="store_const", const=True,
                   help="video bitrate follows the ACM modulation (default with --acm)")
    p.add_argument("--no-adaptive-video", dest="adaptive_video", action="store_const", const=False,
                   help="ACM with the fixed video bitrate of config (ts_muxrate)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = settings_from(a, ["tx_gain", "tx_rms", "interleaver", "acm_modcods", "acm_margin_db", "fb_rx_gain",
                          "adaptive_video", "acm_resend_s"])
    require_ffmpeg()
    run_dir = new_run("tx", s, a.mode, a.name)
    session = secrets.randbits(32)
    cap = capacity(s)
    report = {"version": __version__, "role": "tx", "mode": a.mode, "session": session,
              "settings": s.to_dict(), "capacity": cap, "started": datetime.now().isoformat(),
              "status": "FAIL"}
    LOG.info("TX session=%d mimo=%s mod=%s interleaver=%s Fs=%.3g f=%.6g MHz, TS capacity %.0f kb/s",
             session, s.mimo_mode, "ACM " + s.acm_modcods if s.acm else s.modulation, s.interleaver,
             s.sample_rate, s.frequency / 1e6, cap["ts_capacity_bps_after_rs8_8"] / 1e3)
    ladder = None
    if s.acm:
        LOG.info("ACM TS capacity per modulation (kb/s): %s",
                 {m: round(v / 1e3) for m, v in cap["acm_ts_capacity_bps"].items()})
        if s.video_adaptive:
            from .video_adapt import video_ladder
            ladder = video_ladder(s, cap["acm_ts_capacity_bps"])
            LOG.info("Adaptive video (%.0f%% of capacity, up after %.0f s stable): %s", 100 * s.adaptive_video_fill,
                     s.adaptive_video_hold_s, ", ".join(f"{m} TS {mux // 1000} kb/s" for m, mux, _ in ladder))
        else:
            LOG.info("Fixed video TS mux %d kb/s (--no-adaptive-video)", s.ts_muxrate // 1000)
    ctl = fb = fbh = fbrx = None
    radio_fb = s.acm and s.fb_link == "radio"
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        tb = gr.top_block("N321 MIMO TX", catch_exceptions=True)
        if s.acm:
            from .acm import AcmController, FeedbackReceiver
            ctl = AcmController(s.acm_modcods, margin_db=s.acm_margin_db)
            if radio_fb:
                from .acm import RadioFeedbackHandler
                from .blocks import feedback_rx_settings
                fbh = RadioFeedbackHandler(ctl, session)
                fbrx = MimoRxSink(feedback_rx_settings(s))
                fbrx.on_packet = fbh
                LOG.info("ACM: radio feedback on %.4g MHz (RX2 B), %.3g S/s (starts with %s)",
                         s.fb_freq / 1e6, s.fb_rate, ctl.modulation)
            else:
                fb = FeedbackReceiver(ctl, s.acm_port, session)
                fb.start()
                LOG.info("ACM: waiting for RX feedback on UDP %d (starts with %s)", s.acm_port, ctl.modulation)
        src = MimoTxSource(s, session, throttle=(a.mode == "sim"), acm=ctl)
        keep = [src]
        tx_chans = 2
        if a.mode == "rf":
            from .hardware import (configure_direction, configure_streams, make_radio, make_stream,
                                   validate_rf)
            report["notes"] = validate_rf(s, a.rf_link, a.attenuation_db)
            if radio_fb:
                # channel A transmits the video, channel B (RX2) receives the feedback
                tx_chans = 1
                usrp = make_stream(s, "tx", [0])
                fusrp = make_stream(s, "rx", [1])
                report["hardware"] = configure_streams([
                    {"dev": usrp, "kind": "tx", "n": 1, "freq": s.frequency, "gain": s.tx_gain,
                     "antenna": s.tx_antenna, "rate": s.sample_rate, "name": "video_tx_A"},
                    {"dev": fusrp, "kind": "rx", "n": 1, "freq": s.fb_freq, "gain": s.fb_rx_gain,
                     "antenna": "RX2", "rate": s.fb_rate, "name": "feedback_rx_B"}], s)
                nul_tx, nul_rx = blocks.null_sink(gr.sizeof_gr_complex), blocks.null_source(gr.sizeof_gr_complex)
                tb.connect((src, 0), (usrp, 0))
                tb.connect((src, 1), nul_tx)
                tb.connect((fusrp, 0), (fbrx, 0))
                tb.connect(nul_rx, (fbrx, 1))
                keep += [usrp, fusrp, fbrx, nul_tx, nul_rx]
            else:
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
            if radio_fb:
                # simulated feedback link: RX process PUSH -> here, AWGN + CFO like a real carrier
                from dataclasses import replace
                from .blocks import feedback_rx_settings
                fs = replace(feedback_rx_settings(s), sim_channel="identity", sim_snr_db=s.sim_fb_snr_db,
                             sim_cfo_hz=1500.0, sim_snr_profile="")
                fsrc = zeromq.pull_source(gr.sizeof_gr_complex, 1, s.fb_zmq_addr, 100, False, -1)
                fnul = blocks.null_source(gr.sizeof_gr_complex)
                fch = MimoChannelSim(fs)
                tb.connect(fsrc, (fch, 0))
                tb.connect(fnul, (fch, 1))
                tb.connect((fch, 0), (fbrx, 0))
                tb.connect((fch, 1), (fbrx, 1))
                keep += [fsrc, fnul, fch, fbrx]
                report["hardware"]["feedback_zmq"] = s.fb_zmq_addr
        gain_profile = None
        if a.tx_gain_profile:
            from .blocks import parse_snr_profile
            gain_profile = parse_snr_profile(a.tx_gain_profile)
            peak = max(gain_profile(t / 10) for t in range(6000))
            if peak > s.tx_gain + 1e-9 or min(gain_profile(t / 10) for t in range(6000)) < 0:
                raise ValueError(f"--tx-gain-profile reaches {peak} dB: keep it within 0..tx_gain ({s.tx_gain})")
            if a.mode != "rf":
                LOG.warning("--tx-gain-profile only acts on the radio (use --snr-profile at the RX in sim)")
        tb._keep = keep
        tb.start()
        t_start = time.monotonic()
        LOG.info("TX running: %0.f s IDLE so the RX can lock, then video", a.warmup)
        t_end = time.monotonic() + a.warmup
        while time.monotonic() < t_end and not stop.is_set():
            time.sleep(0.2)
        if ladder:
            from .video_adapt import AdaptiveEncoder
            enc = AdaptiveEncoder(s, a.source, a.input, a.seconds, src, session, run_dir, stop, ctl, ladder)
        else:
            enc = Encoder(encoder_command(s, a.source, a.input, a.seconds), src, session, run_dir, stop)
        enc.start()
        while not stop.is_set() and not enc.done.is_set():
            time.sleep(1)
            if gain_profile is not None and a.mode == "rf":
                g = gain_profile(time.monotonic() - t_start)
                for ch in range(tx_chans):
                    usrp.set_gain(g, ch)
                LOG.info("TX gain %.1f dB (profile)", g)
            t = src.snapshot()
            if s.acm:
                LOG.info("TX data %d | queue %d | frames %d | ACM %s (RX SNR_eff %s dB, %d switches, loop %s s, "
                         "resent %d)", t["tx_enqueued_data"], t["tx_queue"], t["tx_frames"], t["acm_modulation"],
                         None if t["acm_snr_db"] is None else round(t["acm_snr_db"], 1), t["acm_switches"],
                         round(ctl.latency[-1], 2) if ctl.latency else None, t["acm_resent_packets"])
                if ladder:
                    v = enc.snapshot()
                    if v["level"] is not None:
                        LOG.info("   video %s level: TS %d kb/s, backlog %.2f s, %d switches", v["modulation"],
                                 v["ts_muxrate"] // 1000, v["backlog_s"], v["switches"])
                if fbh is not None:
                    fr = fbrx.snapshot()
                    LOG.info("   radio feedback: %d frames (%d used), link SNR %s dB, lost %d",
                             fbh.frames, fbh.used, None if fr["snr_db"] is None else round(fr["snr_db"], 1),
                             fr["lost_sync"])
            else:
                LOG.info("TX data %d | queue %d | frames %d", t["tx_enqueued_data"], t["tx_queue"], t["tx_frames"])
        while not stop.is_set() and src.snapshot()["tx_queue"] > 0:
            time.sleep(0.2)
        time.sleep(2)                        # trailing IDLE: RX closes the last group
        tb.stop()
        tb.wait()
        ts = run_dir / "video_tx.ts"
        if ctl is not None:
            if fb is not None:
                fb.close()
            lat = sorted(ctl.latency)
            report["acm"] = {"switches": ctl.switches, "reports": ctl.reports, "frames": src.snapshot()["acm_frames"],
                             "loop_latency_s_median": lat[len(lat) // 2] if lat else None,
                             "history": list(ctl.history), "fb_link": s.fb_link}
            if fbh is not None:
                report["acm"]["radio_feedback"] = {"frames": fbh.frames, "used": fbh.used, "foreign": fbh.foreign,
                                                   "rx": fbrx.snapshot()}
        if ladder:
            report["video"] = {"adaptive": True, "fill": s.adaptive_video_fill, "hold_s": s.adaptive_video_hold_s,
                               "ladder": [{"modulation": m, "ts_muxrate": mux, "video_bitrate": vb}
                                          for m, mux, vb in ladder],
                               "switches": enc.switches, "backlog_max_s": round(enc.backlog_max_s, 2),
                               "history": enc.history}
        else:
            report["video"] = {"adaptive": False, "ts_muxrate": s.ts_muxrate, "video_bitrate": s.video_bitrate}
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
    p.add_argument("--gui", action="store_true",
                   help="window with the constellation (after equalisation) and the RX spectrum")
    p.add_argument("--const-every", dest="const_every", type=int,
                   help="one constellation snapshot every N good frames (default 8, 0 = off)")
    p.add_argument("--snr", dest="sim_snr_db", type=float)
    p.add_argument("--cfo", dest="sim_cfo_hz", type=float)
    p.add_argument("--sfo", dest="sim_sfo_ppm", type=float)
    p.add_argument("--channel", dest="sim_channel", choices=("identity", "rayleigh", "fixed"))
    p.add_argument("--snr-profile", dest="sim_snr_profile",
                   help="SIM: time-varying SNR, e.g. sine:6:30:40 or steps:28,18,8,14:10")
    p.add_argument("--acm-host", dest="acm_host", help="ACM: IP of the TX PC (default 127.0.0.1)")
    p.add_argument("--fb-delay", dest="acm_fb_delay_s", type=float,
                   help="ACM: extra feedback delay in s (0.25-0.5 emulates a GEO satellite)")
    p.add_argument("--fb-tx-gain", dest="fb_tx_gain", type=float, help="radio feedback: TX/RX B (RF1) gain (30)")
    p.add_argument("--reorder", dest="rx_reorder_s", type=float,
                   help="reorder buffer before RS8+8 in s (default 0.3 with --acm, 0 = off)")
    p.add_argument("--fb-snr", dest="sim_fb_snr_db", type=float, help="SIM: SNR of the feedback link (20)")
    p.add_argument("--rf-link", choices=("antenna", "cable"), default="antenna",
                   help="radio feedback transmitter: antenna (gain <= 30) or cable (+ --attenuation-db)")
    p.add_argument("--attenuation-db", type=float)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = settings_from(a, ["rx_gain", "sim_snr_db", "sim_cfo_hz", "sim_sfo_ppm", "sim_channel", "soft_decoding",
                                "const_every", "sim_snr_profile", "acm_host", "acm_fb_delay_s",
                                "fb_tx_gain", "sim_fb_snr_db", "rx_reorder_s"])
    run_dir = new_run("rx", s, a.mode, a.name)
    report = {"version": __version__, "role": "rx", "mode": a.mode, "settings": s.to_dict(),
              "capacity": capacity(s), "started": datetime.now().isoformat(), "status": "FAIL"}
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    player = None
    gui = app = None
    ch_sim = None
    fbsrc = None
    radio_fb = s.acm and s.fb_link == "radio"
    try:
        if a.gui:
            from .rxgui import RxGui, ensure_app
            app = ensure_app()               # QApplication must exist before any QT sink
        tb = gr.top_block("N321 MIMO RX", catch_exceptions=True)
        rx = MimoRxSink(s)
        vr = VideoReceiver(a.session, run_dir / "video_rx.ts", s.udp_port, reorder_s=s.reorder_s)
        keep = [rx, vr]
        if radio_fb:
            from .acm import FeedbackReporter
            from .blocks import FeedbackRadioSource
            reporter = FeedbackReporter(rx, vr, s.acm_fb_delay_s)
            fbsrc = FeedbackRadioSource(s, reporter.packet, throttle=(a.mode == "sim"))
            keep.append(fbsrc)
        if a.mode == "rf":
            from .hardware import (configure_direction, configure_streams, make_radio, make_stream,
                                   validate_tx_gain)
            if radio_fb:
                # channel A (RX2) receives the video, channel B (TX/RX) transmits the feedback
                validate_tx_gain(s.fb_tx_gain, a.rf_link, a.attenuation_db)
                usrp = make_stream(s, "rx", [0])
                fusrp = make_stream(s, "tx", [1])
                report["hardware"] = configure_streams([
                    {"dev": usrp, "kind": "rx", "n": 1, "freq": s.frequency, "gain": s.rx_gain,
                     "antenna": s.rx_antenna, "rate": s.sample_rate, "name": "video_rx_A"},
                    {"dev": fusrp, "kind": "tx", "n": 1, "freq": s.fb_freq, "gain": s.fb_tx_gain,
                     "antenna": "TX/RX", "rate": s.fb_rate, "name": "feedback_tx_B"}], s)
                nul = blocks.null_source(gr.sizeof_gr_complex)
                tb.connect((usrp, 0), (rx, 0))
                tb.connect(nul, (rx, 1))
                tb.connect(fbsrc, (fusrp, 0))
                keep += [usrp, fusrp, nul]
                iq = [(usrp, 0), (nul, 0)]
            else:
                usrp = make_radio(s, "rx")
                report["hardware"] = configure_direction(usrp, s, "rx")
                tb.connect((usrp, 0), (rx, 0))
                tb.connect((usrp, 1), (rx, 1))
                keep.append(usrp)
                iq = [(usrp, 0), (usrp, 1)]
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
            ch_sim = ch
            iq = [(ch, 0), (ch, 1)]
            report["hardware"] = {"zmq": s.zmq_addr, "sfo_ppm": s.sim_sfo_ppm, "cfo_hz": s.sim_cfo_hz,
                                  "snr_db": s.sim_snr_db, "channel": s.sim_channel}
            if radio_fb:
                fpush = zeromq.push_sink(gr.sizeof_gr_complex, 1, s.fb_zmq_addr, 100, False, -1)
                tb.connect(fbsrc, fpush)
                keep.append(fpush)
                report["hardware"]["feedback_zmq"] = s.fb_zmq_addr
        tb.msg_connect((rx, "pdus"), (vr, "pdus"))
        if a.gui:
            gui = RxGui(tb, rx, iq, s)
            keep += gui.blocks
        tb._keep = keep
        tb.start()
        fbs = None
        if radio_fb:
            LOG.info("ACM: radio feedback on %.4g MHz (TX/RX B, gain %.0f dB), %.3g S/s",
                     s.fb_freq / 1e6, s.fb_tx_gain, s.fb_rate)
        elif s.acm:
            from .acm import FeedbackSender
            fbs = FeedbackSender(rx, vr, s.acm_host, s.acm_port, delay_s=s.acm_fb_delay_s)
            fbs.start()
            LOG.info("ACM: feedback -> %s:%d every %.2f s (extra delay %.2f s)", s.acm_host, s.acm_port, fbs.period,
                     s.acm_fb_delay_s)
        if a.play:
            player = subprocess.Popen(["ffplay", "-hide_banner", "-loglevel", "warning", "-fflags",
                                       "nobuffer", "-flags", "low_delay", "-framedrop",
                                       # small probe: ffplay starts at the first keyframe instead of
                                       # analysing ~5 s of stream first (that video was never shown)
                                       "-probesize", "32768", "-analyzeduration", "500000",
                                       f"udp://127.0.0.1:{s.udp_port}?fifo_size=100000&overrun_nonfatal=1"])
        LOG.info("RX listening (start the TX now). mimo=%s mod=%s f=%.6g MHz, Ctrl+C to stop",
                 s.mimo_mode, s.modulation, s.frequency / 1e6)
        limit = time.monotonic() + a.seconds
        state = {"ended_at": None}

        def tick():
            """One monitor step (every second). Returns False when the RX should stop."""
            vr.poll()
            r, v = rx.snapshot(), vr.snapshot()
            line = ("RX ok %d (soft %d) sig %d rs %d crc %d lost %d | session %s DATA %d missing %d END %s | "
                    "SNR %s EVM %s cond %s dB | rx_rms %.4f" % (
                        r["ok"], r["soft_decoded"], r["sig_fail"], r["rs_fail"], r["crc_fail"], r["lost_sync"],
                        v["session"], v["data_packets"], v["missing_data_packets"], v["end_received"],
                        None if r["snr_db"] is None else round(r["snr_db"], 1),
                        None if r["evm_db"] is None else round(r["evm_db"], 1),
                        None if r["cond_db"] is None else round(r["cond_db"], 1), r["rx_rms"]))
            if s.acm:
                line += " | ACM %s SNR_eff %s dB" % (r["modulation"], None if r["snr_eff_db"] is None
                                                      else round(r["snr_eff_db"], 1))
                if fbsrc is not None:
                    line += " | fb %d frames" % fbsrc.frames
                if a.mode == "sim" and ch_sim is not None and ch_sim.profile is not None:
                    line += " (sim SNR %.1f)" % ch_sim.snr_now
            LOG.info("%s", line)
            if gui is not None:
                gui.set_status(line.replace(" | ", "\n"))
            if stop.is_set() or time.monotonic() >= limit:
                return False
            if vr.ended.is_set():
                state["ended_at"] = state["ended_at"] or time.monotonic()
                if time.monotonic() - state["ended_at"] > 3:
                    return False
            return True

        if gui is None:
            while not stop.is_set() and time.monotonic() < limit:
                time.sleep(1)
                if not tick():
                    break
        else:
            from PyQt5 import Qt
            gui.show()
            timer = Qt.QTimer()
            timer.timeout.connect(lambda: tick() or app.quit())
            timer.start(1000)
            app.lastWindowClosed.connect(app.quit)          # closing the window stops the RX
            pulse = Qt.QTimer()                               # let Python see Ctrl+C while Qt runs
            pulse.timeout.connect(lambda: stop.is_set() and app.quit())
            pulse.start(200)
            app.exec_()
            timer.stop()
            pulse.stop()
        if fbs is not None:
            fbs.close()
        tb.stop()
        tb.wait()
        vr.close()
        r, v = rx.snapshot(), vr.snapshot()
        ts = run_dir / "video_rx.ts"
        info = ffprobe_frames(ts) if ts.stat().st_size else None
        if rx.last_const is not None:              # last constellation snapshot, for offline plots
            import numpy as np
            np.save(run_dir / "const_last.npy", rx.last_const)
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
