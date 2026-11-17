"""Adaptive coding and modulation (ACM): receiver feedback + transmitter controller.

Loop (like DVB-S2 ACM, return channel = UDP over the lab LAN):
    RX: every frame -> effective post-equaliser SNR (harmonic mean over carriers,
        modem.Demodulator) -> filtered -> UDP report every `period` s to the TX PC.
    TX: AcmController picks the highest modulation whose threshold (+ margin) the
        reported SNR exceeds. Down: immediately. Up: only after the SNR has stayed
        above the next threshold + hysteresis for `hold_s`. No report for
        `timeout_s` -> most robust modulation (link may be lost).
Thresholds = smallest SNR_eff with codeword error rate <= 1 % in a random 2x2
Rayleigh channel with soft RS decoding (tools/acm_calibrate.py, alamouti/siso/smux).
"""
import json
import logging
import socket
import threading
import time
from collections import deque

LOG = logging.getLogger(__name__)

ORDER = ("bpsk", "qpsk", "16qam", "64qam")
THRESHOLDS_DB = {"bpsk": 9.0, "qpsk": 14.0, "16qam": 20.0, "64qam": 26.0}


def parse_modcods(text):
    mods = [m.strip().lower() for m in str(text).split(",") if m.strip()]
    bad = [m for m in mods if m not in ORDER]
    if bad or not mods:
        raise ValueError(f"acm_modcods must be a comma list of {ORDER}")
    return [m for m in ORDER if m in mods]


class AcmController:
    def __init__(self, modcods, margin_db=1.0, hysteresis_db=1.0, hold_s=0.5, timeout_s=1.5):
        self.modcods = parse_modcods(modcods)
        self.margin, self.hyst, self.hold, self.timeout = margin_db, hysteresis_db, hold_s, timeout_s
        self.index = 0
        self.snr = None
        self.last_report = None
        self.up_since = None
        self.lock = threading.Lock()
        self.switches = 0
        self.history = deque(maxlen=4000)          # (t, snr, modulation) for the report
        self.reports = 0
        self.last_switch = 0.0
        self.confirmed = True   # RX already reports frames of the current modulation
        self.latency = deque(maxlen=50)   # s: switch -> first RX report in the new modulation

    @property
    def modulation(self):
        with self.lock:
            if self.last_report is not None and time.monotonic() - self.last_report > self.timeout:
                if self.index:
                    LOG.warning("ACM: no feedback for %.1f s -> %s", self.timeout, self.modcods[0])
                    self.index, self.switches = 0, self.switches + 1
                self.last_report = None
            return self.modcods[self.index]

    def _need(self, i):
        return THRESHOLDS_DB[self.modcods[i]] + self.margin

    def update(self, snr_db, cer=0.0, rx_mod=None, now=None):
        """rx_mod: modulation of the frames the RX is decoding now. Codeword errors
        count only once the RX sees the current modulation (frames still in flight
        were sent with the old one); the next step up waits for that too."""
        now = now or time.monotonic()
        with self.lock:
            self.reports += 1
            self.last_report = now
            self.snr = snr_db
            old = self.index
            if rx_mod is None or rx_mod == self.modcods[self.index]:
                if not self.confirmed:
                    self.latency.append(now - self.last_switch)
                self.confirmed = True
            if not self.confirmed:
                cer = 0.0
            # down: below the current threshold, or many codeword errors
            while self.index > 0 and (snr_db < self._need(self.index) or cer > 0.1):
                self.index -= 1
                cer = 0.0
            # up: one step at a time, after hold_s above threshold + hysteresis
            if self.index == old and self.confirmed and self.index + 1 < len(self.modcods) and \
                    snr_db >= self._need(self.index + 1) + self.hyst:
                self.up_since = self.up_since or now
                if now - self.up_since >= self.hold:
                    self.index += 1
                    self.up_since = None
            else:
                self.up_since = None
            if self.index != old:
                self.switches += 1
                self.last_switch = now
                self.confirmed = False
                LOG.info("ACM: SNR_eff %.1f dB -> %s", snr_db, self.modcods[self.index])
            self.history.append((round(now, 3), round(snr_db, 2), self.modcods[self.index]))
            return self.modcods[self.index]


class FeedbackReceiver(threading.Thread):
    """TX side: listens for RX reports (UDP JSON) and drives the controller."""

    def __init__(self, controller, port, session=None):
        super().__init__(daemon=True, name="ACM feedback RX")
        self.ctl, self.session = controller, session
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.running = True

    def run(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(2048)
                msg = json.loads(data)
                if self.session is not None and msg.get("session") not in (None, self.session):
                    continue
                self.ctl.update(float(msg["snr"]), float(msg.get("cer", 0.0)), msg.get("mod"))
            except socket.timeout:
                continue
            except (ValueError, KeyError, OSError):
                continue

    def close(self):
        self.running = False


class FeedbackSender(threading.Thread):
    """RX side: every `period` s sends the filtered SNR_eff and codeword error rate.
    delay_s emulates the return path latency (e.g. 0.5 s for a GEO satellite)."""

    def __init__(self, rx_sink, video, host, port, period=0.1, delay_s=0.0):
        super().__init__(daemon=True, name="ACM feedback TX")
        self.rx, self.video, self.dest = rx_sink, video, (host, port)
        self.period, self.delay = period, delay_s
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = True
        self.sent = 0
        self.queue = deque()

    def run(self):
        prev = None
        while self.running:
            time.sleep(self.period)
            st = self.rx.acm_state()
            if st["snr_eff_db"] is not None:
                cw = (st["cw_ok"], st["cw_bad"])
                cer = 0.0
                if prev is not None:
                    d_ok, d_bad = cw[0] - prev[0], cw[1] - prev[1]
                    cer = d_bad / max(1, d_ok + d_bad)
                prev = cw
                msg = {"snr": round(st["snr_eff_db"], 2), "cer": round(cer, 3), "mod": st["modulation"],
                       "session": self.video.snapshot().get("session"), "t": time.time()}
                self.queue.append((time.monotonic() + self.delay, json.dumps(msg).encode()))
            while self.queue and self.queue[0][0] <= time.monotonic():
                try:
                    self.sock.sendto(self.queue.popleft()[1], self.dest)
                    self.sent += 1
                except OSError:
                    pass

    def close(self):
        self.running = False
