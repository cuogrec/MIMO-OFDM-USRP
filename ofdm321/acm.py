"""Adaptive coding and modulation (ACM): receiver feedback + transmitter controller.

Loop (like DVB-S2 ACM, return channel = UDP over the lab LAN):
    RX: every frame -> effective post-equaliser SNR (harmonic mean over carriers,
        modem.Demodulator) -> filtered -> UDP report every `period` s to the TX PC.
    TX: AcmController picks the highest modulation whose threshold (+ margin) the
        reported SNR exceeds. Down: immediately. Up: only after the SNR has stayed
        above the next threshold + hysteresis for `hold_s`. No report for
        `timeout_s` -> most robust modulation (link may be lost).
Thresholds = smallest SNR_eff with codeword error rate <= 1 % in a random 2x2
Rayleigh channel (tools/acm_calibrate.py), one table per inner code: RS with soft
decoding (v0.3, kept), LDPC (v0.6, ACM frames use the rate-5/6 code punctured to 2016 bits).
"""
import json
import logging
import socket
import struct
import threading
import time
from collections import deque

LOG = logging.getLogger(__name__)

ORDER = ("bpsk", "qpsk", "16qam", "64qam")
THRESHOLDS_BY_FEC = {
    "rs": {"bpsk": 9.0, "qpsk": 14.0, "16qam": 20.0, "64qam": 26.0},
    # tools/acm_calibrate.py (alamouti, 150 frames per cell, 2026-11-20; results/acm_thresholds.json):
    # RS gave 10/14/20/26 (BPSK is the floor of the ladder, its threshold is never used), LDPC 9/12/19/24
    "ldpc": {"bpsk": 9.0, "qpsk": 12.0, "16qam": 19.0, "64qam": 24.0},
}
THRESHOLDS_DB = THRESHOLDS_BY_FEC["rs"]


def parse_modcods(text):
    mods = [m.strip().lower() for m in str(text).split(",") if m.strip()]
    bad = [m for m in mods if m not in ORDER]
    if bad or not mods:
        raise ValueError(f"acm_modcods must be a comma list of {ORDER}")
    return [m for m in ORDER if m in mods]


class AcmController:
    def __init__(self, modcods, margin_db=1.0, hysteresis_db=1.0, hold_s=0.5, timeout_s=1.5, fec="rs",
                 debounce=True, down_reports=3, down_hold_s=0.03, up_guard_s=0.3):
        """debounce (v0.6.1): an SNR-based step down needs >= down_reports consecutive low reports spanning
        >= down_hold_s, counted only on reports of the current modulation; after a step up it is not
        allowed until the RX confirms the new modulation + up_guard_s. Immediate: CER > 10 % and SNR below
        the threshold of the next lower modulation (any report). debounce=False: v0.6.0 behaviour."""
        self.modcods = parse_modcods(modcods)
        self.debounce, self.down_reports, self.down_hold, self.up_guard = debounce, down_reports, down_hold_s, up_guard_s
        self.low_n, self.low_since = 0, None       # streak of fresh reports below the current threshold
        self.last_dir = 0                          # +1 last switch was up, -1 down
        self.confirmed_at = 0.0
        self.ignored_dips = 0                      # low streaks that ended before a step down (debounce)
        self.timeouts = 0                          # feedback silent for timeout_s (-> most robust modulation)
        self.thresholds = THRESHOLDS_BY_FEC[fec]
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
        self.mod_since = time.monotonic()  # when the current modulation was chosen (video bitrate hold)

    @property
    def modulation(self):
        with self.lock:
            if self.last_report is not None and time.monotonic() - self.last_report > self.timeout:
                self.timeouts += 1
                if self.index:
                    LOG.warning("ACM: no feedback for %.1f s -> %s", self.timeout, self.modcods[0])
                    self.index, self.switches = 0, self.switches + 1
                    self.mod_since = time.monotonic()
                self.last_report = None
            return self.modcods[self.index]

    def _need(self, i):
        return self.thresholds[self.modcods[i]] + self.margin

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
            fresh = rx_mod is None or rx_mod == self.modcods[self.index]
            if fresh:
                if not self.confirmed:
                    self.latency.append(now - self.last_switch)
                    self.confirmed_at = now
                self.confirmed = True
            if not self.confirmed:
                cer = 0.0
            if self.debounce:
                self._down_debounced(snr_db, cer, fresh, now)
            else:
                # v0.6.0: down at once below the current threshold, or many codeword errors
                while self.index > 0 and (snr_db < self._need(self.index) or cer > 0.1):
                    self.index -= 1
                    cer = 0.0
            # up: one step at a time, after hold_s above threshold + hysteresis (fresh reports only)
            if self.index == old and self.confirmed and (fresh or not self.debounce) \
                    and self.index + 1 < len(self.modcods) and snr_db >= self._need(self.index + 1) + self.hyst:
                self.up_since = self.up_since or now
                if now - self.up_since >= self.hold:
                    self.index += 1
                    self.up_since = None
            else:
                self.up_since = None
            if self.index != old:
                self.last_dir = 1 if self.index > old else -1
                self.low_n, self.low_since = 0, None
                self.switches += 1
                self.last_switch = now
                self.mod_since = now
                self.confirmed = False
                LOG.info("ACM: SNR_eff %.1f dB -> %s", snr_db, self.modcods[self.index])
            if self.index != old or not self.history or now - self.history[-1][0] >= 0.1:
                self.history.append((round(now, 3), round(snr_db, 2), self.modcods[self.index]))
            return self.modcods[self.index]


    def _down_debounced(self, snr_db, cer, fresh, now):
        """Step-down rules with debouncing (lock held)."""
        if self.index == 0:
            self.low_n, self.low_since = 0, None
            return
        if cer > 0.1:                                       # codeword errors in the current modulation
            self.index -= 1
            return
        if snr_db < self._need(self.index - 1):             # below the lower threshold: emergency, any report
            ok = [i for i in range(self.index) if self._need(i) <= snr_db]
            self.index = ok[-1] if ok else 0                 # straight to the highest level that still fits
            return
        if not fresh:                                       # report about the old modulation: no SNR decision
            return
        if snr_db >= self._need(self.index):
            if self.low_n:
                self.ignored_dips += 1
            self.low_n, self.low_since = 0, None
            return
        if self.last_dir > 0 and now - self.confirmed_at < self.up_guard:   # just stepped up: wait
            return
        self.low_n += 1
        self.low_since = self.low_since if self.low_since is not None else now
        if self.low_n >= self.down_reports and now - self.low_since >= self.down_hold:
            self.index -= 1


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


# ---------------------------------------------------------------- report (shared by UDP and radio)
FB_MAGIC = b"OFB1"
FB = struct.Struct(">4sIIfffBB")      # magic, session, seq, snr_eff dB, cer, age s, modulation, valid
FB_PACKET_BYTES = 212
STALE_S = 0.5                         # no ACM frame decoded for this long -> report marked invalid


class FeedbackReporter:
    """RX side: what the receiver tells the transmitter. SNR_eff (filtered), codeword error
    rate over >= `window` s, the modulation it is decoding now, and whether that is fresh.
    delay_s emulates a long return path (0.25-0.5 s ~ GEO satellite)."""

    def __init__(self, rx_sink, video, delay_s=0.0, window=0.1):
        self.rx, self.video, self.delay, self.window = rx_sink, video, delay_s, window
        self.lock = threading.Lock()
        self.prev, self.prev_t, self.cer = None, 0.0, 0.0
        self.history = deque()
        self.seq = 0

    def _now(self):
        st = self.rx.acm_state()
        now = time.monotonic()
        if now - self.prev_t >= self.window:
            cw = (st["cw_ok"], st["cw_bad"])
            if self.prev is not None:
                d_ok, d_bad = cw[0] - self.prev[0], cw[1] - self.prev[1]
                self.cer = d_bad / max(1, d_ok + d_bad)
            self.prev, self.prev_t = cw, now
        valid = st["snr_eff_db"] is not None and st["age_s"] is not None and st["age_s"] < STALE_S
        return {"snr": round(st["snr_eff_db"], 2) if st["snr_eff_db"] is not None else None,
                "cer": round(self.cer, 3), "mod": st["modulation"], "valid": valid,
                "age": st["age_s"], "session": self.video.snapshot().get("session")}

    def report(self):
        """Latest report, `delay_s` old."""
        with self.lock:
            now = time.monotonic()
            self.history.append((now, self._now()))
            while len(self.history) > 1 and self.history[1][0] <= now - self.delay:
                self.history.popleft()
            return self.history[0][1]

    def packet(self):
        """212-byte application packet for the radio feedback link."""
        r = self.report()
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            seq = self.seq
        mod = ORDER.index(r["mod"]) if r["mod"] in ORDER else 255
        head = FB.pack(FB_MAGIC, r["session"] or 0, seq, r["snr"] if r["snr"] is not None else -99.0,
                       r["cer"], r["age"] if r["age"] is not None else 99.0, mod, 1 if r["valid"] else 0)
        return head + bytes(FB_PACKET_BYTES - len(head))


def parse_feedback_packet(raw):
    """-> dict or None (not a feedback packet)."""
    if len(raw) < FB.size or raw[:4] != FB_MAGIC:
        return None
    _, session, seq, snr, cer, age, mod, valid = FB.unpack(raw[:FB.size])
    return {"session": session or None, "seq": seq, "snr": snr, "cer": cer, "age": age,
            "mod": ORDER[mod] if mod < len(ORDER) else None, "valid": bool(valid)}


class RadioFeedbackHandler:
    """TX side of the radio feedback link: packets from the feedback receiver -> controller."""

    def __init__(self, controller, session=None):
        self.ctl, self.session = controller, session
        self.frames = self.used = self.foreign = 0
        self.last_seq = None

    def __call__(self, raw):
        msg = parse_feedback_packet(bytes(raw))
        if msg is None:
            return
        self.frames += 1
        if self.session is not None and msg["session"] not in (None, self.session):
            self.foreign += 1
            return
        self.last_seq = msg["seq"]
        if msg["valid"]:
            self.used += 1
            self.ctl.update(msg["snr"], msg["cer"], msg["mod"])


class FeedbackWatchdog:
    """TX, radio feedback: warn when no feedback frame has arrived `first_s` after start,
    then every `every_s` until the first frame. check() -> "warn", "ok" (first frame, once) or None."""

    MESSAGE = ("Chưa nhận phản hồi: kiểm tra anten RF1 RX2 (máy phát) và RF1 TX/RX (máy thu), "
               "tăng --fb-rx-gain/--fb-tx-gain")

    def __init__(self, first_s=5.0, every_s=10.0):
        self.next, self.every = first_s, every_s
        self.warnings = 0
        self.first_frame_s = None

    def check(self, elapsed_s, frames):
        if self.first_frame_s is not None:
            return None
        if frames > 0:
            self.first_frame_s = round(elapsed_s, 2)
            return "ok"
        if elapsed_s >= self.next:
            self.warnings += 1
            self.next += self.every
            return "warn"
        return None


class FeedbackSender(threading.Thread):
    """RX side: every `period` s sends the filtered SNR_eff and codeword error rate.
    delay_s emulates the return path latency (e.g. 0.5 s for a GEO satellite)."""

    def __init__(self, rx_sink, video, host, port, period=0.02, delay_s=0.0):
        super().__init__(daemon=True, name="ACM feedback TX")
        self.reporter = FeedbackReporter(rx_sink, video, delay_s)
        self.dest, self.period = (host, port), period
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = True
        self.sent = 0

    def run(self):
        while self.running:
            time.sleep(self.period)
            r = self.reporter.report()
            if not r["valid"]:
                continue
            msg = {"snr": r["snr"], "cer": r["cer"], "mod": r["mod"], "session": r["session"], "t": time.time()}
            try:
                self.sock.sendto(json.dumps(msg).encode(), self.dest)
                self.sent += 1
            except OSError:
                pass

    def close(self):
        self.running = False
