"""Modem self-tests (no radio). Run: python3 tests/test_modem.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
