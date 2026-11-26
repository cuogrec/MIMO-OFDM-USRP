#!/usr/bin/python3
"""Spectrum survey of the 920-923 MHz band, RECEIVE ONLY (no transmit stream is ever created).

Records both N321 channels (RF0 = A:0, RF1 = B:0, RX2) at a few centre frequencies, then per capture:
waterfall (power vs time and frequency), power vs time, narrow lines (and their spacing), bursts and their
period (autocorrelation of the burst indicator), RF0 vs RF1. Over the band: the cleanest 1 MHz video channel
(away from the radio feedback carrier 922.7 MHz +- 1 MHz). Outputs results/survey_<date>.png / .json.

    # N321 connected, nothing transmitting (receive only):
    python3 tools/spectrum_survey.py --args addr=192.168.180.95 --gain 50 --seconds 25
    # recorded IQ, no hardware (.npz from --save-iq, or a raw complex64 .cfile with --rate/--center):
    python3 tools/spectrum_survey.py --iq runs/survey_iq/*.npz
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

BAND = (920.0e6, 923.0e6)          # Vietnam: transmit only here
FEEDBACK = (922.7e6, 1.0e6)        # radio feedback carrier and the guard kept around it
DEFAULT_CENTERS = (920.5e6, 921.0e6, 921.5e6, 922.0e6, 922.5e6)
CH_NAMES = ("RF0", "RF1")


# ---------------------------------------------------------------- capture (receive only)
def capture_n321(args, center, rate, seconds, gain, path):
    """Record 2 channels to `path` (interleaved ch0/ch1 complex64 via two file sinks) with a gr-uhd
    usrp_source. No usrp_sink is created anywhere in this tool."""
    from gnuradio import blocks, gr, uhd
    n = int(seconds * rate)
    src = uhd.usrp_source(args, uhd.stream_args(cpu_format="fc32", otw_format="sc16", channels=[0, 1]))
    src.set_subdev_spec("A:0 B:0", 0)
    src.set_samp_rate(rate)
    for ch in (0, 1):
        src.set_center_freq(center, ch)
        src.set_gain(gain, ch)
        src.set_antenna("RX2", ch)
    time.sleep(0.5)                                        # LO settle
    tb = gr.top_block("spectrum survey (RX only)")
    heads = [blocks.head(gr.sizeof_gr_complex, n) for _ in range(2)]
    files = [blocks.file_sink(gr.sizeof_gr_complex, f"{path}.ch{ch}", False) for ch in (0, 1)]
    for ch in (0, 1):
        tb.connect((src, ch), heads[ch], files[ch])
    tb.run()
    for f in files:
        f.close()
    return [f"{path}.ch{ch}" for ch in (0, 1)]


def load_iq(path, rate=None, center=None):
    """-> (iq (2, n) complex64 array or memmaps, rate, center). .npz: keys iq, rate, center. .cfile: one channel
    (raw complex64), or two files given as 'a.cfile,b.cfile'."""
    p = str(path)
    if p.endswith(".npz"):
        z = np.load(p)
        return z["iq"], float(z["rate"]), float(z["center"])
    files = p.split(",")
    chans = [np.memmap(f, dtype=np.complex64, mode="r") for f in files]
    if len(chans) == 1:
        chans.append(np.zeros_like(chans[0]))
    n = min(c.size for c in chans)
    if rate is None or center is None:
        raise SystemExit(f"{p}: raw IQ needs --rate and --center")
    return [c[:n] for c in chans], float(rate), float(center)


# ---------------------------------------------------------------- analysis
def analyze_capture(iq, rate, center, nfft=1024, row_s=0.025, bin_s=0.01):
    """Streaming analysis of one 2-channel capture -> dict (waterfall rows in dB, power vs time, lines, bursts)."""
    n = min(len(iq[0]), len(iq[1]))
    row = int(row_s * rate) // nfft * nfft
    nbin = int(bin_s * rate)
    win = np.hanning(nfft).astype(np.float32)
    wnorm = np.sum(win ** 2)
    rows, pbins = [], []
    for s in range(0, n - row + 1, row):
        x = np.stack([np.asarray(iq[c][s:s + row]) for c in (0, 1)])            # (2, row)
        seg = x.reshape(2, -1, nfft) * win
        psd = np.mean(np.abs(np.fft.fftshift(np.fft.fft(seg, axis=-1), axes=-1)) ** 2, axis=1) / wnorm
        rows.append(10 * np.log10(psd + 1e-20))
        pb = np.mean(np.abs(x[:, :row // nbin * nbin].reshape(2, -1, nbin)) ** 2, axis=2)
        pbins.append(10 * np.log10(pb + 1e-20))
    W = np.array(rows)                                                            # (T, 2, nfft) dB per bin
    P = np.concatenate(pbins, axis=1).T if pbins else np.zeros((0, 2))           # (Tb, 2) dB
    freqs = center + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rate))
    return {"center": center, "rate": rate, "seconds": n / rate, "nfft": nfft, "row_s": row / rate,
            "bin_s": nbin / rate, "W": W, "P": P, "freqs": freqs}


def bursts_and_period(P, bin_s, thr_db=6.0):
    """Burst indicator per channel (power > median + thr_db), onsets, and the period from the autocorrelation
    of the indicator of the stronger channel (top peaks)."""
    out = {}
    floor = np.median(P, axis=0)
    ind = P > floor + thr_db                                                       # (Tb, 2)
    for c in (0, 1):
        on = np.flatnonzero(ind[1:, c] & ~ind[:-1, c]) + 1
        out[CH_NAMES[c]] = {"floor_db": round(float(floor[c]), 1), "burst_share": round(float(ind[:, c].mean()), 4),
                            "onsets_s": [round(float(t * bin_s), 2) for t in on][:60],
                            "burst_excess_db": round(float(np.median(P[ind[:, c], c]) - floor[c]), 1)
                            if ind[:, c].any() else None}
    c = int(np.argmax(ind.mean(axis=0)))
    v = ind[:, c].astype(float)
    period = None
    if v.sum() >= 2 and len(v) > 40:
        v = v - v.mean()
        ac = np.correlate(v, v, "full")[len(v) - 1:]
        ac /= ac[0] + 1e-12
        lags = np.arange(len(ac)) * bin_s
        ok = (lags >= 0.3) & (lags <= 0.6 * len(v) * bin_s)
        idx = np.flatnonzero(ok)
        if idx.size:
            peaks = [i for i in idx[1:-1] if ac[i] >= ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > 0.2]
            peaks = sorted(peaks, key=lambda i: -ac[i])[:4]
            period = [{"lag_s": round(float(lags[i]), 2), "corr": round(float(ac[i]), 2)} for i in sorted(peaks)]
    intervals = np.diff(out[CH_NAMES[c]]["onsets_s"]) if len(out[CH_NAMES[c]]["onsets_s"]) > 1 else []
    return out, c, period, [round(float(x), 2) for x in intervals][:30]


def lines(W, freqs, burst_rows, thr_db=10.0):
    """Narrow lines in the burst rows (or all rows) per channel: local maxima > median + thr_db."""
    res = {}
    for c in (0, 1):
        rows = W[burst_rows[:, c], c] if burst_rows[:, c].any() else W[:, c]
        avg = 10 * np.log10(np.mean(10 ** (rows / 10), axis=0))
        med = np.median(avg)
        pk = [i for i in range(1, len(avg) - 1) if avg[i] > med + thr_db and avg[i] >= avg[i - 1] and avg[i] >= avg[i + 1]]
        f = np.sort(freqs[pk])
        spacing = float(np.median(np.diff(f))) if f.size >= 3 else None
        res[CH_NAMES[c]] = {"count": int(f.size), "freqs_mhz": [round(float(x) / 1e6, 4) for x in f][:60],
                            "spacing_khz": None if spacing is None else round(spacing / 1e3, 2),
                            "max_excess_db": round(float(avg.max() - med), 1)}
    return res


def recommend(caps, band=BAND, video_bw=1e6, avoid=FEEDBACK, step=0.1e6):
    """Cleanest video centre: candidate windows [fc +- bw/2] inside the band and outside the feedback guard,
    scored by the mean (and peak) power per Hz over all captures and both channels (max-hold over time)."""
    f_all = np.concatenate([c["freqs"] for c in caps])
    p_mean = np.concatenate([10 * np.log10(np.mean(10 ** (c["W"] / 10), axis=(0, 1))) for c in caps])
    p_max = np.concatenate([np.max(c["W"], axis=(0, 1)) for c in caps])
    lo_g, hi_g = avoid[0] - avoid[1], avoid[0] + avoid[1]
    cands = []
    for fc in np.arange(band[0] + video_bw / 2, band[1] - video_bw / 2 + 1, step):
        a, b = fc - video_bw / 2, fc + video_bw / 2
        if b > lo_g and a < hi_g:
            continue
        m = (f_all >= a) & (f_all <= b)
        cover = np.unique(np.round(f_all[m] / 1e3)).size * 1e3 / video_bw if m.any() else 0
        if cover < 0.8:
            continue
        cands.append({"center_mhz": round(fc / 1e6, 3), "mean_db": round(float(np.mean(p_mean[m])), 1),
                      "peak_db": round(float(np.max(p_max[m])), 1)})
    cands.sort(key=lambda d: (d["mean_db"], d["peak_db"]))
    return cands


def survey(caps, video_bw=1e6, avoid=FEEDBACK, band=BAND):
    out = {"captures": []}
    for cap in caps:
        b, strong, period, intervals = bursts_and_period(cap["P"], cap["bin_s"])
        rows = (cap["W"].shape[0])
        # burst rows of the waterfall: rows whose power bins are in bursts
        per_row = max(1, int(round(cap["row_s"] / cap["bin_s"])))
        ind = cap["P"] > np.median(cap["P"], axis=0) + 6.0
        br = np.zeros((rows, 2), bool)
        for r in range(rows):
            br[r] = ind[r * per_row:(r + 1) * per_row].any(axis=0)
        ln = lines(cap["W"], cap["freqs"], br)
        diff = None
        if br[:, strong].any():
            pw = 10 ** (cap["P"] / 10)
            sel = ind[:, strong]
            diff = round(float(10 * np.log10(np.mean(pw[sel, 0]) / np.mean(pw[sel, 1]))), 1)
        out["captures"].append({"center_mhz": round(cap["center"] / 1e6, 3), "rate": cap["rate"],
                                "seconds": round(cap["seconds"], 2), "bursts": b,
                                "burst_channel": CH_NAMES[strong], "period_candidates": period,
                                "onset_intervals_s": intervals, "lines": ln, "rf0_vs_rf1_in_bursts_db": diff})
    out["recommendation"] = recommend(caps, band=band, video_bw=video_bw, avoid=avoid)[:5]
    return out


def plot(caps, res, path, video_bw=1e6):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sys.path.insert(0, str(HERE))
    from sim_sweep_plots import INK2, SERIES, _style
    _style(plt)
    k = len(caps)
    fig, axs = plt.subplots(k + 1, 3, figsize=(16, 3.2 * (k + 1)), squeeze=False,
                            gridspec_kw={"width_ratios": [1, 1, 1.3]})
    for i, (cap, rc) in enumerate(zip(caps, res["captures"])):
        f = (cap["freqs"] - cap["center"]) / 1e3
        t = np.arange(cap["W"].shape[0]) * cap["row_s"]
        vmin, vmax = np.percentile(cap["W"], 5), np.percentile(cap["W"], 99.9)
        for c in (0, 1):
            ax = axs[i, c]
            ax.imshow(cap["W"][:, c], aspect="auto", origin="lower", cmap="viridis", vmin=vmin, vmax=vmax,
                      extent=[f[0], f[-1], t[0], t[-1] + cap["row_s"]])
            ax.grid(False)
            ax.set_title(f"{cap['center'] / 1e6:.3f} MHz, {CH_NAMES[c]}: waterfall (dB)", fontsize=9)
            ax.set_xlabel("kHz quanh tâm")
            ax.set_ylabel("s")
        ax = axs[i, 2]
        tb = np.arange(cap["P"].shape[0]) * cap["bin_s"]
        for c in (0, 1):
            ax.plot(tb, cap["P"][:, c], color=SERIES[c], lw=0.9, label=CH_NAMES[c])
        per = rc["period_candidates"]
        ax.set_title(f"công suất theo thời gian; chu kỳ ứng viên: "
                     f"{', '.join(str(p['lag_s']) for p in per) if per else '—'} s", fontsize=9)
        ax.set_xlabel("s")
        ax.set_ylabel("dBFS")
        ax.legend(loc="upper right")
    ax = axs[k, 0]
    for cap in caps:
        for c in (0, 1):
            ax.plot(cap["freqs"] / 1e6, np.max(cap["W"][:, c], axis=0), color=SERIES[c], lw=0.6,
                    label=CH_NAMES[c] if cap is caps[0] else None)
    ax.axvspan((FEEDBACK[0] - FEEDBACK[1]) / 1e6, (FEEDBACK[0] + FEEDBACK[1]) / 1e6, color="#e1e0d9", alpha=0.6,
               label="phản hồi ±1 MHz")
    best = res["recommendation"][0]["center_mhz"] if res["recommendation"] else None
    if best:
        ax.axvspan(best - video_bw / 2e6, best + video_bw / 2e6, color=SERIES[2], alpha=0.15, label=f"đề xuất {best} MHz")
    ax.set_title("max-hold theo tần số (cả dải)", fontsize=9)
    ax.set_xlabel("MHz")
    ax.set_ylabel("dB")
    ax.legend(loc="upper right", fontsize=7)
    axs[k, 1].axis("off")
    axs[k, 2].axis("off")
    txt = [f"đề xuất tần số video: {', '.join(str(r['center_mhz']) for r in res['recommendation'][:3]) or '—'} MHz"]
    for rc in res["captures"]:
        l0, l1 = rc["lines"]["RF0"], rc["lines"]["RF1"]
        txt.append(f"{rc['center_mhz']} MHz: vạch RF0 {l0['count']} (cách {l0['spacing_khz']} kHz), RF1 {l1['count']}; "
                   f"đợt trên {rc['burst_channel']}, RF0-RF1 {rc['rf0_vs_rf1_in_bursts_db']} dB")
    axs[k, 1].text(0, 1, "\n".join(txt), va="top", fontsize=8, color=INK2, family="DejaVu Sans")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iq", nargs="+", help="recorded captures (.npz, or raw complex64 .cfile[,ch1.cfile])")
    ap.add_argument("--rate", type=float, default=1e6)
    ap.add_argument("--center", type=float, help="centre frequency of raw .cfile captures")
    ap.add_argument("--args", help="N321 device args (receive only), e.g. addr=192.168.180.95")
    ap.add_argument("--centers", default=",".join(f"{c:g}" for c in DEFAULT_CENTERS))
    ap.add_argument("--seconds", type=float, default=25.0, help="per centre (>= 2 periods of the bursts)")
    ap.add_argument("--gain", type=float, default=50.0)
    ap.add_argument("--save-iq", help="folder: keep the recorded captures as .npz")
    ap.add_argument("--video-bw", type=float, default=1e6)
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--out", default=str(REPO / "results"))
    a = ap.parse_args(argv)
    caps = []
    if a.iq:
        for p in a.iq:
            iq, rate, center = load_iq(p, a.rate, a.center)
            caps.append(analyze_capture(iq, rate, center))
            print(f"  {p}: {center / 1e6:.3f} MHz, {caps[-1]['seconds']:.1f} s", flush=True)
    elif a.args:
        tmp = Path(tempfile.mkdtemp(prefix="survey_"))
        for fc in [float(c) for c in a.centers.split(",")]:
            if not BAND[0] <= fc <= BAND[1]:
                raise SystemExit(f"centre {fc / 1e6} MHz outside 920-923 MHz")
            print(f"  recording {fc / 1e6:.3f} MHz, {a.seconds:.0f} s, 2 channels (receive only) ...", flush=True)
            f0, f1 = capture_n321(a.args, fc, a.rate, a.seconds, a.gain, tmp / f"c{fc:.0f}")
            iq = [np.memmap(f0, dtype=np.complex64, mode="r"), np.memmap(f1, dtype=np.complex64, mode="r")]
            caps.append(analyze_capture(iq, a.rate, fc))
            if a.save_iq:
                Path(a.save_iq).mkdir(parents=True, exist_ok=True)
                np.savez(Path(a.save_iq) / f"survey_{a.date}_{fc / 1e6:.3f}.npz",
                         iq=np.stack([np.asarray(iq[0]), np.asarray(iq[1])]), rate=a.rate, center=fc)
            del iq
            for f in (f0, f1):
                Path(f).unlink(missing_ok=True)
    else:
        ap.error("give --iq files, or --args to record with the N321 (receive only)")
    res = survey(caps, video_bw=a.video_bw)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"survey_{a.date}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    plot(caps, res, out / f"survey_{a.date}.png", a.video_bw)
    for rc in res["captures"]:
        print(f"  {rc['center_mhz']} MHz: lines RF0 {rc['lines']['RF0']['count']} (spacing {rc['lines']['RF0']['spacing_khz']} kHz), "
              f"RF1 {rc['lines']['RF1']['count']}; bursts on {rc['burst_channel']}, period candidates "
              f"{[p['lag_s'] for p in rc['period_candidates'] or []]} s; RF0-RF1 {rc['rf0_vs_rf1_in_bursts_db']} dB")
    print("  recommended video centre (MHz):", [r["center_mhz"] for r in res["recommendation"]])
    print(out / f"survey_{a.date}.png")
    return res


if __name__ == "__main__":
    main()
