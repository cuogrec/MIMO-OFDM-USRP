"""GNU Radio blocks: 2-channel MIMO OFDM source/sink, 2x2 channel simulator,
video PDU receiver. All PHY maths lives in modem.py (numpy)."""
import logging
import queue
import socket
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

from .config import PACKET_BYTES
from .modem import Modulator, Demodulator, SYM, N_CP
from .protocol import DATA, IDLE, pack_packet, unpack_packet

LOG = logging.getLogger(__name__)


class MimoTxSource(gr.sync_block):
    """Application packets -> continuous 2-channel baseband (IDLE frames when empty)."""

    def __init__(self, settings, session, throttle=False):
        gr.sync_block.__init__(self, "MIMO OFDM TX source", [], [np.complex64, np.complex64])
        s = settings
        self.mod = Modulator(s.mimo_mode, s.modulation, s.frame_gap_samples, s.tx_rms, s.interleaver)
        self.packets = queue.Queue(maxsize=s.queue_packets)
        self.idle = pack_packet(IDLE, session)
        self.idle_frame = self.mod.frame(self.idle)   # cached: IDLE costs no CPU
        self.pending = np.zeros((2, 0), np.complex64)
        self.pos = 0
        self.throttle = throttle
        self.rate = s.sample_rate
        self.t0 = None
        self.produced = 0
        self.lock = threading.Lock()
        self.frames = self.data_frames = self.enqueued_data = 0
        self.set_output_multiple(SYM)
        self.set_min_noutput_items(8 * SYM)

    def enqueue(self, raw, stop_event):
        while not stop_event.is_set():
            try:
                self.packets.put(raw, timeout=0.1)
                if raw[5] == DATA:
                    with self.lock:
                        self.enqueued_data += 1
                return True
            except queue.Full:
                continue
        return False

    def work(self, input_items, output_items):
        out0, out1 = output_items
        n = len(out0)
        if self.throttle:
            if self.t0 is None:
                self.t0 = time.monotonic()
            ahead = self.produced / self.rate - (time.monotonic() - self.t0)
            if ahead > 0.02:
                time.sleep(min(ahead - 0.01, 0.05))
        written = 0
        while written < n:
            if self.pos >= self.pending.shape[1]:
                try:
                    raw = self.packets.get_nowait()
                    self.pending = self.mod.frame(raw)
                except queue.Empty:
                    raw = self.idle
                    self.pending = self.idle_frame
                self.pos = 0
                with self.lock:
                    self.frames += 1
                    self.data_frames += raw[5] == DATA
            take = min(n - written, self.pending.shape[1] - self.pos)
            out0[written:written + take] = self.pending[0, self.pos:self.pos + take]
            out1[written:written + take] = self.pending[1, self.pos:self.pos + take]
            written += take
            self.pos += take
        self.produced += n
        return n

    def snapshot(self):
        with self.lock:
            return {"tx_frames": self.frames, "tx_data_frames": self.data_frames,
                    "tx_enqueued_data": self.enqueued_data, "tx_queue": self.packets.qsize(),
                    "frame_samples": self.mod.frame_len}


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
        self.demod = Demodulator(s.mimo_mode, s.modulation, s.sync_threshold, soft=s.soft_decoding)
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
                          corrected_bytes=0, lost_sync=0, soft_decoded=0)
        self.interleaver = None
        self.snr = self.evm = self.cond = None
        self.rx_rms = 0.0

    # -- buffer ---------------------------------------------------------
    def _drop(self, count):
        count = min(count, self.n)
        self.buf[:, :self.n - count] = self.buf[:, count:self.n]
        self.n -= count
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
        if m:
            self.rx_rms = float(np.sqrt(np.mean(np.abs(a[-4096:]) ** 2)))
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
            s0, p = self.demod.find(self.buf[:, lo:lo + 2 * SYM + N_CP])
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
        for s0, (packet, info) in zip(starts, results):
            reason = info["reason"]
            with self.lock:
                self.stats["detected"] += 1
                self.stats[{"ok": "ok", "sig": "sig_fail", "rs": "rs_fail", "crc": "crc_fail"}[reason]] += 1
                self.stats["corrected_bytes"] += info.get("corrected", 0)
                self.stats["soft_decoded"] += bool(info.get("soft_erasures"))
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
            if packet is not None:
                self.message_port_pub(self.port, pmt.cons(
                    pmt.PMT_NIL, pmt.init_u8vector(len(packet), list(packet))))
        nxt = starts[-1] + self.frame_len
        self._drop(nxt - SYM)
        self.expected = SYM
        return True

    def snapshot(self):
        with self.lock:
            return dict(self.stats, snr_db=self.snr, evm_db=self.evm, cond_db=self.cond,
                        rx_rms=self.rx_rms, tracking=self.expected is not None,
                        interleaver=self.interleaver, soft_skipped=self.demod.soft_skipped)


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
        y += self.noise[:, self.noise_pos:self.noise_pos + n]
        self.noise_pos = (self.noise_pos + n) % (self.noise.shape[1] - 65536)
        rot = np.exp(2j * np.pi * (self.cfo * np.arange(n)) + 1j * self.phase).astype(np.complex64)
        self.phase = (self.phase + 2 * np.pi * self.cfo * n) % (2 * np.pi)
        output_items[0][:] = y[0] * rot
        output_items[1][:] = y[1] * rot
        return n


class VideoReceiver(gr.basic_block):
    """Post-CRC packets -> verified RS8+8 reassembly -> TS file + UDP (as in N210 v1.23)."""

    def __init__(self, session, path, udp_port):
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
        self.message_port_register_in(pmt.intern("pdus"))
        self.set_msg_handler(pmt.intern("pdus"), self.handle)

    @staticmethod
    def _new_state(session):
        from .verified_fec import RSParityCheckedState
        return RSParityCheckedState(session)

    def handle(self, msg):
        try:
            raw = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
            with self.lock:
                if self.closed:
                    return
                self.pdus += 1
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
        except Exception:      # noqa: BLE001
            self.errors += 1
            LOG.exception("PDU handler failed")

    def snapshot(self):
        with self.lock:
            if self.state is None:
                snap = {"data_packets": 0, "missing_data_packets": 0, "end_received": False,
                        "final_packet_count": None}
            else:
                snap = self.state.snapshot()
            snap.update(post_crc_pdus=self.pdus, handler_errors=self.errors, session=self.session)
            return snap

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.file.close()
                self.sock.close()


class SfoResampler(gr.basic_block):
    """Simulate a sample-clock offset between two separate radios (linear interpolation).

    Output sample k is taken at input time k * (1 + ppm*1e-6). Simulation only.
    """

    def __init__(self, ppm):
        gr.basic_block.__init__(self, "SFO resampler", [np.complex64, np.complex64],
                                [np.complex64, np.complex64])
        self.step = 1.0 + ppm * 1e-6
        self.t = 0.0                       # read position relative to the first unread input

    def forecast(self, noutput_items, ninputs):
        need = int(noutput_items * self.step) + 2
        return [need] * ninputs

    def general_work(self, input_items, output_items):
        n_in = min(len(input_items[0]), len(input_items[1]))
        n_out = min(len(output_items[0]), int((n_in - 2 - self.t) / self.step))
        if n_out <= 0:
            return 0
        pos = self.t + self.step * np.arange(n_out)
        i0 = pos.astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        for ch in (0, 1):
            x = input_items[ch]
            output_items[ch][:n_out] = x[i0] * (1 - frac) + x[i0 + 1] * frac
        end = self.t + self.step * n_out
        used = int(end)
        self.t = end - used
        self.consume_each(used)
        return n_out
