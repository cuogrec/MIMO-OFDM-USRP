"""GNU Radio blocks: 2-channel MIMO OFDM source/sink, 2x2 channel simulator,
video PDU receiver. All PHY maths lives in modem.py (numpy)."""
import bisect
from collections import deque
from functools import lru_cache
import logging
import queue
import socket
import sys
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

from .config import PACKET_BYTES, parse_stall
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
    air (TX) or was sampled (RX).

    dev0 = host time of device time 0; the stream is anchored: sample anchor_k is at device time anchor_dev.
    RF: dev0 from set_time_now(0) (refined every second with get_time_now()), first anchor = timed start
    (sample 0 at start_delay_s). A TX source re-anchors when it falls behind (TxPacing._resync: new burst
    with a tx_time tag). Simulation: TxDeviceSim sets dev0 at its first sample."""

    def __init__(self, rate):
        self.rate = float(rate)
        self.dev0 = None                    # host monotonic time of device time 0
        self.anchor_k, self.anchor_dev = 0, 0.0
        self.resyncs = 0                    # device-time refinements (get_time_now)
        self.reanchors = 0                  # timeline restarts after a late event
        self.max_correction = 0.0

    def start(self, dev0, start_dev=0.0):
        self.dev0, self.anchor_k, self.anchor_dev = float(dev0), 0, float(start_dev)

    @property
    def t0(self):
        """host time of sample 0 on the current timeline (None before the start)"""
        return None if self.dev0 is None else self.dev0 + self.anchor_dev - self.anchor_k / self.rate

    def time_of(self, k):
        return None if self.dev0 is None else self.dev0 + self.anchor_dev + (k - self.anchor_k) / self.rate

    def device_time(self, host):
        return host - self.dev0

    def reanchor(self, k, host_time):
        """Sample k will be on air at host_time; returns its device time (for the tx_time tag)."""
        self.anchor_k, self.anchor_dev = k, host_time - self.dev0
        self.reanchors += 1
        return self.anchor_dev

    def sync_device(self, device_now_s, host_now):
        """device time now (get_time_now) -> host/device offset; keeps the largest correction."""
        dev0 = host_now - device_now_s
        if self.dev0 is not None:
            self.max_correction = max(self.max_correction, abs(dev0 - self.dev0))
        self.dev0 = dev0
        self.resyncs += 1


@lru_cache(maxsize=1)
def _gil_items_per_s():
    t = time.perf_counter()
    sum(range(2_000_000))
    return 2_000_000 / max(1e-6, time.perf_counter() - t)


def hold_gil(seconds):
    """Busy for `seconds` inside ONE C call (sum over a range): the GIL is not released meanwhile."""
    sum(range(int(seconds * _gil_items_per_s())))


TX_TIME, TX_SOB, TX_EOB = pmt.intern("tx_time"), pmt.intern("tx_sob"), pmt.intern("tx_eob")


class TxPacing:
    """Shared by the TX sources (video MimoTxSource, radio feedback FeedbackRadioSource).

    - Pacing: never produce more than `lead_s` ahead of the air time of the produced samples
      (lead_s = 0: as fast as the downstream buffers accept, v0.6.0).
    - Late recovery (v0.6.2): gr-uhd's usrp_sink stamps every packet with start time + samples sent / Fs.
      After a stall longer than the lead the packets are late ("L") and the radio drops everything until an
      end-of-burst. When the next sample's air time has passed (or an async "L" arrives, force_resync), the
      source ends the burst (tx_eob on the next sample) and starts a new one (tx_sob + tx_time = device now
      + lead), and moves its AirClock to that timeline. `resync=False`: v0.6.1 behaviour (stays late).
    - Logs: buffer delay per frame (air time - build time), work() time, margin before the air time,
      late events. Simulation: optional stalls (parse_stall)."""

    def _pacing_init(self, rate, clock, lead_s, resync=True, stall=None, nports=1):
        self.rate = float(rate)
        self.clock = clock
        self.lead_s = float(lead_s or 0.0)
        self.resync = bool(resync)
        self.nports = nports
        self.buffer_lat = deque(maxlen=2000)      # s, per frame: built -> on air
        self.buffer_lat_tick = []                 # since the last tick (history)
        self.late = 0                             # frames built after their air time
        self.late_events = 0                      # timeline found late (resync or not)
        self.force_resync = False                 # set by an async "L" (RF)
        self.work_s = deque(maxlen=20000)         # time inside work() without the pacing sleep
        self.margin_s = deque(maxlen=20000)       # air time of the next sample - now, at each work()
        self.work_tick = []
        self.stall = parse_stall(stall) if isinstance(stall, str) else stall
        self.stalls = 0
        self._next_stall = None
        self._late_state = False
        self._rng = np.random.default_rng(11)
        self.prod_log = deque(maxlen=100000)      # (index after the chunk, host time) of every work(): when the
                                                  # samples left the source (TxDeviceSim: arrival at the device)
        self.prod_hist = deque(maxlen=4096)       # same, kept for the source's own late-frame search
        self.late_from_dev = None                 # first late sample index reported by the device (sim)
        self.resent_late = 0                      # packets sent again after a late event

    def late_hint(self, j=None):
        """The device reported a late packet (UHD async "L"; TxDeviceSim gives the sample index)."""
        if j is not None:
            self.late_from_dev = j if self.late_from_dev is None else min(self.late_from_dev, j)
        self.force_resync = True

    def _first_late(self, now):
        """First sample index the device dropped (old timeline): from the device, else the oldest chunk of
        the recent run of chunks that left the source after their air time, else (forced, no clue) the first
        sample not yet on air. The device drops everything from there to the end of the burst."""
        cands = [self.late_from_dev] if self.late_from_dev is not None else []
        hist = list(self.prod_hist)
        first = None
        for i in range(len(hist) - 1, 0, -1):
            start, (end, t) = hist[i - 1][0], hist[i]
            if t > self.clock.time_of(start):
                first = start
            else:
                break
        if first is not None:
            cands.append(first)
        if not cands and self.force_resync:
            cands.append(self.clock.anchor_k + int((self.clock.device_time(now) - self.clock.anchor_dev) * self.rate))
        self.late_from_dev = None
        return min(cands) if cands else None

    def _resend_from(self, j):
        """Packets of frames from sample j on were dropped: send them again (MimoTxSource)."""

    def _resync_now(self, now):
        """End the late burst and start a new one `lead_s` from now (tags on the next two samples)."""
        j = self._first_late(now)
        if j is not None:
            self._resend_from(j)
        k = self.produced
        dev_t = self.clock.reanchor(k + 1, now + self.lead_s)
        secs = int(dev_t)
        when = pmt.make_tuple(pmt.from_uint64(secs), pmt.from_double(dev_t - secs))
        for port in range(self.nports):
            self.add_item_tag(port, k, TX_EOB, pmt.PMT_T)
            self.add_item_tag(port, k + 1, TX_SOB, pmt.PMT_T)
            self.add_item_tag(port, k + 1, TX_TIME, when)

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
                margin = self.clock.time_of(self.produced) - now
                if margin < 0.0005 or self.force_resync:                    # behind the air timeline
                    if not self._late_state or self.force_resync:
                        with self.lock:
                            self.late_events += 1
                    self._late_state = True
                    if not self.resync:
                        self.force_resync = False           # v0.6.1: noted, nothing done
                    if self.resync:
                        self.force_resync = False
                        self._resync_now(now)
                        self._late_state = False
                        margin = self.clock.time_of(self.produced) - now
                else:
                    self._late_state = False
                budget = (self.lead_s - margin) * self.rate
            # at least ~5 ms per call (or what fits): tiny chunks would make the thread spin
            need = min(n, max(multiple, int(0.005 * self.rate) // multiple * multiple))
            if budget >= need:
                if self.clock is not None and self.clock.t0 is not None:
                    self.margin_s.append(self.clock.time_of(self.produced) - time.monotonic())
                # at most ~1/4 lead per call: a chunk reaches the device only when work() returns, a big one
                # would make its first samples late
                cap = max(need, int(0.25 * self.lead_s * self.rate) // multiple * multiple)
                return min(n, cap, int(budget) // multiple * multiple)
            # sleep until ~5 ms of samples may be produced (fewer wake-ups: GIL, see TxDeviceSim)
            time.sleep(max(0.004, (multiple - budget) / self.rate))

    def _maybe_stall(self):
        if self.stall is None:
            return
        rate, lo, hi, gil = self.stall
        now = time.monotonic()
        if self._next_stall is None:
            self._next_stall = now + self._rng.exponential(1.0 / rate)
        if now >= self._next_stall:
            d = float(self._rng.uniform(lo, hi))
            hold_gil(d) if gil else time.sleep(d)
            self.stalls += 1
            self._next_stall = time.monotonic() + self._rng.exponential(1.0 / rate)

    def _work_done(self, t_start):
        now = time.monotonic()
        d = now - t_start
        self.prod_log.append((self.produced, now))
        self.prod_hist.append((self.produced, now))
        with self.lock:
            self.work_s.append(d)
            self.work_tick.append(d)

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

    def work_tick_stats(self):
        """work() time since the previous call: p99 (ms), or None."""
        with self.lock:
            v, self.work_tick = sorted(self.work_tick), []
        return None if not v else round(1e3 * v[min(len(v) - 1, int(0.99 * len(v)))], 1)

    def latency_report(self):
        with self.lock:
            v, w, m = list(self.buffer_lat), sorted(self.work_s), sorted(self.margin_s)
        pct = lambda a, q: None if not a else round(1e3 * a[min(len(a) - 1, int(q * len(a)))], 1)
        return {"buffer": _stats_ms(v), "lead_ms": round(1e3 * self.lead_s, 1), "late_frames": self.late,
                "late_events": self.late_events, "reanchors": self.clock.reanchors if self.clock else 0,
                "resync": self.resync, "work_ms": {"p50": pct(w, 0.5), "p99": pct(w, 0.99), "max": pct(w, 1.0)},
                "margin_ms": {"p1": pct(m, 0.01), "min": pct(m, 0.0)}, "sim_stalls": self.stalls}


class TxDeviceSim(gr.basic_block):
    """Simulation of the USRP TX side with gr-uhd/UHD timing rules.

    Every input sample has an air time: timed start (sample 0 at device time 0), then contiguous; a tx_time tag
    starts a new timeline. The output is the air signal, one sample per sample period of real time (paced):
    the input sample whose air time is that slot, or 0 when nothing is scheduled (idle gap between bursts).
    A sample that reached the device after its air time is late: "L" is printed, on_late(index) is called
    (the async message of UHD), and, like the radio, everything is dropped until a tx_eob tag. Arrival at the
    device = when the source's work() returned (source.prod_log): the real usrp_sink is C++ and does not wait
    for the GIL, this Python block does, so its own delays must not count. The GNU Radio buffer in front of it
    (set_min_output_buffer of the source, `sim_tx_device_samples`) plays the role of the device/host buffers."""

    def __init__(self, nchan, rate, clock, print_late=True, on_late=None, source=None):
        gr.basic_block.__init__(self, "TX device (sim)", [np.complex64] * nchan, [np.complex64] * nchan)
        self.rate, self.clock = float(rate), clock
        self.on_late, self.source, self.print_late = on_late, source, print_late
        self.GRAIN = 0.01
        self.anchor = (0, 0.0)             # (input index, device time) of the current burst; None: wait for a time
        self.dropping = False
        self.out_n = 0                     # output samples = air slots since device time 0
        self.late_events = self.dropped = self.idle = 0
        self.set_tag_propagation_policy(gr.TPP_DONT)

    def forecast(self, noutput_items, ninputs):
        return [0] * ninputs               # zeros can be emitted without input (idle air)

    def _arrival(self, j, now):
        """(time sample j left the source, first index after that chunk); without a source: now."""
        if self.source is None:                    # unknown: only the slot order can make a sample late
            return float("-inf"), 1 << 62
        log = self.source.prod_log
        if not log:
            return now, 1 << 62
        while len(log) > 1 and log[0][0] <= j:
            log.popleft()
        end, t = log[0]
        return (t, end) if end > j else (now, 1 << 62)

    def _tags(self, n):
        k0, tags = self.nitems_read(0), {}
        for t in self.get_tags_in_window(0, 0, n):
            d = tags.setdefault(t.offset, {})
            key = pmt.symbol_to_string(t.key)
            if key == "tx_time":
                d["time"] = pmt.to_uint64(pmt.tuple_ref(t.value, 0)) + pmt.to_double(pmt.tuple_ref(t.value, 1))
            elif key in ("tx_sob", "tx_eob"):
                d[key] = True
        return k0, tags

    def general_work(self, input_items, output_items):
        nin, nout = len(input_items[0]), len(output_items[0])
        now = time.monotonic()
        if self.clock.dev0 is None:
            if nin == 0:                           # the air clock starts with the first sample
                return 0
            self.clock.start(now, 0.0)
        # air slots that may be emitted now (paced, up to one grain ahead); sleep if ahead
        while True:
            budget = int((now - self.clock.dev0 + self.GRAIN) * self.rate) - self.out_n
            if budget >= int(0.5 * self.GRAIN * self.rate):
                break
            time.sleep(max(0.004, (0.5 * self.GRAIN * self.rate - budget) / self.rate))
            now = time.monotonic()
        budget = min(budget, nout)
        k0, tags = self._tags(nin)
        cuts = sorted(o - k0 for o in tags)
        i = o = 0                                  # consumed inputs, produced outputs
        while o < budget:
            if i >= nin:                            # nothing queued: idle air (zeros) up to now
                if self.anchor is None or self.dropping:
                    m = budget - o
                else:
                    break                           # the next sample may still arrive in time
                for out in output_items:
                    out[o:o + m] = 0
                o += m
                self.idle += m
                break
            j = k0 + i
            tg = tags.get(j)
            if tg and "time" in tg and not self.dropping:
                self.anchor = (j, tg["time"])
            if self.dropping:                       # radio after a late packet: drop until an EOB
                e = next((c for c in cuts if c >= i and tags[k0 + c].get("tx_eob")), None)
                stop = nin if e is None else e + 1
                self.dropped += stop - i
                i = stop
                if e is not None:
                    self.dropping, self.anchor = False, None
                continue
            if self.anchor is None:                 # no time spec: "send now" at the next free slot
                self.anchor = (j, self.out_n / self.rate)
            slot = int(round((self.anchor[1] + (j - self.anchor[0]) / self.rate) * self.rate))
            arrival, arr_end = self._arrival(j, now)
            if slot < self.out_n + o or arrival > self.clock.dev0 + slot / self.rate + 0.0005:   # late
                self.late_events += 1
                self.dropping = True
                if self.on_late is not None:
                    self.on_late(j)
                if self.print_late:
                    sys.stderr.write("L")
                    sys.stderr.flush()
                continue
            gap = slot - (self.out_n + o)
            if gap > 0:                             # idle air before this sample's slot
                m = min(gap, budget - o)
                for out in output_items:
                    out[o:o + m] = 0
                o += m
                self.idle += m
                continue
            seg_end = min(next((c for c in cuts if c > i), nin), arr_end - k0)
            m = min(seg_end - i, budget - o)
            for out, x in zip(output_items, input_items):
                out[o:o + m] = x[i:i + m]
            o += m
            i += m
            if tg and tg.get("tx_eob") and m:       # end of a good burst: the next one needs a time
                self.anchor = None
        self.consume_each(i)
        self.out_n += o
        return o


class UhdAsyncMonitor(gr.basic_block):
    """RF: counts the async messages of a gr-uhd usrp_sink ("async_msgs" port): late packets (UHD prints
    "L"), underflows ("U"), sequence errors. A late packet calls on_late (TxPacing.force_resync)."""
    CODES = {0x1: "burst_ack", 0x2: "underflow", 0x4: "seq_error", 0x8: "late", 0x10: "underflow_in_packet",
             0x20: "seq_error_in_burst", 0x40: "user_payload"}

    def __init__(self, on_late=None):
        gr.basic_block.__init__(self, "UHD async monitor", [], [])
        self.on_late = on_late
        self.counts = {}
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self._handle)

    def _handle(self, msg):
        try:
            code = None
            if pmt.is_dict(msg):
                v = pmt.dict_ref(msg, pmt.intern("event_code"), pmt.PMT_NIL)
                code = None if pmt.is_null(v) else int(pmt.to_python(v))
            elif pmt.is_pair(msg):
                code = int(pmt.to_python(pmt.dict_ref(pmt.cdr(msg), pmt.intern("event_code"), pmt.from_long(0))))
        except Exception:          # noqa: BLE001
            code = None
        name = self.CODES.get(code, "other")
        self.counts[name] = self.counts.get(name, 0) + 1
        if name == "late" and self.on_late is not None:
            self.on_late()


class MimoTxSource(TxPacing, gr.sync_block):
    """Application packets -> continuous 2-channel baseband (IDLE frames when empty)."""

    def __init__(self, settings, session, throttle=False, acm=None, clock=None, lead_s=0.0, resync=True, stall=None):
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
        self._pacing_init(s.sample_rate, clock, lead_s, resync, stall, nports=2)
        self.sent_frames = deque(maxlen=400)      # (first sample, end sample, packets) of recent frames
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
        t_work = time.monotonic()
        self._maybe_stall()
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
                        raw = self.resend.popleft() if self.resend else self.packets.get_nowait()
                        self.pending = self.mod.frame(raw)
                    except queue.Empty:
                        raw = self.idle
                        self.pending = self.idle_frame
                    if raw[5] != IDLE and self.lead_s > 0:
                        k = self.produced + written
                        self.sent_frames.append((k, k + self.pending.shape[1], [raw]))
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
        self._work_done(t_work)
        return n

    def _resend_from(self, j):
        again = []
        while self.sent_frames and self.sent_frames[-1][1] > j:
            again[:0] = self.sent_frames.pop()[2]
        if again:
            self.resend.extendleft(reversed(again))
            with self.lock:
                self.resent_late += len(again)

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
        if batch and self.lead_s > 0:
            fl = self.mod.frame_len
            self.sent_frames.append((k, k + fl, list(batch)))
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
            out["tx_late_events"], out["tx_resent_late"] = self.late_events, self.resent_late
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
                                 ldpc_iter=int(getattr(s, "ldpc_iter", 20)),
                                 interf_mitigation=bool(getattr(s, "rx_interf_mitigation", True)),
                                 ltf_confirm=0.25 if bool(getattr(s, "rx_interf_mitigation", True)) else None,
                                 gap=int(s.frame_gap_samples))
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
        self.work_tick = []                    # time inside work() (CPU / GIL budget of this receiver)
        self.rx_time_tags = 0
        self.h_db = None
        self.work_all = deque(maxlen=20000)

    # -- buffer ---------------------------------------------------------
    def _drop(self, count):
        count = min(count, self.n)
        self.buf[:, :self.n - count] = self.buf[:, count:self.n]
        self.n -= count
        self.dropped += count
        if self.expected is not None:
            self.expected -= count

    def work(self, input_items, output_items):
        t_work = time.monotonic()
        a, b = input_items
        m = len(a)
        if m:                                  # gr-uhd tags rx_time at the start and after every overflow (O / D)
            for t in self.get_tags_in_window(0, 0, m):
                if pmt.symbol_to_string(t.key) == "rx_time":
                    self.rx_time_tags += 1
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
        d = time.monotonic() - t_work
        self.work_tick.append(d)
        self.work_all.append(d)
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
                # window was complete: sync lost. Skip past this position, otherwise _acquire may find the
                # same frame again (its fine-timed start outside the tracking window) and loop for ever
                self._drop(min(self.n, max(0, self.expected) + SYM))
                self.expected = None
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
                    self.h_db = info.get("h_db", self.h_db)
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
        w, self.work_tick = self.work_tick, []
        return {"proc": _stats_ms(p), "ingest": _stats_ms(i), "work": _stats_ms(w),
                "work_s": round(sum(w), 3)}

    def latency_report(self):
        with self.lock:
            return {"proc": _stats_ms(self.proc_lat), "ingest": _stats_ms(self.ingest_lat),
                    "work": _stats_ms(list(self.work_all))}

    def snapshot(self):
        with self.lock:
            return dict(self.stats, fec_mismatch=int(self.demod.fec_mismatch),
                        false_detections=int(self.demod.false_detections),
                        noise=self.demod.noise_summary(), alt_sig=dict(self.demod.alt_sig), h_db=self.h_db,
                        uhd_rx_time_tags=self.rx_time_tags, uhd_overflows=max(0, self.rx_time_tags - 1), snr_db=self.snr, evm_db=self.evm, cond_db=self.cond,
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

    def __init__(self, settings, provider, throttle=False, clock=None, lead_s=0.0, resync=True, stall=None):
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
        self._pacing_init(settings.fb_rate, clock, lead_s, resync, stall)   # a report waits this long
        self.set_output_multiple(SYM)

    def work(self, input_items, output_items):
        out = output_items[0]
        n = self._allowed(len(out), SYM)
        t_work = time.monotonic()
        self._maybe_stall()
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
        self._work_done(t_work)
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
        cond = getattr(s, "sim_cond_db", None)
        if cond is not None:                          # 2x2 channel with a given condition number (smux tests)
            def unitary():
                q, _ = np.linalg.qr(rng.standard_normal((2, 2)) + 1j * rng.standard_normal((2, 2)))
                return q
            sv = np.array([1.0, 10 ** (-cond / 20)])
            self.H = unitary() @ np.diag(sv * np.sqrt(2 / np.sum(sv ** 2))) @ unitary()
            self.H2 = 0.05 * self.H
        self.H = self.H.astype(np.complex64)
        self.H2 = self.H2.astype(np.complex64)
        self.interf = parse_interference(getattr(s, "sim_interf", ""), s.sample_rate, self.tx_rms ** 2)
        self.irng = np.random.default_rng(s.sim_seed + 100)

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
        for src in self.interf:
            v = _interference_samples(src, self.samples / self.rate, n, self.rate, self.irng)
            if v is not None:
                for port in src["ports"]:
                    y[port] += v
        self.samples += n
        y += noise
        self.noise_pos = (self.noise_pos + n) % (self.noise.shape[1] - 65536)
        rot = np.exp(2j * np.pi * (self.cfo * np.arange(n)) + 1j * self.phase).astype(np.complex64)
        self.phase = (self.phase + 2 * np.pi * self.cfo * n) % (2 * np.pi)
        output_items[0][:] = y[0] * rot
        output_items[1][:] = y[1] * rot
        return n


def parse_interference(text, rate, ref_power):
    """'' -> []. Else a list of interference sources (simulation, RX ports), comma separated:
         comb:SPACING:DB:PERIOD:GAP[:DUR[:PORT]]  lines every SPACING Hz over the band, DB = total power vs the
                                                 signal, on in pairs of DUR s (default 0.4) every PERIOD s, the
                                                 second GAP s after the first (RF 26/11: comb:75e3:-15:11:3)
         tone:FREQ:DB[:PORT]                     one continuous line at FREQ Hz from the centre
         burst:DB:DUR:PERIOD[:PORT]              wideband noise bursts of DUR s every PERIOD s (impulses)
       PORT: 0 (default), 1 or both."""
    out = []
    rng = np.random.default_rng(3)
    for spec in [t for t in str(text or "").split(",") if t.strip()]:
        kind, *p = spec.strip().split(":")
        port = lambda v: {"0": [0], "1": [1], "both": [0, 1]}[v]
        if kind == "comb":
            sp, db, per, gap = map(float, p[:4])
            dur = float(p[4]) if len(p) > 4 else 0.4
            ports = port(p[5]) if len(p) > 5 else [0]
            m = np.arange(-int(rate / 2 / sp), int(rate / 2 / sp) + 1)
            f = (m + 0.37) * sp
            f = f[np.abs(f) < 0.5 * rate]
            out.append({"kind": kind, "f": f / rate, "amp": np.sqrt(ref_power * 10 ** (db / 10) / len(f)),
                        "phase": rng.uniform(0, 2 * np.pi, len(f)), "period": per, "gap": gap, "dur": dur,
                        "ports": ports})
        elif kind == "tone":
            out.append({"kind": kind, "f": np.array([float(p[0]) / rate]), "amp": np.sqrt(ref_power * 10 ** (float(p[1]) / 10)),
                        "phase": np.zeros(1), "period": None, "ports": port(p[2]) if len(p) > 2 else [0]})
        elif kind == "burst":
            db, dur, per = map(float, p[:3])
            out.append({"kind": kind, "sigma": np.sqrt(ref_power * 10 ** (db / 10) / 2), "dur": dur, "period": per,
                        "gap": None, "ports": port(p[3]) if len(p) > 3 else [0]})
        else:
            raise ValueError("sim_interf: comb:SPACING:DB:PERIOD:GAP[:DUR[:PORT]] | tone:FREQ:DB[:PORT] | "
                             "burst:DB:DUR:PERIOD[:PORT]")
    return out


def _interference_samples(src, t0, n, rate, rng):
    """Samples of one interference source for the n samples starting at time t0 (s), or None when off."""
    t = t0 + np.arange(n) / rate
    if src["period"] is not None:
        ph = np.mod(t, src["period"])
        on = (ph < src["dur"]) | ((src["gap"] is not None) & (ph >= (src["gap"] or 0)) & (ph < (src["gap"] or 0) + src["dur"]))
        if not on.any():
            return None
    else:
        on = None
    if src["kind"] == "burst":
        v = src["sigma"] * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    else:
        k = np.round(t * rate).astype(np.int64)
        v = src["amp"] * np.exp(1j * (2 * np.pi * np.outer(k, src["f"]) % (2 * np.pi) + src["phase"])).sum(axis=1)
    return (v * on if on is not None else v).astype(np.complex64)


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
