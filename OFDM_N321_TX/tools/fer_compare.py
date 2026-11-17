#!/usr/bin/python3
"""So sánh tỉ lệ mất khung (FER): interleaver none/carrier/block x giải cứng/mềm.

    python3 tools/fer_compare.py [--mimo alamouti] [--mod qpsk] [--frames 400] [--snr 8 10 12 14 16]

Kênh mô phỏng: fading Rayleigh chọn lọc tần số (4 tap, trải trễ ~6 mẫu) độc lập cho từng
cặp anten, mỗi khung một kênh mới, AWGN. --burst N: thêm nhiễu xung phủ N symbol OFDM.
Cùng một dãy kênh/nhiễu cho mọi cấu hình. Không cần radio, không cần GNU Radio.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ofdm321.modem import Demodulator, Modulator, SYM  # noqa: E402

TAPS = np.array([0, 1, 3, 6])
POWER = np.array([1.0, 0.6, 0.35, 0.2])
POWER = POWER / POWER.sum()


def fading(x, rng):
    y = np.zeros((2, x.shape[1] + 8), complex)
    for r in range(2):
        for t in range(2):
            g = (rng.standard_normal(4) + 1j * rng.standard_normal(4)) * np.sqrt(POWER / 2)
            for d, c in zip(TAPS, g):
                y[r, d:d + x.shape[1]] += c * x[t]
    return y


def run(mimo, mod, interleaver, soft, snr_db, frames, burst, seed=1):
    rng = np.random.default_rng(seed)
    tx = Modulator(mimo, mod, interleaver=interleaver)
    rx = Demodulator(mimo, mod, soft=soft, soft_cpu=1e9)
    lost = softed = 0
    ref = tx.scale ** 2
    for _ in range(frames):
        pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
        y = fading(tx.frame(pkt).astype(complex), rng)
        y = np.concatenate([np.zeros((2, 200)), y, np.zeros((2, 200))], axis=1)
        sigma = np.sqrt(ref / 10 ** (snr_db / 10) / 2)
        y += sigma * (rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape))
        if burst:
            a = 200 + SYM * int(rng.integers(5, 5 + rx.np - burst))
            y[:, a:a + burst * SYM] += 10 * sigma * (rng.standard_normal((2, burst * SYM))
                                                    + 1j * rng.standard_normal((2, burst * SYM)))
        s0, p = rx.find(y)
        if s0 is None:
            lost += 1
            continue
        out, info = rx.demodulate(y, s0, p)
        lost += out != pkt
        softed += bool(info.get("soft_erasures"))
    return lost / frames, softed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mimo", default="alamouti")
    ap.add_argument("--mod", default="qpsk")
    ap.add_argument("--frames", type=int, default=400)
    ap.add_argument("--snr", type=float, nargs="+", default=[8, 10, 12, 14, 16])
    ap.add_argument("--burst", type=int, default=0)
    a = ap.parse_args()
    cases = [("none", False), ("none", True), ("carrier", True), ("block", False), ("block", True)]
    print(f"{a.mimo} {a.mod}, {a.frames} khung/điểm, burst={a.burst} symbol. FER (%):")
    print("SNR dB | " + " | ".join(f"{i:>7}+{'mềm' if s else 'cứng':<4}" for i, s in cases))
    for snr in a.snr:
        row = [run(a.mimo, a.mod, i, s, snr, a.frames, a.burst)[0] * 100 for i, s in cases]
        print(f"{snr:6.1f} | " + " | ".join(f"{v:12.1f}" for v in row))


if __name__ == "__main__":
    main()
