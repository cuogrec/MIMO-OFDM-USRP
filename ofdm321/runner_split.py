"""Two N321 (or two processes): role 'tx' or 'rx'. Each role opens only its own radio.

TX: FFmpeg -> DATA/RS8_PARITY/END -> MIMO OFDM (2 channels) -> N321 #1 (RF0 TX/RX, RF1 TX/RX)
RX: N321 #2 (RF0 RX2, RF1 RX2) -> MIMO OFDM RX -> verified RS8+8 -> video_rx.ts + UDP -> ffplay
Port names follow the N321 front panel: RF0 = UHD channel 0 (A:0), RF1 = channel 1 (B:0).
--mode sim replaces the air link with ZeroMQ (TX PUSH -> RX PULL) plus, at the RX,
a sample-clock offset, a 2x2 channel, noise and CFO: the same impairments as two
separate radios with independent clocks.
"""
import argparse
from datetime import datetime
import json
import logging
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time

from gnuradio import blocks, gr

from . import __version__
from .blocks import (AirClock, MimoChannelSim, MimoRxSink, MimoTxSource, SfoResampler, TxDeviceSim,  # noqa: E501
                     UhdAsyncMonitor, VideoReceiver)
from .config import app_root, load_settings
from .media import encoder_command, require_ffmpeg, sha256_file
from .protocol import END, pack_packet
from .runner import Encoder, capacity

LOG = logging.getLogger("ofdm321")


def common_args(p):
    p.add_argument("--mode", choices=("rf", "sim"), default="rf")
    p.add_argument("--config", default=str(app_root() / "config/default.json"))
    p.add_argument("--mimo", dest="mimo_mode", choices=("siso", "simo", "alamouti", "smux"))
    p.add_argument("--mod", dest="modulation", choices=("bpsk", "qpsk", "16qam", "64qam"))
    p.add_argument("--acm", action="store_const", const=True,
                   help="adaptive modulation: TX picks BPSK..64QAM per frame from RX feedback (both sides)")
    p.add_argument("--acm-port", dest="acm_port", type=int, help="UDP port of the feedback channel (5003)")
    p.add_argument("--fb-link", dest="fb_link", choices=("udp", "radio"),
                   help="ACM feedback path: udp (network, default) or radio (RF1 of both N321, --mimo siso)")
    p.add_argument("--fb-freq", dest="fb_freq", type=float, help="radio feedback carrier (default 922.7e6)")
    p.add_argument("--fb-rate", dest="fb_rate", type=float, help="radio feedback sample rate (default 500e3)")
    p.add_argument("--fec", choices=("rs", "ldpc"),
                   help="inner code, the SAME on both sides: rs (RS(248,216), default) or ldpc (802.11n QC-LDPC)")
    p.add_argument("--ldpc-rate", dest="ldpc_rate", choices=("1/2", "2/3", "3/4", "5/6"),
                   help="LDPC code rate of fixed-modulation frames (3/4); ACM frames always use 5/6 punctured")
    p.add_argument("--rate", dest="sample_rate", type=float)
    p.add_argument("--freq", dest="frequency", type=float)
    p.add_argument("--args", dest="device_args")
    p.add_argument("--sync", choices=("internal", "gpsdo", "external"),
                   help="clock+time source of THIS N321 (gpsdo: lock to GPS, needs antenna on GPS port)")
    p.add_argument("--zmq", dest="zmq_addr")
    p.add_argument("--tx-lead-ms", dest="tx_lead_ms", type=float,
                   help="TX sources (video TX, radio feedback on the RX PC) build samples at most this far ahead "
                        "of the air time (50; 0 = v0.6 behaviour: fill the USRP buffers, ~0.4 s ACM loop)")
    p.add_argument("--no-tx-resync", dest="tx_resync", action="store_const", const=False,
                   help="v0.6.1: a TX source that falls behind its air timeline stays late (UHD 'L' forever)")
    p.add_argument("--sim-stall", dest="sim_stall",
                   help="SIM: random stalls of the TX source RATE:MIN_MS:MAX_MS[:gil], e.g. 0.5:20:150:gil")
    p.add_argument("--sim-dev-buffer", dest="sim_tx_device_samples", type=int,
                   help="SIM: emulated USRP TX buffer in samples per stream (131072, like the RF loop delay)")
    p.add_argument("--name", default="")


def settings_from(a, extra):
    keys = ["mimo_mode", "modulation", "sample_rate", "frequency", "device_args", "zmq_addr",
            "acm", "acm_port", "fb_link", "fb_freq", "fb_rate", "fec", "ldpc_rate", "tx_lead_ms",
            "sim_tx_device_samples", "tx_resync", "sim_stall"] + extra
    over = {k: getattr(a, k) for k in keys}
    if a.sync:
        over.update(clock_source=a.sync, time_source=a.sync)
    return load_settings(a.config, **over)


def new_run(role, s, mode, name):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = app_root() / "runs" / f"{stamp}_{role}_{mode}_{s.mimo_mode}_{s.modulation}{('_' + name) if name else ''}"
    run_dir.mkdir(parents=True)
    (app_root() / "runs/latest_run.txt").write_text(str(run_dir) + "\n")
    return run_dir


class TxEndWatchdog:
    """The TX must always end (RF 25/11: after late packets it ran until Ctrl+C). Abort reasons: past the
    deadline (warm-up + --seconds + 30 s), or, once the video is done, a TX queue that has not shrunk for
    `stuck_s` seconds."""

    def __init__(self, deadline, stuck_s=10.0):
        self.deadline, self.stuck_s = deadline, stuck_s
        self.last_q = self.last_t = None

    def check(self, now, queue, video_done):
        if now > self.deadline:
            return f"quá thời hạn (warm-up + --seconds + 30 s), hàng đợi còn {queue} gói"
        if video_done and queue > 0:
            if self.last_q is None or queue < self.last_q:
                self.last_q, self.last_t = queue, now
            elif now - self.last_t > self.stuck_s:
                return f"hàng đợi phát không giảm trong {self.stuck_s:.0f} s sau khi hết video ({queue} gói)"
        else:
            self.last_q = self.last_t = None
        return None


def stop_flowgraph(tb, timeout=5.0):
    """tb.stop() + tb.wait() in a thread; False if they did not return within `timeout` s (a block stuck)."""
    done = threading.Event()

    def run():
        try:
            tb.stop()
            tb.wait()
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    return done.wait(timeout)


def run_medians(history):
    """Medians of rx_rms / snr_db / evm_db / snr_eff_db over the seconds of `history` (RX rows) in which
    frames were decoded (the `ok` counter grew). The values at stop time are often taken after the TX
    has ended (noise only), so the report uses these medians instead."""
    from statistics import median
    rows, prev = [], 0
    for h in history:
        if h.get("ok") is not None and h["ok"] > prev:
            rows.append(h)
        prev = h.get("ok") or prev
    out = {"seconds": len(rows)}
    for k in ("rx_rms", "snr_db", "evm_db", "snr_eff_db", "rx_rms_rf0", "rx_rms_rf1"):
        vals = [h[k] for h in rows if h.get(k) is not None]
        out[k] = round(median(vals), 6 if k.startswith("rx_rms") else 2) if vals else None
    if out.get("rx_rms_rf0") and out.get("rx_rms_rf1"):        # branch imbalance (alamouti/smux): weak RF1?
        import math
        out["rf1_vs_rf0_db"] = round(20 * math.log10(out["rx_rms_rf1"] / out["rx_rms_rf0"]), 1)
    return out


def apply_run_medians(rx_snapshot, history):
    """report["rx"]: rx_rms / snr_db / evm_db = medians while frames were decoded; *_final = at stop."""
    r = dict(rx_snapshot)
    med = run_medians(history)
    for k in ("rx_rms", "snr_db", "evm_db"):
        r[k + "_final"] = r.get(k)
        if med[k] is not None:
            r[k] = med[k]
    r["median_during_run"] = med
    return r


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
    p.add_argument("--tx-gain-profile", dest="tx_gain_profile", help="RF test of ACM: TX gain over time, e.g. steps:25,15,5,15:10 "
                   "(never above --tx-gain)")
    p.add_argument("--acm-margin", dest="acm_margin_db", type=float, help="ACM SNR margin dB (1)")
    p.add_argument("--no-acm-debounce", dest="acm_debounce", action="store_const", const=False,
                   help="v0.6.0 ACM controller: step down on a single report below the threshold")
    p.add_argument("--fb-rx-gain", dest="fb_rx_gain", type=float, help="radio feedback: RF1 RX2 gain (60)")
    p.add_argument("--acm-resend", dest="acm_resend_s", type=float,
                   help="ACM: on a downshift resend the packets of the last max(S, 1.5 x loop) s (0.1, 0 = off)")
    p.add_argument("--adaptive-video", dest="adaptive_video", action="store_const", const=True,
                   help="video bitrate follows the ACM modulation (default with --acm)")
    p.add_argument("--no-adaptive-video", dest="adaptive_video", action="store_const", const=False,
                   help="ACM with the fixed video bitrate of config (ts_muxrate)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = settings_from(a, ["tx_gain", "tx_gain_profile", "tx_rms", "interleaver", "acm_modcods", "acm_margin_db",
                          "fb_rx_gain", "acm_debounce",
                          "adaptive_video", "acm_resend_s"])
    require_ffmpeg()
    run_dir = new_run("tx", s, a.mode, a.name)
    session = secrets.randbits(32)
    cap = capacity(s)
    report = {"version": __version__, "role": "tx", "mode": a.mode, "session": session,
              "settings": s.to_dict(), "capacity": cap, "started": datetime.now().isoformat(),
              "status": "FAIL"}
    LOG.info("TX session=%d mimo=%s mod=%s interleaver=%s fec=%s Fs=%.3g f=%.6g MHz, TS capacity %.0f kb/s",
             session, s.mimo_mode, "ACM " + s.acm_modcods if s.acm else s.modulation, s.interleaver,
             cap["fec"], s.sample_rate, s.frequency / 1e6, cap["ts_capacity_bps_after_rs8_8"] / 1e3)
    if not s.acm and s.ts_muxrate > cap["ts_capacity_bps_after_rs8_8"]:
        LOG.warning("TS mux %d kb/s > TS capacity %.0f kb/s (%s): the queue will overflow; lower ts_muxrate, "
                    "raise --rate or use a higher --ldpc-rate", s.ts_muxrate // 1000,
                    cap["ts_capacity_bps_after_rs8_8"] / 1e3, cap["fec"])
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
    from .hardware import antenna_summary
    report["antennas"] = antenna_summary(s, "tx")
    LOG.info("Cắm anten (máy phát%s): %s", "" if a.mode == "rf" else ", mô phỏng", report["antennas"])
    ctl = fb = fbh = fbrx = None
    radio_fb = s.acm and s.fb_link == "radio"
    stop = threading.Event()
    state = {"abort": None}

    def on_sigint(*_):
        if stop.is_set():                    # second Ctrl+C: write what we have and leave now
            report.update(status="TX_ABORTED", abort_reason=(state["abort"] or "Ctrl+C") + " (Ctrl+C lần 2)",
                          completed=datetime.now().isoformat())
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
            os._exit(130)
        state["abort"] = state["abort"] or "Ctrl+C"
        stop.set()
    signal.signal(signal.SIGINT, on_sigint)
    try:
        tb = gr.top_block("N321 MIMO TX", catch_exceptions=True)
        if s.acm:
            from .acm import AcmController, FeedbackReceiver
            ctl = AcmController(s.acm_modcods, margin_db=s.acm_margin_db, fec=s.fec, debounce=s.acm_debounce,
                                down_reports=s.acm_down_reports, down_hold_s=s.acm_down_hold_s,
                                up_guard_s=s.acm_up_guard_s)
            if radio_fb:
                from .acm import RadioFeedbackHandler
                from .blocks import feedback_rx_settings
                fbh = RadioFeedbackHandler(ctl, session)
                fbrx = MimoRxSink(feedback_rx_settings(s))
                fbrx.on_packet = fbh
                LOG.info("ACM: radio feedback on %.4g MHz (RF1 RX2, gain %.0f dB), %.3g S/s (starts with %s)",
                         s.fb_freq / 1e6, s.fb_rx_gain, s.fb_rate, ctl.modulation)
            else:
                fb = FeedbackReceiver(ctl, s.acm_port, session)
                fb.start()
                LOG.info("ACM: waiting for RX feedback on UDP %d (starts with %s)", s.acm_port, ctl.modulation)
        from .blocks import AirClock, TxDeviceSim, _stats_ms
        vclock = AirClock(s.sample_rate)
        src = MimoTxSource(s, session, acm=ctl, clock=vclock, lead_s=s.tx_lead_ms / 1e3, resync=s.tx_resync,
                           stall=s.sim_stall if a.mode == "sim" else None)
        keep = [src]
        dev_sim = uhd_mon = None
        resync = []                          # RF: (usrp, AirClock) refined once a second
        tx_chans = 2
        if a.mode == "rf":
            from .hardware import (configure_direction, configure_streams, make_radio, make_stream,
                                   validate_rf)
            report["notes"] = validate_rf(s, a.rf_link, a.attenuation_db)
            if radio_fb:
                # RF0 TX/RX transmits the video, RF1 RX2 receives the feedback
                tx_chans = 1
                usrp = make_stream(s, "tx", [0])
                fusrp = make_stream(s, "rx", [1])
                report["hardware"] = configure_streams([
                    {"dev": usrp, "kind": "tx", "n": 1, "freq": s.frequency, "gain": s.tx_gain,
                     "antenna": s.tx_antenna, "rate": s.sample_rate, "name": "video_tx_RF0", "chans": [0]},
                    {"dev": fusrp, "kind": "rx", "n": 1, "freq": s.fb_freq, "gain": s.fb_rx_gain,
                     "antenna": "RX2", "rate": s.fb_rate, "name": "feedback_rx_RF1", "chans": [1]}], s)
                nul_tx, nul_rx = blocks.null_sink(gr.sizeof_gr_complex), blocks.null_source(gr.sizeof_gr_complex)
                tb.connect((src, 0), (usrp, 0))
                tb.connect((src, 1), nul_tx)
                tb.connect((fusrp, 0), (fbrx, 0))
                tb.connect(nul_rx, (fbrx, 1))
                keep += [usrp, fusrp, fbrx, nul_tx, nul_rx]
                fbrx.clock = AirClock(s.fb_rate)
                fbrx.clock.start(report["hardware"]["t_set_monotonic"], s.start_delay_s)
                resync.append((fusrp, fbrx.clock))
            else:
                usrp = make_radio(s, "tx")
                report["hardware"] = configure_direction(usrp, s, "tx")
                tb.connect((src, 0), (usrp, 0))
                tb.connect((src, 1), (usrp, 1))
                keep.append(usrp)
            vclock.start(report["hardware"]["t_set_monotonic"], s.start_delay_s)
            resync.append((usrp, vclock))
            uhd_mon = UhdAsyncMonitor(on_late=src.late_hint)
            tb.msg_connect((usrp, "async_msgs"), (uhd_mon, "in"))
            keep.append(uhd_mon)
            src.set_max_output_buffer(s.tx_buffer_samples)
        else:
            from gnuradio import zeromq
            # USRP emulation: real-time output, and a buffer in front of it as large as the device's
            dev_sim = TxDeviceSim(2, s.sample_rate, vclock, on_late=src.late_hint,
                                  source=src)
            if s.sim_tx_device_samples:
                src.set_min_output_buffer(s.sim_tx_device_samples)
            il = blocks.interleave(gr.sizeof_gr_complex, 1)
            sink = zeromq.push_sink(gr.sizeof_gr_complex, 1, s.zmq_addr, 100, False, -1)
            for i in (0, 1):
                tb.connect((src, i), (dev_sim, i))
                tb.connect((dev_sim, i), (il, i))
            tb.connect(il, sink)
            keep += [il, sink, dev_sim]
            report["hardware"] = {"zmq": s.zmq_addr, "sim_tx_device_samples": s.sim_tx_device_samples}
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
        if s.tx_gain_profile:                # recorded in report["settings"]
            from .blocks import parse_snr_profile
            gain_profile = parse_snr_profile(s.tx_gain_profile)
            peak = max(gain_profile(t / 10) for t in range(6000))
            if peak > s.tx_gain + 1e-9 or min(gain_profile(t / 10) for t in range(6000)) < 0:
                raise ValueError(f"--tx-gain-profile reaches {peak} dB: keep it within 0..tx_gain ({s.tx_gain})")
            if a.mode != "rf":
                LOG.warning("--tx-gain-profile only acts on the radio (use --snr-profile at the RX in sim)")
        tb._keep = keep
        tb.start()
        t_start = time.monotonic()
        report["clock"] = {"monotonic": t_start, "time": time.time()}   # maps acm.history times to wall clock
        history = report["history"] = []
        fb_prev = [0]
        watchdog = None
        if fbh is not None:
            from .acm import FeedbackWatchdog
            watchdog = FeedbackWatchdog()

        def make_encoder():
            if ladder:
                from .video_adapt import AdaptiveEncoder
                return AdaptiveEncoder(s, a.source, a.input, a.seconds, src, session, run_dir, stop, ctl, ladder)
            return Encoder(encoder_command(s, a.source, a.input, a.seconds), src, session, run_dir, stop)

        def tick(now, enc):
            """Once a second: log line(s) + one row of report["history"]."""
            t = src.snapshot()
            row = {"t": round(now - t_start, 2), "t_wall": round(time.time(), 2), "data": t["tx_enqueued_data"],
                   "queue": t["tx_queue"], "frames": t["tx_frames"], "video": enc is not None}
            if s.acm:
                LOG.info("TX data %d | queue %d | frames %d | ACM %s (RX SNR_eff %s dB, %d switches, loop %s s, "
                         "resent %d)", t["tx_enqueued_data"], t["tx_queue"], t["tx_frames"], t["acm_modulation"],
                         None if t["acm_snr_db"] is None else round(t["acm_snr_db"], 1), t["acm_switches"],
                         round(ctl.latency[-1], 2) if ctl.latency else None, t["acm_resent_packets"])
                row.update(modulation=t["acm_modulation"], acm_snr_db=t["acm_snr_db"],
                           resent=t["acm_resent_packets"], loop_s=round(ctl.latency[-1], 3) if ctl.latency else None)
            else:
                LOG.info("TX data %d | queue %d | frames %d", t["tx_enqueued_data"], t["tx_queue"], t["tx_frames"])
                row["modulation"] = s.modulation
            if enc is not None:
                if ladder:
                    v = enc.snapshot()
                    if v["level"] is not None:
                        LOG.info("   video %s level: TS %d kb/s, backlog %.2f s, %d switches", v["modulation"],
                                 v["ts_muxrate"] // 1000, v["backlog_s"], v["switches"])
                        row.update(video_ts_kbps=v["ts_muxrate"] / 1e3, video_kbps=v["video_bitrate"] / 1e3,
                                   video_backlog_s=v["backlog_s"])
                else:
                    row.update(video_ts_kbps=s.ts_muxrate / 1e3, video_kbps=s.video_bitrate / 1e3)
            if fbh is not None:
                fr = fbrx.snapshot()
                LOG.info("   radio feedback: %d frames (%d used), link SNR %s dB, lost %d",
                         fbh.frames, fbh.used, None if fr["snr_db"] is None else round(fr["snr_db"], 1),
                         fr["lost_sync"])
                row.update(fb_frames=fbh.frames, fb_frames_per_s=fbh.frames - fb_prev[0], fb_used=fbh.used,
                           fb_snr_db=fr["snr_db"], fb_lost_sync=fr["lost_sync"])
                fb_prev[0] = fbh.frames
                state = watchdog.check(now - t_start, fbh.frames)
                if state == "warn":
                    LOG.warning("%s (%.0f s, 0 khung phản hồi)", watchdog.MESSAGE, now - t_start)
                elif state == "ok":
                    LOG.info("Đã nhận phản hồi radio sau %.1f s (link SNR %s dB)", watchdog.first_frame_s,
                             None if fr["snr_db"] is None else round(fr["snr_db"], 1))
            lat = src.latency_tick()
            row["tx_buffer_ms"] = lat["median_ms"] if lat else None
            row["tx_late_events"], row["tx_reanchors"] = src.late_events, vclock.reanchors
            row["tx_work_p99_ms"] = src.work_tick_stats()
            if row["tx_work_p99_ms"] is not None and src.lead_s > 0 and row["tx_work_p99_ms"] > 500 * src.lead_s:
                LOG.warning("work() p99 %.0f ms > lead/2 (%.0f ms): nguồn phát có thể trễ (UHD 'L'); tăng --tx-lead-ms",
                            row["tx_work_p99_ms"], 500 * src.lead_s)
            if fbrx is not None:
                fl = fbrx.latency_tick()
                row["fb_rx_proc_ms"] = fl["proc"]["median_ms"] if fl["proc"] else None
                row["fb_rx_proc_p95_ms"] = fl["proc"]["p95_ms"] if fl["proc"] else None
                row["fb_rx_work_p95_ms"] = fl["work"]["p95_ms"] if fl["work"] else None
                row["fb_rx_cpu"] = round(fl["work_s"], 3)          # s of work() per s: CPU/GIL share
                row["acm_timeouts"] = ctl.timeouts
                expect = s.fb_rate / fbrx.frame_len
                if row.get("fb_frames_per_s") is not None and now - t_start > 5 and row["fb_frames_per_s"] < 0.5 * expect:
                    LOG.warning("Phản hồi radio yếu: %d khung/s giải được (cần ~%.0f); xử lý %s ms (p95 %s), work %.0f%% "
                                "thời gian -> ACM có thể không nhận được báo cáo", row["fb_frames_per_s"], expect,
                                row["fb_rx_proc_ms"], row["fb_rx_proc_p95_ms"], 100 * row["fb_rx_cpu"])
                row["fb_rx_ingest_ms"] = fl["ingest"]["median_ms"] if fl["ingest"] else None
            if lat:
                LOG.info("   latency: TX buffer %.0f ms (p95 %.0f)%s", lat["median_ms"], lat["p95_ms"],
                         "" if fbrx is None or row["fb_rx_proc_ms"] is None else
                         f", feedback RX processing {row['fb_rx_proc_ms']:.0f} ms")
            history.append(row)

        LOG.info("TX running: %0.f s IDLE so the RX can lock, then video", a.warmup)
        enc = None
        next_tick = t_start + 1
        watchdog_end = TxEndWatchdog(t_start + a.warmup + a.seconds + 30)
        while not stop.is_set() and not (enc is not None and enc.done.is_set()):
            time.sleep(0.2)
            now = time.monotonic()
            why = watchdog_end.check(now, src.snapshot()["tx_queue"], False)
            if why:
                state["abort"] = why
                break
            if enc is None and now - t_start >= a.warmup:
                enc = make_encoder()
                enc.start()
            if now >= next_tick:
                next_tick += 1
                for dev, clk in resync:
                    try:
                        clk.sync_device(dev.get_time_now().get_real_secs(), time.monotonic())
                    except Exception:          # noqa: BLE001
                        pass
                if gain_profile is not None and a.mode == "rf":
                    g = gain_profile(now - t_start)
                    for ch in range(tx_chans):
                        usrp.set_gain(g, ch)
                    LOG.info("TX gain %.1f dB (profile)", g)
                tick(now, enc)
        if enc is None and state["abort"] is None and not stop.is_set():
            enc = make_encoder()             # (cannot happen: the loop starts it after the warm-up)
            enc.start()
            enc.done.wait(15)
        while state["abort"] is None and not stop.is_set() and src.snapshot()["tx_queue"] > 0:
            time.sleep(0.2)
            why = watchdog_end.check(time.monotonic(), src.snapshot()["tx_queue"], True)
            if why:
                state["abort"] = why
        if state["abort"] is not None:
            LOG.error("TX dừng: %s", state["abort"])
            if state["abort"] != "Ctrl+C" and enc is not None:
                # END packets so the RX closes the session; the queue content is given up
                with src.packets.mutex:
                    src.packets.queue.clear()
                for _ in range(12):
                    try:
                        src.packets.put_nowait(pack_packet(END, session, enc.count))
                    except queue.Full:
                        break
                time.sleep(1.0)
            report.update(status="TX_ABORTED", abort_reason=state["abort"])
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
        else:
            time.sleep(2)                    # trailing IDLE: RX closes the last group
        if not stop_flowgraph(tb):
            LOG.error("flowgraph không dừng trong 5 s (khối bị kẹt): ghi report và thoát")
            report.update(status="TX_ABORTED", abort_reason=(state["abort"] or "") + "; flowgraph kẹt khi dừng",
                          completed=datetime.now().isoformat(), tx=src.snapshot(), latency={"tx_video": src.latency_report()})
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
            os._exit(3)
        ts = run_dir / "video_tx.ts"
        if ctl is not None:
            if fb is not None:
                fb.close()
            lat = sorted(ctl.latency)
            report["acm"] = {"switches": ctl.switches, "reports": ctl.reports, "frames": src.snapshot()["acm_frames"],
                             "debounce": s.acm_debounce, "ignored_dips": ctl.ignored_dips, "timeouts": ctl.timeouts,
                             "loop_latency_s_median": lat[len(lat) // 2] if lat else None,
                             "history": list(ctl.history), "fb_link": s.fb_link}
            if fbh is not None:
                fsnr = [r["fb_snr_db"] for r in history if r.get("fb_snr_db") is not None]
                report["acm"]["radio_feedback"] = {
                    "frames": fbh.frames, "used": fbh.used, "foreign": fbh.foreign, "rx": fbrx.snapshot(),
                    "first_frame_s": watchdog.first_frame_s, "no_feedback_warnings": watchdog.warnings,
                    "link_snr_db": {"min": min(fsnr), "median": sorted(fsnr)[len(fsnr) // 2], "max": max(fsnr)}
                    if fsnr else None}
        if ladder:
            report["video"] = {"adaptive": True, "fill": s.adaptive_video_fill, "hold_s": s.adaptive_video_hold_s,
                               "ladder": [{"modulation": m, "ts_muxrate": mux, "video_bitrate": vb}
                                          for m, mux, vb in ladder],
                               "switches": enc.switches, "backlog_max_s": round(enc.backlog_max_s, 2),
                               "history": enc.history}
        else:
            report["video"] = {"adaptive": False, "ts_muxrate": s.ts_muxrate, "video_bitrate": s.video_bitrate}
        report["latency"] = {
            "tx_video": src.latency_report(),
            "acm_switch_to_air": _stats_ms(list(src.switch_air)) if ctl is not None else None,
            "acm_loop": _stats_ms(list(ctl.latency)) if ctl is not None else None,
            "feedback_rx": dict(fbrx.latency_report(), frames_per_s={
                "min": min(fps), "median": sorted(fps)[len(fps) // 2], "expected": round(s.fb_rate / fbrx.frame_len)}
                if (fps := [r["fb_frames_per_s"] for r in history[5:] if r.get("fb_frames_per_s") is not None])
                else None) if fbrx is not None else None,
            "clock": {"resyncs": vclock.resyncs, "max_correction_ms": round(1e3 * vclock.max_correction, 2)},
            "uhd_async": dict(uhd_mon.counts) if uhd_mon is not None else None,
            "sim_device_late": dev_sim.late_events if dev_sim is not None else None,
            "sim_device_dropped": dev_sim.dropped if dev_sim is not None else None,
            "tx_buffer_samples": s.tx_buffer_samples if a.mode == "rf" else None,
            "sim_tx_device_samples": s.sim_tx_device_samples if a.mode == "sim" else None}
        report.update(tx=src.snapshot(), encoder_error=enc.error, video_tx_bytes=ts.stat().st_size,
                      video_tx_sha256=sha256_file(ts), video_tx_frames=ffprobe_frames(ts))
        if state["abort"] is not None:
            report.update(status="TX_ABORTED", abort_reason=state["abort"])
        elif enc is not None and not enc.error and ts.stat().st_size > 0 and not stop.is_set():
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
    p.add_argument("--sim-interf", dest="sim_interf",
                   help="SIM: interference at the RX, e.g. comb:75e3:-15:11:3 (lines every 75 kHz, -15 dB, pairs of "
                        "bursts every 11 s, 3 s apart), tone:FREQ:DB, burst:DB:DUR:PERIOD (comma separated)")
    p.add_argument("--sim-cond", dest="sim_cond_db", type=float, help="SIM: 2x2 channel condition number in dB")
    p.add_argument("--no-interf-mitigation", dest="rx_interf_mitigation", action="store_const", const=False,
                   help="v0.6.2 receiver: one noise level per frame, no LTF check of the detections")
    p.add_argument("--snr-profile", dest="sim_snr_profile",
                   help="SIM: time-varying SNR, e.g. sine:6:30:40 or steps:28,18,8,14:10")
    p.add_argument("--acm-host", dest="acm_host", help="ACM: IP of the TX PC (default 127.0.0.1)")
    p.add_argument("--fb-delay", dest="acm_fb_delay_s", type=float,
                   help="ACM: extra feedback delay in s (0.25-0.5 emulates a GEO satellite)")
    p.add_argument("--fb-tx-gain", dest="fb_tx_gain", type=float, help="radio feedback: RF1 TX/RX gain (30)")
    p.add_argument("--reorder", dest="rx_reorder_s", type=float,
                   help="reorder buffer before RS8+8 in s (default 0.3 with --acm, 0 = off)")
    p.add_argument("--fb-snr", dest="sim_fb_snr_db", type=float, help="SIM: SNR of the feedback link (20)")
    p.add_argument("--rf-link", choices=("antenna", "cable"), default="antenna",
                   help="radio feedback transmitter: antenna (gain <= 30) or cable (+ --attenuation-db)")
    p.add_argument("--attenuation-db", type=float)
    p.add_argument("--sync-threshold", dest="sync_threshold", type=float,
                   help="Schmidl-Cox detection threshold (0.35; v0.5 used 0.6)")
    p.add_argument("--legacy-sync", action="store_true",
                   help="v0.5 receiver front end: threshold 0.6, no LTF fine timing, no SIG-less recovery")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.legacy_sync:
        a.fine_timing = a.sig_recover = False
        a.sync_threshold = a.sync_threshold or 0.6
    else:
        a.fine_timing = a.sig_recover = None
    s = settings_from(a, ["sync_threshold", "fine_timing", "sig_recover",
                          "rx_gain", "sim_snr_db", "sim_cfo_hz", "sim_sfo_ppm", "sim_channel", "soft_decoding",
                                "const_every", "sim_snr_profile", "acm_host", "acm_fb_delay_s", "sim_interf", "sim_cond_db",
                                "rx_interf_mitigation",
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
    from .hardware import antenna_summary
    report["antennas"] = antenna_summary(s, "rx")
    LOG.info("Cắm anten (máy thu%s): %s", "" if a.mode == "rf" else ", mô phỏng", report["antennas"])
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
            fclock = AirClock(s.fb_rate)
            fbsrc = FeedbackRadioSource(s, reporter.packet, clock=fclock, lead_s=s.tx_lead_ms / 1e3,
                                        resync=s.tx_resync, stall=s.sim_stall if a.mode == "sim" else None)
            keep.append(fbsrc)
        resync, fdev_sim, fb_mon = [], None, None
        if a.mode == "rf":
            from .hardware import (configure_direction, configure_streams, make_radio, make_stream,
                                   validate_tx_gain)
            if radio_fb:
                # RF0 RX2 receives the video, RF1 TX/RX transmits the feedback
                validate_tx_gain(s.fb_tx_gain, a.rf_link, a.attenuation_db)
                usrp = make_stream(s, "rx", [0])
                fusrp = make_stream(s, "tx", [1])
                report["hardware"] = configure_streams([
                    {"dev": usrp, "kind": "rx", "n": 1, "freq": s.frequency, "gain": s.rx_gain,
                     "antenna": s.rx_antenna, "rate": s.sample_rate, "name": "video_rx_RF0", "chans": [0]},
                    {"dev": fusrp, "kind": "tx", "n": 1, "freq": s.fb_freq, "gain": s.fb_tx_gain,
                     "antenna": "TX/RX", "rate": s.fb_rate, "name": "feedback_tx_RF1", "chans": [1]}], s)
                nul = blocks.null_source(gr.sizeof_gr_complex)
                tb.connect((usrp, 0), (rx, 0))
                tb.connect(nul, (rx, 1))
                tb.connect(fbsrc, (fusrp, 0))
                keep += [usrp, fusrp, nul]
                iq = [(usrp, 0), (nul, 0)]
                fclock.start(report["hardware"]["t_set_monotonic"], s.start_delay_s)
                fb_mon = UhdAsyncMonitor(on_late=fbsrc.late_hint)
                tb.msg_connect((fusrp, "async_msgs"), (fb_mon, "in"))
                keep.append(fb_mon)
                fbsrc.set_max_output_buffer(s.tx_buffer_samples)
                resync.append((fusrp, fclock))
            else:
                usrp = make_radio(s, "rx")
                report["hardware"] = configure_direction(usrp, s, "rx")
                tb.connect((usrp, 0), (rx, 0))
                tb.connect((usrp, 1), (rx, 1))
                keep.append(usrp)
                iq = [(usrp, 0), (usrp, 1)]
            rx.clock = AirClock(s.sample_rate)
            rx.clock.start(report["hardware"]["t_set_monotonic"], s.start_delay_s)
            resync.append((usrp, rx.clock))
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
                fdev_sim = TxDeviceSim(1, s.fb_rate, fclock,        # USRP emulation, as for the video TX
                                       on_late=fbsrc.late_hint, source=fbsrc)
                if s.sim_tx_device_samples:
                    fbsrc.set_min_output_buffer(s.sim_tx_device_samples)
                tb.connect(fbsrc, fdev_sim)
                tb.connect(fdev_sim, fpush)
                keep += [fpush, fdev_sim]
                report["hardware"]["feedback_zmq"] = s.fb_zmq_addr
        tb.msg_connect((rx, "pdus"), (vr, "pdus"))
        if a.gui:
            gui = RxGui(tb, rx, iq, s)
            keep += gui.blocks
        tb._keep = keep
        tb.start()
        fbs = None
        if radio_fb:
            LOG.info("ACM: radio feedback on %.4g MHz (RF1 TX/RX, gain %.0f dB), %.3g S/s",
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
        t_start = time.monotonic()
        limit = t_start + a.seconds
        state = {"ended_at": None}
        history = report["history"] = []           # one row per second (tools/plot_run.py)
        report["clock"] = {"monotonic": t_start, "time": time.time()}

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
            if r.get("sig_recovered"):
                line += " | SIG cứu %d" % r["sig_recovered"]
            if r.get("fec_mismatch"):
                line += " | KHÁC FEC %d khung (kiểm tra --fec/--ldpc-rate hai bên)" % r["fec_mismatch"]
            if s.acm:
                line += " | ACM %s SNR_eff %s dB" % (r["modulation"], None if r["snr_eff_db"] is None
                                                      else round(r["snr_eff_db"], 1))
                if fbsrc is not None:
                    line += " | fb %d frames" % fbsrc.frames
                if a.mode == "sim" and ch_sim is not None and ch_sim.profile is not None:
                    line += " (sim SNR %.1f)" % ch_sim.snr_now
            for dev, clk in resync:
                try:
                    clk.sync_device(dev.get_time_now().get_real_secs(), time.monotonic())
                except Exception:          # noqa: BLE001
                    pass
            lat = rx.latency_tick()
            flat = fbsrc.latency_tick() if fbsrc is not None else None
            LOG.info("%s", line)
            if lat["proc"] or flat:
                LOG.info("   latency: RX processing %s ms, antenna -> RX buffer %s ms, feedback TX buffer %s ms",
                         lat["proc"]["median_ms"] if lat["proc"] else None,
                         lat["ingest"]["median_ms"] if lat["ingest"] else None,
                         flat["median_ms"] if flat else None)
            history.append({
                "rx_proc_ms": lat["proc"]["median_ms"] if lat["proc"] else None,
                "rx_ingest_ms": lat["ingest"]["median_ms"] if lat["ingest"] else None,
                "fb_tx_buffer_ms": flat["median_ms"] if flat else None,
                "t": round(time.monotonic() - t_start, 2), "t_wall": round(time.time(), 2), "ok": r["ok"],
                "rs": r["rs_fail"], "sig": r["sig_fail"], "crc": r["crc_fail"], "lost": r["lost_sync"],
                "snr_db": r["snr_db"], "evm_db": r["evm_db"], "snr_eff_db": r["snr_eff_db"],
                "modulation": r["modulation"], "rx_rms": round(r["rx_rms"], 5), "cond_db": r["cond_db"],
                # per input = per N321 port (RF0 = channel A, RF1 = channel B); RF1 unused with radio feedback
                "rx_rms_rf0": round(r["rx_rms_ch"][0], 5),
                "rx_rms_rf1": None if radio_fb else round(r["rx_rms_ch"][1], 5),
                "data": v["data_packets"], "missing": v["missing_data_packets"],
                "fb_frames": fbsrc.frames if fbsrc is not None else None,
                "sim_snr_db": round(ch_sim.snr_now, 2) if ch_sim is not None else None})
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
        report.update(rx=apply_run_medians(r, history), video=v, video_rx_bytes=ts.stat().st_size,
                      video_rx_sha256=sha256_file(ts) if ts.stat().st_size else None, video_rx_frames=info)
        if a.compare:
            from pathlib import Path
            ref = Path(a.compare)
            tx_hash = (json.loads(ref.read_text()).get("video_tx_sha256") if ref.suffix == ".json"
                       else sha256_file(ref))
            report["tx_sha256"] = tx_hash
            report["ts_identical"] = tx_hash == report["video_rx_sha256"]
            if ref.suffix == ".json":                 # both sides must use the same inner code
                txs = json.loads(ref.read_text()).get("settings", {})
                tx_fec = (txs.get("fec", "rs"), txs.get("ldpc_rate", "3/4"))
                report["fec_match"] = tx_fec[0] == s.fec and (s.fec == "rs" or s.acm or tx_fec[1] == s.ldpc_rate)
                if not report["fec_match"]:
                    LOG.error("FEC differs: TX %s, RX %s %s", tx_fec, s.fec, s.ldpc_rate)
        ok = v["end_received"] and v["missing_data_packets"] == 0 and info and int(info.get("nb_read_frames", 0)) > 0
        report["latency"] = {"rx": rx.latency_report(),
                             "feedback_tx": fbsrc.latency_report() if fbsrc is not None else None,
                             "uhd_async_feedback": dict(fb_mon.counts) if fb_mon is not None else None,
                             "sim_device_late": fdev_sim.late_events if fdev_sim is not None else None,
                             "sim_device_dropped": fdev_sim.dropped if fdev_sim is not None else None}
        report["fec"] = {"fec": s.fec, "ldpc_rate": None if s.fec == "rs" else "5/6 punctured (ACM)" if s.acm
                         else s.ldpc_rate,
                         "frames_with_other_fec": r.get("fec_mismatch", 0)}
        if r.get("fec_mismatch"):
            LOG.error("%d frames carried the other inner code (TX --fec/--ldpc-rate differ from RX)",
                      r["fec_mismatch"])
        if ok and report.get("ts_identical", True) and report.get("fec_match", True):
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
