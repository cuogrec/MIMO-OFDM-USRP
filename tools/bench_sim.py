#!/usr/bin/python3
"""Real-time budget of the Python modem.

    python3 tools/bench_sim.py [mimo] [mod] [rate] [--no-channel] [--fec rs|ldpc] [--ldpc-rate R] [--acm]
        GNU Radio TX -> (channel) -> RX at full speed (IDLE frames, no video): decoded frames/s vs real time
    python3 tools/bench_sim.py --decoder [--seconds 3]
        LDPC decoder alone: codewords/s and Mb/s (coded bits) per rate, at a high SNR (few iterations, the usual
        case on a working link) and at the FER ~ 1 % SNR (many iterations, the worst case that still matters)
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root


def bench_decoder(seconds):
    from ofdm321 import ldpc
    from ofdm321.modem import ACM_PACKET_BITS, ldpc_coder
    rng = np.random.default_rng(0)
    # Eb/N0 (BPSK, AWGN) near FER 1 % for each mother code (measured), and a comfortable 2 dB above
    cases = [("1/2", 2.0), ("2/3", 2.8), ("3/4", 3.4), ("5/6", 4.2), ("acm", 4.6)]
    print(f"{'code':22s} {'Eb/N0':>6s} {'iter':>5s} {'cw/s':>7s} {'Mb/s coded':>11s} {'packets/s':>10s}")
    for rate, ebn0_lo in cases:
        coder = ldpc_coder("5/6", True) if rate == "acm" else ldpc_coder(rate)
        c = coder.code
        for ebn0 in (ebn0_lo, ebn0_lo + 2):
            P = 64                                         # packets per decode call (16 frames x up to 4)
            u = rng.integers(0, 2, (P, ldpc.INFO_BITS))
            x = coder.encode(u)
            r = ldpc.INFO_BITS / coder.bits
            sig = np.sqrt(1 / (2 * r * 10 ** (ebn0 / 10)))
            n_cw = n_pk = it = 0
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < seconds:
                llr = 2 * ((1 - 2.0 * x) + sig * rng.standard_normal(x.shape)) / sig ** 2
                info, ok, iters = coder.decode(llr)
                n_pk += P
                n_cw += P * coder.m
                it += iters.sum()
            dt = time.perf_counter() - t0
            name = f"n={c.n} r={rate}" if rate != "acm" else f"ACM n={c.n} 5/6->{ACM_PACKET_BITS}b"
            print(f"{name:22s} {ebn0:6.1f} {it / n_cw:5.1f} {n_cw / dt:7.0f} {n_cw * c.n / dt / 1e6:11.2f} "
                  f"{n_pk / dt:10.0f}", flush=True)


def bench_chain(a):
    from gnuradio import gr
    from ofdm321.config import load_settings
    from ofdm321.blocks import MimoTxSource, MimoRxSink, MimoChannelSim
    from ofdm321.acm import AcmController
    s = load_settings(None, mimo_mode=a.mimo, modulation=a.mod, sample_rate=a.rate, sim_channel="fixed",
                      sim_snr_db=30, fec=a.fec, ldpc_rate=a.ldpc_rate, acm=a.acm or None)
    tb = gr.top_block()
    ctl = None
    if a.acm:
        ctl = AcmController(a.mod, fec=a.fec)     # ACM frames, modulation held at --mod
    src, rx = MimoTxSource(s, 1, throttle=False, acm=ctl), MimoRxSink(s)
    keep = [src, rx]
    if a.no_channel:
        tb.connect((src, 0), (rx, 0))
        tb.connect((src, 1), (rx, 1))
    else:
        ch = MimoChannelSim(s)
        keep.append(ch)
        for i in (0, 1):
            tb.connect((src, i), (ch, i))
            tb.connect((ch, i), (rx, i))
    tb.start()
    time.sleep(1)
    n0 = rx.snapshot()["ok"]
    t = time.time()
    time.sleep(5)
    n1 = rx.snapshot()["ok"]
    dt = time.time() - t
    tb.stop()
    tb.wait()
    per_frame = 1
    if a.acm:
        from ofdm321.modem import codewords_per_frame
        per_frame = codewords_per_frame(a.mimo, a.mod)          # ok counts packets in ACM frames
    got = (n1 - n0) / dt / per_frame
    need = a.rate / src.mod.frame_len
    fec = a.fec + (f" {a.ldpc_rate}" if a.fec == "ldpc" and not a.acm else "")
    print(f"{a.mimo} {a.mod} {'ACM ' if a.acm else ''}{fec} Fs={a.rate:g}: decoded {got:.0f} frames/s, "
          f"real time needs {need:.0f} -> margin x{got / need:.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mimo", nargs="?", default="alamouti")
    ap.add_argument("mod", nargs="?", default="qpsk")
    ap.add_argument("rate", nargs="?", type=float, default=1e6)
    ap.add_argument("--no-channel", action="store_true")
    ap.add_argument("--fec", choices=("rs", "ldpc"), default="rs")
    ap.add_argument("--ldpc-rate", default="3/4", choices=("1/2", "2/3", "3/4", "5/6"))
    ap.add_argument("--acm", action="store_true", help="ACM frames (modulation held at MOD)")
    ap.add_argument("--decoder", action="store_true", help="LDPC decoder alone")
    ap.add_argument("--seconds", type=float, default=3)
    a = ap.parse_args()
    if a.decoder:
        bench_decoder(a.seconds)
    else:
        bench_chain(a)


if __name__ == "__main__":
    main()
