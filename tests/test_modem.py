"""Modem self-tests (no radio). Run: python3 tests/test_modem.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from ofdm321.modem import (Demodulator, Modulator, demodulate, modulate, frame_samples,  # noqa: E402
                           interleave_perm, INTERLEAVERS, DATA_BINS, N_FFT, SYM)

MODES = ("siso", "simo", "alamouti", "smux")
MODS = ("qpsk", "16qam")


def channel(x, H, snr_db, rng, cfo=0.0, H2=None, pad=300):
    y = H @ x
    if H2 is not None:
        y = y + H2 @ np.roll(x, 3, axis=1)
    y = np.concatenate([np.zeros((2, pad)), y, np.zeros((2, pad))], axis=1)
    p = np.mean(np.abs(H @ x) ** 2)
    sigma = np.sqrt(p / 10 ** (snr_db / 10) / 2)
    y = y + sigma * (rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape))
    return y * np.exp(2j * np.pi * cfo * np.arange(y.shape[1]))


def run(mode, mod, H, snr, rng, cfo=0.0, H2=None):
    tx, rx = Modulator(mode, mod), Demodulator(mode, mod)
    pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
    y = channel(tx.frame(pkt), H, snr, rng, cfo, H2)
    s0, p = rx.find(y)
    if s0 is None:
        return False, None
    out, info = rx.demodulate(y, s0, p)
    return out == pkt, (s0, info)


def test_mapping():
    for bps in (2, 4):
        b = np.random.default_rng(0).integers(0, 2, bps * 500).astype(np.uint8)
        assert np.array_equal(demodulate(modulate(b, bps), bps), b)


def test_timing_and_clean_decode():
    rng = np.random.default_rng(1)
    for mode in MODES:
        for mod in MODS:
            ok, (s0, info) = run(mode, mod, np.eye(2), 45, rng)
            assert ok and abs(s0 - 300) <= 2, (mode, mod, s0, info)


def test_fixed_channel_multipath_cfo():
    rng = np.random.default_rng(2)
    H = np.array([[1.0, 0.45j], [-0.35, 0.9 * np.exp(0.7j)]])
    H2 = 0.15 * np.array([[1, -1j], [0.5j, 1]])
    for mode in MODES:
        for mod in MODS:
            snr = 32 if mod == "16qam" else 24
            assert all(run(mode, mod, H, snr, rng, cfo=3e-4, H2=H2)[0] for _ in range(10)), (mode, mod)


def test_diversity_gain():
    """In Rayleigh fading at the same SNR, 2x2 Alamouti must lose fewer frames than SISO."""
    rng = np.random.default_rng(3)
    lost = {}
    for mode in ("siso", "alamouti"):
        r = np.random.default_rng(7)
        bad = 0
        for _ in range(150):
            H = (r.standard_normal((2, 2)) + 1j * r.standard_normal((2, 2))) / np.sqrt(2)
            bad += not run(mode, "qpsk", H, 12, rng)[0]
        lost[mode] = bad
    assert lost["alamouti"] < lost["siso"], lost
    print("  Rayleigh 12 dB, 150 frames lost:", lost)


def test_interleaver_is_permutation_and_followed_by_rx():
    rng = np.random.default_rng(5)
    for mode in MODES:
        for mod in MODS:
            for name in INTERLEAVERS:
                perm = interleave_perm(mode, mod, name)
                if perm is not None:
                    assert np.array_equal(np.sort(perm), np.arange(perm.size)), (mode, mod, name)
                tx, rx = Modulator(mode, mod, interleaver=name), Demodulator(mode, mod)
                pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
                y = channel(tx.frame(pkt), np.eye(2), 40, rng)
                s0, p = rx.find(y)
                out, info = rx.demodulate(y, s0, p)
                assert out == pkt and info["interleaver"] == name, (mode, mod, name, info)


def _one_bad(kind, soft, interleaver, rng):
    """SISO QPSK, clean channel; wipe one carrier or one OFDM payload symbol."""
    tx, rx = Modulator("siso", "qpsk", interleaver=interleaver), Demodulator("siso", "qpsk", soft=soft, soft_cpu=1e9)
    pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
    x = tx.frame(pkt).astype(complex)
    n = rng.standard_normal(x.shape) + 1j * rng.standard_normal(x.shape)
    if kind == "symbol":                       # strong impulse over payload symbol 7
        a = (4 + 7) * SYM
        x[:, a:a + SYM] += 0.3 * n[:, a:a + SYM]
    else:                                      # narrow-band jammer on one data carrier, payload only
        k = DATA_BINS[10] if DATA_BINS[10] < 32 else DATA_BINS[10] - N_FFT
        t = np.arange(4 * SYM, x.shape[1])
        x[:, 4 * SYM:] += 0.08 * np.exp(2j * np.pi * (k + 0.0) * t / N_FFT + 1j) * (1 + 0.5 * np.cos(t / 37.0))
    y = np.concatenate([np.zeros((2, 300)), x + 1e-3 * n, np.zeros((2, 300))], axis=1)
    out, _ = rx.demodulate(y, 300, None)
    return out == pkt


def test_block_interleaver_with_soft_survives_bad_carrier_and_bad_symbol():
    rng = np.random.default_rng(9)
    res = {(k, i, s): sum(_one_bad(k, s, i, rng) for _ in range(10))
           for k in ("carrier", "symbol") for i in ("none", "block") for s in (False, True)}
    print("  decoded of 10:", {f"{k}/{i}/{'soft' if s else 'hard'}": v for (k, i, s), v in res.items()})
    assert res[("carrier", "block", True)] == 10 and res[("symbol", "block", True)] == 10, res
    assert res[("carrier", "none", False)] < 10, res      # the case interleaving fixes


def test_geometry():
    assert frame_samples("alamouti", "qpsk", 80) == 2080
    assert frame_samples("smux", "qpsk", 80) == 1280
    assert frame_samples("smux", "16qam", 80) == 880


def test_constellation_snapshot():
    """const_every=1: every good frame carries its equalised data symbols (for the GUI)."""
    rng = np.random.default_rng(5)
    for mode in MODES:
        for mod in MODS:
            tx, rx = Modulator(mode, mod), Demodulator(mode, mod)
            rx.const_every = 1
            pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
            y = channel(tx.frame(pkt), np.array([[1, 0.3j], [-0.2, 0.9]]), 30, rng)
            s0, p = rx.find(y)
            (out, info), = rx.demodulate_batch(y, [s0], [p])
            c = info["const"]
            bps = 2 if mod == "qpsk" else 4
            ideal = modulate(demodulate(c, bps), bps)
            evm = 10 * np.log10(np.mean(np.abs(c - ideal) ** 2) / np.mean(np.abs(ideal) ** 2))
            assert out == pkt and len(c) == rx.n_code and evm < -18, (mode, mod, len(c), evm)


def test_acm_frames_any_modulation():
    """ACM: fixed-length frames, modulation per frame from SIG, several codewords each."""
    from ofdm321.modem import codewords_per_frame
    rng = np.random.default_rng(11)
    H = np.array([[1, 0.3j], [-0.2, 0.9]])
    for mode in MODES:
        tx, rx = Modulator(mode, "qpsk", acm=True), Demodulator(mode, "qpsk", acm=True)
        lens = set()
        for mod in ("bpsk", "qpsk", "16qam", "64qam"):
            n = codewords_per_frame(mode, mod)
            pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(n)]
            f = tx.frame(pk, mod)
            lens.add(f.shape[1])
            y = channel(f, H, 38, rng)
            s0, p = rx.find(y)
            (out, info), = rx.demodulate_batch(y, [s0], [p])
            assert out == pk and info["modulation"] == mod, (mode, mod, info)
        assert len(lens) == 1                         # receiver timing unchanged


def test_acm_controller():
    from ofdm321.acm import AcmController
    c = AcmController("bpsk,qpsk,16qam,64qam", margin_db=1.0, hold_s=0.5)
    import time
    t = time.monotonic()          # reports stamped ahead of the clock: no timeout
    for _ in range(30):                               # good link: climbs one step per hold time
        t += 0.1
        c.update(32.0, 0.0, c.modulation, now=t)
    assert c.modulation == "64qam"
    c.update(16.0, 0.0, "64qam", now=t + 0.1)        # deep fade: straight down
    assert c.modulation == "qpsk"
    c.update(16.0, 0.5, "64qam", now=t + 0.2)        # errors of frames still in flight: ignored
    assert c.modulation == "qpsk"
    c.update(16.0, 0.5, "qpsk", now=t + 0.3)         # errors in the new modulation: step down
    assert c.modulation == "bpsk"


def test_radio_feedback_packet():
    """ACM report -> SISO BPSK frame (radio feedback link) -> parsed back, with CFO and noise."""
    from ofdm321.acm import FB, FB_MAGIC, FB_PACKET_BYTES, parse_feedback_packet
    rng = np.random.default_rng(21)
    raw = FB.pack(FB_MAGIC, 4242, 9, 17.25, 0.04, 0.03, 1, 1)
    raw += bytes(FB_PACKET_BYTES - len(raw))
    tx, rx = Modulator("siso", "bpsk", interleaver="none"), Demodulator("siso", "bpsk")
    y = channel(tx.frame(raw), np.array([[0.8 * np.exp(1j), 0], [0, 0]]), 12, rng, cfo=2e-3)
    s0, p = rx.find(y)
    (out, info), = rx.demodulate_batch(y, [s0], [p])
    msg = parse_feedback_packet(out)
    assert msg["session"] == 4242 and msg["seq"] == 9 and msg["mod"] == "qpsk" and msg["valid"], msg
    assert abs(msg["snr"] - 17.25) < 1e-4


def test_acm_resend_on_downshift():
    """TX: after an ACM downshift the packets of the last loop delay are sent again, first."""
    import time
    from ofdm321.acm import AcmController
    from ofdm321.blocks import MimoTxSource
    from ofdm321.config import Settings
    from ofdm321.protocol import DATA, pack_packet, unpack_packet
    s = Settings(acm=True, mimo_mode="siso", acm_resend_s=0.2)
    ctl = AcmController("qpsk,16qam")
    ctl.index = 1                                                  # 16qam: 4 packets per frame
    src = MimoTxSource(s, 5, acm=ctl)
    sent = []
    src.mod.frame = lambda pk, m: (sent.append((m, [unpack_packet(p)[2] for p in pk if p[5] == DATA])),
                                   np.zeros((2, 3760), np.complex64))[1]
    for i in range(12):
        src.packets.put(pack_packet(DATA, 5, i, bytes(188)))
    src._next_acm_frame()
    src._next_acm_frame()                                          # 16qam frames: 0-3, 4-7
    ctl.index = 0                                                  # fade reported: down to qpsk
    src._next_acm_frame()
    src._next_acm_frame()
    src._next_acm_frame()
    assert [x[1] for x in sent] == [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1], [2, 3], [4, 5]], sent
    assert src.snapshot()["acm_resent_packets"] == 8
    time.sleep(0.25)                                               # older than the window: no resend
    ctl.index = 1
    src._next_acm_frame()
    time.sleep(0.25)
    ctl.index = 0
    src._next_acm_frame()
    assert src.snapshot()["acm_resend_events"] == 1


def test_rx_reorder_buffer():
    """A packet of the next RS group overtakes resent packets: lost without the reorder buffer."""
    import tempfile
    import time
    from pathlib import Path
    import pmt
    from ofdm321.blocks import VideoReceiver
    from ofdm321.fec_rs import encode_parity
    from ofdm321.protocol import DATA, END, RS8_PARITY, pack_packet, rs_parity_stamp
    rng = np.random.default_rng(5)
    pay = [rng.integers(0, 256, 188, dtype=np.uint8).tobytes() for _ in range(16)]
    pk = {i: pack_packet(DATA, 9, i, pay[i]) for i in range(16)}
    for g in (0, 8):
        for i, par in enumerate(encode_parity(pay[g:g + 8], 8)):
            pk[("p", g, i)] = pack_packet(RS8_PARITY, 9, g, par, rs_parity_stamp(8, i, 8))
    # air order: 0-2 ok, then the fade: 3-7 + parity lost, but DATA 8 gets through; then the resend
    order = [0, 1, 2, 8] + [3, 4, 5, 6, 7] + [("p", 0, i) for i in range(8)] + \
            list(range(8, 16)) + [("p", 8, i) for i in range(8)]
    results = {}
    for reorder in (0.0, 0.05):
        with tempfile.TemporaryDirectory() as d:
            vr = VideoReceiver(9, Path(d) / "v.ts", 0, reorder_s=reorder)
            for k in order + ["end"]:
                raw = pack_packet(END, 9, 16) if k == "end" else pk[k]
                vr.handle(pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(raw), list(raw))))
            time.sleep(0.06)
            vr.poll()
            vr.close()
            results[reorder] = (vr.snapshot()["missing_data_packets"], (Path(d) / "v.ts").read_bytes())
    assert results[0.0][0] > 0                                     # group 0 closed too early
    assert results[0.05] == (0, b"".join(pay))                    # in order again: complete video


def test_feedback_watchdog():
    from ofdm321.acm import FeedbackWatchdog
    w = FeedbackWatchdog(first_s=5, every_s=10)
    seen = [(t, w.check(t, 0)) for t in range(1, 27)]
    assert [t for t, s in seen if s == "warn"] == [5, 15, 25] and w.warnings == 3
    assert w.check(27, 3) == "ok" and w.first_frame_s == 27
    assert w.check(40, 3) is None and w.check(50, 3) is None     # quiet once feedback arrived
    w2 = FeedbackWatchdog()
    assert w2.check(1, 12) == "ok" and w2.warnings == 0


def test_port_labels_in_hardware_report():
    """report.hardware names ports like the N321 front panel (RF0 TX/RX, RF1 RX2); fake UHD device."""
    from ofdm321.config import Settings
    from ofdm321.hardware import antenna_summary, configure_direction, configure_streams

    class Dev:
        def __init__(self, n):
            self.f, self.g, self.ant = [0.0] * n, [0.0] * n, [""] * n
        set_clock_source = set_time_source = set_subdev_spec = set_time_now = set_start_time = \
            lambda self, *a: None
        def set_samp_rate(self, r): self.r = r
        def get_samp_rate(self): return self.r
        def set_center_freq(self, tr, ch): self.f[ch] = tr.target_freq
        def get_center_freq(self, ch): return self.f[ch]
        def set_gain(self, g, ch): self.g[ch] = g
        def get_gain(self, ch): return self.g[ch]
        def set_antenna(self, a, ch): self.ant[ch] = a
        def get_antenna(self, ch): return self.ant[ch]
        def get_sensor_names(self, ch): return []
        def get_usrp_info(self, ch): return {}
        def get_clock_rate(self, ch): return 200e6

    s = Settings(mimo_mode="siso", acm=True, fb_link="radio", frequency=921e6).validate()
    rep = configure_streams([
        {"dev": Dev(1), "kind": "rx", "n": 1, "freq": 921e6, "gain": 50, "antenna": "RX2", "rate": 1e6,
         "name": "video_rx_RF0", "chans": [0]},
        {"dev": Dev(1), "kind": "tx", "n": 1, "freq": 922.7e6, "gain": 30, "antenna": "TX/RX", "rate": 5e5,
         "name": "feedback_tx_RF1", "chans": [1]}], s)
    assert rep["video_rx_RF0"]["channels"][0]["port"] == "RF0 RX2"
    assert rep["feedback_tx_RF1"]["channels"][0]["port"] == "RF1 TX/RX"
    rep = configure_direction(Dev(2), Settings(), "tx")
    assert [c["port"] for c in rep["channels"]] == ["RF0 TX/RX", "RF1 TX/RX"]
    assert antenna_summary(s, "rx") == "RF0 RX2 thu video 921.000 MHz | RF1 TX/RX phát phản hồi 922.700 MHz"
    assert antenna_summary(Settings(), "tx").startswith("RF0 TX/RX + RF1 TX/RX")


def _ts(pid, cc, payload=True, keyframe=False):
    """Synthetic 188-byte TS packet (PUSI + random access indicator for a keyframe)."""
    afc = (1 if payload else 0) | (2 if keyframe or not payload else 0)
    head = bytes([0x47, (0x40 if keyframe else 0) | pid >> 8, pid & 0xFF, afc << 4 | cc])
    af = bytes([1, 0x40 if keyframe else 0]) if afc & 2 else b""
    return head + af + bytes(188 - 4 - len(af))


def test_ts_splice_helpers():
    from ofdm321.video_adapt import ContinuityRewriter, is_keyframe_start, ts_pid
    assert is_keyframe_start(_ts(256, 3, keyframe=True)) and not is_keyframe_start(_ts(256, 3))
    assert ts_pid(_ts(4096, 0)) == 4096
    cc = ContinuityRewriter()
    # rendition A: video cc 0,1,2 ; switch to rendition B whose counters are elsewhere (9, 10)
    seq = [_ts(256, 0, keyframe=True), _ts(256, 1), _ts(256, 2), _ts(256, 9, keyframe=True), _ts(256, 10),
           _ts(256, 10, payload=False), _ts(0x1FFF, 7)]
    out = [cc(p) for p in seq]
    assert [p[3] & 15 for p in out[:6]] == [0, 1, 2, 3, 4, 4]      # payload +1, adaptation-only repeats
    assert out[6] == seq[6]                                         # null packets untouched
    assert all(len(p) == 188 and p[:3] == q[:3] for p, q in zip(out, seq))


def test_video_rate_policy():
    from ofdm321.config import Settings
    from ofdm321.video_adapt import VideoRatePolicy, video_ladder
    s = Settings(acm=True)
    lad = video_ladder(s, {"bpsk": 200e3, "qpsk": 400e3, "16qam": 800e3, "64qam": 1200e3})
    assert [mux for _, mux, _ in lad] == [150000, 300000, 600000, 900000]
    assert all(vb < mux for _, mux, vb in lad)
    p = VideoRatePolicy(["bpsk", "qpsk", "16qam", "64qam"], hold_s=5)
    assert p.decide(2, "qpsk", 0.0) == (1, "down")                 # down at once
    assert p.decide(1, "64qam", 4.9) == (1, None)                  # up only after 5 s stable
    assert p.decide(1, "64qam", 5.0) == (3, "up")
    assert p.decide(2, "16qam", 99) == (2, None)


def test_adaptive_encoder_splices_cleanly():
    """Real FFmpeg ladder, 6 s: up and down switches, DATA numbering and the spliced TS stay valid."""
    import shutil
    import subprocess
    import tempfile
    import threading
    import time
    from pathlib import Path
    from ofdm321.config import Settings
    from ofdm321.protocol import DATA, END, unpack_packet
    from ofdm321.video_adapt import AdaptiveEncoder, video_ladder
    if shutil.which("ffmpeg") is None:
        print("  (skipped: no ffmpeg)")
        return

    class Src:
        def __init__(self):
            self.pk = []

        def enqueue(self, raw, stop):
            self.pk.append(raw)
            return True

    class Ctl:                                                     # ACM: qpsk, 64qam from 1 s, bpsk from 4 s
        def __init__(self):
            self.t0 = time.monotonic()

        def _now(self):
            t = time.monotonic() - self.t0
            return (0, "qpsk") if t < 1 else (1, "64qam") if t < 4 else (4, "bpsk")

        modulation = property(lambda self: self._now()[1])
        mod_since = property(lambda self: self.t0 + self._now()[0])

    s = Settings(acm=True)
    lad = video_ladder(s, {"bpsk": 200e3, "qpsk": 400e3, "16qam": 800e3, "64qam": 1200e3})
    with tempfile.TemporaryDirectory() as d:
        src = Src()
        enc = AdaptiveEncoder(s, "testpattern", None, 6, src, 77, Path(d), threading.Event(), Ctl(), lad,
                              hold_s=1.5)
        enc.start()
        enc.done.wait(30)
        assert enc.error is None, enc.error
        assert [h["reason"] for h in enc.history] == ["start", "up", "down"], enc.history
        assert [h["modulation"] for h in enc.history] == ["qpsk", "64qam", "bpsk"]
        seqs = [unpack_packet(r)[2] for r in src.pk if r[5] == DATA]
        assert seqs == list(range(len(seqs))) and sum(r[5] == END for r in src.pk) == 12
        out = subprocess.run(["ffmpeg", "-v", "warning", "-i", str(Path(d) / "video_tx.ts"), "-f", "null", "-"],
                             capture_output=True, text=True)
        assert out.returncode == 0 and not out.stderr.strip(), out.stderr
        n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_read_frames", "-of", "csv=p=0", str(Path(d) / "video_tx.ts")],
                           capture_output=True, text=True).stdout.strip()
        assert int(n.split()[0]) == 6 * s.fps, n


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
