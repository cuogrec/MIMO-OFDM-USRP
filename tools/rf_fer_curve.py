#!/usr/bin/python3
"""RF frame/codeword error rate vs -EVM, from RX report.json history (one point per second), several runs
together, with the simulated curves of results/fer_sweep.csv on top.

FER of one second = (RS + CRC failures + SIG failures x codewords per frame) / all codewords of that second
(counter differences of the RX history). Skipped: ACM seconds in which the modulation changed, and seconds
whose EVM differs by more than 1.5 dB from the previous or next one (level steps: the EVM is a snapshot at the
end of the second, the errors accumulate over it).
The simulated curves are given against SNR; they are mapped to EVM with the receiver's own decision-directed
EVM estimate (the one in the RX log), measured here on simulated frames of the same mode/modulation/channel.

    python3 tools/rf_fer_curve.py rf_data/20261123            # -> results/rf_20261123.csv / .png
    python3 tools/rf_fer_curve.py runA/report.json runB --date 20261123 --out results
"""
import argparse
import csv
import json
from pathlib import Path
import re
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

MODS = ("bpsk", "qpsk", "16qam", "64qam")
MIN_CODEWORDS = 20                     # skip seconds with fewer codewords
STABLE_DB = 1.5                        # skip seconds whose EVM differs more from the previous/next second
FER_FLOOR = 1e-4


def find_reports(paths):
    """RX report.json files under the given files/folders; one per run folder name (copies are skipped)."""
    seen, out = set(), []
    for p in map(Path, paths):
        files = [p] if p.is_file() else sorted(p.rglob("report.json"))
        for f in files:
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if r.get("role") != "rx" or not r.get("history") or f.parent.name in seen:
                continue
            seen.add(f.parent.name)
            r["_path"] = f
            out.append(r)
    return out


def points(rep):
    """One row per second with enough codewords."""
    from ofdm321.modem import codewords_per_frame
    s = rep["settings"]
    mimo, acm = s["mimo_mode"], bool(s.get("acm"))
    rows, prev = [], None
    hist = rep["history"]
    for i, h in enumerate(hist):
        nxt = hist[i + 1] if i + 1 < len(hist) else None
        # the EVM is a snapshot at the end of the second, the errors accumulate over it: a second in which
        # the level changed (gain steps) would pair errors of one level with the EVM of another
        stable = all(o is not None and o.get("evm_db") is not None and h.get("evm_db") is not None
                     and abs(o["evm_db"] - h["evm_db"]) <= STABLE_DB for o in (prev, nxt))
        if prev is not None and stable and h.get("ok") is not None and prev.get("ok") is not None:
            mod = h.get("modulation") or s["modulation"]
            same = not acm or (prev.get("modulation") == mod)
            d = {k: (h.get(k) or 0) - (prev.get(k) or 0) for k in ("ok", "rs", "crc", "sig")}
            k = codewords_per_frame(mimo, mod) if acm else 1
            bad = d["rs"] + d["crc"] + d["sig"] * k
            tot = d["ok"] + bad
            if same and tot >= MIN_CODEWORDS and h.get("evm_db") is not None and mod in MODS:
                rows.append({"run": rep["_path"].parent.name, "mimo": mimo, "mod": mod, "acm": int(acm),
                             "t": h["t"], "evm_db": round(h["evm_db"], 2),
                             "snr_db": None if h.get("snr_db") is None else round(h["snr_db"], 2),
                             "rx_rms": h.get("rx_rms"), "codewords": tot, "errors": bad,
                             "fer": round(bad / tot, 5)})
        prev = h
    return rows


def evm_of_snr(mimo, mod, channel, snrs, frames=24, seed=5):
    """Median decision-directed EVM (dB, as Demodulator reports it) of simulated frames at each SNR."""
    from ofdm321.modem import Demodulator, Modulator
    from sim_sweep import draw_channel, through, tx_power
    rng = np.random.default_rng(seed)
    tx, rx = Modulator(mimo, mod), Demodulator(mimo, mod, soft=False)
    out = []
    for snr in snrs:
        ev = []
        for _ in range(frames):
            x = tx.frame(rng.integers(0, 256, 212, dtype=np.uint8).tobytes())
            H, H2 = draw_channel(channel, rng)
            y = through(x, H, H2, snr, tx_power(x), rng, rng.uniform(0, 2 * np.pi))
            s0, p = rx.find(y)
            if s0 is None or s0 + rx.body_len > y.shape[1]:
                continue
            rx.count = 0                        # link statistics (EVM) on every frame
            _, info = rx.demodulate(y, s0, p)
            if "evm_db" in info:
                ev.append(info["evm_db"])
        out.append(float(np.median(ev)) if ev else np.nan)
    return np.array(out)


def sim_curves(combos, sweep_csv):
    """{(mimo, mod, channel): (minus_evm array, fer array)} from fer_sweep.csv (soft decoding)."""
    from sim_sweep import read_csv
    rows = read_csv(sweep_csv)
    curves = {}
    for mimo, mod in combos:
        for ch in ("awgn", "fixed"):
            pts = sorted((r["snr_db"], r["fer"]) for r in rows if r["mimo"] == mimo and r["mod"] == mod
                         and r["channel"] == ch and r["soft"] == 1)
            if not pts:
                continue
            snr = np.array([p[0] for p in pts])
            evm = evm_of_snr(mimo, mod, ch, snr)
            ok = ~np.isnan(evm)
            curves[(mimo, mod, ch)] = (-evm[ok], np.array([p[1] for p in pts])[ok])
    return curves


def plot(rows, curves, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sim_sweep_plots import INK2, MUTED, SERIES, _style
    _style(plt)
    mimos = [m for m in ("siso", "simo", "alamouti", "smux") if any(r["mimo"] == m for r in rows)]
    fig, axs = plt.subplots(1, len(mimos), figsize=(6.5 * len(mimos), 5.2), squeeze=False)
    runs = sorted({r["run"] for r in rows})
    shapes = "os^Dv<>p*hPXd"
    markers = {run: shapes[i % len(shapes)] for i, run in enumerate(runs)}
    for ax, mimo in zip(axs[0], mimos):
        ax.set_yscale("log")
        ax.axhline(0.01, color=MUTED, lw=0.8, ls=(0, (2, 3)))
        for c, mod in zip(SERIES, MODS):
            for ch, ls in (("awgn", "-"), ("fixed", (0, (4, 2)))):
                if (mimo, mod, ch) in curves:
                    x, f = curves[(mimo, mod, ch)]
                    ax.plot(x, np.maximum(f, FER_FLOOR), color=c, lw=1.4, ls=ls, alpha=0.9,
                            label=f"mô phỏng {mod.upper()} {'AWGN' if ch == 'awgn' else 'kênh cố định'}")
            for run in runs:
                pts = [r for r in rows if r["mimo"] == mimo and r["mod"] == mod and r["run"] == run]
                if pts:
                    ax.scatter([-r["evm_db"] for r in pts], [max(r["fer"], FER_FLOOR) for r in pts], s=18,
                               color=c, marker=markers[run], edgecolors="white", linewidths=0.4, zorder=3,
                               label=f"RF {mod.upper()} {run[9:15]}{' ACM' if pts[0]['acm'] else ''}")
        ax.set_ylim(FER_FLOOR * 0.7, 1.3)
        ax.set_xlabel("−EVM (dB, ước lượng của bộ thu)")
        ax.set_ylabel(f"tỉ lệ codeword lỗi mỗi giây (0 vẽ ở {FER_FLOOR:g})")
        ax.set_title(mimo)
        ax.legend(loc="upper right", fontsize=6.5, ncol=1)
    fig.suptitle(title, fontsize=10, color=INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_timelines(reps, path, max_runs=16):
    """Per run (rows): codeword errors per second (RS + CRC + SIG), RX level per port (rx_rms_rf0 / rf1, dBFS)
    and cond (2-TX modes): periodic error bursts and a weak or interfered port show up side by side."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sim_sweep_plots import INK2, MUTED, SERIES, _style
    _style(plt)
    runs = []
    for r in reps:
        h = r["history"]
        if len(h) < 3 or not any((x.get("ok") or 0) > 0 for x in h):
            continue
        runs.append(r)
    runs = runs[:max_runs]
    if not runs:
        return None
    fig, axs = plt.subplots(len(runs), 3, figsize=(16, 1.9 * len(runs) + 0.8), squeeze=False)
    for i, r in enumerate(runs):
        h, s = r["history"], r["settings"]
        t = [x["t"] for x in h[1:]]
        d = lambda k: [max(0, (b.get(k) or 0) - (a.get(k) or 0)) for a, b in zip(h, h[1:])]
        err = [a + b + c for a, b, c in zip(d("rs"), d("crc"), d("sig"))]
        ok = d("ok")
        name = f"{r['_path'].parent.name[9:15]} {s['mimo_mode']} {s['modulation']}{' ACM' if s.get('acm') else ''}"
        ax = axs[i, 0]
        ax.plot(t, err, color=SERIES[1], lw=1.2, drawstyle="steps-post")
        ax.set_ylabel(name, fontsize=7, color=INK2)
        ax.set_title("codeword lỗi / s (RS + CRC + SIG)" if i == 0 else "", fontsize=9)
        ax.text(0.99, 0.92, f"tổng {sum(err)} lỗi / {sum(ok) + sum(err)}", transform=ax.transAxes, ha="right",
                va="top", fontsize=7, color=MUTED)
        ax = axs[i, 1]
        for c, k in enumerate(("rx_rms_rf0", "rx_rms_rf1")):
            pts = [(x["t"], 20 * np.log10(x[k])) for x in h if x.get(k)]
            if pts:
                ax.plot(*zip(*pts), color=SERIES[c], lw=1.0, label="RF0" if c == 0 else "RF1")
        ax.set_title("mức thu (dBFS)" if i == 0 else "", fontsize=9)
        if i == 0:
            ax.legend(loc="lower right", fontsize=7)
        ax = axs[i, 2]
        pts = [(x["t"], x["cond_db"]) for x in h if x.get("cond_db") is not None]
        if pts:
            ax.plot(*zip(*pts), color=SERIES[2], lw=1.0)
        else:
            ax.text(0.5, 0.5, "1 anten phát: không có cond", transform=ax.transAxes, ha="center", fontsize=7,
                    color=MUTED)
        ax.set_title("cond (dB)" if i == 0 else "", fontsize=9)
    for ax in axs[-1]:
        ax.set_xlabel("s")
    fig.tight_layout()
    fig.savefig(path, dpi=95)
    plt.close(fig)
    return path


def _stays_below(xs, fers, target=0.01):
    """Smallest x from which every point (sorted by x) has FER <= target, or None."""
    ok = [i for i in range(len(xs)) if all(f <= target for f in fers[i:])]
    return None if not ok else float(xs[ok[0]])


def summary(rows, curves):
    """Per MIMO mode / modulation: -EVM (1 dB bins, all RF seconds pooled) from which the codeword error
    rate stays <= 1 %, the same for the simulated AWGN and fixed-channel curves, and the gap RF - sim."""
    out = []
    for mimo, mod in sorted({(r["mimo"], r["mod"]) for r in rows}, key=lambda k: (k[0], MODS.index(k[1]))):
        pts = [r for r in rows if r["mimo"] == mimo and r["mod"] == mod]
        bins = {}
        for r in pts:
            b = np.floor(-r["evm_db"])
            e, c = bins.get(b, (0, 0))
            bins[b] = (e + r["errors"], c + r["codewords"])
        xs = sorted(b for b, (e, c) in bins.items() if c >= 200)
        fers = [bins[b][0] / bins[b][1] for b in xs]
        rf = _stays_below(xs, fers)
        # the RF levels come in gain steps: the threshold lies between the last failing and the first good bin
        bad = [x for x, f in zip(xs, fers) if f > 0.01 and (rf is None or x < rf)]
        lo = bad[-1] + 1 if bad else None
        d = {"mimo": mimo, "mod": mod, "seconds": len(pts), "runs": sorted({r["run"] for r in pts}),
             "minus_evm_range_db": [round(min(-r["evm_db"] for r in pts), 1), round(max(-r["evm_db"] for r in pts), 1)],
             "codewords": sum(r["codewords"] for r in pts), "errors": sum(r["errors"] for r in pts),
             "rf_minus_evm_fer1pct_db": rf, "rf_between_db": [lo, rf],
             "fer_per_bin": {f"{b:g}": round(f, 4) for b, f in zip(xs, fers)}}
        for ch in ("awgn", "fixed"):
            if (mimo, mod, ch) in curves:
                x, f = curves[(mimo, mod, ch)]
                o = np.argsort(x)
                v = _stays_below(list(x[o]), list(f[o]))
                d[f"sim_{ch}_minus_evm_fer1pct_db"] = None if v is None else round(v, 1)

        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="RX report.json files or folders (searched recursively)")
    ap.add_argument("--date", help="name of the outputs rf_<date>.* (default: from the run folder names)")
    ap.add_argument("--out", default=str(REPO / "results"))
    ap.add_argument("--sweep", default=str(REPO / "results" / "fer_sweep.csv"))
    a = ap.parse_args(argv)
    reps = find_reports(a.paths)
    if not reps:
        sys.exit("no RX report.json with a history found")
    rows = [p for r in reps for p in points(r)]
    if not rows:
        sys.exit("no usable seconds (too few codewords or no EVM)")
    date = a.date or (re.match(r"(\d{8})", reps[0]["_path"].parent.name) or [None, "rf"])[1]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    combos = sorted({(r["mimo"], r["mod"]) for r in rows})
    curves = sim_curves(combos, a.sweep) if Path(a.sweep).exists() else {}
    with open(out / f"rf_{date}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    summ = summary(rows, curves)
    (out / f"rf_{date}_summary.json").write_text(json.dumps(summ, indent=1))
    plot(rows, curves, out / f"rf_{date}.png",
         f"RF {date}: tỉ lệ codeword lỗi theo −EVM ({len(reps)} lượt, {len(rows)} giây); đường: mô phỏng")
    print("  -EVM (dB) from which the codeword error rate stays <= 1 %:  RF | sim AWGN | sim fixed channel")
    for s in summ:
        print(f"  {s['mimo']:8s} {s['mod']:6s} {s['seconds']:4d} s, -EVM {s['minus_evm_range_db'][0]:5.1f}..{s['minus_evm_range_db'][1]:4.1f}: "
              f"{'?' if s['rf_between_db'][0] is None else s['rf_between_db'][0]:>4}..{s['rf_between_db'][1]} | "
              f"{s.get('sim_awgn_minus_evm_fer1pct_db')} | {s.get('sim_fixed_minus_evm_fer1pct_db')}"
              f"  (errors {s['errors']}/{s['codewords']})")
    print(out / f"rf_{date}.csv")
    print(out / f"rf_{date}.png")
    tl = plot_timelines(reps, out / f"rf_{date}_timeline.png")
    if tl:
        print(tl)


if __name__ == "__main__":
    main()
