#!/usr/bin/env python3
"""Plot one run (TX, RX or both) from report.json: SNR / SNR_eff, modulation (steps),
video bitrate and losses over time. Works offline (matplotlib, Agg backend).

    python3 tools/plot_run.py runs/<stamp>_rx_...                      # RX only
    python3 tools/plot_run.py <TX report.json> <RX report.json>        # both (copy one to the other PC)
    python3 tools/plot_run.py --latest                                 # runs/latest_run.txt of this folder

The PNG (plot_run.png) is written into the folder of every report given (or -o FILE).
TX and RX are aligned on wall-clock time: on two PCs the plot is only as aligned as their clocks.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root
from ofdm321.config import app_root  # noqa: E402
ROOT = app_root()   # --latest: runs/latest_run.txt of the folder you are in
ORDER = ("bpsk", "qpsk", "16qam", "64qam")


def load(path):
    p = Path(path).expanduser()
    if p.is_dir():
        p = p / "report.json"
    rep = json.loads(p.read_text())
    rep["_path"] = p
    return rep


def wall_of(rep, t_mono):
    clk = rep.get("clock")
    return None if clk is None else t_mono - clk["monotonic"] + clk["time"]


def stairs(ax, xs, ys, **kw):
    if xs:
        ax.step(xs, ys, where="post", **kw)


def per_second(rows, key):
    xs, ys, prev = [], [], None
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        if prev is not None:
            xs.append(r["_x"])
            ys.append(max(0, v - prev))
        prev = v
    return xs, ys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="*", help="report.json files or run folders (TX and/or RX)")
    ap.add_argument("--latest", action="store_true", help="add runs/latest_run.txt of this folder")
    ap.add_argument("-o", "--output", help="PNG path (default: plot_run.png in each run folder)")
    a = ap.parse_args(argv)
    paths = list(a.reports)
    if a.latest:
        paths.append((ROOT / "runs/latest_run.txt").read_text().strip())
    if not paths:
        ap.error("give at least one report.json / run folder (or --latest)")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib missing: sudo apt-get install python3-matplotlib")

    reps = [load(p) for p in paths]
    tx = next((r for r in reps if r.get("role") == "tx"), None)
    rx = next((r for r in reps if r.get("role") == "rx"), None)
    walls = [h["t_wall"] for r in reps for h in r.get("history", [])[:1]]
    if not walls:
        sys.exit("no 'history' in these reports (runs before v0.5 have none)")
    t0 = min(walls)
    for r in reps:
        for h in r.get("history", []):
            h["_x"] = h["t_wall"] - t0

    fig, axs = plt.subplots(4, 1, figsize=(12, 11), sharex=True)
    ax_snr, ax_mod, ax_rate, ax_loss = axs

    # 1. SNR
    if rx:
        h = rx.get("history", [])
        for key, lab, st in (("snr_db", "RX SNR (pilot)", "-"), ("snr_eff_db", "RX SNR_eff", "-"),
                             ("sim_snr_db", "SNR mô phỏng (kênh)", ":")):
            pts = [(r["_x"], r[key]) for r in h if r.get(key) is not None]
            if pts:
                ax_snr.plot(*zip(*pts), st, label=lab)
    if tx:
        acm = tx.get("acm", {}).get("history", [])
        pts = [(wall_of(tx, t) - t0, snr) for t, snr, _ in acm if wall_of(tx, t) is not None]
        if pts:
            ax_snr.plot(*zip(*pts), ".", ms=2, alpha=0.5, label="SNR_eff TX nhận qua phản hồi")
        pts = [(r["_x"], r["fb_snr_db"]) for r in tx.get("history", []) if r.get("fb_snr_db") is not None]
        if pts:
            ax_snr.plot(*zip(*pts), "--", label="SNR đường phản hồi radio")
    ax_snr.set_ylabel("dB")
    ax_snr.set_title("SNR")
    ax_snr.grid(True, alpha=0.3)
    ax_snr.legend(loc="lower left", fontsize=8)

    # 2. modulation
    if tx and tx.get("acm", {}).get("history"):
        acm = tx["acm"]["history"]
        pts = [(wall_of(tx, t) - t0, ORDER.index(m)) for t, _, m in acm if wall_of(tx, t) is not None]
        if pts:
            stairs(ax_mod, *zip(*pts), label="TX chọn (ACM)")
    if rx:
        pts = [(r["_x"], ORDER.index(r["modulation"])) for r in rx.get("history", [])
               if r.get("modulation") in ORDER]
        if pts:
            stairs(ax_mod, *zip(*pts), ls="--", label="RX đang giải")
    ax_mod.set_yticks(range(4))
    ax_mod.set_yticklabels(ORDER)
    ax_mod.set_ylim(-0.5, 3.5)
    ax_mod.set_title("Điều chế")
    ax_mod.grid(True, alpha=0.3)
    ax_mod.legend(loc="lower left", fontsize=8)

    # 3. video bitrate (+ TS capacity of the modulation in use)
    if tx:
        h = tx.get("history", [])
        pts = [(r["_x"], r["video_ts_kbps"]) for r in h if r.get("video_ts_kbps") is not None]
        vh = tx.get("video", {}).get("history", [])
        if vh:
            xs = [e["t"] - t0 for e in vh] + ([h[-1]["_x"]] if h else [])
            ys = [e["ts_muxrate"] / 1e3 for e in vh] + ([vh[-1]["ts_muxrate"] / 1e3] if h else [])
            stairs(ax_rate, xs, ys, lw=2, label="TS video (bitrate thích ứng)")
        elif pts:
            stairs(ax_rate, *zip(*pts), lw=2, label="TS video (cố định)")
        cap = tx.get("capacity", {}).get("acm_ts_capacity_bps")
        if cap:
            pts = [(r["_x"], cap[r["modulation"]] / 1e3) for r in h if r.get("modulation") in cap]
            if pts:
                stairs(ax_rate, *zip(*pts), ls=":", label="dung lượng TS của điều chế đang dùng")
        q = [(r["_x"], r["queue"]) for r in h]
        if q:
            ax_q = ax_rate.twinx()
            ax_q.plot(*zip(*q), color="tab:gray", alpha=0.5, lw=1)
            ax_q.set_ylabel("hàng đợi TX (gói)", color="tab:gray")
    ax_rate.set_ylabel("kb/s")
    ax_rate.set_title("Bitrate video")
    ax_rate.grid(True, alpha=0.3)
    ax_rate.legend(loc="upper left", fontsize=8)

    # 4. losses per second
    if rx:
        h = rx.get("history", [])
        for key, lab in (("rs", "codeword RS lỗi /s"), ("missing", "gói DATA thiếu /s"), ("lost", "mất đồng bộ /s")):
            xs, ys = per_second(h, key)
            if any(ys):
                ax_loss.plot(xs, ys, drawstyle="steps-post", label=lab)
        miss = h[-1].get("missing") if h else None
        ax_loss.set_title(f"Lỗi / mất gói (RX tổng: thiếu {miss} gói DATA, {rx.get('status')})")
    else:
        ax_loss.set_title("Lỗi / mất gói (cần report RX)")
    if tx and tx.get("history") and "resent" in tx["history"][0]:
        xs, ys = per_second(tx["history"], "resent")
        if any(ys):
            ax_loss.plot(xs, ys, drawstyle="steps-post", ls="--", label="TX phát lại /s")
    ax_loss.set_xlabel("thời gian (s)")
    ax_loss.grid(True, alpha=0.3)
    if ax_loss.get_legend_handles_labels()[0]:
        ax_loss.legend(loc="upper left", fontsize=8)

    names = " + ".join(r["_path"].parent.name for r in reps)
    fig.suptitle(names, fontsize=9)
    fig.tight_layout()
    outs = [Path(a.output)] if a.output else [r["_path"].parent / "plot_run.png" for r in reps]
    for out in outs:
        fig.savefig(out, dpi=110)
        print(out)


if __name__ == "__main__":
    main()
