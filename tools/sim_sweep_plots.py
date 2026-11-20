"""Plots and summary numbers for tools/sim_sweep.py (matplotlib, Agg)."""
import json
from pathlib import Path

import numpy as np

MIMOS = ("siso", "simo", "alamouti", "smux")
MODS = ("bpsk", "qpsk", "16qam", "64qam")
CHANNELS = ("awgn", "rayleigh", "fixed")
CH_NAME = {"awgn": "AWGN (H = I)", "rayleigh": "Rayleigh 2×2 + tiếng vọng", "fixed": "kênh cố định có tiếng vọng"}
MOD_NAME = {"bpsk": "BPSK", "qpsk": "QPSK", "16qam": "16QAM", "64qam": "64QAM"}

# reference categorical palette, fixed order (slots 1-4), light mode; chrome tokens
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
FER_FLOOR = 1e-3            # plotted floor: FER 0 is drawn here (1000 frames per point resolve 1e-3)


def _style(plt):
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titlesize": 10,
        "axes.labelsize": 9, "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8,
        "ytick.labelsize": 8, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "grid.linestyle": "-", "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "legend.fontsize": 8, "lines.linewidth": 1.8,
        "font.family": "DejaVu Sans", "text.color": INK})


def _series(rows, **sel):
    pts = sorted((r["snr_db"], r) for r in rows if all(r[k] == v for k, v in sel.items()))
    return [p[0] for p in pts], [p[1] for p in pts]


def snr_at_fer(rows, target=0.01, **sel):
    """Smallest SNR (dB, log-interpolated) from which FER stays <= target; None if never."""
    x, rs = _series(rows, **sel)
    f = [r["fer"] for r in rs]
    ok = [i for i in range(len(f)) if all(v <= target for v in f[i:])]
    if not ok:
        return None
    i = ok[0]
    if i == 0:
        return x[0]
    f0, f1 = max(f[i - 1], 1e-6), max(f[i], 1e-6)
    t = (np.log10(f0) - np.log10(target)) / (np.log10(f0) - np.log10(f1))
    return round(x[i - 1] + t * (x[i] - x[i - 1]), 1)


def summary(fer_rows, acm_rows):
    s = {"snr_fer1pct_soft": {}, "snr_fer1pct_hard": {}, "soft_gain_db": {}, "best_fixed": {}, "acm": {}}
    for ch in CHANNELS:
        for mi in MIMOS:
            for mo in MODS:
                key = f"{ch}/{mi}/{mo}"
                a = snr_at_fer(fer_rows, mimo=mi, mod=mo, channel=ch, soft=1)
                b = snr_at_fer(fer_rows, mimo=mi, mod=mo, channel=ch, soft=0)
                s["snr_fer1pct_soft"][key], s["snr_fer1pct_hard"][key] = a, b
                s["soft_gain_db"][key] = None if a is None or b is None else round(b - a, 1)
    for ch in CHANNELS:
        for mi in MIMOS:
            snrs, _ = _series(acm_rows, mimo=mi, channel=ch)
            for snr in snrs:
                fixed = {mo: next(r["goodput_kbps"] for r in fer_rows if r["mimo"] == mi and r["mod"] == mo
                                  and r["channel"] == ch and r["soft"] == 1 and r["snr_db"] == snr) for mo in MODS}
                best = max(fixed, key=fixed.get)
                acm = next(r for r in acm_rows if r["mimo"] == mi and r["channel"] == ch and r["snr_db"] == snr)
                s["best_fixed"][f"{ch}/{mi}/{snr:g}"] = [best, fixed[best]]
                s["acm"][f"{ch}/{mi}/{snr:g}"] = [acm["goodput_kbps"], acm["cer"]]
    return s


def make_plots(fer_rows, acm_rows, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _style(plt)
    out = Path(out)
    snr_max = max(r["snr_db"] for r in fer_rows)

    def fer_axis(ax):
        ax.set_yscale("log")
        ax.set_ylim(FER_FLOOR * 0.8, 1.2)
        ax.set_xlim(0, snr_max)
        ax.axhline(0.01, color=MUTED, lw=0.8, ls=(0, (2, 3)))

    # 1. FER vs SNR: rows = channel, columns = MIMO mode, one line per modulation (soft decoding)
    fig, axs = plt.subplots(3, 4, figsize=(15, 10), sharex=True, sharey=True)
    for i, ch in enumerate(CHANNELS):
        for j, mi in enumerate(MIMOS):
            ax = axs[i, j]
            fer_axis(ax)
            for c, mo in zip(SERIES, MODS):
                x, rs = _series(fer_rows, mimo=mi, mod=mo, channel=ch, soft=1)
                ax.plot(x, [max(r["fer"], FER_FLOOR) for r in rs], color=c, label=MOD_NAME[mo])
            if i == 0:
                ax.set_title(mi, fontsize=11)
            if j == 0:
                ax.set_ylabel(f"{CH_NAME[ch]}\nFER (tỉ lệ mất gói)")
            if i == 2:
                ax.set_xlabel("SNR (dB)")
    handles, labels = axs[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 0.995))
    fig.suptitle("FER theo SNR, giải mềm bật (đường chấm: FER 1%; FER 0 vẽ ở 10⁻³)", y=0.965, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out / "fer_vs_snr.png", dpi=110)
    plt.close(fig)

    # 2. goodput vs SNR, fixed modulations and ACM: rows = awgn / rayleigh, columns = MIMO mode
    fig, axs = plt.subplots(2, 4, figsize=(15, 7.5), sharex=True)
    for i, ch in enumerate(("awgn", "rayleigh")):
        for j, mi in enumerate(MIMOS):
            ax = axs[i, j]
            ax.set_xlim(0, snr_max)
            for c, mo in zip(SERIES, MODS):
                x, rs = _series(fer_rows, mimo=mi, mod=mo, channel=ch, soft=1)
                ax.plot(x, [r["goodput_kbps"] for r in rs], color=c, lw=1.4, label=f"{MOD_NAME[mo]} cố định")
            x, rs = _series(acm_rows, mimo=mi, channel=ch)
            ax.plot(x, [r["goodput_kbps"] for r in rs], color=INK, lw=2.6, label="ACM")
            ax.set_ylim(bottom=0)
            if i == 0:
                ax.set_title(mi, fontsize=11)
            if j == 0:
                ax.set_ylabel(f"{CH_NAME[ch]}\nthông lượng hữu ích (kb/s TS)")
            if i == 1:
                ax.set_xlabel("SNR (dB)")
    handles, labels = axs[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 0.995))
    fig.suptitle("Thông lượng hữu ích ở 1 MS/s (sau RS8+8): điều chế cố định so với ACM", y=0.955, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out / "throughput_vs_snr.png", dpi=110)
    plt.close(fig)

    # 3. MIMO comparison on Rayleigh: QPSK FER, and the best goodput any modulation gives
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, mo in zip(axs[:2], ("qpsk", "16qam")):
        fer_axis(ax)
        for c, mi in zip(SERIES, MIMOS):
            x, rs = _series(fer_rows, mimo=mi, mod=mo, channel="rayleigh", soft=1)
            ax.plot(x, [max(r["fer"], FER_FLOOR) for r in rs], color=c, label=mi)
        ax.set_title(f"{MOD_NAME[mo]}, Rayleigh: FER theo chế độ MIMO")
        ax.set_xlabel("SNR (dB)")
        ax.set_ylabel("FER")
        ax.legend(loc="lower left")
    ax = axs[2]
    for c, mi in zip(SERIES, MIMOS):
        x, rs = _series(acm_rows, mimo=mi, channel="rayleigh")
        ax.plot(x, [r["goodput_kbps"] for r in rs], color=c, label=mi)
    ax.set_xlim(0, snr_max)
    ax.set_ylim(bottom=0)
    ax.set_title("ACM, Rayleigh: thông lượng hữu ích theo chế độ MIMO")
    ax.set_xlabel("SNR (dB)")
    ax.set_ylabel("kb/s TS")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "mimo_compare.png", dpi=110)
    plt.close(fig)

    # 4. soft decoding: hard (dashed) vs soft (solid) on Rayleigh, and the SNR gain at FER 1%
    s = summary(fer_rows, acm_rows)
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, mi in zip(axs[:2], ("siso", "alamouti")):
        fer_axis(ax)
        for c, mo in zip(SERIES, MODS):
            for soft, ls in ((1, "-"), (0, (0, (4, 2)))):
                x, rs = _series(fer_rows, mimo=mi, mod=mo, channel="rayleigh", soft=soft)
                ax.plot(x, [max(r["fer"], FER_FLOOR) for r in rs], color=c, ls=ls,
                        label=f"{MOD_NAME[mo]} {'mềm' if soft else 'cứng'}")
        ax.set_title(f"{mi}, Rayleigh: giải mềm (liền) và cứng (đứt)")
        ax.set_xlabel("SNR (dB)")
        ax.set_ylabel("FER")
        ax.legend(loc="lower left", ncol=2, fontsize=7)
    ax = axs[2]
    width = 0.18
    for k, (c, mo) in enumerate(zip(SERIES, MODS)):
        vals = [s["soft_gain_db"].get(f"rayleigh/{mi}/{mo}") for mi in MIMOS]
        xs = np.arange(len(MIMOS)) + (k - 1.5) * (width + 0.02)
        ax.bar(xs, [v if v is not None else 0 for v in vals], width=width, color=c, label=MOD_NAME[mo])
        for xx, v in zip(xs, vals):
            ax.text(xx, (v or 0) + 0.05, "–" if v is None else f"{v:.1f}", ha="center", va="bottom",
                    fontsize=7, color=INK2)
    ax.set_xticks(np.arange(len(MIMOS)))
    ax.set_xticklabels(MIMOS)
    ax.axhline(0, color=AXIS, lw=1)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("dB")
    ax.set_title("Lợi ích giải mềm ở FER 1% (Rayleigh)")
    ax.set_xlabel("– : không đạt FER 1% trong 0–35 dB")
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out / "soft_gain.png", dpi=110)
    plt.close(fig)

    (out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
    for name in ("fer_vs_snr.png", "throughput_vs_snr.png", "mimo_compare.png", "soft_gain.png", "summary.json"):
        print(out / name)
    return s
