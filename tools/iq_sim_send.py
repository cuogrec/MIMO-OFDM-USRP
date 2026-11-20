"""Check an IQ file from make_iq.py WITHOUT radios: stream it to the simulated RX over
ZeroMQ, exactly as the replay on the N321 would put it on the air.

    # folder RX:  bash scripts/sim_rx.sh --compare <iq folder>/report.json
    # folder TX:  python3 tools/iq_sim_send.py runs/<stamp>_iq_alamouti_qpsk
"""
import argparse
import json
import sys
import time
from pathlib import Path

from gnuradio import blocks, gr, zeromq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="output folder of make_iq.py")
    ap.add_argument("--rate", type=float, default=1e6)
    ap.add_argument("--zmq", default="tcp://127.0.0.1:5560")
    a = ap.parse_args()
    folder = Path(a.folder)
    rep = json.loads((folder / "report.json").read_text())
    path = folder / rep["iq_file"]
    tb = gr.top_block()
    src = blocks.file_source(gr.sizeof_short, str(path), False)
    conv = blocks.interleaved_short_to_complex(False, False, 32767.0)   # divides: -> ch A, ch B, ...
    thr = blocks.throttle(gr.sizeof_gr_complex, 2 * a.rate, True)
    sink = zeromq.push_sink(gr.sizeof_gr_complex, 1, a.zmq, 100, False, -1)
    tb.connect(src, conv, thr, sink)
    dur = rep["iq_samples"] / a.rate
    print(f"streaming {path.name}: {dur:.1f} s at {a.rate:g} S/s, session {rep['session']}")
    tb.start()
    t0 = time.time()
    try:
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()
    print(f"IQ_SENT {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
