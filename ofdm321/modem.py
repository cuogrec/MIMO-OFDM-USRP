"""2x2 MIMO OFDM modem (numpy, frame based).

Frame (per TX antenna, 80-sample symbols = 16 CP + 64 FFT):
    STF   1 sym  Schmidl-Cox (even carriers only -> period 32). TX1 sends a
                 cyclic-shifted copy (CSD) so the two antennas never cancel.
    LTF   2 sym  P-matrix training: TX0 [L, L], TX1 [L, -L]  ->  H (nrx x 2) per carrier
    SIG   1 sym  48 BPSK bits: magic, mode, modulation, frame seq, CRC16
    DATA  Np sym payload (one 248-byte inner-RS codeword), 4 pilots per symbol
    gap   frame_gap_samples zeros

MIMO schemes:
    siso      1 TX, RX0 only
    simo      1 TX, 2 RX, maximum-ratio combining
    alamouti  2 TX, SFBC over carrier pairs (rate 1, diversity 2x2)
    smux      2 TX, 2 independent streams, per-carrier MMSE (rate 2)
Pilots are known on both antennas; the receiver predicts them through the
estimated H and removes the common phase error of every symbol.

Interleaving decides which carriers/OFDM symbols one RS byte occupies:
    none     byte = adjacent carriers of ONE OFDM symbol (weak against a faded
             or jammed carrier: it damages a byte in every OFDM symbol)
    block    byte = 2 carriers x 2 OFDM symbols (1-stream QPSK; default). A dead
             carrier costs ~11 bytes, a dead OFDM symbol ~24: both within the
             16 errors / 32 erasures of RS(248,216). Other modes already have
             this balance and keep the plain order.
    carrier  byte = ONE carrier over consecutive OFDM symbols (best against
             frequency-selective fading, weak against impulse noise)
The SIG field carries the interleaver id; the receiver follows it per frame.

ACM (adaptive coding and modulation, --acm): every frame has the same length
(NP_ACM = 42 data symbols) and carries as many 248-byte codewords as its
modulation allows: 1-stream BPSK 1, QPSK 2, 16QAM 4, 64QAM 6 (smux twice as
many). The modulation is chosen per frame by the transmitter (from the SNR the
receiver reports back) and written in SIG (byte 1: bit 7 = ACM, bits 4..6 mode,
bits 0..1 modulation). Fixed frame length keeps the receiver timing unchanged;
the receiver demaps each frame with the modulation its SIG announces.

Soft decoding: every equalised symbol gets a reliability (distance to the
decision boundary x post-equaliser SNR of its carrier/stream, reduced when the
null carriers of that OFDM symbol show extra noise = impulse). When ordinary
RS decoding fails, the least reliable bytes are erased step by step
(errors-and-erasures, up to 32 erasures); a candidate is accepted only when
all RS syndromes and the CRC32 agree.
"""
from functools import lru_cache
import struct
import time
import zlib

import numpy as np

from . import inner_rs
from . import ldpc
from . import soft_inner_rs

N_FFT, N_CP = 64, 16
SYM = N_FFT + N_CP
PILOT_K = np.array([-21, -7, 7, 21])
DATA_K = np.array([k for k in range(-26, 27) if k not in (0, -21, -7, 7, 21)])
ACTIVE_K = np.array([k for k in range(-26, 27) if k != 0])
NULL_K = np.array([k for k in range(-32, 32) if abs(k) > 27])     # noise reference
DATA_BINS, PILOT_BINS = DATA_K % N_FFT, PILOT_K % N_FFT
ACTIVE_BINS, NULL_BINS = ACTIVE_K % N_FFT, NULL_K % N_FFT
N_DATA = len(DATA_K)                       # 48
CODE_BYTES = inner_rs.CODE_BYTES           # 248
MESSAGE_BYTES = inner_rs.MESSAGE_BYTES     # 216 = 212 app + CRC32
CSD_SAMPLES = 4                            # cyclic shift of TX1 STF
SIG_MAGIC = 0xA5
MODE_CODES = {"siso": 0, "simo": 1, "alamouti": 2, "smux": 3}
MOD_CODES = {"qpsk": 0, "16qam": 1, "bpsk": 2, "64qam": 3}
MOD_NAMES = {v: k for k, v in MOD_CODES.items()}
BPS = {"bpsk": 1, "qpsk": 2, "16qam": 4, "64qam": 6}
ACM_FLAG = 0x80
NP_ACM = 42                                   # data symbols of every ACM frame
INTERLEAVERS = ("none", "carrier", "block")   # id = index, sent in SIG bits 2..3
INTERLEAVE_MASK = 0xC
SOFT_ERASURES = (2, 4, 6, 8, 12, 16, 20, 24, 28, 32)
POWER_NORM = np.sqrt(len(ACTIVE_K) / N_FFT)   # ortho IFFT of 52 unit carriers
# inner FEC (v0.6): RS(248,216) or 802.11n LDPC; SIG byte 1 bit 6 = LDPC (v0.5 receivers reject such frames)
FECS = ("rs", "ldpc")
FEC_FLAG = 0x40
LDPC_RATES = ldpc.RATES
DEFAULT_LDPC_RATE = "3/4"
ACM_PACKET_BITS = NP_ACM * N_DATA       # 2016 bits per packet in every ACM frame (BPSK 1 ... 64QAM 6 packets)
ACM_LDPC_RATE = "5/6"                    # ACM + LDPC: rate-5/6 code, shortened + punctured to 2016 bits


def _bpsk(seed, n):
    return np.random.RandomState(seed).randint(0, 2, n) * 2.0 - 1.0


@lru_cache(maxsize=1)
def tables():
    stf = np.zeros(N_FFT, complex)
    even = np.array([k for k in ACTIVE_K if k % 2 == 0])
    stf[even % N_FFT] = np.sqrt(2) * _bpsk(42, len(even))
    ltf = np.zeros(N_FFT, complex)
    ltf[ACTIVE_BINS] = _bpsk(43, len(ACTIVE_K))
    polarity = _bpsk(44, 128)
    pilot = np.array([1, 1, 1, -1], float)
    pilot_tx1 = np.array([1, -1, -1, 1], float)
    scramble = np.random.RandomState(0x5EED).randint(0, 256, CODE_BYTES).astype(np.uint8)
    return dict(stf=stf, ltf=ltf, polarity=polarity, pilot=pilot, pilot_tx1=pilot_tx1,
                scramble=scramble)


# ----------------------------------------------------------------- mapping
def _levels(bits_per_axis):
    if bits_per_axis == 1:
        return np.array([1.0, -1.0])                                # b=0 -> +1
    if bits_per_axis == 2:
        return np.array([-3.0, -1.0, 3.0, 1.0]) / np.sqrt(5)        # gray: 00,01,10,11
    return _pam_gray(bits_per_axis)[0]


@lru_cache(maxsize=None)
def _pam_gray(m):
    """Gray PAM with 2^m levels: level[bits value] and the sorted amplitudes,
    normalised so that the complex constellation (I and Q) has unit power."""
    M = 1 << m
    amp = (2 * np.arange(M) - (M - 1)) / np.sqrt((M * M - 1) / 3)   # per-axis power 1
    level = np.empty(M)
    gray = np.arange(M) ^ (np.arange(M) >> 1)
    level[gray] = amp
    bits = ((gray[:, None] >> np.arange(m)[::-1]) & 1).astype(np.uint8)   # bits of sorted amp
    return level, amp, bits


def modulate(bits, bps):
    if bps == 1:
        return (1.0 - 2.0 * bits.astype(float)).astype(complex)
    half = bps // 2
    lv = _levels(half)
    b = bits.reshape(-1, bps)
    idx_i = b[:, :half] @ (1 << np.arange(half)[::-1])
    idx_q = b[:, half:] @ (1 << np.arange(half)[::-1])
    return (lv[idx_i] + 1j * lv[idx_q]) / np.sqrt(2)


def demodulate(symbols, bps):
    """Hard decision, inverse of modulate() (gray, per axis)."""
    re, im = symbols.real, symbols.imag
    if bps == 1:
        return (re < 0).astype(np.uint8)
    if bps == 2:
        return np.stack([re < 0, im < 0], axis=1).reshape(-1).astype(np.uint8)
    if bps == 4:
        t = 2 / np.sqrt(10)
        return np.stack([re > 0, np.abs(re) < t, im > 0, np.abs(im) < t],
                        axis=1).reshape(-1).astype(np.uint8)
    m = bps // 2
    _, amp, bits = _pam_gray(m)
    scale = np.sqrt(2)
    def axis(v):
        idx = np.clip(np.round((v * scale - amp[0]) / (amp[1] - amp[0])), 0, len(amp) - 1).astype(int)
        return bits[idx]
    return np.concatenate([axis(re), axis(im)], axis=1).reshape(-1)


def crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def acm_sig_info(byte1, seq):
    """ACM SIG: 24 info bits (mode/modulation byte, seq low byte, CRC8), sent twice on
    carriers 24 apart -> soft combining gives ~3 dB and frequency diversity, so the
    header is decodable wherever BPSK payload is."""
    head = bytes([byte1, seq & 0xFF])
    return np.unpackbits(np.frombuffer(head + bytes([crc8(head)]), np.uint8))


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


# ----------------------------------------------------------------- geometry
@lru_cache(maxsize=None)
def ldpc_coder(rate, acm=False):
    """PacketCoder of one 212-byte packet: fixed frames use the natural layout of `rate`; ACM frames
    give every packet exactly ACM_PACKET_BITS (rate-5/6 code, punctured)."""
    return ldpc.PacketCoder(ACM_LDPC_RATE, ACM_PACKET_BITS) if acm else ldpc.PacketCoder(rate)


def packet_bits(fec="rs", ldpc_rate=DEFAULT_LDPC_RATE, acm=False):
    """Coded bits of one application packet."""
    if fec == "rs":
        return CODE_BYTES * 8
    if fec != "ldpc":
        raise ValueError(f"fec must be one of {FECS}")
    return ldpc_coder(ldpc_rate, acm).bits


def payload_symbols(mode, modulation, acm=False, fec="rs", ldpc_rate=DEFAULT_LDPC_RATE):
    if acm:
        return NP_ACM
    bps = BPS[modulation]
    streams = 2 if mode == "smux" else 1
    per_symbol = N_DATA * bps * streams
    return -(-packet_bits(fec, ldpc_rate) // per_symbol)


def frame_samples(mode, modulation, gap, acm=False, fec="rs", ldpc_rate=DEFAULT_LDPC_RATE):
    return (4 + payload_symbols(mode, modulation, acm, fec, ldpc_rate)) * SYM + gap


def codewords_per_frame(mode, modulation):
    """ACM: number of 248-byte codewords in one NP_ACM frame."""
    streams = 2 if mode == "smux" else 1
    return NP_ACM * N_DATA * BPS[modulation] * streams // (CODE_BYTES * 8)


@lru_cache(maxsize=None)
def interleave_perm(mode, modulation, interleaver):
    """perm[i] = flat payload-grid position of code symbol i, or None (plain order).

    Grid layout is (ofdm_symbol, carrier[, stream]) flattened.
    """
    n_sym = payload_symbols(mode, modulation)
    streams = 2 if mode == "smux" else 1
    grid = np.arange(n_sym * N_DATA * streams).reshape(n_sym, N_DATA, streams)
    if interleaver == "carrier":
        perm = np.transpose(grid, (1, 2, 0)).reshape(-1)
    elif interleaver == "block" and streams == 1 and modulation == "qpsk":
        pairs = n_sym // 2
        tiles = grid[:2 * pairs, :, 0].reshape(pairs, 2, N_DATA // 2, 2)   # (pair, t, cpair, c)
        perm = np.concatenate([np.transpose(tiles, (0, 2, 1, 3)).reshape(-1),
                               grid[2 * pairs:].reshape(-1)])
    else:
        return None
    perm.setflags(write=False)
    return perm


def bit_reliability(symbols, weight, bps):
    """|LLR|-like reliability of every bit, same order as demodulate()."""
    re, im = np.abs(symbols.real), np.abs(symbols.imag)
    if bps == 1:
        return re * weight
    if bps == 2:
        out = np.stack([re, im], axis=1)
    elif bps == 4:
        t = 2 / np.sqrt(10)
        out = np.stack([re, np.abs(re - t), im, np.abs(im - t)], axis=1)
    else:                                     # max-log |LLR| per bit, per axis (64QAM)
        m = bps // 2
        _, amp, bits = _pam_gray(m)
        amp = amp / np.sqrt(2)
        def axis(v):
            d = (v[:, None] - amp[None, :]) ** 2                     # (n, M)
            r = []
            for j in range(m):
                one = bits[:, j] == 1
                r.append(np.abs(d[:, one].min(axis=1) - d[:, ~one].min(axis=1)))
            return np.stack(r, axis=1)
        out = np.concatenate([axis(symbols.real), axis(symbols.imag)], axis=1)
    return (out * weight[:, None]).reshape(-1)


def bit_llr(symbols, weight, bps):
    """Max-log LLR of every bit (positive = bit 0), same bit order as modulate()/demodulate().
    weight = post-equaliser SNR of each symbol (1 / complex noise variance), as from _combine()."""
    if bps == 1:
        return 4.0 * symbols.real * weight
    half = bps // 2
    lv = _levels(half) / np.sqrt(2)                                  # level[value of the axis bits]
    val = np.arange(1 << half)
    bits = (val[:, None] >> np.arange(half)[::-1]) & 1               # (M, half), MSB first

    def axis(v):
        d = (v[:, None] - lv[None, :]) ** 2
        return np.stack([d[:, bits[:, j] == 1].min(axis=1) - d[:, bits[:, j] == 0].min(axis=1)
                         for j in range(half)], axis=1)
    out = np.concatenate([axis(symbols.real), axis(symbols.imag)], axis=1)
    return (out * weight[:, None]).reshape(-1)


@lru_cache(maxsize=None)
def bit_perm(n):
    """Bit interleaver between LDPC and the mapper: coded bit perm[i] is sent in position i."""
    perm = np.random.RandomState(0xB175).permutation(n)
    perm.setflags(write=False)
    return perm


def ldpc_info_bits(packet):
    """212-byte packet -> 1728 scrambled info bits (packet + CRC32)."""
    if len(packet) != MESSAGE_BYTES - 4:
        raise ValueError("Application packet must be 212 bytes")
    msg = np.frombuffer(packet + struct.pack(">I", zlib.crc32(packet)), np.uint8)
    return np.unpackbits(msg ^ tables()["scramble"][:MESSAGE_BYTES])


@lru_cache(maxsize=1)
def _ltf_body():
    return ofdm_time(tables()["ltf"])[N_CP:]


def ofdm_time(freq):
    """freq: (..., 64) natural bin order -> (..., 80) time samples with CP."""
    t = np.fft.ifft(freq, axis=-1) * np.sqrt(N_FFT) / POWER_NORM
    return np.concatenate([t[..., -N_CP:], t], axis=-1)


# ----------------------------------------------------------------- coding
def encode_packet(packet):
    if len(packet) != MESSAGE_BYTES - 4:
        raise ValueError("Application packet must be 212 bytes")
    word = inner_rs.encode(packet + struct.pack(">I", zlib.crc32(packet)))
    return np.frombuffer(word, np.uint8) ^ tables()["scramble"]


def _check(message):
    packet, crc = message[:-4], struct.unpack(">I", message[-4:])[0]
    return packet if zlib.crc32(packet) == crc else None


def decode_codeword(raw, reliability=None):
    """Return (packet or None, corrected_bytes, reason, soft_erasures).

    reliability: None (hard decoding only) or a callable returning 248 byte
    reliabilities (small = unreliable), evaluated only if hard decoding fails.
    """
    word = bytes(np.asarray(raw, np.uint8) ^ tables()["scramble"])
    reason, corrected = "rs", 0
    try:
        message, corrected = inner_rs.decode(word)
        packet = _check(message)
        if packet is not None:
            return packet, corrected, "ok", 0
        reason = "crc"
    except inner_rs.Uncorrectable:
        pass
    if reliability is None:
        return None, corrected, reason, 0
    confidence = np.asarray(reliability(), float)
    if confidence.shape != (CODE_BYTES,) or not np.all(np.isfinite(confidence)):
        return None, corrected, reason, 0
    order = np.argsort(confidence, kind="stable")[:inner_rs.PARITY_BYTES].tolist()
    syndrome = inner_rs.syndromes(word)
    filtered, locator, erased = syndrome.copy(), [1], []
    for position in order:
        erased.append(position)
        filtered, locator = soft_inner_rs._add_erasure(filtered, locator, position)
        if len(erased) not in SOFT_ERASURES:
            continue
        try:
            message, changed = soft_inner_rs._decode_filtered(word, erased, syndrome, filtered, locator)
        except inner_rs.Uncorrectable:
            continue
        packet = _check(message)
        if packet is not None:
            return packet, changed, "ok", len(erased)
    return None, corrected, reason, 0


# ----------------------------------------------------------------- transmitter
class Modulator:
    def __init__(self, mode, modulation, gap=80, tx_rms=0.1, interleaver="block", acm=False, fec="rs",
                 ldpc_rate=DEFAULT_LDPC_RATE):
        if interleaver not in INTERLEAVERS:
            raise ValueError(f"interleaver must be one of {INTERLEAVERS}")
        if fec not in FECS or ldpc_rate not in LDPC_RATES:
            raise ValueError(f"fec must be one of {FECS}, ldpc_rate one of {LDPC_RATES}")
        self.acm, self.fec, self.ldpc_rate = acm, fec, ldpc_rate
        self.coder = ldpc_coder(ldpc_rate, acm) if fec == "ldpc" else None
        if acm or fec == "ldpc":
            interleaver = "none"          # ACM frames: codewords in plain order; LDPC: bit interleaver
        self.mode, self.modulation = mode, modulation
        self.interleaver = interleaver
        self.perm = None if acm else interleave_perm(mode, modulation, interleaver)
        self.bps = BPS[modulation]
        self.n_tx = 2 if mode in ("alamouti", "smux") else 1
        self.np = payload_symbols(mode, modulation, acm, fec, ldpc_rate)
        self.gap = gap
        self.scale = tx_rms
        self.seq = 0
        t = tables()
        stf = ofdm_time(t["stf"])
        ltf = ofdm_time(t["ltf"])
        self.pre = np.zeros((2, 3 * SYM), complex)
        self.pre[0] = np.concatenate([stf, ltf, ltf])
        if self.n_tx == 2:
            body = np.roll(stf[N_CP:], CSD_SAMPLES)
            stf1 = np.concatenate([body[-N_CP:], body])
            self.pre[1] = np.concatenate([stf1, ltf, -ltf])
            self.pre /= np.sqrt(2)                   # same total power as 1 TX
        self.frame_len = (4 + self.np) * SYM + gap

    def _sfbc(self, s):
        x0, x1 = s.copy(), np.empty_like(s)
        x1[..., 0::2] = -np.conj(s[..., 1::2])
        x1[..., 1::2] = np.conj(s[..., 0::2])
        return x0, x1

    def _grid(self, symbols_per_tx, first_polarity):
        """symbols_per_tx: (n_tx, nsym, 48) -> (n_tx, nsym, 64) with pilots."""
        t = tables()
        n_tx, nsym, _ = symbols_per_tx.shape
        grid = np.zeros((2, nsym, N_FFT), complex)
        pol = t["polarity"][(first_polarity + np.arange(nsym)) % 128][:, None]
        grid[0][:, DATA_BINS] = symbols_per_tx[0]
        grid[0][:, PILOT_BINS] = pol * t["pilot"]
        if self.n_tx == 2:
            grid[1][:, DATA_BINS] = symbols_per_tx[1]
            grid[1][:, PILOT_BINS] = pol * t["pilot_tx1"]
            grid /= np.sqrt(2)                      # total power per carrier = 1
        return grid

    def sig_bits(self, seq, modulation=None):
        flag = INTERLEAVERS.index(self.interleaver) << 2 | (ACM_FLAG if self.acm else 0) \
            | (FEC_FLAG if self.coder else 0)
        mod = MOD_CODES[modulation or self.modulation]
        if self.acm:
            b24 = acm_sig_info(MODE_CODES[self.mode] << 4 | mod | flag, seq)
            return np.concatenate([b24, b24])
        # LDPC fixed frames: the top 2 bits of the 16-bit sequence number carry the code rate
        seq = LDPC_RATES.index(self.ldpc_rate) << 14 | seq & 0x3FFF if self.coder else seq & 0xFFFF
        head = bytes([SIG_MAGIC, MODE_CODES[self.mode] << 4 | mod | flag]) + struct.pack(">H", seq)
        data = head + struct.pack(">H", crc16(head))
        return np.unpackbits(np.frombuffer(data, np.uint8))

    def frame(self, packet, modulation=None):
        """212-byte application packet -> (2, frame_len) complex64.
        ACM: `packet` is a list of codewords_per_frame(mode, modulation) packets."""
        modulation = modulation or self.modulation
        bps = BPS[modulation]
        packets = packet if self.acm else [packet]
        if self.coder:
            bits = self.coder.encode(np.stack([ldpc_info_bits(p) for p in packets])).reshape(-1)
            bits = bits[bit_perm(bits.size)]
        else:
            bits = np.unpackbits(np.concatenate([encode_packet(p) for p in packets]))
        cap = self.np * N_DATA * bps * (2 if self.mode == "smux" else 1)
        if bits.size > cap:
            raise ValueError("too many codewords for this frame")
        # unused tail: random bits, so the constellation stays uniform
        fill = np.random.RandomState(self.seq & 0xFFFF).randint(0, 2, cap - bits.size).astype(np.uint8)
        bits = np.concatenate([bits, fill])
        syms = modulate(bits, bps)
        if self.perm is not None:
            placed = np.empty_like(syms)
            placed[self.perm] = syms
            syms = placed
        sig = (1.0 - 2.0 * self.sig_bits(self.seq, modulation)).astype(complex)[None, :]
        if self.mode == "smux":
            data = syms.reshape(self.np, N_DATA, 2)
            per_tx = np.stack([data[..., 0], data[..., 1]])
            sig_tx = np.stack(self._sfbc(sig))
        elif self.mode == "alamouti":
            data = syms.reshape(self.np, N_DATA)
            per_tx = np.stack(self._sfbc(data))
            sig_tx = np.stack(self._sfbc(sig))
        else:
            per_tx = syms.reshape(1, self.np, N_DATA)
            sig_tx = sig[None]
        grid = np.concatenate([self._grid(sig_tx, 0), self._grid(per_tx, 1)], axis=1)
        body = ofdm_time(grid).reshape(2, -1)
        out = np.zeros((2, self.frame_len), complex)
        out[:, :3 * SYM] = self.pre
        out[:, 3 * SYM:3 * SYM + body.shape[1]] = body
        if self.n_tx == 1:
            out[1] = 0
        self.seq += 1
        return (out * self.scale).astype(np.complex64)


# ----------------------------------------------------------------- receiver
class Demodulator:
    """Stateless per-frame demodulation given the frame start index."""

    def __init__(self, mode, modulation, threshold=0.35, backoff=3, soft=True, soft_cpu=0.3, acm=False,
                 fine_timing=True, sig_recover=True, fec="rs", ldpc_rate=DEFAULT_LDPC_RATE, ldpc_iter=20):
        if fec not in FECS or ldpc_rate not in LDPC_RATES:
            raise ValueError(f"fec must be one of {FECS}, ldpc_rate one of {LDPC_RATES}")
        self.mode, self.modulation = mode, modulation
        self.fec, self.ldpc_rate, self.ldpc_iter = fec, ldpc_rate, ldpc_iter
        self.coder = ldpc_coder(ldpc_rate, acm) if fec == "ldpc" else None
        self.fec_flag = FEC_FLAG if self.coder else 0
        self.fec_mismatch = 0             # frames with a valid SIG of the other inner code / LDPC rate
        self.fine_timing, self.sig_recover = fine_timing, sig_recover
        self.acm = acm
        self.soft = soft
        self.soft_cpu = soft_cpu          # max share of wall time spent in erasure trials
        self.soft_budget, self.soft_clock = 0.5, time.monotonic()
        self.soft_skipped = 0
        self.perms = [interleave_perm(mode, modulation, name) for name in INTERLEAVERS]
        self.bps = BPS[modulation]
        self.n_tx = 2 if mode in ("alamouti", "smux") else 1
        self.n_rx = 1 if mode == "siso" else 2
        self.np = payload_symbols(mode, modulation, acm, fec, ldpc_rate)
        self.threshold = threshold
        self.backoff = backoff
        self.body_len = (4 + self.np) * SYM
        self.const_every = 0              # >0: attach equalised symbols to every N-th good frame
        self.n_code = -(-packet_bits(fec, ldpc_rate) // self.bps)   # symbols holding one packet (RS 64QAM: 331)
        self.n_const = 1008 if acm else self.n_code      # points per constellation snapshot
        self.keep_raw = False             # measurements: info["raw"] = hard-decided codeword bytes
        # mode byte of the last good SIG (interleaver / ACM modulation), used by sig_recover
        self.sig_hint = MODE_CODES[mode] << 4 | MOD_CODES[modulation] | self.fec_flag | (
            ACM_FLAG if acm else 0 if self.coder else INTERLEAVERS.index("block") << 2)

    # -- synchronisation ------------------------------------------------
    def schmidl_cox(self, x):
        """x: (nrx, n). Returns metric M(d), P(d) for d in [0, n-64]."""
        L = N_FFT // 2
        prod = np.conj(x[:, :-L]) * x[:, L:]
        energy = np.abs(x) ** 2
        cp = np.concatenate([np.zeros((x.shape[0], 1), complex), np.cumsum(prod, axis=1)], axis=1)
        ce = np.concatenate([np.zeros((x.shape[0], 1)), np.cumsum(energy, axis=1)], axis=1)
        P = (cp[:, L:] - cp[:, :-L]).sum(axis=0)
        E = (ce[:, L:] - ce[:, :-L]).sum(axis=0)          # energy of every L window
        E1, E2 = E[:-L], E[L:]
        # Cauchy-Schwarz normalisation: 0 <= M <= 1 even at signal/gap edges
        M = np.abs(P) ** 2 / (E1 * E2 + 1e-30)
        M[E2 < 1e-12 * L] = 0
        return M, P

    def find(self, x, start=0, stop=None):
        """Return (STF start = first CP sample >= start, P) or (None, None).

        stop: only metric crossings before this index count (the rest of x is still used to
        locate the plateau and for the LTF fine timing).

        Only the antennas this mode uses are searched (siso: RX0), so an unused, noise-only input
        does not dilute the metric. With fine_timing the coarse Schmidl-Cox estimate is refined
        by cross-correlation with the known LTF (both LTF symbols, both TX antennas)."""
        x = x[:self.n_rx] if self.fine_timing else x
        M, P = self.schmidl_cox(x)
        mask = M[start:stop] > self.threshold
        if mask.size and mask[0]:
            # plateau already running at the window edge: its start is unknown, skip it
            first_low = np.flatnonzero(~mask)
            if first_low.size == 0:
                return None, None
            mask[:first_low[0]] = False
        above = np.flatnonzero(mask)
        if above.size == 0:
            return None, None
        d0 = start + above[0]
        if d0 < N_CP // 2:
            return None, None
        seg = M[d0:d0 + 2 * SYM]
        peak = seg.max()
        plateau = np.flatnonzero(seg >= 0.9 * peak)
        centre = d0 + int(round(plateau.mean()))
        s0 = centre - N_CP // 2
        if self.fine_timing:
            s0 = self._ltf_timing(x, s0, P[centre])
        return s0, P[centre]

    FINE_RANGE = 24                       # +- samples searched around the coarse start

    def _ltf_timing(self, x, s0, p):
        """Sample-accurate frame start from the two LTF symbols (falls back to s0 near buffer ends).

        TX0 sends [L, L], TX1 [L, -L]: correlating (w1 + w2) and (w1 - w2) with L collects both
        antennas. The earliest lag within 8 samples before the peak holding >= 30 % of it is taken
        (first path, not just the strongest echo)."""
        R, n = self.FINE_RANGE, x.shape[1]
        base = s0 + SYM + N_CP                     # coarse first body sample of LTF 1
        lo, hi = base - R, base + R + SYM + N_FFT
        if lo < 0 or hi > n:
            return s0
        cfo = np.angle(p) / (2 * np.pi * (N_FFT // 2))
        seg = x[:, lo:hi] * np.exp(-2j * np.pi * cfo * np.arange(lo, hi))
        idx = np.arange(2 * R + 1)[:, None] + np.arange(N_FFT)[None, :]
        w1, w2 = seg[:, idx], seg[:, idx + SYM]                     # (nrx, 2R+1, 64)
        ref = np.conj(_ltf_body())
        m = np.sum(np.abs((w1 + w2) @ ref) ** 2 + np.abs((w1 - w2) @ ref) ** 2, axis=0)
        k = int(np.argmax(m))
        early = np.flatnonzero(m[max(0, k - 8):k + 1] >= 0.3 * m[k])
        k = max(0, k - 8) + int(early[0])
        return max(0, s0 + k - R)

    # -- frames (batched: K frames per call amortises numpy overhead) ---
    def demodulate(self, x, s0, p_metric=None):
        return self.demodulate_batch(x, [s0], [p_metric])[0]

    def demodulate_batch(self, x, starts, p_metrics, known_sig=None):
        """x: (2, n); starts: frame (STF CP) indices. Returns [(packet|None, info)].

        known_sig (measurements only, tools/link_budget.py): per frame the SIG head the receiver
        should assume (bytes as decoded: magic, mode byte, seq hi, seq lo) or None -> 'genie' SIG."""
        t = tables()
        K, nsym = len(starts), 4 + self.np
        rx = np.stack([x[:self.n_rx, s0:s0 + self.body_len] for s0 in starts]).astype(complex)
        cfo = np.array([0.0 if p is None else np.angle(p) / (2 * np.pi * (N_FFT // 2))
                        for p in p_metrics])
        if np.any(cfo):
            rx *= np.exp(-2j * np.pi * cfo[:, None] * np.arange(self.body_len)[None, :])[:, None, :]
        sym = rx.reshape(K, self.n_rx, nsym, SYM)
        start = N_CP - self.backoff
        F = np.fft.fft(sym[..., start:start + N_FFT], axis=-1) * (POWER_NORM / np.sqrt(N_FFT))
        # FFT window starts `backoff` samples early: remove the known phase ramp
        F *= np.exp(2j * np.pi * np.fft.fftfreq(N_FFT) * self.backoff)
        Y1, Y2 = F[:, :, 1], F[:, :, 2]                             # (K, nrx, 64)
        L = t["ltf"][ACTIVE_BINS]
        H = np.zeros((K, self.n_rx, 2, N_FFT), complex)
        # Effective channel (includes the 1/sqrt(2) per-antenna TX scaling)
        H[:, :, 0, ACTIVE_BINS] = (Y1 + Y2)[..., ACTIVE_BINS] / (2 * L)
        if self.n_tx == 2:
            H[:, :, 1, ACTIVE_BINS] = (Y1 - Y2)[..., ACTIVE_BINS] / (2 * L)
        noise = np.mean(np.abs(F[:, :, 1:3][..., NULL_BINS]) ** 2, axis=(1, 2, 3)) + 1e-15
        # SIG
        Fs = self._cpe(F[:, :, 3:4], H, 0)
        sig_eq, sig_w = self._combine(Fs, H, noise, "sfbc" if self.n_tx == 2 else "mrc")
        sig_eq = sig_eq[:, 0]
        mode_byte = MODE_CODES[self.mode] << 4 | MOD_CODES[self.modulation] | self.fec_flag
        if self.acm:            # repeated 24-bit SIG, soft combined; any modulation, plain order
            soft = sig_eq.real * np.broadcast_to(sig_w[:, 0], sig_eq.shape)
            b = np.packbits(((soft[:, :24] + soft[:, 24:]) < 0).astype(np.uint8), axis=1)
            heads = [bytes([SIG_MAGIC, b[k, 0], 0, b[k, 1]]) for k in range(K)]
            crc_ok = [crc8(bytes(b[k, :2])) == b[k, 2] and b[k, 0] & INTERLEAVE_MASK == 0 for k in range(K)]
            want = ACM_FLAG | self.fec_flag | MODE_CODES[self.mode] << 4
            valid = [crc_ok[k] and b[k, 0] & 0xF0 == want for k in range(K)]
            self.fec_mismatch += int(sum(crc_ok[k] and b[k, 0] & 0xF0 == want ^ FEC_FLAG for k in range(K)))
        else:
            sig_bytes = np.packbits((sig_eq.real < 0).astype(np.uint8), axis=1)
            heads = [sig_bytes[k].tobytes() for k in range(K)]
            rate_bits = LDPC_RATES.index(self.ldpc_rate) if self.coder else None
            valid, crc_ok = [], []
            for d in heads:
                ok = d[0] == SIG_MAGIC and (d[1] >> 2 & 3) < 3 and crc16(d[:4]) == struct.unpack(">H", d[4:6])[0]
                same = (d[1] & ~INTERLEAVE_MASK) == mode_byte and (rate_bits is None or d[2] >> 6 == rate_bits)
                valid.append(ok and same)
                self.fec_mismatch += ok and not same and (d[1] & ~INTERLEAVE_MASK & ~FEC_FLAG) == mode_byte & ~FEC_FLAG
        if known_sig is not None:
            for k, h in enumerate(known_sig):
                if h is not None:
                    heads[k], valid[k] = bytes(h), True
        # SIG-less recovery: a frame whose SIG fails is still decoded with what the receiver already
        # knows (mode and modulation from the configuration, the interleaver / ACM modulation of the
        # last good SIG); the packet is kept only if RS and CRC32 pass. No format change.
        recovered = [False] * K
        for k in range(K):
            if valid[k]:
                self.sig_hint = heads[k][1]
            elif self.sig_recover:
                heads[k], valid[k], recovered[k] = bytes([SIG_MAGIC, self.sig_hint, 0, 0]), True, True
        # payload: equalise, de-interleave (per frame, as its SIG says), decide
        Fd = self._cpe(F[:, :, 4:], H, 1)
        scheme = {"siso": "mrc", "simo": "mrc", "alamouti": "sfbc", "smux": "mmse"}[self.mode]
        eq, weight = self._combine(Fd, H, noise, scheme)
        eq = eq.reshape(K, -1)
        # impulse noise: null carriers of each data symbol vs the LTF noise floor
        burst = np.mean(np.abs(F[:, :, 4:][..., NULL_BINS]) ** 2, axis=(1, 3)) / noise[:, None]   # (K, np)
        derate = 1.0 / np.maximum(burst / 2.0, 1.0)
        # effective post-equaliser SNR of each frame (harmonic mean over data carriers/streams):
        # the quantity the ACM controller compares with its thresholds
        snr_eff = -10 * np.log10(np.mean(1.0 / np.maximum(weight.reshape(K, -1), 1e-6), axis=1))
        weight = weight * derate.reshape((K, self.np) + (1,) * (weight.ndim - 2))
        weight = weight.reshape(K, -1)
        if self.coder:
            return self._ldpc_decode(eq, weight, heads, valid, cfo, snr_eff, H, noise, recovered)
        if self.acm:
            return self._acm_decode(eq, weight, heads, valid, cfo, snr_eff, H, noise, recovered)
        for k in range(K):
            perm = self.perms[heads[k][1] >> 2 & 3] if valid[k] else None
            if perm is not None:
                eq[k] = eq[k][perm]
                weight[k] = weight[k][perm]
        n_code = -(-CODE_BYTES * 8 // self.bps)   # ceil: 1984 bits are not a multiple of 6 (64QAM)
        bits = demodulate(eq.reshape(-1), self.bps).reshape(K, -1)[:, :CODE_BYTES * 8]
        raw = np.packbits(bits, axis=1)
        results = []
        for k in range(K):
            d = heads[k]
            if not valid[k]:
                results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                continue
            rel = None
            if self.soft and self._soft_allowed():
                rel = (lambda k=k: bit_reliability(eq[k, :n_code], weight[k, :n_code], self.bps)
                       [:CODE_BYTES * 8].reshape(CODE_BYTES, 8).min(axis=1))
            t0 = time.perf_counter()
            packet, corrected, reason, erasures = decode_codeword(raw[k], rel)
            if rel is not None and (erasures or reason != "ok"):
                self.soft_budget -= time.perf_counter() - t0
            if recovered[k]:
                if packet is None:
                    results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                    continue
            info = {"reason": reason, "corrected": corrected, "cfo": cfo[k], "soft_erasures": erasures,
                    "interleaver": INTERLEAVERS[d[1] >> 2 & 3],
                    "seq": None if recovered[k] else struct.unpack(">H", d[2:4])[0]}
            if recovered[k]:
                info["sig_recovered"] = True
            if self.keep_raw:
                info["raw"] = raw[k]
            self.count = getattr(self, "count", 0) + 1
            if self.const_every and reason == "ok" and self.count % self.const_every == 0:
                # constellation after equalisation (smux: both streams), before demapping
                info["const"] = eq[k, :n_code].astype(np.complex64)
            if self.count % 8 == 1:                    # link statistics: every 8th frame only
                p = float(np.mean(np.sum(np.abs(H[k, :, :self.n_tx][..., DATA_BINS]) ** 2, axis=1)))
                info.update(snr_db=10 * np.log10(p / noise[k]), evm_db=self._evm(eq[k]))
                if self.n_tx == 2:
                    info["cond_db"] = self._cond(H[k])
            results.append((packet, info))
        return results

    def _acm_decode(self, eq, weight, heads, valid, cfo, snr_eff, H, noise, recovered):
        """ACM frames: demap with the modulation of each SIG, decode every codeword.
        Returns [(list of packets, info)] per frame."""
        results = []
        nbits = CODE_BYTES * 8
        for k, d in enumerate(heads):
            if not valid[k]:
                results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                continue
            modulation = MOD_NAMES[d[1] & 3]
            bps = BPS[modulation]
            ncw = codewords_per_frame(self.mode, modulation)
            nsym_used = -(-ncw * nbits // bps)
            e, w = eq[k, :nsym_used], weight[k, :nsym_used]
            bits = demodulate(e, bps)[:ncw * nbits]
            raw = np.packbits(bits.reshape(ncw, nbits), axis=1)
            rel_all = {}
            def rel(j):
                if "r" not in rel_all:
                    rel_all["r"] = bit_reliability(e, w, bps)[:ncw * nbits].reshape(ncw, CODE_BYTES, 8).min(axis=2)
                return rel_all["r"][j]
            packets, reasons, corrected, erasures = [], [], 0, 0
            for j in range(ncw):
                r = (lambda j=j: rel(j)) if self.soft and self._soft_allowed() else None
                t0 = time.perf_counter()
                packet, c, reason, er = decode_codeword(raw[j], r)
                if r is not None and (er or reason != "ok"):
                    self.soft_budget -= time.perf_counter() - t0
                reasons.append(reason)
                corrected += c
                erasures += er
                if packet is not None:
                    packets.append(packet)
            if recovered[k] and not packets:
                results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                continue
            info = {"reason": "ok" if packets else reasons[0], "reasons": reasons, "corrected": corrected,
                    "soft_erasures": erasures, "cfo": cfo[k], "modulation": modulation,
                    "snr_eff_db": float(snr_eff[k]), "interleaver": "none",
                    "seq": None if recovered[k] else struct.unpack(">H", d[2:4])[0]}
            if recovered[k]:
                info["sig_recovered"] = True
            if self.keep_raw:
                info["raw"] = raw
            self.count = getattr(self, "count", 0) + 1
            if self.const_every and packets and self.count % self.const_every == 0:
                info["const"] = e[:self.n_const].astype(np.complex64)
            if self.count % 8 == 1:
                p = float(np.mean(np.sum(np.abs(H[k, :, :self.n_tx][..., DATA_BINS]) ** 2, axis=1)))
                info.update(snr_db=10 * np.log10(p / noise[k]), evm_db=self._evm(e, bps))
                if self.n_tx == 2:
                    info["cond_db"] = self._cond(H[k])
            results.append((packets, info))
        return results

    def _ldpc_decode(self, eq, weight, heads, valid, cfo, snr_eff, H, noise, recovered):
        """LDPC frames (fixed or ACM): LLRs of every valid frame, bit de-interleaving, one batched
        LDPC decode for all packets of all frames, CRC32 per packet."""
        K, cb = len(heads), self.coder.bits
        jobs, llrs = {}, []
        for k in range(K):
            if not valid[k]:
                continue
            modulation = MOD_NAMES[heads[k][1] & 3] if self.acm else self.modulation
            bps = BPS[modulation]
            npk = codewords_per_frame(self.mode, modulation) if self.acm else 1
            nb = npk * cb
            nsym = -(-nb // bps)
            raw = bit_llr(eq[k, :nsym], weight[k, :nsym], bps)[:nb]
            llr = np.empty_like(raw)
            llr[bit_perm(nb)] = raw
            jobs[k] = (modulation, bps, npk, nsym, len(llrs))
            llrs.append(llr.reshape(npk, cb))
        if llrs:
            info_bits, cw_ok, iters = self.coder.decode(np.concatenate(llrs), self.ldpc_iter)
            msgs = np.packbits(info_bits, axis=1) ^ tables()["scramble"][:MESSAGE_BYTES]
        results, row = [], 0
        for k in range(K):
            if k not in jobs:
                results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                continue
            modulation, bps, npk, nsym, _ = jobs[k]
            packets, reasons = [], []
            for j in range(row, row + npk):
                p = _check(msgs[j].tobytes())
                reasons.append("ok" if p is not None else "crc" if cw_ok[j] else "rs")
                if p is not None:
                    packets.append(p)
            it = int(iters[row:row + npk].sum())
            row += npk
            if recovered[k] and not packets:
                results.append((None, {"reason": "sig", "cfo": cfo[k], "seq": None}))
                continue
            seq = None if recovered[k] else struct.unpack(">H", heads[k][2:4])[0] & (0xFFFF if self.acm else 0x3FFF)
            info = {"reason": "ok" if packets else reasons[0], "corrected": 0, "soft_erasures": 0, "cfo": cfo[k],
                    "interleaver": "none", "seq": seq, "fec": "ldpc", "ldpc_iter": it}
            if self.acm:
                info.update(reasons=reasons, modulation=modulation, snr_eff_db=float(snr_eff[k]))
            if recovered[k]:
                info["sig_recovered"] = True
            e = eq[k, :nsym]
            self.count = getattr(self, "count", 0) + 1
            if self.const_every and packets and self.count % self.const_every == 0:
                info["const"] = e[:self.n_const].astype(np.complex64)
            if self.count % 8 == 1:
                p = float(np.mean(np.sum(np.abs(H[k, :, :self.n_tx][..., DATA_BINS]) ** 2, axis=1)))
                info.update(snr_db=10 * np.log10(p / noise[k]), evm_db=self._evm(e, bps))
                if self.n_tx == 2:
                    info["cond_db"] = self._cond(H[k])
            results.append((packets if self.acm else (packets[0] if packets else None), info))
        return results

    def _soft_allowed(self):
        """Token bucket: erasure trials may use at most soft_cpu of wall time, so a
        bad link cannot make the receiver fall behind real time."""
        now = time.monotonic()
        self.soft_budget = min(0.5, self.soft_budget + (now - self.soft_clock) * self.soft_cpu)
        self.soft_clock = now
        if self.soft_budget > 0:
            return True
        self.soft_skipped += 1
        return False

    # -- helpers (leading axis K = frames) --------------------------------
    def _cpe(self, F, H, first_pol):
        """Remove per-symbol common phase using pilots. F: (K, nrx, nsym, 64)."""
        t = tables()
        nsym = F.shape[2]
        pol = t["polarity"][(first_pol + np.arange(nsym)) % 128]
        pred = H[:, :, 0, PILOT_BINS][:, :, None, :] * (pol[:, None] * t["pilot"])
        if self.n_tx == 2:
            pred = pred + H[:, :, 1, PILOT_BINS][:, :, None, :] * (pol[:, None] * t["pilot_tx1"])
        phase = np.angle(np.sum(np.conj(pred) * F[..., PILOT_BINS], axis=(1, 3)))   # (K, nsym)
        return F * np.exp(-1j * phase)[:, None, :, None]

    def _combine(self, F, H, noise, scheme):
        """Equalised data symbols (K, nsym, 48[, 2]) and their post-equaliser SNR
        (K, 1, 48[, 2]) used as soft-decision weight."""
        Yd = F[..., DATA_BINS]                        # (K, nrx, nsym, 48)
        nz = noise[:, None]
        Hd = H[..., DATA_BINS]                        # (K, nrx, 2, 48)
        if scheme == "mrc":
            h = Hd[:, :, 0]                           # (K, nrx, 48)
            g = np.sum(np.abs(h) ** 2, axis=1)        # (K, 48)
            return (np.sum(np.conj(h)[:, :, None, :] * Yd, axis=1) / g[:, None, :],
                    (g / nz)[:, None, :])
        if scheme == "sfbc":
            # Exact per-pair zero-forcing (does not assume h(k) == h(k+1)):
            # y1 = h0a s1 - h1a s2*,  y2* = h1b* s1 + h0b* s2*   for every RX antenna
            h0a, h0b = Hd[:, :, 0, 0::2], Hd[:, :, 0, 1::2]
            h1a, h1b = Hd[:, :, 1, 0::2], Hd[:, :, 1, 1::2]
            A = np.concatenate([np.stack([h0a, -h1a], axis=-1),
                                np.stack([np.conj(h1b), np.conj(h0b)], axis=-1)], axis=1)
            A = np.transpose(A, (0, 2, 1, 3))                       # (K, 24, 2nrx, 2)
            y = np.concatenate([Yd[..., 0::2], np.conj(Yd[..., 1::2])], axis=1)
            y = np.transpose(y, (0, 3, 1, 2))                       # (K, 24, 2nrx, nsym)
            Ah = np.conj(np.swapaxes(A, -1, -2))
            M = Ah @ A                                               # (K, 24, 2, 2) Hermitian
            u = np.linalg.solve(M + 1e-12 * np.eye(2), Ah @ y)       # (K, 24, 2, nsym)
            out = np.empty(Yd.shape[:1] + Yd.shape[2:], complex)    # (K, nsym, 48)
            out[..., 0::2] = np.swapaxes(u[:, :, 0], 1, 2)
            out[..., 1::2] = np.conj(np.swapaxes(u[:, :, 1], 1, 2))
            # ZF noise = noise * diag(inv(M)): SNR_i = det / (other diagonal * noise)
            a, d = M[..., 0, 0].real, M[..., 1, 1].real
            det = np.maximum(a * d - np.abs(M[..., 0, 1]) ** 2, 0.0)
            w = np.empty(Yd.shape[:1] + (1, N_DATA))
            w[:, 0, 0::2] = det / (d * nz + 1e-30)
            w[:, 0, 1::2] = det / (a * nz + 1e-30)
            return out, w
        # 2-stream MMSE per carrier, bias removed
        Hk = np.transpose(Hd, (0, 3, 1, 2))           # (K, 48, nrx, 2)
        Hh = np.conj(np.swapaxes(Hk, -1, -2))
        W = np.linalg.solve(Hh @ Hk + noise[:, None, None, None] * np.eye(2), Hh)
        S = W @ np.transpose(Yd, (0, 3, 1, 2))        # (K, 48, 2, nsym)
        G = np.real(np.einsum("kcij,kcji->kci", W, Hk))
        S = S / G[..., None]
        sinr = G / np.maximum(1 - G, 1e-9)            # (K, 48, 2)
        return np.transpose(S, (0, 3, 1, 2)), sinr[:, None]   # (K, nsym, 48, 2)

    def _evm(self, eq, bps=None):
        bps = bps or self.bps
        s = eq.reshape(-1)
        ref = modulate(demodulate(s, bps), bps)
        return float(10 * np.log10(np.mean(np.abs(s - ref) ** 2) + 1e-15))

    def _cond(self, H):
        sv = np.linalg.svd(np.transpose(H[:, :, DATA_BINS], (2, 0, 1)), compute_uv=False)
        return float(np.mean(20 * np.log10(sv[:, 0] / np.maximum(sv[:, -1], 1e-12))))
