"""GNU Radio blocks: 2-channel MIMO OFDM source/sink, 2x2 channel simulator,
video PDU receiver. All PHY maths lives in modem.py (numpy)."""
import bisect
from collections import deque
import logging
import queue
import socket
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

from .config import PACKET_BYTES
from .modem import Modulator, Demodulator, SYM, N_CP, N_FFT, codewords_per_frame
from .protocol import DATA, IDLE, pack_packet, unpack_packet

LOG = logging.getLogger(__name__)


def _stats_ms(values):
    """median / p95 / max of latencies (s) in ms, or None."""
    v = sorted(values)
    if not v:
        return None
    return {"median_ms": round(1e3 * v[len(v) // 2], 1), "p95_ms": round(1e3 * v[min(len(v) - 1, int(0.95 * len(v)))], 1),
            "max_ms": round(1e3 * v[-1], 1), "n": len(v)}


class AirClock:
    """Maps the sample index of a stream to the host monotonic time at which that sample is on the
    air (TX) or was sampled (RX). RF: sample 0 is at device time `start_delay_s` (timed start), mapped
    once from the host time of set_time_now() and refined every second with get_time_now(). Simulation:
    TxDeviceSim starts it when it emits its first sample. Underflows/overflows shift the real mapping
    (not tracked)."""

    def __init__(self, rate):
        self.rate = float(rate)
        self.t0 = None                      # host monotonic time of sample 0
        self.resyncs = 0
        self.max_correction = 0.0

    def start(self, t0):
        self.t0 = float(t0)

    def time_of(self, k):
        return None if self.t0 is None else self.t0 + k / self.rate

    def sync_device(self, device_now_s, start_s, host_now):
        """device time now (get_time_now) -> t0; keeps the largest correction for the report."""
        t0 = host_now - (device_now_s - start_s)
        if self.t0 is not None:
            self.max_correction = max(self.max_correction, abs(t0 - self.t0))
        self.t0 = t0
        self.resyncs += 1


class TxPacing:
    """Shared by the TX sources: never produce more than `lead_s` ahead of the air time of the
    produced samples (lead_s = 0: produce as fast as the downstream buffers accept, v0.5 behaviour,
    which fills the GNU Radio and USRP buffers). Also logs the buffer delay of every frame:
    air time of its first sample - host time it was built."""

    def _pacing_init(self, rate, clock, lead_s):
        self.rate = float(rate)
        self.clock = clock
        self.lead_s = float(lead_s or 0.0)
        self.buffer_lat = deque(maxlen=2000)      # s, per frame: built -> on air
        self.buffer_lat_tick = []                 # since the last tick (history)
        self.late = 0                             # frames built after their air time (underflow risk)

    def _allowed(self, n, multiple):
        """Samples this work() call may produce (a multiple of `multiple`, never 0: returning 0 would
        leave the GNU Radio scheduler waiting for a buffer notification that never comes). Sleeps in
        short steps until the air clock allows one more chunk."""
        if self.lead_s <= 0:
            return n
        while True:
            now = time.monotonic()
            if self.clock is None or self.clock.t0 is None:
                budget = self.lead_s * self.rate - self.produced          # before the start: one lead
                if budget < multiple:                                       # clock not started yet
                    time.sleep(0.005)
                    continue
            else:
                budget = (now - self.clock.t0 + self.lead_s) * self.rate - self.produced
            # at least ~5 ms per call (or what fits): tiny chunks would make the thread spin
            need = min(n, max(multiple, int(0.005 * self.rate) // multiple * multiple))
            if budget >= need:
                return min(n, int(budget) // multiple * multiple)
            # sleep until ~5 ms of samples may be produced (fewer wake-ups: GIL, see TxDeviceSim)
            time.sleep(max(0.004, (multiple - budget) / self.rate))

    def _frame_built(self, k):
        """Frame whose first sample has index k was just built."""
        if self.clock is None or self.clock.t0 is None:
            return None
        lat = self.clock.time_of(k) - time.monotonic()
        with self.lock:
            self.buffer_lat.append(lat)
            self.buffer_lat_tick.append(lat)
            self.late += lat < 0
        return lat

    def latency_tick(self):
        """Buffer delay stats since the previous call (one history row)."""
        with self.lock:
            v, self.buffer_lat_tick = self.buffer_lat_tick, []
        return _stats_ms(v)

    def latency_report(self):
        with self.lock:
            v = list(self.buffer_lat)
        return {"buffer": _stats_ms(v), "lead_ms": round(1e3 * self.lead_s, 1), "late_frames": self.late}


class TxDeviceSim(gr.sync_block):
    """Simulation of the USRP TX side: emits samples at the real sample rate (the air clock starts
    at its first sample). The GNU Radio buffer in front of it (set_min_output_buffer of the source,
    `sim_tx_device_samples`) plays the role of the device/host TX buffers, so a source without
    pacing runs that far ahead, as on RF."""

    def __init__(self, nchan, rate, clock):
        gr.sync_block.__init__(self, "TX device (sim)", [np.complex64] * nchan, [np.complex64] * nchan)
        self.rate, self.clock = float(rate), clock
        self.out = 0
        self.GRAIN = 0.01
        self.underruns = 0
        self._late = False

    def work(self, input_items, output_items):
        n = len(output_items[0])
        now = time.monotonic()
        if self.clock.t0 is None:
            self.clock.start(now)
        due = (now - self.clock.t0) * self.rate - self.out        # samples that should be on air by now
        if due > n + 0.005 * self.rate:                           # input arrives late: the device underflows
            if not self._late:
                self.underruns += 1
            self._late = True
        else:
            self._late = False
        # 10 ms granularity and sleeps >= 4 ms: frequent short wake-ups would steal the GIL from the
        # demodulator threads of the same process
        grain = self.GRAIN * self.rate
        while due < -0.5 * grain:          # more than half a grain ahead: sleep (never return 0, the
            time.sleep(max(0.004, (-due - 0.5 * grain) / self.rate))     # scheduler would stall)
            due = (time.monotonic() - self.clock.t0) * self.rate - self.out
        allow = int(due + grain)                                  # emit up to one grain ahead
        k = min(n, allow)
        for i, o in zip(input_items, output_items):
            o[:k] = i[:k]
        self.out += k
        return k


class MimoTxSource(TxPacing, gr.sync_block):
    """Application packets -> continuous 2-channel baseband (IDLE frames when empty)."""

    def __init__(self, settings, session, throttle=False, acm=None, clock=None, lead_s=0.0):
        """acm: None (fixed modulation) or an acm.AcmController that picks the
        modulation of every frame (frames then carry several packets).
        clock / lead_s: AirClock of this stream and the pacing lead (TxPacing)."""
        gr.sync_block.__init__(self, "MIMO OFDM TX source", [], [np.complex64, np.complex64])
        s = settings
        self.acm = acm
        self.mod = Modulator(s.mimo_mode, s.modulation, s.frame_gap_samples, s.tx_rms, s.interleaver,
                             acm=acm is not None, fec=getattr(s, "fec", "rs"),
                             ldpc_rate=getattr(s, "ldpc_rate", "3/4"))
        self.packets = queue.Queue(maxsize=s.queue_packets)
        self.idle = pack_packet(IDLE, session)
        self.idle_frames = {}
        if acm is None:
            self.idle_frame = self.mod.frame(self.idle)   # cached: IDLE costs no CPU
        else:
            for m in acm.modcods:
                n = codewords_per_frame(s.mimo_mode, m)
                self.idle_frames[m] = self.mod.frame([self.idle] * n, m)
        self.mod_frames = {}                              # frames sent per modulation (ACM)
        # ACM resend on downshift: packets sent in the last loop delay at a higher modulation were
        # most likely lost (the RX had not reported the fade yet). They are sent again, first.
        # The RX ignores copies it already has (closed or duplicate), so nothing changes there.
        self.resend_min_s = float(getattr(s, "acm_resend_s", 0.0)) if acm is not None else 0.0
        self.recent = deque()                             # (monotonic t, modulation rank, packet)
        self.resend = deque()
        self.last_rank = None
        self.resend_events = self.resent_packets = 0
        self.packets_sent = 0
        self.pending = np.zeros((2, 0), np.complex64)
        self.pos = 0
        self.throttle = throttle
        self.rate = s.sample_rate
        self.t0 = None
        self.produced = 0
        self.lock = threading.Lock()
        self.frames = self.data_frames = self.enqueued_data = 0
        self.queue_max = 0
        self._pacing_init(s.sample_rate, clock, lead_s)
        self.switch_air = deque(maxlen=500)       # ACM: controller switch -> first frame of it on air (s)
        self.switch_air_tick = []
        self.prev_mod = None
        self.set_output_multiple(SYM)
        self.set_min_noutput_items(SYM if self.lead_s > 0 else 8 * SYM)

    def enqueue(self, raw, stop_event):
        while not stop_event.is_set():
            try:
                self.packets.put(raw, timeout=0.1)
                with self.lock:
                    self.queue_max = max(self.queue_max, self.packets.qsize())
                    self.enqueued_data += raw[5] == DATA
                return True
            except queue.Full:
                continue
        return False

    def work(self, input_items, output_items):
        out0, out1 = output_items
        n = self._allowed(len(out0), SYM)
        if self.throttle:
            if self.t0 is None:
                self.t0 = time.monotonic()
            ahead = self.produced / self.rate - (time.monotonic() - self.t0)
            if ahead > 0.02:
                time.sleep(min(ahead - 0.01, 0.05))
        written = 0
        while written < n:
            if self.pos >= self.pending.shape[1]:
                if self.acm is not None:
                    self._next_acm_frame(self.produced + written)
                else:
                    try:
                        raw = self.packets.get_nowait()
                        self.pending = self.mod.frame(raw)
                    except queue.Empty:
                        raw = self.idle
                        self.pending = self.idle_frame
                    with self.lock:
                        self.frames += 1
                        self.data_frames += raw[5] == DATA
                        self.packets_sent += raw[5] != IDLE
                    self._frame_built(self.produced + written)
                self.pos = 0
            take = min(n - written, self.pending.shape[1] - self.pos)
            out0[written:written + take] = self.pending[0, self.pos:self.pos + take]
            out1[written:written + take] = self.pending[1, self.pos:self.pos + take]
            written += take
            self.pos += take
        self.produced += n
        return n

    def _resend_window(self):
        lat = sorted(self.acm.latency)
        loop = lat[len(lat) // 2] if lat else 0.0
        return min(0.5, max(self.resend_min_s, 1.5 * loop))

    def _next_acm_frame(self, k=0):
        m = self.acm.modulation
        lat = self._frame_built(k)
        if self.prev_mod is not None and m != self.prev_mod and lat is not None:
            # this frame is the first of the new modulation: switch decision -> on air
            d = time.monotonic() + lat - self.acm.last_switch
            with self.lock:
                self.switch_air.append(d)
                self.switch_air_tick.append(d)
        self.prev_mod = m
        n = codewords_per_frame(self.mod.mode, m)
        rank = self.acm.modcods.index(m)
        if self.resend_min_s > 0:
            now = time.monotonic()
            win = self._resend_window()
            while self.recent and self.recent[0][0] < now - win:
                self.recent.popleft()
            if self.last_rank is not None and rank < self.last_rank and self.recent:
                again = [pk for _, r, pk in self.recent if r > rank]
                self.resend.extend(again)
                self.recent.clear()
                with self.lock:
                    self.resend_events += 1
                    self.resent_packets += len(again)
        self.last_rank = rank
        batch = []
        while self.resend and len(batch) < n:
            batch.append(self.resend.popleft())
        while len(batch) < n:
            try:
                batch.append(self.packets.get_nowait())
            except queue.Empty:
                break
        if batch:
            if self.resend_min_s > 0:
                now = time.monotonic()
                self.recent.extend((now, rank, pk) for pk in batch)
            data = sum(r[5] == DATA for r in batch)
            self.pending = self.mod.frame(batch + [self.idle] * (n - len(batch)), m)
        else:
            data = 0
            self.pending = self.idle_frames[m]
        with self.lock:
            self.frames += 1
            self.data_frames += data
            self.packets_sent += len(batch)
            self.mod_frames[m] = self.mod_frames.get(m, 0) + 1

    def snapshot(self):
        with self.lock:
            out = {"tx_frames": self.frames, "tx_data_frames": self.data_frames,
                   "tx_enqueued_data": self.enqueued_data, "tx_queue": self.packets.qsize(),
                   "tx_queue_max": self.queue_max, "tx_queue_size": self.packets.maxsize,
                   "tx_packets": self.packets_sent, "frame_samples": self.mod.frame_len}
            out["tx_late_frames"] = self.late
            if self.acm is not None:
                out.update(acm_modulation=self.acm.modulation, acm_snr_db=self.acm.snr,
                           acm_switches=self.acm.switches, acm_frames=dict(self.mod_frames),
                           acm_resend_events=self.resend_events, acm_resent_packets=self.resent_packets)
            return out


class MimoRxSink(gr.sync_block):
    """2-channel baseband -> post-CRC application packets on message port 'pdus'.

    Acquisition: Schmidl-Cox search over the whole buffer. Tracking: frames are
    back to back, so up to MAX_BATCH frames are re-timed in small windows and
    demodulated together (numpy batch) to keep up in real time.
    """
    MAX_BATCH = 16

    def __init__(self, settings):
        gr.sync_block.__init__(self, "MIMO OFDM RX", [np.complex64, np.complex64], [])
        s = settings
        self.acm = bool(getattr(s, "acm", False))
        self.demod = Demodulator(s.mimo_mode, s.modulation, s.sync_threshold, soft=s.soft_decoding,
                                 acm=self.acm, fine_timing=bool(getattr(s, "fine_timing", True)),
                                 sig_recover=bool(getattr(s, "sig_recover", True)),
                                 fec=getattr(s, "fec", "rs"), ldpc_rate=getattr(s, "ldpc_rate", "3/4"),
                                 ldpc_iter=int(getattr(s, "ldpc_iter", 20)))
        self.snr_eff = None                    # filtered effective SNR (ACM feedback)
        self.modulation = None if self.acm else s.modulation
        self.mod_frames = {}
        self.cw_ok = self.cw_bad = 0
        self.last_frame_t = None               # monotonic time of the last decoded ACM frame
        self.on_packet = None                  # optional callable(packet bytes), e.g. radio feedback
        self.frame_len = (4 + self.demod.np) * SYM + s.frame_gap_samples
        self.body = self.demod.body_len
        self.cap = 1 << 18
        self.buf = np.zeros((2, self.cap), np.complex64)
        self.n = 0
        self.expected = None          # index of the next frame start while tracking
        self.port = pmt.intern("pdus")
        self.message_port_register_out(self.port)
        # Constellation: equalised data symbols of one good frame every `const_every` frames,
        # published as a c32 PDU (pdu_to_tagged_stream -> QT GUI Constellation Sink).
        self.const_port = pmt.intern("const")
        self.message_port_register_out(self.const_port)
        self.demod.const_every = int(getattr(s, "const_every", 8))
        self.last_const = None
        self.lock = threading.Lock()
        self.stats = dict(detected=0, ok=0, sig_fail=0, rs_fail=0, crc_fail=0,
                          corrected_bytes=0, lost_sync=0, soft_decoded=0, sig_recovered=0)
        self.interleaver = None
        self.snr = self.evm = self.cond = None
        self.rx_rms = 0.0
        self.rx_rms_ch = (0.0, 0.0)            # input 0 / 1 (RF0 / RF1 in two-channel RF mode)
        # latency: absolute sample index of buf[0], arrival time of every input chunk
        self.dropped = 0
        self.arr_idx, self.arr_t = [], []      # absolute index after each input chunk, its host time
        self.clock = None                      # AirClock of the RX stream (RF), for the ingest delay
        self.proc_lat, self.ingest_lat = deque(maxlen=4000), deque(maxlen=4000)
        self.proc_tick, self.ingest_tick = [], []

    # -- buffer ---------------------------------------------------------
    def _drop(self, count):
        count = min(count, self.n)
        self.buf[:, :self.n - count] = self.buf[:, count:self.n]
        self.n -= count
        self.dropped += count
        if self.expected is not None:
            self.expected -= count

    def work(self, input_items, output_items):
        a, b = input_items
        m = len(a)
        if self.n + m > self.cap:
            keep = self.body + 4 * SYM
            self._drop(self.n - keep)
            self.expected = None
            with self.lock:
                self.stats["lost_sync"] += 1
        self.buf[0, self.n:self.n + m] = a
        self.buf[1, self.n:self.n + m] = b
        self.n += m
        self.arr_idx.append(self.dropped + self.n)
        self.arr_t.append(time.monotonic())
        if len(self.arr_idx) > 4096:                       # forget chunks already dropped from the buffer
            cut = max(1, bisect.bisect_left(self.arr_idx, self.dropped))
            del self.arr_idx[:cut], self.arr_t[:cut]
        if m:
            self.rx_rms = float(np.sqrt(np.mean(np.abs(a[-4096:]) ** 2)))
            self.rx_rms_ch = (self.rx_rms, float(np.sqrt(np.mean(np.abs(b[-4096:]) ** 2))))
        self._process()
        return m

    # -- state machine ----------------------------------------------------
    def _process(self):
        while True:
            if self.expected is None:
                if not self._acquire():
                    return
            elif not self._track():
                return

    def _acquire(self):
        need = self.body + 2 * SYM
        if self.n < need + SYM:
            return False
        limit = self.n - need
        s0, _ = self.demod.find(self.buf[:, :limit + 2 * SYM])
        if s0 is None or s0 > limit:
            self._drop(self.n - (need + SYM))      # keep the tail, search again later
            return False
        self._drop(max(0, s0 - SYM))
        self.expected = min(s0, SYM)
        return True

    def _track(self):
        starts, metrics = [], []
        approx = self.expected
        while len(starts) < self.MAX_BATCH and approx + self.frame_len + 2 * SYM <= self.n:
            lo = max(0, approx - SYM // 2)
            # coarse search over 2 symbols; the extra 4 symbols let the LTF fine timing see both LTFs
            s0, p = self.demod.find(self.buf[:, lo:lo + 6 * SYM + N_CP], stop=2 * SYM + N_CP - N_FFT + 1)
            if s0 is None:
                break
            starts.append(lo + s0)
            metrics.append(p)
            approx = lo + s0 + self.frame_len
        if not starts:
            if self.expected + self.frame_len + 2 * SYM <= self.n:
                self.expected = None               # window was complete: sync lost
                with self.lock:
                    self.stats["lost_sync"] += 1
                return True
            return False
        results = self.demod.demodulate_batch(self.buf, starts, metrics)
        self._log_latency(starts)
        for s0, (packet, info) in zip(starts, results):
            reason = info["reason"]
            with self.lock:
                self.stats["detected"] += 1
                if "reasons" in info:              # ACM: count every codeword
                    for r in info["reasons"]:
                        self.stats[{"ok": "ok", "rs": "rs_fail", "crc": "crc_fail"}[r]] += 1
                    good = sum(r == "ok" for r in info["reasons"])
                    self.cw_ok += good
                    self.cw_bad += len(info["reasons"]) - good
                    m = info["modulation"]
                    self.modulation = m
                    self.mod_frames[m] = self.mod_frames.get(m, 0) + 1
                    x = info["snr_eff_db"]
                    self.snr_eff = x if self.snr_eff is None else 0.8 * self.snr_eff + 0.2 * x
                    self.last_frame_t = time.monotonic()
                else:
                    self.stats[{"ok": "ok", "sig": "sig_fail", "rs": "rs_fail", "crc": "crc_fail"}[reason]] += 1
                self.stats["corrected_bytes"] += info.get("corrected", 0)
                self.stats["soft_decoded"] += bool(info.get("soft_erasures"))
                self.stats["sig_recovered"] += bool(info.get("sig_recovered"))
                self.interleaver = info.get("interleaver", self.interleaver)
                if "snr_db" in info:
                    self.snr, self.evm = info["snr_db"], info["evm_db"]
                    self.cond = info.get("cond_db", self.cond)
            if reason == "sig":                    # false or broken frame: re-acquire after it
                self._drop(s0 + SYM)
                self.expected = None
                return True
            if "const" in info:
                c = info["const"]
                self.last_const = c
                self.message_port_pub(self.const_port, pmt.cons(
                    pmt.PMT_NIL, pmt.init_c32vector(len(c), c.tolist())))
            for pk in (packet if isinstance(packet, list) else [packet] if packet is not None else []):
                if self.on_packet is not None:
                    self.on_packet(pk)
                self.message_port_pub(self.port, pmt.cons(
                    pmt.PMT_NIL, pmt.init_u8vector(len(pk), list(pk))))
        nxt = starts[-1] + self.frame_len
        self._drop(nxt - SYM)
        self.expected = SYM
        return True

    def _log_latency(self, starts):
        """Per decoded frame: arrival of its last sample -> decoded (processing: ring buffer + batch),
        and, with an AirClock, sampled at the antenna -> arrival (USRP + UHD + GNU Radio buffers)."""
        now = time.monotonic()
        for s0 in starts:
            end = self.dropped + s0 + self.body
            j = min(bisect.bisect_left(self.arr_idx, end), len(self.arr_idx) - 1)
            t_arr = self.arr_t[j]
            with self.lock:
                self.proc_lat.append(now - t_arr)
                self.proc_tick.append(now - t_arr)
                if self.clock is not None and self.clock.t0 is not None:
                    d = t_arr - self.clock.time_of(end)
                    self.ingest_lat.append(d)
                    self.ingest_tick.append(d)

    def latency_tick(self):
        with self.lock:
            p, i = self.proc_tick, self.ingest_tick
            self.proc_tick, self.ingest_tick = [], []
        return {"proc": _stats_ms(p), "ingest": _stats_ms(i)}

    def latency_report(self):
        with self.lock:
            return {"proc": _stats_ms(self.proc_lat), "ingest": _stats_ms(self.ingest_lat)}

    def snapshot(self):
        with self.lock:
            return dict(self.stats, fec_mismatch=int(self.demod.fec_mismatch), snr_db=self.snr, evm_db=self.evm, cond_db=self.cond,
                        rx_rms=self.rx_rms, rx_rms_ch=self.rx_rms_ch, tracking=self.expected is not None,
                        interleaver=self.interleaver, soft_skipped=self.demod.soft_skipped,
                        acm=self.acm, modulation=self.modulation, snr_eff_db=self.snr_eff,
                        acm_frames=dict(self.mod_frames))

    def acm_state(self):
        with self.lock:
            age = None if self.last_frame_t is None else time.monotonic() - self.last_frame_t
            return {"snr_eff_db": self.snr_eff, "cw_ok": self.cw_ok, "cw_bad": self.cw_bad,
                    "modulation": self.modulation, "age_s": age}


class FeedbackRadioSource(TxPacing, gr.sync_block):
    """RX machine, radio feedback (--fb-link radio): continuous SISO BPSK OFDM frames on
    N321 #2 port RF1 TX/RX (channel B:0), each carrying the latest ACM report (acm.FeedbackReporter).
    About 133 frames/s at 500 kS/s: the TX sees a fresh report every ~7.5 ms."""

    def __init__(self, settings, provider, throttle=False, clock=None, lead_s=0.0):
        gr.sync_block.__init__(self, "ACM radio feedback source", [], [np.complex64])
        from dataclasses import replace
        fs = replace(settings, mimo_mode="siso", modulation="bpsk", acm=False)
        self.mod = Modulator("siso", "bpsk", fs.frame_gap_samples, fs.tx_rms, "none")
        self.provider = provider              # callable -> 212-byte packet
        self.pending = np.zeros(0, np.complex64)
        self.pos = 0
        self.throttle, self.rate = throttle, settings.fb_rate
        self.t0, self.produced = None, 0
        self.frames = 0
        self.lock = threading.Lock()
        self._pacing_init(settings.fb_rate, clock, lead_s)    # a report waits in the buffers this long
        self.set_output_multiple(SYM)

    def work(self, input_items, output_items):
        out = output_items[0]
        n = self._allowed(len(out), SYM)
        if self.throttle:
            if self.t0 is None:
                self.t0 = time.monotonic()
            ahead = self.produced / self.rate - (time.monotonic() - self.t0)
            if ahead > 0.02:
                time.sleep(min(ahead - 0.01, 0.05))
        written = 0
        while written < n:
            if self.pos >= self.pending.size:
                self.pending = self.mod.frame(self.provider())[0]
                self.pos = 0
                self.frames += 1
                self._frame_built(self.produced + written)
            take = min(n - written, self.pending.size - self.pos)
            out[written:written + take] = self.pending[self.pos:self.pos + take]
            written += take
            self.pos += take
        self.produced += n
        return n


def feedback_rx_settings(settings):
    """Settings of the feedback receiver on the TX machine (SISO BPSK at fb_rate)."""
    from dataclasses import replace
    return replace(settings, mimo_mode="siso", modulation="bpsk", acm=False, const_every=0,
                   sample_rate=settings.fb_rate)


class MimoChannelSim(gr.sync_block):
    """2x2 flat or 2-tap channel + AWGN + CFO (simulation only)."""

    def __init__(self, settings):
        gr.sync_block.__init__(self, "2x2 channel simulator",
                               [np.complex64, np.complex64], [np.complex64, np.complex64])
        s = settings
        rng = np.random.default_rng(s.sim_seed)
        if s.sim_channel == "identity":
            self.H = np.eye(2, dtype=complex)
            self.H2 = np.zeros((2, 2), complex)
        elif s.sim_channel == "fixed":
            self.H = np.array([[1.0, 0.45j], [-0.35, 0.9 * np.exp(0.7j)]])
            self.H2 = 0.15 * np.array([[1, -1j], [0.5j, 1]])
        else:
            g = lambda: (rng.standard_normal((2, 2)) + 1j * rng.standard_normal((2, 2))) / np.sqrt(2)
            self.H, self.H2 = g(), 0.25 * g()
        self.snr_db = s.sim_snr_db
        self.tx_rms = s.tx_rms
        self.cfo = s.sim_cfo_hz / s.sample_rate
        self.phase = 0.0
        self.mem = np.zeros((2, 3), np.complex64)
        sigma = self.tx_rms / np.sqrt(10 ** (self.snr_db / 10) * 2)
        n = 1 << 18                                   # pre-generated noise, reused cyclically
        self.noise = (sigma * (rng.standard_normal((2, n + 65536)) +
                               1j * rng.standard_normal((2, n + 65536)))).astype(np.complex64)
        self.noise_pos = 0
        self.rate = s.sample_rate
        self.samples = 0
        self.profile = parse_snr_profile(getattr(s, "sim_snr_profile", ""))
        self.snr_now = self.snr_db
        self.H = self.H.astype(np.complex64)
        self.H2 = self.H2.astype(np.complex64)

    def work(self, input_items, output_items):
        x = np.stack(input_items)
        n = x.shape[1]
        ext = np.concatenate([self.mem, x], axis=1)
        y = self.H @ x + self.H2 @ ext[:, :n]
        self.mem = ext[:, -3:]
        if n > 65536:
            raise RuntimeError("channel simulator chunk too large")
        noise = self.noise[:, self.noise_pos:self.noise_pos + n]
        if self.profile is not None:            # time-varying SNR (ACM tests): scale the noise
            self.snr_now = self.profile(self.samples / self.rate)
            noise = noise * np.float32(10 ** ((self.snr_db - self.snr_now) / 20))
        self.samples += n
        y += noise
        self.noise_pos = (self.noise_pos + n) % (self.noise.shape[1] - 65536)
        rot = np.exp(2j * np.pi * (self.cfo * np.arange(n)) + 1j * self.phase).astype(np.complex64)
        self.phase = (self.phase + 2 * np.pi * self.cfo * n) % (2 * np.pi)
        output_items[0][:] = y[0] * rot
        output_items[1][:] = y[1] * rot
        return n


def parse_snr_profile(text):
    """'' -> None (fixed SNR). Otherwise f(t) -> SNR dB:
         sine:MIN:MAX:PERIOD        e.g. sine:6:30:40
         steps:V1,V2,...:DWELL      e.g. steps:28,18,8,14,24:10  (repeats)
         ramp:START:END:DURATION    linear, then holds END
         dips:BASE:DEPTH:RATE:MS    BASE dB with random fades DEPTH dB deep, RATE per second, MS long"""
    if not text:
        return None
    kind, _, rest = str(text).partition(":")
    parts = rest.split(":")
    if kind == "sine":
        lo, hi, period = map(float, parts)
        return lambda t: lo + (hi - lo) * 0.5 * (1 + np.cos(2 * np.pi * t / period))
    if kind == "steps":
        vals, dwell = [float(v) for v in parts[0].split(",")], float(parts[1])
        return lambda t: vals[int(t // dwell) % len(vals)]
    if kind == "ramp":
        a, b, dur = map(float, parts)
        return lambda t: a + (b - a) * min(1.0, t / dur)
    if kind == "dips":
        # steady SNR with short random fades (RF 23/11: SNR_eff 18.5 dB with isolated reports at ~14 dB)
        base, depth, rate, dur_ms = map(float, parts)
        starts = np.cumsum(np.random.default_rng(7).exponential(1.0 / rate, int(rate * 3600) + 10))
        dur = dur_ms / 1e3

        def f(t):
            i = np.searchsorted(starts, t) - 1
            return base - depth if i >= 0 and t - starts[i] < dur else base
        return f
    raise ValueError("sim_snr_profile: sine:MIN:MAX:PERIOD | steps:V1,V2,..:DWELL | ramp:A:B:SECONDS | "
                     "dips:BASE:DEPTH:PER_SECOND:MS")


class VideoReceiver(gr.basic_block):
    """Post-CRC packets -> verified RS8+8 reassembly -> TS file + UDP (as in N210 v1.23).

    reorder_s > 0 (ACM): packets wait up to reorder_s in a small buffer sorted by
    (RS group, DATA before parity, index) before the RS8+8 state sees them. After an ACM
    downshift the TX resends the packets of the last loop delay; a few codewords of the old
    modulation may still get through first, and without this buffer they would close the RS
    group before the resent copies arrive. The state itself (and the wire format) is unchanged."""

    def __init__(self, session, path, udp_port, reorder_s=0.0):
        """session=None: adopt the session of the first valid packet (two-machine RX)."""
        gr.basic_block.__init__(self, "Video PDU receiver", [], [])
        self.session = session
        self.state = None if session is None else self._new_state(session)
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.file = open(path, "wb")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dest = ("127.0.0.1", udp_port)
        self.pdus = self.errors = 0
        self.closed = False
        self.reorder_s = float(reorder_s)
        self.heap = []
        self.heap_n = 0                         # tie-breaker: arrival order
        self.reordered = 0                      # packets that overtook an earlier one
        self.last_key = None
        self.message_port_register_in(pmt.intern("pdus"))
        self.set_msg_handler(pmt.intern("pdus"), self.handle)

    @staticmethod
    def _new_state(session):
        from .verified_fec import RSParityCheckedState
        return RSParityCheckedState(session)

    @staticmethod
    def _order_key(raw):
        """(group base, 0 DATA / 1 parity / 2 END, index) or None for IDLE / unknown."""
        from .protocol import END, RS8_PARITY
        kind, seq, stamp = raw[5], int.from_bytes(raw[12:16], "big"), int.from_bytes(raw[16:24], "big")
        if kind == DATA:
            return (seq - seq % 8, 0, seq)
        if kind == RS8_PARITY:
            return (seq, 1, stamp & 0xFF)
        if kind == END:
            return (seq, 2, 0)
        return None

    def handle(self, msg):
        try:
            raw = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
            with self.lock:
                if self.closed:
                    return
                self.pdus += 1
                key = self._order_key(raw) if self.reorder_s > 0 and len(raw) == PACKET_BYTES else None
                if key is None:
                    self._ingest(raw)
                else:
                    import heapq
                    now = time.monotonic()
                    heapq.heappush(self.heap, (key, self.heap_n, now, raw))
                    self.heap_n += 1
                    self._release(now - self.reorder_s)
        except Exception:      # noqa: BLE001
            self.errors += 1
            LOG.exception("PDU handler failed")

    def _release(self, older_than):
        import heapq
        while self.heap and self.heap[0][2] <= older_than:
            key, _, _, raw = heapq.heappop(self.heap)
            if self.last_key is not None and key < self.last_key:
                self.reordered += 1
            self.last_key = key if self.last_key is None else max(key, self.last_key)
            self._ingest(raw)

    def _ingest(self, raw):
        if self.state is None:
            try:
                self.session = unpack_packet(raw)[1]
            except ValueError:
                return
            self.state = self._new_state(self.session)
            LOG.info("RX adopted TX session %d", self.session)
        payload = self.state.ingest(raw)
        if payload:
            self.file.write(payload)
            try:
                self.sock.sendto(payload, self.dest)
            except OSError:
                pass
        if self.state.final_packet_count is not None:
            self.file.flush()
            self.ended.set()

    def poll(self):
        """Release packets that waited long enough (call when no PDU arrives, e.g. once a second)."""
        with self.lock:
            if not self.closed and self.heap:
                self._release(time.monotonic() - self.reorder_s)

    def snapshot(self):
        with self.lock:
            if self.state is None:
                snap = {"data_packets": 0, "missing_data_packets": 0, "end_received": False,
                        "final_packet_count": None}
            else:
                snap = self.state.snapshot()
            snap.update(post_crc_pdus=self.pdus, handler_errors=self.errors, session=self.session)
            if self.reorder_s > 0:
                snap.update(reorder_s=self.reorder_s, reorder_waiting=len(self.heap))
            return snap

    def close(self):
        with self.lock:
            if not self.closed:
                self._release(float("inf"))     # whatever still waits goes to the RS8+8 state
                self.closed = True
                self.file.close()
                self.sock.close()


class SfoResampler(gr.basic_block):
    """Simulate a sample-clock offset between two separate radios.

    Output sample k is taken at input time k * (1 + ppm*1e-6) with a 16-tap
    windowed-sinc fractional-delay interpolator (256 phases): flat over the OFDM
    band, unlike linear interpolation which attenuates the edge carriers by up to
    ~11 dB when the fractional delay is near 0.5. Simulation only.
    """
    TAPS, PHASES, HIST = 16, 256, 8

    def __init__(self, ppm):
        gr.basic_block.__init__(self, "SFO resampler", [np.complex64, np.complex64],
                                [np.complex64, np.complex64])
        self.step = 1.0 + ppm * 1e-6
        self.t = float(self.HIST)          # read position relative to the first unread input
        off = np.arange(self.TAPS) - (self.HIST - 1)            # -7 .. 8
        d = np.arange(self.PHASES + 1)[:, None] / self.PHASES
        x = off[None, :] - d
        h = np.sinc(x) * (0.42 + 0.5 * np.cos(np.pi * x / 8.5) + 0.08 * np.cos(2 * np.pi * x / 8.5))
        self.table = (h / h.sum(axis=1, keepdims=True)).astype(np.float32)
        self.off = off

    def forecast(self, noutput_items, ninputs):
        need = int(noutput_items * self.step) + self.TAPS + 2
        return [need] * ninputs

    def general_work(self, input_items, output_items):
        n_in = min(len(input_items[0]), len(input_items[1]))
        n_out = min(len(output_items[0]), int((n_in - self.TAPS - 1 - self.t) / self.step))
        if n_out <= 0:
            return 0
        # The fractional delay drifts by only ppm*1e-6 per sample: keep it constant over
        # blocks of 1024 outputs (drift < 0.01 sample) -> 16 vector multiply-adds per block.
        B = 1024
        for b in range(0, n_out, B):
            m = min(B, n_out - b)
            p0 = self.t + self.step * b
            i0 = int(np.floor(p0))
            taps = self.table[int(round((p0 - i0) * self.PHASES))]
            for ch in (0, 1):
                x = input_items[ch]
                acc = np.zeros(m, np.complex64)
                for k, o in enumerate(self.off):
                    acc += taps[k] * x[i0 + o:i0 + o + m]
                output_items[ch][b:b + m] = acc
        end = self.t + self.step * n_out
        used = max(0, int(end) - self.HIST)
        self.t = end - used
        self.consume_each(used)
        return n_out
