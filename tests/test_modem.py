"""Modem self-tests (no radio). Run: python3 tests/test_modem.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from ofdm321.modem import (Demodulator, Modulator, demodulate, modulate, frame_samples, codewords_per_frame,  # noqa: E402,E501
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


def _acm_replay(c, snr_fn, seconds, period=0.008, loop=0.08, cer_fn=None):
    """Feed controller c with reports every `period` s; the RX decodes what the TX chose `loop` s ago.
    Returns [(t, modulation)] of every switch."""
    t0 = 1000.0
    chosen = [(t0 - 1, c.modcods[c.index])]
    switches = []
    t = t0
    while t < t0 + seconds:
        t += period
        rx_mod = [m for ts, m in chosen if ts <= t - loop][-1]
        before = c.index
        c.update(snr_fn(t - t0), cer_fn(t - t0) if cer_fn else 0.0, rx_mod, now=t)
        if c.index != before:
            chosen.append((t, c.modcods[c.index]))
            switches.append((round(t - t0, 3), c.modcods[c.index]))
    return switches


def test_acm_no_chatter_at_18_5_db():
    """RF 23/11: SNR_eff ~18.5 dB with isolated reports at 14.3 dB made v0.6.0 jump BPSK<->QPSK 9 times in 15 s.
    With debouncing (3 reports + 30 ms, fresh reports only, 0.3 s guard after a step up): no switch after
    reaching QPSK. A real fade (13 dB for 0.3 s) still steps down within ~50 ms, and back up afterwards."""
    from ofdm321.acm import AcmController
    rng = np.random.default_rng(41)
    noise = rng.normal(0, 0.6, 4000)
    dips = {}
    for k, t in enumerate(np.arange(0.9, 15, 0.7)):        # single and double dips, some right after start
        dips[round(t, 3)] = 1 + k % 2

    def snr(t):
        i = int(t / 0.008)
        for td, n in dips.items():
            if 0 <= t - td < n * 0.008 - 1e-9:
                return 14.3
        return 18.5 + noise[i % len(noise)]
    counts = {}
    for debounce in (False, True):
        c = AcmController("bpsk,qpsk,16qam,64qam", debounce=debounce)
        sw = _acm_replay(c, snr, 15.0)
        first_qpsk = next(t for t, m in sw if m == "qpsk")
        counts[debounce] = len([1 for t, m in sw if t > first_qpsk])
    assert counts[True] == 0 and counts[False] >= 5, counts
    print("  switches after reaching QPSK in 15 s at 18.5 dB with 14.3 dB dips: v0.6.0", counts[False],
          "| debounced", counts[True])
    # a real fade still acts quickly
    c = AcmController("bpsk,qpsk,16qam,64qam")
    sw = _acm_replay(c, lambda t: 13.0 if 3.0 <= t < 3.3 else 18.5, 6.0)
    down = [t for t, m in sw if m == "bpsk" and t > 2.9]
    assert down and down[0] - 3.0 <= 0.05, sw
    assert sw[-1][1] == "qpsk", sw
    # codeword errors in the current modulation: down at once, even during the guard after a step up
    c = AcmController("bpsk,qpsk,16qam,64qam")
    sw = _acm_replay(c, lambda t: 18.5, 2.0, cer_fn=lambda t: 0.3 if t > 1.5 else 0.0)
    assert any(m == "bpsk" and 1.5 < t < 1.52 for t, m in sw), sw


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
    try:
        from gnuradio import uhd  # noqa: F401  (tune_request / time_spec are used with the fake device)
    except ImportError:
        print("  (skipped: gnuradio.uhd not installed)")
        return
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


def test_report_uses_medians_while_receiving():
    """report rx_rms / snr_db / evm_db = medians of the seconds with decoded frames, *_final = at stop."""
    from ofdm321.runner_split import apply_run_medians
    hist = [{"ok": 0, "rx_rms": 0.0003, "snr_db": None, "evm_db": None, "snr_eff_db": None},     # before TX
            {"ok": 400, "rx_rms": 0.020, "snr_db": 24.0, "evm_db": -20.0, "snr_eff_db": 23.0},
            {"ok": 900, "rx_rms": 0.022, "snr_db": 26.0, "evm_db": -22.0, "snr_eff_db": 25.0},
            {"ok": 1400, "rx_rms": 0.021, "snr_db": 25.0, "evm_db": -21.0, "snr_eff_db": 24.0},
            {"ok": 1400, "rx_rms": 0.0003, "snr_db": 25.0, "evm_db": -21.0, "snr_eff_db": 24.0}]  # TX ended
    r = apply_run_medians({"rx_rms": 0.0003, "snr_db": 3.0, "evm_db": -2.0, "ok": 1400}, hist)
    assert (r["rx_rms"], r["snr_db"], r["evm_db"]) == (0.021, 25.0, -21.0), r
    assert (r["rx_rms_final"], r["snr_db_final"], r["evm_db_final"]) == (0.0003, 3.0, -2.0)
    assert r["median_during_run"]["seconds"] == 3 and r["median_during_run"]["snr_eff_db"] == 24.0
    r = apply_run_medians({"rx_rms": 0.0001, "snr_db": None, "evm_db": None}, hist[:1])   # nothing decoded
    assert r["rx_rms"] == 0.0001 and r["median_during_run"]["seconds"] == 0


def test_soft_decoding_every_modulation_fixed_frames():
    """Fixed (non-ACM) frames, soft RS at a SNR where hard decoding fails: no exception for any modulation
    (64QAM: 1984 codeword bits are not a multiple of 6 bits/symbol; the soft path used to crash)."""
    rng = np.random.default_rng(31)
    for mod, snr in (("bpsk", 2), ("qpsk", 6), ("16qam", 10), ("64qam", 15)):
        for mode in ("siso", "alamouti"):
            tx, rx = Modulator(mode, mod), Demodulator(mode, mod, soft=True, soft_cpu=1e9)
            for _ in range(6):
                pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
                y = channel(tx.frame(pkt), np.eye(2), snr, rng)
                s0, p = rx.find(y)
                if s0 is not None:
                    out, info = rx.demodulate(y, s0, p)        # must not raise
                    assert out is None or out == pkt


def _ts(pid, cc, payload=True, keyframe=False):
    """Synthetic 188-byte TS packet (PUSI + random access indicator for a keyframe)."""
    afc = (1 if payload else 0) | (2 if keyframe or not payload else 0)
    head = bytes([0x47, (0x40 if keyframe else 0) | pid >> 8, pid & 0xFF, afc << 4 | cc])
    af = bytes([1, 0x40 if keyframe else 0]) if afc & 2 else b""
    return head + af + bytes(188 - 4 - len(af))


def test_ltf_fine_timing_and_unused_antenna():
    """v0.6 front end: exact frame start from the LTF; siso ignores a noise-only RX1 input."""
    rng = np.random.default_rng(21)
    H2 = 0.15 * np.array([[1, -1j], [0.5j, 1]])
    for mode in ("siso", "alamouti", "smux"):
        tx, rx = Modulator(mode, "qpsk"), Demodulator(mode, "qpsk")
        for _ in range(5):
            y = channel(tx.frame(bytes(212)), np.eye(2), 20, rng, cfo=2e-4, H2=H2)
            s0, _ = rx.find(y)
            assert s0 == 300, (mode, s0)
    # siso at 5 dB: TX0 reaches RX0 only (H = I); v0.5 summed the noise of RX1 into the metric
    tx = Modulator("siso", "bpsk")
    found = {}
    for name, kw in (("v0.5", dict(threshold=0.6, fine_timing=False, sig_recover=False)), ("v0.6", {})):
        rx, r, n = Demodulator("siso", "bpsk", **kw), np.random.default_rng(22), 0
        for _ in range(60):
            y = channel(tx.frame(bytes(212)), np.eye(2), 5, r)
            s0, _ = rx.find(y)
            n += s0 is not None and -10 <= s0 - 300 <= 3
        found[name] = n
    assert found["v0.6"] >= 59 and found["v0.5"] < 50, found


def test_sig_recovery_keeps_packets_when_sig_fails():
    """A destroyed SIG symbol: v0.6 still delivers the packet (RS + CRC32 check it), v0.5 drops it."""
    rng = np.random.default_rng(23)
    for acm in (False, True):
        for mode in ("siso", "alamouti"):
            tx = Modulator(mode, "qpsk", acm=acm)
            new, old = Demodulator(mode, "qpsk", acm=acm), Demodulator(
                mode, "qpsk", acm=acm, threshold=0.6, fine_timing=False, sig_recover=False)
            for i in range(3):
                pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(2 if acm else 1)]
                x = tx.frame(pk if acm else pk[0]).astype(complex)
                if i:                                       # frame 0 teaches the receiver the SIG
                    x[:, 3 * SYM:4 * SYM] = 0               # SIG symbol blanked: only noise left
                y = channel(x, np.eye(2), 30, rng)
                (out, info), = new.demodulate_batch(y, [new.find(y)[0]], [None])
                assert (out if acm else [out]) == pk, (acm, mode, i, info)
                assert bool(info.get("sig_recovered")) == bool(i), info
                (out, info), = old.demodulate_batch(y, [old.find(y)[0]], [None])
                assert (out is None and info["reason"] == "sig") if i else out is not None, info
    # noise only: recovery must not invent packets
    rx = Demodulator("siso", "qpsk")
    y = (rng.standard_normal((2, 4000)) + 1j * rng.standard_normal((2, 4000))) * 0.1
    (out, info), = rx.demodulate_batch(y, [200], [None])
    assert out is None and info["reason"] == "sig"


def test_bpsk_threshold_separates_from_qpsk():
    """At 3.5 dB here (6.5 dB in tools/link_budget.py terms: this helper averages the power over both
    TX rows) BPSK frames must get through and QPSK must not: the front end no longer limits BPSK
    (v0.5: FER 1% at 9.6 / 10.0 dB, i.e. both failed)."""
    lost = {}
    for mod in ("bpsk", "qpsk"):
        rng = np.random.default_rng(24)
        bad = sum(not run("siso", mod, np.eye(2), 3.5, rng)[0] for _ in range(60))
        lost[mod] = bad
    assert lost["bpsk"] <= 1 and lost["qpsk"] >= 30, lost
    print("  siso AWGN 3.5 dB, 60 frames lost:", lost)


def test_ldpc_codes_against_dense_parity_check():
    """All 12 802.11n codes: H built independently from the base matrix (identity rolled by the shift) gives a
    zero syndrome for encoded words; noiseless and noisy decoding; a frozen codeword checksum."""
    import zlib
    from ofdm321 import ldpc
    rng = np.random.default_rng(31)
    for (n, rate) in ldpc._BASE:
        c = ldpc.code(n, rate)
        B, Z = ldpc.base_matrix(n, rate), n // 24
        Hd = np.zeros((B.shape[0] * Z, n), np.uint8)
        for i, j in zip(*np.nonzero(B >= 0)):
            Hd[i * Z:(i + 1) * Z, j * Z:(j + 1) * Z] = np.roll(np.eye(Z, dtype=np.uint8), B[i, j], axis=1)
        u = rng.integers(0, 2, (4, c.k))
        cw = c.encode(u)
        assert np.array_equal(cw[:, :c.k], u) and not ((Hd.astype(int) @ cw.T) % 2).any(), (n, rate)
        assert np.linalg.matrix_rank(Hd.astype(float)) >= Hd.shape[0] - 1      # full rank (GF(2) rank <= real)
        bits, ok, it = c.decode(8.0 * (1 - 2.0 * cw))
        assert ok.all() and np.array_equal(bits, cw) and not it.any()
    c = ldpc.code(648, "1/2")
    assert not c.encode(np.zeros((1, c.k))).any()                           # all-zero word
    e0 = np.zeros((1, c.k), np.uint8)
    e0[0, 0] = 1
    assert zlib.crc32(np.packbits(c.encode(e0)).tobytes()) == FROZEN_LDPC_CRC, \
        hex(zlib.crc32(np.packbits(c.encode(e0)).tobytes()))
    # n=1944 rate 1/2, BPSK AWGN Eb/N0 3 dB: every codeword corrected (802.11n: FER ~1e-4 there)
    c = ldpc.code(1944, "1/2")
    cw = c.encode(rng.integers(0, 2, (40, c.k)))
    sig = np.sqrt(1 / (2 * 0.5 * 10 ** 0.3))
    y = (1 - 2.0 * cw) + sig * rng.standard_normal(cw.shape)
    assert (y < 0).astype(int).sum() > 1000                                  # many raw errors
    bits, ok, it = c.decode(2 * y / sig ** 2)
    assert ok.all() and np.array_equal(bits, cw), (ok.sum(), it.max())


FROZEN_LDPC_CRC = 0x7D29063B          # 648 rate 1/2, info = unit vector e0 (regression guard)


def test_bit_llr_matches_hard_decisions():
    from ofdm321.modem import bit_llr
    rng = np.random.default_rng(32)
    for bps in (1, 2, 4, 6):
        b = rng.integers(0, 2, bps * 400).astype(np.uint8)
        s = modulate(b, bps)
        y = s + 0.02 * (rng.standard_normal(s.shape) + 1j * rng.standard_normal(s.shape))
        llr = bit_llr(y, np.full(len(y), 10.0), bps)
        assert np.array_equal((llr < 0).astype(np.uint8), demodulate(y, bps)), bps
        assert np.array_equal((llr < 0).astype(np.uint8), b), bps
    y = np.array([0.3 + 0j, -0.7 + 0j])
    assert np.allclose(bit_llr(y, np.array([2.0, 2.0]), 1), 4 * y.real * 2)  # BPSK: 4 y SNR


def test_ldpc_frames_every_mode_and_fec_flag_in_sig():
    rng = np.random.default_rng(33)
    H = np.array([[1, 0.3], [0.2, 1]])
    for acm in (False, True):
        for mode in MODES:
            for mod in ("bpsk", "qpsk", "16qam", "64qam"):
                rates = ("3/4",) if acm else ("1/2", "2/3", "3/4", "5/6")
                for rate in rates:
                    tx = Modulator(mode, mod, acm=acm, fec="ldpc", ldpc_rate=rate)
                    rx = Demodulator(mode, mod, acm=acm, fec="ldpc", ldpc_rate=rate)
                    n = codewords_per_frame(mode, mod) if acm else 1
                    pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(n)]
                    y = channel(tx.frame(pk if acm else pk[0], mod), H, 35, rng)
                    (out, info), = rx.demodulate_batch(y, [rx.find(y)[0]], [None])
                    assert (out if acm else [out]) == pk and info["fec"] == "ldpc", (acm, mode, mod, rate, info)
    # the other inner code / another LDPC rate: rejected as SIG and counted, never mis-decoded
    pkt = bytes(range(212))
    for txk, rxk in ((dict(fec="ldpc"), dict(fec="rs")), (dict(fec="rs"), dict(fec="ldpc")),
                     (dict(fec="ldpc", ldpc_rate="1/2"), dict(fec="ldpc", ldpc_rate="3/4")),
                     (dict(fec="ldpc", acm=True), dict(fec="rs", acm=True))):
        acm = txk.get("acm", False)
        tx, rx = Modulator("siso", "qpsk", **txk), Demodulator("siso", "qpsk", **rxk)
        rx.sig_recover = False
        y = channel(tx.frame([pkt, pkt] if acm else pkt), np.eye(2), 30, rng)
        (out, info), = rx.demodulate_batch(y, [rx.find(y)[0]], [None])
        assert out is None and info["reason"] == "sig" and rx.fec_mismatch == 1, (txk, rxk, info)


def test_ldpc_beats_rs_at_low_snr():
    """siso AWGN BPSK at a SNR where RS(248,216) loses most frames: LDPC 1/2 and 5/6 deliver them."""
    lost = {}
    for fec, rate in (("rs", "3/4"), ("ldpc", "5/6"), ("ldpc", "1/2")):
        rng = np.random.default_rng(34)
        tx = Modulator("siso", "bpsk", fec=fec, ldpc_rate=rate)
        rx = Demodulator("siso", "bpsk", fec=fec, ldpc_rate=rate)
        bad = 0
        for _ in range(40):
            pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
            y = channel(tx.frame(pkt), np.eye(2), 1.5, rng)
            s0, p = rx.find(y)
            bad += s0 is None or rx.demodulate(y, s0, p)[0] != pkt
        lost[f"{fec}{'' if fec == 'rs' else rate}"] = bad
    assert lost["rs"] >= 20 and lost["ldpc5/6"] <= 4 and lost["ldpc1/2"] <= 1, lost
    print("  siso AWGN BPSK 1.5 dB (4.5 dB in tools terms), 40 frames lost:", lost)


def test_tx_pacing_limits_buffer_delay():
    """v0.6.1: a TX source in front of an emulated USRP (TxDeviceSim, 64k-sample buffer) runs ~65 ms ahead
    without pacing; with --tx-lead-ms 30 the frames reach the air ~<= 30 ms after they are built, at
    real-time rate, and the paced blocks do not spin (CPU)."""
    import time
    try:
        from gnuradio import gr, blocks
    except ImportError:
        print("  (no GNU Radio: skipped)")
        return
    from ofdm321.blocks import AirClock, MimoTxSource, TxDeviceSim
    from ofdm321.config import load_settings
    out = {}
    for lead in (0.0, 0.03):
        s = load_settings(None, mimo_mode="siso", sample_rate=500e3)
        clk = AirClock(s.sample_rate)
        src = MimoTxSource(s, 1, clock=clk, lead_s=lead)
        dev = TxDeviceSim(2, s.sample_rate, clk)
        src.set_min_output_buffer(65536)
        tb = gr.top_block()
        sinks = [blocks.null_sink(gr.sizeof_gr_complex) for _ in range(2)]
        for i in (0, 1):
            tb.connect((src, i), (dev, i))
            tb.connect((dev, i), sinks[i])
        tb.start()
        time.sleep(0.5)
        f0, c0, t0 = src.frames, time.process_time(), time.monotonic()
        time.sleep(1.5)
        fps = (src.frames - f0) / (time.monotonic() - t0)
        cpu = (time.process_time() - c0) / 1.5
        tb.stop()
        tb.wait()
        lat = src.latency_report()["buffer"]
        out[lead] = (lat["median_ms"], round(fps), round(cpu, 2))
        need = s.sample_rate / src.mod.frame_len
        assert abs(fps - need) < 0.1 * need, (lead, fps, need)
        assert cpu < 0.6, (lead, cpu)
    assert out[0.0][0] > 100 and out[0.03][0] <= 36, out     # 65536 samples at 500 kS/s = 131 ms
    print("  TX buffer delay median / frames/s / CPU: no pacing", out[0.0], "| lead 30 ms", out[0.03])


def test_report_fields_and_stale_package_message():
    """v0.6.1: per-port RX level medians + RF1/RF0 imbalance; tx_gain_profile kept in the settings;
    run_*.py explains a stale ofdm321/ namespace folder instead of failing on an import error."""
    import subprocess
    import tempfile
    from pathlib import Path
    from ofdm321.config import load_settings
    from ofdm321.runner_split import run_medians
    hist = [{"ok": 100 * i, "rx_rms": 0.01, "rx_rms_rf0": 0.01, "rx_rms_rf1": 0.0032, "snr_db": 20.0}
            for i in range(1, 6)]
    med = run_medians(hist)
    assert med["rx_rms_rf0"] == 0.01 and med["rx_rms_rf1"] == 0.0032 and med["rf1_vs_rf0_db"] == -9.9, med
    s = load_settings(None, tx_gain_profile="steps:30,24,18:10")
    assert s.to_dict()["tx_gain_profile"] == "steps:30,24,18:10"
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as d:
        app = Path(d) / "OFDM_N321_RX"
        (app / "ofdm321" / "__pycache__").mkdir(parents=True)
        (app / "run_rx.py").write_text((repo / "OFDM_N321_RX" / "run_rx.py").read_text())
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        out = subprocess.run([sys.executable, "-S", str(app / "run_rx.py"), "--help"], capture_output=True,
                             text=True, cwd=d, env=env)
        assert out.returncode == 2 and "rm -rf" in out.stderr and str(app / "ofdm321") in out.stderr, out


def test_rf_fer_curve_points():
    """tools/rf_fer_curve.py: FER per second from the RX history counters; seconds at a level step (EVM jump
    to the previous/next second) and ACM seconds with a modulation change are left out."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from rf_fer_curve import points
    evm = [-24, -24, -24, -20, -20, -20, -20]
    hist, ok, rs = [], 0, 0
    for i, e in enumerate(evm):
        ok, rs = ok + 700, rs + (0 if e < -22 else 70)
        hist.append({"t": i + 1.0, "ok": ok, "rs": rs, "crc": 0, "sig": 0, "evm_db": e, "modulation": "16qam"})
    rep = {"settings": {"mimo_mode": "siso", "modulation": "16qam", "acm": False}, "history": hist,
           "_path": Path("/x/20261123_154215_rx_rf_siso_16qam/report.json")}
    rows = points(rep)
    assert [r["t"] for r in rows] == [2.0, 5.0, 6.0], rows          # 3.0 and 4.0 sit at the -24 -> -20 step
    assert rows[0]["fer"] == 0 and abs(rows[1]["fer"] - 70 / 770) < 1e-4
    acm = dict(rep, settings={"mimo_mode": "siso", "modulation": "qpsk", "acm": True},
               history=[dict(h, modulation="qpsk" if i < 5 else "16qam") for i, h in enumerate(hist)])
    assert [r["t"] for r in points(acm)] == [2.0, 5.0], points(acm)   # 6.0: qpsk -> 16qam; 7.0: no next second


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
