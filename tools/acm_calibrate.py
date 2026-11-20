#!/usr/bin/python3
"""ACM threshold calibration: codeword (packet) error rate vs effective post-EQ SNR, per modulation and inner code.

Random 2x2 channel (flat Rayleigh + weak echo) per frame, AWGN, CFO; ACM frames decoded by the project's
receiver (RS: soft decoding on; LDPC: layered min-sum). For every modulation the result is the smallest
SNR_eff bin above which the packet error rate stays <= --target. These numbers go into
acm.THRESHOLDS_BY_FEC (one table per inner code).

    python3 tools/acm_calibrate.py --fec rs ldpc --out results/acm_thresholds.json
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from ofdm321.modem import Demodulator, Modulator, codewords_per_frame   # noqa: E402

MODS = ("bpsk", "qpsk", "16qam", "64qam")
EDGES = np.arange(-2, 40, 1.0)


def channel(x, rng, snr_db, cfo=1e-4):
    g = lambda s=1.0: s * (rng.standard_normal((2, 2)) + 1j * rng.standard_normal((2, 2))) / np.sqrt(2)
    H, H2 = g(), g(0.3)
    y = H @ x + H2 @ np.roll(x, 3, axis=1)
    y = np.concatenate([np.zeros((2, 300)), y, np.zeros((2, 300))], axis=1)
    p = np.mean(np.abs(x[:, 400:]) ** 2) * 2            # TX power (both antennas) through E|H|^2 = 1
    sigma = np.sqrt(p / 10 ** (snr_db / 10) / 2)
    y = y + sigma * (rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape))
    return y * np.exp(2j * np.pi * cfo * np.arange(y.shape[1]))


def job(args):
    """One (fec, modulation, SNR) cell: per SNR_eff bin, packets ok / sent."""
    fec, mode, mod, snr, frames, seed = args
    rng = np.random.default_rng(seed)
    tx = Modulator(mode, "qpsk", acm=True, fec=fec)
    rx = Demodulator(mode, "qpsk", acm=True, soft_cpu=1e9, fec=fec)
    n = codewords_per_frame(mode, mod)
    ok, tot = np.zeros(len(EDGES)), np.zeros(len(EDGES))
    for _ in range(frames):
        pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(n)]
        y = channel(tx.frame(pk, mod), rng, snr)
        s0, p = rx.find(y)
        if s0 is None or s0 + rx.body_len > y.shape[1]:
            continue
        (out, info), = rx.demodulate_batch(y, [s0], [p])
        if "reasons" not in info:
            continue
        b = np.searchsorted(EDGES, info["snr_eff_db"]) - 1
        if 0 <= b < len(EDGES):
            tot[b] += n
            ok[b] += sum(r == "ok" for r in info["reasons"])
    return fec, mod, ok, tot


def thresholds(results, target):
    out, curves = {}, {}
    for fec in sorted({r[0] for r in results}):
        out[fec], curves[fec] = {}, {}
        for mod in MODS:
            ok = sum(r[2] for r in results if r[0] == fec and r[1] == mod)
            tot = sum(r[3] for r in results if r[0] == fec and r[1] == mod)
            if np.isscalar(ok):
                continue
            cer = np.where(tot > 0, 1 - ok / np.maximum(tot, 1), np.nan)
            good = [i for i in range(len(EDGES)) if tot[i] >= 20 and all(
                np.isnan(cer[j]) or cer[j] <= target for j in range(i, len(EDGES)))]
            out[fec][mod] = float(EDGES[good[0]]) if good else None
            curves[fec][mod] = {f"{e:g}": round(float(c), 4) for e, c, t in zip(EDGES, cer, tot) if t >= 20}
    return out, curves


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="alamouti")
    ap.add_argument("--fec", nargs="+", default=["rs", "ldpc"], choices=("rs", "ldpc"))
    ap.add_argument("--frames", type=int, default=150, help="frames per (modulation, SNR) cell")
    ap.add_argument("--target", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--out", help="write thresholds + CER curves as JSON")
    a = ap.parse_args()
    jobs = [(fec, a.mode, mod, float(snr), a.frames, 100 * i + j)
            for i, (fec, mod) in enumerate((f, m) for f in a.fec for m in MODS)
            for j, snr in enumerate(np.arange(0, 40, 1.0))]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(job, jobs, chunksize=1))
    thr, curves = thresholds(results, a.target)
    for fec, t in thr.items():
        print(f"{fec:5s} thresholds (packet error rate <= {a.target}, SNR_eff dB): {t}")
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"mode": a.mode, "target": a.target, "frames_per_cell": a.frames, "thresholds_db": thr,
                       "cer_vs_snr_eff": curves}, f, indent=1)
        print(a.out)


if __name__ == "__main__":
    main()
