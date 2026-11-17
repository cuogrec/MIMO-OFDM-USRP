#!/usr/bin/env python3
"""Runs ON the N321 #1 (embedded ARM, no PC link needed): replay a 2-channel IQ file
made by tools/make_iq.py on TX/RX A:0 and B:0. Needs only python3, numpy and the
UHD python module of the N321 filesystem (no GNU Radio, no FFmpeg).

    python3 n321_replay.py --check                      # what the N321 has
    python3 n321_replay.py --freq 915e6 --gain 20       # antenna link
    python3 n321_replay.py --rate 500e3 ...             # if it reports underflows (U)

The RX on the PC must use the same --mimo/--mod/--freq/--rate.
"""
import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def check():
    ok = True
    print("python", sys.version.split()[0])
    try:
        import numpy
        print("numpy", numpy.__version__)
    except ImportError:
        ok = False
        print("numpy: MISSING")
    try:
        import uhd
        print("uhd python module", uhd.get_version_string() if hasattr(uhd, "get_version_string") else "ok")
    except ImportError as exc:
        ok = False
        print("uhd python module: MISSING (%s)" % exc)
    os.system("nproc; free -m | head -2; df -h %s | tail -1" % HERE)
    print("CHECK_OK" if ok else "CHECK_FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--file", default=os.path.join(HERE, "tx_iq_sc16.bin"))
    ap.add_argument("--args", default="master_clock_rate=200e6", help="UHD device args (local device)")
    ap.add_argument("--rate", type=float, default=1e6)
    ap.add_argument("--freq", type=float, default=915e6)
    ap.add_argument("--gain", type=float, default=20.0)
    ap.add_argument("--rf-link", choices=("antenna", "cable"), default="antenna")
    ap.add_argument("--attenuation-db", type=float)
    ap.add_argument("--repeat", type=int, default=1, help="play the file N times (0 = forever)")
    ap.add_argument("--chunk", type=int, default=16384)
    a = ap.parse_args()
    if a.check:
        return check()
    # same RF safety rules as start_tx.sh
    if a.rf_link == "cable":
        if a.attenuation_db is None or a.attenuation_db < 30 or a.gain > a.attenuation_db - 10:
            raise SystemExit("cable: needs --attenuation-db >= 30 and gain <= attenuation-10")
    elif a.gain > 30:
        raise SystemExit("antenna: gain must be <= 30 dB (check local regulations)")
    if not 3e6 <= a.freq <= 6e9:
        raise SystemExit("frequency out of N321 range")
    import numpy as np
    import uhd

    usrp = uhd.usrp.MultiUSRP(a.args)
    usrp.set_tx_subdev_spec(uhd.usrp.SubdevSpec("A:0 B:0"))
    usrp.set_tx_rate(a.rate)
    for ch in (0, 1):
        usrp.set_tx_freq(uhd.types.TuneRequest(a.freq), ch)
        usrp.set_tx_gain(a.gain, ch)
        usrp.set_tx_antenna("TX/RX", ch)
    rate = usrp.get_tx_rate()
    if abs(rate - a.rate) > 1e-3 * a.rate:
        raise SystemExit("rate coerced to %g (use MCR/even integer, e.g. 1e6, 500e3)" % rate)
    print("N321 TX: rate %g, freq %.6g MHz, gain %.1f dB, antenna TX/RX A+B" % (rate, a.freq / 1e6, a.gain))
    st = uhd.usrp.StreamArgs("fc32", "sc16")
    st.channels = [0, 1]
    tx = usrp.get_tx_stream(st)
    n_file = os.path.getsize(a.file) // 8
    print("file %s: %.1f M samples = %.1f s" % (a.file, n_file / 1e6, n_file / rate))

    stats = {"U": 0, "L": 0, "S": 0}
    running = [True]

    def async_rx():
        md = uhd.types.TXAsyncMetadata()
        while running[0]:
            if tx.recv_async_msg(md, 0.1):
                ev = md.event_code
                if ev == uhd.types.TXMetadataEventCode.underflow or \
                        ev == uhd.types.TXMetadataEventCode.underflow_in_packet:
                    stats["U"] += 1
                elif ev == uhd.types.TXMetadataEventCode.seq_error or \
                        ev == uhd.types.TXMetadataEventCode.seq_error_in_burst:
                    stats["S"] += 1
                elif ev == uhd.types.TXMetadataEventCode.time_error:
                    stats["L"] += 1
    th = threading.Thread(target=async_rx, daemon=True)
    th.start()

    md = uhd.types.TXMetadata()
    md.start_of_burst, md.end_of_burst, md.has_time_spec = True, False, False
    buf = np.zeros((2, a.chunk), np.complex64)
    sent, loops, t0, last = 0, 0, time.time(), 0
    scale = np.float32(1.0 / 32767)
    try:
        while a.repeat == 0 or loops < a.repeat:
            with open(a.file, "rb") as f:
                while True:
                    raw = f.read(a.chunk * 8)
                    if not raw:
                        break
                    x = np.frombuffer(raw, "<i2").astype(np.float32).reshape(-1, 4) * scale
                    n = x.shape[0]
                    buf[0, :n].real, buf[0, :n].imag = x[:, 0], x[:, 1]
                    buf[1, :n].real, buf[1, :n].imag = x[:, 2], x[:, 3]
                    done = 0
                    while done < n:
                        done += tx.send(buf[:, done:n], md)
                        md.start_of_burst = False
                    sent += n
                    if time.time() - last > 1:
                        last = time.time()
                        print("sent %.1f s | underflow %d seq %d | x%.2f real time"
                              % (sent / rate, stats["U"], stats["S"], (sent / rate) / (last - t0)), flush=True)
            loops += 1
    except KeyboardInterrupt:
        pass
    md.end_of_burst = True
    tx.send(np.zeros((2, 0), np.complex64), md)
    time.sleep(0.3)
    running[0] = False
    print("REPLAY_DONE sent %.1f s, underflows %d" % (sent / rate, stats["U"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
