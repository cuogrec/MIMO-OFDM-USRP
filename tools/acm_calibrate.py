"""ACM threshold calibration: codeword error rate vs effective post-EQ SNR, per modulation.

Random 2x2 channel (flat Rayleigh + weak echo) per frame, AWGN, soft decoding on.
Prints, for every modulation, the smallest SNR_eff bin above which the codeword
error rate stays below --target. Use the result for ACM_THRESHOLDS_DB in acm.py.

    python3 tools/acm_calibrate.py --mode alamouti --frames 40
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from ofdm321.modem import Demodulator, Modulator, codewords_per_frame   # noqa: E402

MODS = ("bpsk", "qpsk", "16qam", "64qam")


def channel(x, rng, snr_db, cfo=1e-4):
    g = lambda s=1.0: s * (rng.standard_normal((2, 2)) + 1j * rng.standard_normal((2, 2))) / np.sqrt(2)
    H, H2 = g(), g(0.3)
    y = H @ x + H2 @ np.roll(x, 3, axis=1)
    y = np.concatenate([np.zeros((2, 300)), y, np.zeros((2, 300))], axis=1)
    p = np.mean(np.abs(x[:, 400:]) ** 2) * 2            # TX power (both antennas) through E|H|^2 = 1
    sigma = np.sqrt(p / 10 ** (snr_db / 10) / 2)
    y = y + sigma * (rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape))
    return y * np.exp(2j * np.pi * cfo * np.arange(y.shape[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="alamouti")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--target", type=float, default=0.01)
    a = ap.parse_args()
    rng = np.random.default_rng(7)
    tx = Modulator(a.mode, "qpsk", acm=True)
    rx = Demodulator(a.mode, "qpsk", acm=True, soft_cpu=1e9)
    edges = np.arange(-2, 40, 1.0)
    for mod in MODS:
        n = codewords_per_frame(a.mode, mod)
        ok = np.zeros(len(edges)); tot = np.zeros(len(edges))
        for snr in np.arange(0, 40, 2.0):
            for _ in range(a.frames):
                pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(n)]
                y = channel(tx.frame(pk, mod), rng, snr)
                s0, p = rx.find(y)
                if s0 is None:
                    continue
                (out, info), = rx.demodulate_batch(y, [s0], [p])
                if out is None:
                    continue
                b = np.searchsorted(edges, info["snr_eff_db"]) - 1
                if 0 <= b < len(edges):
                    tot[b] += n
                    ok[b] += sum(r == "ok" for r in info["reasons"])
        cer = np.where(tot > 0, 1 - ok / np.maximum(tot, 1), np.nan)
        good = [i for i in range(len(edges)) if tot[i] >= 5 and all(np.isnan(cer[j]) or cer[j] <= a.target
                                                                     for j in range(i, len(edges)))]
        thr = edges[good[0]] if good else None
        row = " ".join(f"{e:.0f}:{c:.2f}" for e, c in zip(edges, cer) if not np.isnan(c))
        print(f"{mod:6s} threshold(CER<={a.target}) = {thr} dB | {row}", flush=True)


if __name__ == "__main__":
    main()
