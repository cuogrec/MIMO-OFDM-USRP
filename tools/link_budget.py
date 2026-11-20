#!/usr/bin/python3
"""Where are frames lost at low SNR? Stage-by-stage breakdown of the receiver (numpy only, no radio).

For every frame the stages are checked in order:
    (a) detected      Schmidl-Cox found a frame start
    (b) timing ok     start inside the ISI-free range (true start -10 .. +3 samples: CP 16, backoff 3, echo 3)
    (c) SIG ok        SIG CRC passed (sig_crc_err); sig_err also counts frames the v0.6 SIG-less
                      recovery rescued as good
    (d) RS ok         the codeword(s) decoded and passed CRC32, given (c)
    (e) raw BER / byte error rate before RS, measured on a "genie" pass
A genie pass decodes the same samples again with the true start, the true CFO and the true SIG, so it
shows the limit of the data path alone (equaliser + demapper + RS).

Frames: fixed-modulation frames (one 212-byte packet) and ACM frames (k codewords, modulation held
fixed), siso/alamouti x bpsk/qpsk, channel awgn/fixed (as tools/sim_sweep.py; SNR = total TX power
x E|h|^2 / noise per RX antenna). Receivers: --rx v0.5 (as released) and v0.6 (current defaults).

    python3 tools/link_budget.py                     # both receivers -> results/stage_breakdown.{csv,png}
    python3 tools/link_budget.py --quick
    python3 tools/link_budget.py --plots-only
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from ofdm321.modem import (Demodulator, Modulator, N_FFT, SIG_MAGIC, codewords_per_frame,  # noqa: E402
                           encode_packet)
from sim_sweep import CFO, GAP, PAD, draw_channel, read_csv, through, tx_power, write_csv  # noqa: E402

KINDS = ("fixed", "acm")
MIMOS = ("siso", "alamouti")
MODS = ("bpsk", "qpsk")
CHANNELS = ("awgn", "fixed")
# receiver variants: Demodulator keyword arguments
RX_VARIANTS = {
    "v0.5": dict(threshold=0.60, fine_timing=False, sig_recover=False),
    "v0.6": dict(),                      # current defaults
}
TIMING_OK = (-10, 3)


def sig_head(tx, seq, modulation):
    """SIG head exactly as the receiver decodes it (magic, mode byte, seq hi, seq lo[, crc])."""
    b = np.packbits(tx.sig_bits(seq, modulation))
    if tx.acm:
        return bytes([SIG_MAGIC, b[0], 0, b[1]])
    return b.tobytes()


def point(args):
    kind, mimo, mod, channel, snr, frames, variant, seed = args
    rng = np.random.default_rng(seed)
    acm = kind == "acm"
    tx = Modulator(mimo, mod, gap=GAP, acm=acm)
    rx = Demodulator(mimo, mod, soft=True, soft_cpu=1e9, acm=acm, **RX_VARIANTS[variant])
    rx.keep_raw = True
    k = codewords_per_frame(mimo, mod) if acm else 1
    p_true = np.exp(2j * np.pi * CFO * (N_FFT // 2))
    c = dict(false_alarms=0, detected=0, timing_ok=0, sig_crc_ok=0, sig_ok=0, cw_ok=0, cw_genie_ok=0, bit_err=0, byte_err=0)
    power = None
    for _ in range(frames):
        pk = [rng.integers(0, 256, 212, dtype=np.uint8).tobytes() for _ in range(k)]
        head = sig_head(tx, tx.seq, mod)
        x = tx.frame(pk if acm else pk[0], mod)
        power = power or tx_power(x)
        H, H2 = draw_channel(channel, rng)
        y = through(x, H, H2, snr, power, rng, rng.uniform(0, 2 * np.pi))
        sent = np.stack([encode_packet(p) for p in pk])            # transmitted (scrambled) codewords
        # genie: true start, true CFO, true SIG
        (out, info), = rx.demodulate_batch(y, [PAD], [p_true], known_sig=[head])
        got = out if acm else [out]
        c["cw_genie_ok"] += sum(p in got for p in pk)
        raw = np.asarray(info["raw"]).reshape(k, -1)
        c["bit_err"] += int(np.unpackbits(raw ^ sent).sum())
        c["byte_err"] += int(np.count_nonzero(raw != sent))
        # the real receiver
        # the real receiver: like MimoRxSink, a detection whose SIG fails is skipped (search again
        # one symbol later); detections far from the true start are false alarms
        start, found = 0, None
        while True:
            s0, p = rx.find(y, start)
            if s0 is None or s0 + rx.body_len > y.shape[1]:
                break
            (out, info), = rx.demodulate_batch(y, [s0], [p])
            if abs(s0 - PAD) <= 2 * N_FFT:
                found = s0, out, info
                break
            c["false_alarms"] += 1
            if info["reason"] != "sig":
                break
            start = s0 + N_FFT + 16
        if found is None:
            continue
        s0, out, info = found
        c["detected"] += 1
        c["timing_ok"] += TIMING_OK[0] <= s0 - PAD <= TIMING_OK[1]
        c["sig_crc_ok"] += info["reason"] != "sig" and not info.get("sig_recovered")
        if info["reason"] == "sig":
            continue
        c["sig_ok"] += 1
        got = (out or []) if acm else [out]
        c["cw_ok"] += sum(p in got for p in pk)
    n = frames
    return {"rx": variant, "kind": kind, "mimo": mimo, "mod": mod, "channel": channel, "snr_db": snr,
            "frames": n, "codewords": n * k, **c,
            "miss": round(1 - c["detected"] / n, 5),
            "timing_err": round(1 - c["timing_ok"] / max(1, c["detected"]), 5),
            "sig_crc_err": round(1 - c["sig_crc_ok"] / max(1, c["detected"]), 5),
            "sig_err": round(1 - c["sig_ok"] / max(1, c["detected"]), 5),
            "rs_err": round(1 - c["cw_ok"] / max(1, c["sig_ok"] * k), 5) if c["sig_ok"] else 1.0,
            "fer": round(1 - c["cw_ok"] / (n * k), 5),
            "genie_fer": round(1 - c["cw_genie_ok"] / (n * k), 5),
            "ber": c["bit_err"] / (n * k * 248 * 8), "byte_er": c["byte_err"] / (n * k * 248)}


# ------------------------------------------------------------------ analysis
def snr_at(rows, col, target=0.01, **sel):
    """Smallest SNR (log-interpolated) from which `col` stays <= target; None if never."""
    pts = sorted((r["snr_db"], r[col]) for r in rows if all(r[a] == b for a, b in sel.items()))
    x, f = [p[0] for p in pts], [p[1] for p in pts]
    ok = [i for i in range(len(f)) if all(v <= target for v in f[i:])]
    if not ok:
        return None
    i = ok[0]
    if i == 0:
        return x[0]
    f0, f1 = max(f[i - 1], 1e-6), max(f[i], 1e-6)
    t = (np.log10(f0) - np.log10(target)) / (np.log10(f0) - np.log10(f1))
    return round(x[i - 1] + t * (x[i] - x[i - 1]), 1)


def summarize(rows):
    out = {}
    for v in sorted({r["rx"] for r in rows}):
        for kind in KINDS:
            for ch in CHANNELS:
                for mi in MIMOS:
                    for mo in MODS:
                        sel = dict(rx=v, kind=kind, channel=ch, mimo=mi, mod=mo)
                        if not any(all(r[a] == b for a, b in sel.items()) for r in rows):
                            continue
                        out[f"{v}/{kind}/{ch}/{mi}/{mo}"] = {
                            col: snr_at(rows, col, **sel)
                            for col in ("fer", "genie_fer", "miss", "timing_err", "sig_crc_err", "sig_err")}
    return out


def make_plot(rows, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sys.path.insert(0, str(HERE))
    from sim_sweep_plots import FER_FLOOR, INK, MUTED, SERIES, _style
    _style(plt)
    variants = sorted({r["rx"] for r in rows})
    stages = (("miss", "không dò được khung", SERIES[0]), ("sig_crc_err", "SIG sai CRC (khi đã dò được)", SERIES[1]),
              ("rs_err", "RS hỏng (khi SIG đúng)", SERIES[2]), ("genie_fer", "genie: chỉ đường dữ liệu", SERIES[3]))
    combos = [(kind, mi, ch) for kind in KINDS for ch in CHANNELS for mi in MIMOS]
    fig, axs = plt.subplots(len(combos), 2 * len(variants), figsize=(4.2 * 2 * len(variants), 2.6 * len(combos)),
                            sharex=True, sharey=True)
    axs = np.atleast_2d(axs)
    for i, (kind, mi, ch) in enumerate(combos):
        for j, (v, mo) in enumerate((v, mo) for v in variants for mo in MODS):
            ax = axs[i, j]
            ax.set_yscale("log")
            ax.set_ylim(FER_FLOOR * 0.8, 1.2)
            ax.axhline(0.01, color=MUTED, lw=0.8, ls=(0, (2, 3)))
            sel = [r for r in rows if r["rx"] == v and r["kind"] == kind and r["mimo"] == mi and r["channel"] == ch
                   and r["mod"] == mo]
            sel.sort(key=lambda r: r["snr_db"])
            if not sel:
                continue
            x = [r["snr_db"] for r in sel]
            for col, name, color in stages:
                ax.plot(x, [max(r[col], FER_FLOOR) for r in sel], color=color, lw=1.4, label=name)
            ax.plot(x, [max(r["fer"], FER_FLOOR) for r in sel], color=INK, lw=2.4, label="FER tổng")
            if i == 0:
                ax.set_title(f"bộ thu {v}: {mo.upper()}", fontsize=10)
            if j == 0:
                ax.set_ylabel(f"{kind} {mi}\n{ch}", fontsize=8)
            if i == len(combos) - 1:
                ax.set_xlabel("SNR (dB)")
    h, lab = axs[0, 0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 0.998))
    fig.suptitle("Tỉ lệ lỗi từng khâu theo SNR (khung cố định / ACM; đường chấm: 1%)", y=0.985, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    fig.savefig(Path(out) / "stage_breakdown.png", dpi=100)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "results"))
    ap.add_argument("--frames", type=int, default=1000)
    ap.add_argument("--snr", type=float, nargs=3, default=[0, 20, 1], metavar=("MIN", "MAX", "STEP"))
    ap.add_argument("--rx", nargs="+", default=list(RX_VARIANTS), choices=list(RX_VARIANTS))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--plots-only", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.quick:
        a.frames, a.snr = 100, [2, 14, 2]
    path = out / "stage_breakdown.csv"
    if not a.plots_only:
        snrs = [float(s) for s in np.arange(a.snr[0], a.snr[1] + 1e-9, a.snr[2])]
        jobs = [(kind, mi, mo, ch, s, a.frames, v, 3000 + i) for i, (v, kind, mi, mo, ch, s) in enumerate(
            (v, kind, mi, mo, ch, s) for v in a.rx for kind in KINDS for mi in MIMOS for mo in MODS
            for ch in CHANNELS for s in snrs)]
        print(f"stage breakdown: {len(jobs)} points x {a.frames} frames, {a.workers} processes", flush=True)
        t0, rows = time.time(), []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for i, r in enumerate(ex.map(point, jobs, chunksize=1), 1):
                rows.append(r)
                if i % max(1, len(jobs) // 10) == 0 or i == len(jobs):
                    print(f"  {i}/{len(jobs)} points, {time.time() - t0:.0f} s", flush=True)
        rows.sort(key=lambda r: (r["rx"], KINDS.index(r["kind"]), MIMOS.index(r["mimo"]), MODS.index(r["mod"]),
                                 CHANNELS.index(r["channel"]), r["snr_db"]))
        write_csv(path, rows)
    rows = read_csv(path)
    for r in rows:
        r["snr_db"] = float(r["snr_db"])
    s = summarize(rows)
    (out / "stage_breakdown.json").write_text(json.dumps(s, indent=1))
    make_plot(rows, out)
    print("SNR (dB) where each error rate stays <= 1%:  fer | genie | miss | timing | SIG CRC | SIG after recovery")
    for key, v in s.items():
        print(f"  {key:34s} " + " | ".join("  –  " if v[c] is None else f"{v[c]:5.1f}"
                                           for c in ("fer", "genie_fer", "miss", "timing_err", "sig_crc_err", "sig_err")))
    print(path)


if __name__ == "__main__":
    main()
