"""ACM-driven video bitrate (v0.5).

One FFmpeg process encodes the same source into one MPEG-TS rendition per ACM modulation
(split filter -> N libx264 encoders, identical frames, GOP = 1 s with fixed keyframe positions,
same muxdelay -> identical PTS). Each rendition comes out on its own pipe. The sender forwards
ONE rendition and switches only at a keyframe boundary (GOP g of rendition A is followed by GOP g
of rendition B), like HLS/DASH adaptive streaming. TS continuity counters are rewritten so the
spliced stream is clean; PIDs, PAT/PMT and SPS/PPS (repeat-headers) are the same in every
rendition, so ffplay and the reference file video_tx.ts play straight through a switch.

Everything below the TS bytes is unchanged: DATA sequence numbers, RS8+8 groups and END are
produced by runner.Encoder exactly as with the fixed-rate encoder.

Policy (VideoRatePolicy): rendition i = 75 % of the TS capacity of modulation i.
  ACM modulation lower than the video level -> step down at the next keyframe (<= 1 GOP).
  ACM modulation higher -> step up only after it has been stable for hold_s (5 s).
"""
from collections import deque
import logging
import os
import subprocess
import threading
import time

from .config import TS_CHUNK_BYTES
from .protocol import DATA, END, pack_packet

LOG = logging.getLogger(__name__)

TS = TS_CHUNK_BYTES          # 188
NULL_PID = 0x1FFF


# ---------------------------------------------------------------- TS helpers
def ts_pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def is_keyframe_start(pkt):
    """First TS packet of a video keyframe PES: PUSI + adaptation field random_access_indicator
    (set by the FFmpeg mpegts muxer on keyframes)."""
    return (pkt[0] == 0x47 and pkt[1] & 0x40 and (pkt[3] >> 4) & 2 and pkt[4] > 0
            and pkt[5] & 0x40 and ts_pid(pkt) != NULL_PID)


class ContinuityRewriter:
    """Renumbers the 4-bit continuity counter per PID across rendition switches."""

    def __init__(self):
        self.cc = {}

    def __call__(self, pkt):
        pid = ts_pid(pkt)
        if pid == NULL_PID:
            return pkt
        afc = (pkt[3] >> 4) & 3
        prev = self.cc.get(pid)
        if afc & 1:                                    # payload: counter advances
            cc = 0 if prev is None else (prev + 1) & 15
        else:                                          # adaptation only: counter repeats
            cc = 0 if prev is None else prev
        self.cc[pid] = cc
        if cc == pkt[3] & 15:
            return pkt
        b = bytearray(pkt)
        b[3] = (b[3] & 0xF0) | cc
        return bytes(b)


# ---------------------------------------------------------------- ladder + policy
def video_ladder(settings, acm_capacity_bps, fill=None):
    """[(modulation, ts_muxrate, video_bitrate)] in ACM ladder order."""
    s = settings
    fill = s.adaptive_video_fill if fill is None else fill
    ratio = s.video_bitrate / s.ts_muxrate            # 200/260: H.264 share of the TS rate
    out = []
    for m, cap in acm_capacity_bps.items():
        mux = int(round(fill * cap / 1000.0)) * 1000
        out.append((m, mux, int(round(ratio * mux / 1000.0)) * 1000))
    return out


class VideoRatePolicy:
    """Video level follows the ACM modulation: down at once, up after `hold_s` of stability."""

    def __init__(self, modcods, hold_s=5.0):
        self.modcods = list(modcods)
        self.hold = hold_s

    def decide(self, level, modulation, stable_s):
        target = self.modcods.index(modulation) if modulation in self.modcods else 0
        if target < level:
            return target, "down"
        if target > level and stable_s >= self.hold:
            return target, "up"
        return level, None


# ---------------------------------------------------------------- FFmpeg with N outputs
class _Rendition:
    def __init__(self, index, fd):
        self.index, self.fd = index, fd
        self.pkts = deque()                           # (gop, 188-byte TS packet)
        self.gop = 0
        self.keyframes = 0
        self.eof = False
        self.bytes = 0


class AdaptiveEncoder(threading.Thread):
    """Same contract as runner.Encoder (count, error, done, video_tx.ts) with a bitrate ladder."""

    def __init__(self, settings, source, input_path, seconds, src, session, run_dir, stop,
                 controller, ladder, hold_s=None):
        super().__init__(daemon=True, name="FFmpeg ladder to OFDM")
        self.s, self.source, self.input_path, self.seconds = settings, source, input_path, seconds
        self.src, self.session, self.run_dir, self.stop = src, session, run_dir, stop
        self.ctl, self.ladder = controller, ladder
        self.policy = VideoRatePolicy([m for m, _, _ in ladder],
                                      settings.adaptive_video_hold_s if hold_s is None else hold_s)
        self.count = 0
        self.error = None
        self.done = threading.Event()
        self.cond = threading.Condition()
        self.rends = []
        self.level = None
        self.gop = 0
        self.history = []
        self.switches = 0
        self.backlog_max_s = 0.0
        self.t0 = None

    # -- the parity / END logic is shared with the fixed-rate encoder ------
    def _parity(self, group):
        from .runner import Encoder
        return Encoder._parity(self, group)

    def snapshot(self):
        with self.cond:
            lvl = self.level
            backlog = len(self.rends[lvl].pkts) if lvl is not None and self.rends else 0
        out = {"level": lvl, "switches": self.switches, "backlog_max_s": round(self.backlog_max_s, 2)}
        if lvl is not None:
            m, mux, vb = self.ladder[lvl]
            out.update(modulation=m, ts_muxrate=mux, video_bitrate=vb,
                       backlog_s=round(backlog * TS * 8 / mux, 2))
        return out

    def _reader(self, r):
        with os.fdopen(r.fd, "rb", buffering=0) as f:
            buf = b""
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                buf += chunk
                n = len(buf) // TS
                with self.cond:
                    for k in range(n):
                        pkt = buf[k * TS:(k + 1) * TS]
                        if is_keyframe_start(pkt):
                            if r.keyframes:
                                r.gop += 1
                            r.keyframes += 1
                        r.pkts.append((r.gop, pkt))
                    r.bytes += n * TS
                    self.cond.notify_all()
                buf = buf[n * TS:]
        with self.cond:
            r.eof = True
            self.cond.notify_all()

    def _record(self, reason, modulation):
        m, mux, vb = self.ladder[self.level]
        e = {"t": round(time.time(), 3), "t_s": round(time.monotonic() - self.t0, 2), "gop": self.gop,
             "level": self.level, "modulation": m, "ts_muxrate": mux, "video_bitrate": vb,
             "acm_modulation": modulation, "reason": reason}
        self.history.append(e)
        LOG.info("VIDEO %s: %s -> TS %d kb/s (H.264 %d kb/s) at GOP %d", reason, m, mux // 1000, vb // 1000,
                 self.gop)

    def _acm_state(self):
        m = self.ctl.modulation
        return m, time.monotonic() - self.ctl.mod_since

    def run(self):
        from .media import ladder_command
        self.t0 = time.monotonic()
        proc = None
        try:
            pipes = [os.pipe() for _ in self.ladder]
            cmd = ladder_command(self.s, self.source, self.input_path, self.seconds,
                                 [(mux, vb) for _, mux, vb in self.ladder], [w for _, w in pipes])
            with open(self.run_dir / "ffmpeg_tx.log", "wb") as err, \
                    open(self.run_dir / "video_tx.ts", "wb") as ref:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err,
                                        pass_fds=[w for _, w in pipes])
                for _, w in pipes:
                    os.close(w)
                self.rends = [_Rendition(i, r) for i, (r, _) in enumerate(pipes)]
                readers = [threading.Thread(target=self._reader, args=(r,), daemon=True) for r in self.rends]
                for t in readers:
                    t.start()
                m, _ = self._acm_state()
                self.level = self.policy.modcods.index(m) if m in self.policy.modcods else 0
                self._record("start", m)
                cc = ContinuityRewriter()
                group = []
                while not self.stop.is_set():
                    pkt = self._next_packet()
                    if pkt is None:
                        break
                    pkt = cc(pkt)
                    if not self.src.enqueue(pack_packet(DATA, self.session, self.count, pkt,
                                                        time.monotonic_ns()), self.stop):
                        break
                    ref.write(pkt)
                    self.count += 1
                    group.append(pkt)
                    if len(group) == 8:
                        if not self._parity(group):
                            break
                        group = []
                if self.stop.is_set():
                    proc.terminate()
                with self.cond:                        # unblock the readers of unused renditions
                    for r in self.rends:
                        r.pkts.clear()
                proc.wait(timeout=10)
                for t in readers:
                    t.join(timeout=5)
                ref.flush()
                if not self.stop.is_set():
                    if group:
                        self._parity(group)
                    for _ in range(12):
                        self.src.enqueue(pack_packet(END, self.session, self.count), self.stop)
                if proc.returncode not in (0, None) and not self.stop.is_set():
                    self.error = f"ffmpeg exited with {proc.returncode} (see ffmpeg_tx.log)"
        except Exception as exc:     # noqa: BLE001
            self.error = str(exc)
            LOG.exception("adaptive encoder failed")
            if proc is not None and proc.poll() is None:
                proc.terminate()
        finally:
            self.done.set()

    def _next_packet(self):
        """Next TS packet of the selected rendition; switches rendition at keyframe boundaries."""
        with self.cond:
            while True:
                cur = self.rends[self.level]
                while not self.stop.is_set():
                    while cur.pkts and cur.pkts[0][0] < self.gop:   # rendition still behind: skip
                        cur.pkts.popleft()
                    if cur.pkts or cur.eof:
                        break
                    self.cond.wait(0.2)
                if self.stop.is_set() or (cur.eof and not cur.pkts):
                    return None
                g, pkt = cur.pkts[0]
                if g > self.gop:                       # keyframe of the next GOP: decision point
                    self.gop = g
                    for r in self.rends:               # drop what can never be sent again
                        while r.pkts and r.pkts[0][0] < g:
                            r.pkts.popleft()
                    m, stable = self._acm_state()
                    new, reason = self.policy.decide(self.level, m, stable)
                    if new != self.level:
                        self.level = new
                        self.switches += 1
                        self._record(reason, m)
                        continue                       # take GOP g from the new rendition
                cur.pkts.popleft()
                backlog = len(cur.pkts) * TS * 8 / self.ladder[self.level][1]
                self.backlog_max_s = max(self.backlog_max_s, backlog)
                return pkt
