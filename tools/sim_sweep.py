#!/usr/bin/python3
"""System characterisation by simulation (numpy only, no GNU Radio, no radio).

1. Fixed modulation sweep: siso/simo/alamouti/smux x bpsk/qpsk/16qam/64qam x channel awgn/rayleigh/fixed x
   soft on/off, SNR 0..35 dB step 2. Real fixed-mode frames (one 212-byte packet per frame, block
   interleaver), the project's Modulator/Demodulator incl. Schmidl-Cox detection and a small CFO.
   FER = lost packets / sent packets; goodput = (1 - FER) x TS capacity after RS8+8 (kb/s at 1 MS/s).
2. ACM curve: ACM frames (3760 samples, k codewords), block fading (one 2x2 channel per second), the
   project's AcmController fed like the real link (EWMA 0.8/0.2 of SNR_eff, report every 0.02 s,
   ~40 ms loop delay, codeword error rate over 0.1 s). Goodput = delivered codewords x 752 bits.

SNR = total transmit power x E|h|^2 / noise power per receive antenna (both TX together for alamouti/smux).

    python3 tools/sim_sweep.py                    # full run (~minutes on 16 cores) -> results/
    python3 tools/sim_sweep.py --quick            # small check
    python3 tools/sim_sweep.py --plots-only       # redraw PNGs from results/*.csv
"""
import argparse
import csv
from concurrent.futures import ProcessPoolExecutor
import os
from pathlib import Path
import sys
import time

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from ofdm321.modem import Demodulator, Modulator, codewords_per_frame, frame_samples  # noqa: E402

MIMOS = ("siso", "simo", "alamouti", "smux")
MODS = ("bpsk", "qpsk", "16qam", "64qam")
CHANNELS = ("awgn", "rayleigh", "fixed")
RATE = 1e6                        # sample rate for the kb/s figures
GAP = 80
TS_BITS_PER_PACKET = 188 * 8 * 8 / 16          # TS bits per packet after the RS8+8 outer code (752)
CFO = 1e-4                        # cycles/sample (100 Hz at 1 MS/s)
PAD = 300
ACM_MODS = "bpsk,qpsk,16qam,64qam"

H_FIXED = np.array([[1.0, 0.45j], [-0.35, 0.9 * np.exp(0.7j)]])
H2_FIXED = 0.15 * np.array([[1, -1j], [0.5j, 1]])


# ---------------------------------------------------------------- channel
def draw_channel(kind, rng):
    if kind == "awgn":
        return np.eye(2, dtype=complex), np.zeros((2, 2), complex)
    if kind == "fixed":
        # normalised so that E|h|^2 per path = 1 like the other two channels
        p = np.mean(np.abs(H_FIXED) ** 2) + np.mean(np.abs(H2_FIXED) ** 2)
        return H_FIXED / np.sqrt(p), H2_FIXED / np.sqrt(p)
    g = lambda s: s * (rng.standard_normal((2, 2)) + 1j * rng.standard_normal((2, 2))) / np.sqrt(2)
    h, h2 = g(1.0), g(0.3)
    p = 1 + 0.09
    return h / np.sqrt(p), h2 / np.sqrt(p)


def through(x, H, H2, snr_db, tx_power, rng, phase0=0.0):
    y = H @ x + H2 @ np.roll(x, 3, axis=1)
    y = np.concatenate([np.zeros((2, PAD)), y, np.zeros((2, PAD))], axis=1)
    sigma = np.sqrt(tx_power / 10 ** (snr_db / 10) / 2)
    y = y + sigma * (rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape))
    return y * np.exp(2j * np.pi * CFO * np.arange(y.shape[1]) + 1j * phase0)


def tx_power(frame):
    """Total transmit power (both TX), measured on the frame body (after the preamble)."""
    return float(np.sum(np.mean(np.abs(frame[:, 400:]) ** 2, axis=1)))


def receive(rx, y):
    """Like MimoRxSink: detect, decode; a detection whose SIG fails is skipped and the search goes on one
    symbol later. Returns (packet(s), info) of the first detection that passed SIG, else (None, None)."""
    start = 0
    while True:
        s0, p = rx.find(y, start)
        if s0 is None or s0 + rx.body_len > y.shape[1]:
            return None, None
        (out, info), = rx.demodulate_batch(y, [s0], [p])
        if info["reason"] != "sig":
            return out, info
        start = s0 + 80


# ---------------------------------------------------------------- 1. fixed modulation
def fixed_point(args):
    mimo, mod, channel, soft, snr, frames, seed = args
    rng = np.random.default_rng(seed)
    tx = Modulator(mimo, mod, gap=GAP)
    rx = Demodulator(mimo, mod, soft=soft, soft_cpu=1e9)
    lost = missed = 0
    snr_eff = []
    for _ in range(frames):
        pkt = rng.integers(0, 256, 212, dtype=np.uint8).tobytes()
        x = tx.frame(pkt)
        H, H2 = draw_channel(channel, rng)
        y = through(x, H, H2, snr, tx_power(x), rng, rng.uniform(0, 2 * np.pi))
        out, info = receive(rx, y)
        missed += info is None
        lost += out != pkt
    fl = frame_samples(mimo, mod, GAP)
    cap = RATE / fl * TS_BITS_PER_PACKET / 1e3
    fer = lost / frames
    return {"mimo": mimo, "mod": mod, "channel": channel, "soft": int(soft), "snr_db": snr, "frames": frames,
            "lost": lost, "not_detected": missed, "fer": round(fer, 5), "capacity_kbps": round(cap, 1),
            "goodput_kbps": round((1 - fer) * cap, 1)}


# ---------------------------------------------------------------- 2. ACM
def acm_point(args):
    """One SNR point of the ACM curve: `blocks` channel realisations, each held for `block_s` seconds."""
    mimo, channel, snr, blocks, block_s, seed = args
    from ofdm321.acm import AcmController
    rng = np.random.default_rng(seed)
    tx = Modulator(mimo, "qpsk", gap=GAP, acm=True)
    rx = Demodulator(mimo, "qpsk", soft=True, soft_cpu=1e9, acm=True)
    T = frame_samples(mimo, "qpsk", GAP, acm=True) / RATE          # 3.76 ms
    frames_per_block = int(round(block_s / T))
    ctl = AcmController(ACM_MODS)
    delay = int(round(0.04 / T))                                    # ~40 ms feedback loop
    report_every = max(1, int(round(0.02 / T)))                     # report every 0.02 s
    window = max(1, int(round(0.1 / T)))                            # codeword error rate over 0.1 s
    t = 0.0
    snr_f = None
    pending, cw_hist = [], []
    sent = ok = 0
    used = {m: 0 for m in MODS}
    powers = {}
    for _ in range(blocks):
        H, H2 = draw_channel(channel, rng)
        phase = rng.uniform(0, 2 * np.pi)
        for _ in range(frames_per_block):
            t += T
            m = ctl.modcods[ctl.index]       # (the .modulation property times out on the wall clock)
            k = codewords_per_frame(mimo, m)
            pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(k)]
            x = tx.frame(pk, m)
            if m not in powers:
                powers[m] = tx_power(x)
            y = through(x, H, H2, snr, powers[m], rng, phase)
            sent += k
            used[m] += 1
            rx_mod, good = None, 0
            out, info = receive(rx, y)
            if out is not None and "reasons" in info:
                good = sum(r == "ok" for r in info["reasons"])
                rx_mod = info["modulation"]
                x_eff = info["snr_eff_db"]
                snr_f = x_eff if snr_f is None else 0.8 * snr_f + 0.2 * x_eff
                cw_hist.append((good, k))
            ok += good
            cw_hist = cw_hist[-window:]
            if snr_f is not None and int(round(t / T)) % report_every == 0 and rx_mod is not None:
                bad = sum(kk - g for g, kk in cw_hist)
                tot = sum(kk for _, kk in cw_hist)
                pending.append((t + delay * T, snr_f, bad / max(1, tot), rx_mod))
            while pending and pending[0][0] <= t:
                _, s_, c_, m_ = pending.pop(0)
                ctl.update(s_, c_, m_, now=t)
            if ctl.last_report is not None and t - ctl.last_report > ctl.timeout:   # feedback lost
                ctl.index, ctl.last_report = 0, None                                # (simulated time)
    dur = t
    frames = sum(used.values())
    return {"mimo": mimo, "channel": channel, "snr_db": snr, "seconds": round(dur, 2),
            "codewords_sent": sent, "codewords_ok": ok, "cer": round(1 - ok / max(1, sent), 5),
            "goodput_kbps": round(ok * TS_BITS_PER_PACKET / dur / 1e3, 1),
            **{f"share_{m}": round(used[m] / frames, 4) for m in MODS}}


def acm_capacity_kbps(mimo, mod):
    T = frame_samples(mimo, mod, GAP, acm=True) / RATE
    return codewords_per_frame(mimo, mod) * TS_BITS_PER_PACKET / T / 1e3


# ---------------------------------------------------------------- driver
def run_all(fn, jobs, workers, label):
    out = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(fn, jobs, chunksize=1), 1):
            out.append(r)
            if i % max(1, len(jobs) // 20) == 0 or i == len(jobs):
                print(f"  {label}: {i}/{len(jobs)} points, {time.time() - t0:.0f} s", flush=True)
    return out


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            try:
                r[k] = float(v) if any(c in v for c in ".e") else int(v)
            except (ValueError, TypeError):
                pass
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "results"))
    ap.add_argument("--frames", type=int, default=1000, help="frames per fixed-modulation point (1000)")
    ap.add_argument("--snr", type=float, nargs=3, default=[0, 35, 2], metavar=("MIN", "MAX", "STEP"))
    ap.add_argument("--acm-blocks", type=int, default=12, help="channel blocks per ACM point (12)")
    ap.add_argument("--acm-block-s", type=float, default=3.0, help="seconds per channel block (3.0)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--quick", action="store_true", help="few frames, coarse SNR: a fast check")
    ap.add_argument("--plots-only", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.quick:
        a.frames, a.acm_blocks, a.snr = 40, 2, [0, 35, 7]
    snrs = [float(s) for s in np.arange(a.snr[0], a.snr[1] + 1e-9, a.snr[2])]
    if not a.plots_only:
        t0 = time.time()
        jobs = [(mi, mo, ch, so, s, a.frames, 1000 + i)
                for i, (mi, mo, ch, so, s) in enumerate(
                    (mi, mo, ch, so, s) for mi in MIMOS for mo in MODS for ch in CHANNELS
                    for so in (True, False) for s in snrs)]
        print(f"fixed modulation: {len(jobs)} points x {a.frames} frames, {a.workers} processes", flush=True)
        jobs.sort(key=lambda j: -j[4])           # high SNR first: no soft trials, keeps workers busy evenly
        rows = run_all(fixed_point, jobs, a.workers, "fixed")
        rows.sort(key=lambda r: (MIMOS.index(r["mimo"]), MODS.index(r["mod"]), CHANNELS.index(r["channel"]),
                                 -r["soft"], r["snr_db"]))
        write_csv(out / "fer_sweep.csv", rows)
        acm_jobs = [(mi, ch, s, a.acm_blocks if ch == "rayleigh" else max(2, a.acm_blocks // 4),
                     a.acm_block_s, 5000 + i)
                    for i, (mi, ch, s) in enumerate((mi, ch, s) for mi in MIMOS for ch in CHANNELS for s in snrs)]
        print(f"ACM: {len(acm_jobs)} points", flush=True)
        arows = run_all(acm_point, acm_jobs, a.workers, "acm")
        arows.sort(key=lambda r: (MIMOS.index(r["mimo"]), CHANNELS.index(r["channel"]), r["snr_db"]))
        write_csv(out / "acm_sweep.csv", arows)
        print(f"done in {time.time() - t0:.0f} s -> {out}", flush=True)
    from sim_sweep_plots import make_plots   # noqa: E402  (tools/sim_sweep_plots.py)
    make_plots(read_csv(out / "fer_sweep.csv"), read_csv(out / "acm_sweep.csv"), out)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
