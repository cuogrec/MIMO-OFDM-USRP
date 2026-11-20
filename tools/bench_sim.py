#!/usr/bin/python3
"""Measure how many frames/s the Python TX+RX chain sustains (IDLE frames, no video).
Usage: python3 tools/bench_sim.py [mimo] [mod] [rate] [--no-channel]"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # repo root
from gnuradio import gr
from ofdm321.config import load_settings
from ofdm321.blocks import MimoTxSource, MimoRxSink, MimoChannelSim
mimo, mod = (sys.argv[1:3] + ["alamouti", "qpsk"][len(sys.argv[1:3]):])[:2]
rate = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3][0].isdigit() else 1e6
s = load_settings(None, mimo_mode=mimo, modulation=mod, sample_rate=rate, sim_channel="fixed", sim_snr_db=30)
tb = gr.top_block()
src, rx = MimoTxSource(s, 1, throttle=False), MimoRxSink(s)
keep = [src, rx]
if "--no-channel" in sys.argv:
    tb.connect((src, 0), (rx, 0)); tb.connect((src, 1), (rx, 1))
else:
    ch = MimoChannelSim(s); keep.append(ch)
    for i in (0, 1):
        tb.connect((src, i), (ch, i)); tb.connect((ch, i), (rx, i))
tb.start(); time.sleep(1); a = rx.snapshot()["ok"]; t = time.time(); time.sleep(5)
b = rx.snapshot()["ok"]; dt = time.time() - t; tb.stop(); tb.wait()
need = rate / src.mod.frame_len
print(f"{mimo} {mod} Fs={rate:g}: decoded {(b - a) / dt:.0f} frames/s, real time needs {need:.0f} "
      f"-> margin x{(b - a) / dt / need:.2f}")
