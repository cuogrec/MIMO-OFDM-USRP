# Copied unchanged from OFDM_Video_N210 v1.23 (ofdmvideo). Wire format kept identical.
"""Systematic RS(12,8) / RS(16,8) erasure coding over GF(256), no dependencies.

Each byte position is a separate shortened Reed-Solomon codeword. Eight
uncoded DATA packets and four/eight parity packets tolerate four/eight erasures.
Only PHY-CRC-accepted packets may enter the receiver. This is an erasure code,
not a decoder for unknown bit errors. A single ordered RF stream is assumed.
"""

from functools import lru_cache

from .protocol import (DATA, END, IDLE, RS_PARITY, RS8_PARITY, ReceiverState,
                       pack_packet, rs_parity_fields, unpack_packet)

DATA_COUNT = 8
PARITY_COUNT = 4
MAX_PARITY_COUNT = 8
PAYLOAD_BYTES = 188


# GF(2^8), primitive polynomial x^8+x^4+x^3+x^2+1 (0x11d).
_EXP = [0] * 510
_LOG = [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i] = _value
    _LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 510):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _inverse(matrix):
    size = len(matrix)
    if any(len(row) != size for row in matrix):
        raise ValueError("Expected a square GF(256) matrix")
    rows = [list(row) + [int(i == j) for j in range(size)]
            for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = next((i for i in range(column, size) if rows[i][column]), None)
        if pivot is None:
            raise ValueError("Singular GF(256) matrix")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = _EXP[255 - _LOG[rows[column][column]]]
        rows[column] = [_mul(value, scale) for value in rows[column]]
        for i in range(size):
            if i != column and rows[i][column]:
                factor = rows[i][column]
                rows[i] = [a ^ _mul(factor, b)
                           for a, b in zip(rows[i], rows[column])]
    return [row[size:] for row in rows]


def _parity_matrix():
    # Evaluate degree <8 polynomials at 16 distinct field elements. Right
    # multiply by the inverse of the first eight rows to make DATA systematic.
    vandermonde = [[_EXP[(row * column) % 255] for column in range(DATA_COUNT)]
                   for row in range(DATA_COUNT + MAX_PARITY_COUNT)]
    inverse = _inverse(vandermonde[:DATA_COUNT])
    result = []
    for row in vandermonde[DATA_COUNT:]:
        output = []
        for column in range(DATA_COUNT):
            value = 0
            for i in range(DATA_COUNT):
                value ^= _mul(row[i], inverse[i][column])
            output.append(value)
        result.append(output)
    return result


PARITY_MATRIX = _parity_matrix()


@lru_cache(maxsize=256)
def _multiply_table(coefficient):
    return bytes(_mul(coefficient, value) for value in range(256))


def _combine(coefficients, payloads):
    output = bytearray(PAYLOAD_BYTES)
    for coefficient, payload in zip(coefficients, payloads):
        if coefficient:
            scaled = payload if coefficient == 1 else payload.translate(_multiply_table(coefficient))
            for i, value in enumerate(scaled):
                output[i] ^= value
    return bytes(output)


class NotEnoughShards(ValueError):
    pass


def encode_parity(payloads, parity_count=PARITY_COUNT):
    """Four/eight parity payloads; omitted final DATA positions are zero."""
    if (parity_count not in (4, 8) or not 1 <= len(payloads) <= DATA_COUNT
            or any(len(p) != PAYLOAD_BYTES for p in payloads)):
        raise ValueError("RS needs one to eight complete MPEG-TS payloads and four/eight parity")
    return [_combine(row[:len(payloads)], payloads) for row in PARITY_MATRIX[:parity_count]]


def recover_payloads(data, parity, count=DATA_COUNT):
    """Return DATA in order, or fail without fabricating unrecoverable bytes.

    data maps zero-based group positions to payloads; parity maps 0..7 to
    parity payloads. Shortened groups use count<8 and the same generator.
    """
    if (not 1 <= count <= DATA_COUNT
            or any(not 0 <= i < count for i in data)
            or any(not 0 <= i < MAX_PARITY_COUNT for i in parity)
            or any(len(p) != PAYLOAD_BYTES for p in list(data.values()) + list(parity.values()))):
        raise ValueError("Invalid RS shard metadata or payload")
    missing = [i for i in range(count) if i not in data]
    if len(missing) > len(parity):
        raise NotEnoughShards("Not enough CRC-valid parity packets")
    result = dict(data)
    if missing:
        selected = sorted(parity)[:len(missing)]
        known = sorted(data)
        residuals = []
        for index in selected:
            contribution = _combine([PARITY_MATRIX[index][i] for i in known],
                                    [data[i] for i in known])
            residuals.append(bytes(a ^ b for a, b in zip(parity[index], contribution)))
        inverse = _inverse([[PARITY_MATRIX[j][i] for i in missing] for j in selected])
        for index, coefficients in zip(missing, inverse):
            result[index] = _combine(coefficients, residuals)
    return [result[i] for i in range(count)]


class RSReceiverState:
    """Bounded one-group receiver; report direct and recovered DATA separately."""

    def __init__(self, session, parity_count=PARITY_COUNT):
        if parity_count not in (4, 8):
            raise ValueError("RS receiver needs four or eight parity packets")
        self.parity_count = parity_count
        self.parity_kind = RS8_PARITY if parity_count == 8 else RS_PARITY
        self.mode = "rs8_8" if parity_count == 8 else "rs8_4"
        self.inner = ReceiverState(session)
        self.raw_valid = 0
        self.received_data = self.recovered = self.parity_frames = 0
        self.failed_groups = 0
        self.base = None
        self.pending = {}
        self.parity = {}
        self.count = None
        self.closed_before = 0
        self.last_direct_sequence = None
        self.failed_group_details = []
        self.failed_group_details_truncated = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @property
    def valid_frames(self):
        return self.raw_valid

    def _flush(self, count=DATA_COUNT, close_reason="group_boundary"):
        if self.base is None:
            return b""
        data = {sequence - self.base: unpack_packet(raw)[4]
                for sequence, raw in self.pending.items()
                if self.base <= sequence < self.base + count}
        try:
            decoded = recover_payloads(data, self.parity, count)
        except NotEnoughShards:
            decoded = None
            self.failed_groups += 1
            if len(self.failed_group_details) < 64:
                self.failed_group_details.append({
                    "base_data_sequence": self.base, "data_count": count,
                    "received_data_sequences": [self.base + i for i in sorted(data)],
                    "missing_data_sequences": [self.base + i for i in range(count) if i not in data],
                    "received_parity_indices": sorted(self.parity),
                    "valid_shards": len(data) + len(self.parity), "required_shards": count,
                    "parity_packets_per_group": self.parity_count,
                    "close_reason": close_reason,
                })
            else:
                self.failed_group_details_truncated = True
        output = []
        for i in range(count):
            raw = self.pending.get(self.base + i)
            if raw is None and decoded is not None:
                raw = pack_packet(DATA, self.inner.session, self.base + i, decoded[i])
                self.recovered += 1
            if raw is not None:
                payload = self.inner.ingest(raw)
                if payload is not None:
                    output.append(payload)
        self.closed_before = self.base + count
        # Closing a group confirms its missing tail immediately, rather than
        # waiting for another delivered DATA packet or the final END marker.
        self.inner.confirm_until(self.closed_before)
        self.base, self.pending, self.parity, self.count = None, {}, {}, None
        return b"".join(output)

    def ingest(self, raw):
        try:
            kind, session, sequence, stamp, payload = unpack_packet(raw)
        except ValueError:
            self.inner.malformed += 1
            return None
        if session != self.inner.session:
            self.inner.foreign_session += 1
            return None
        if kind not in (DATA, END, IDLE, self.parity_kind):
            self.inner.malformed += 1
            return None
        self.raw_valid += 1
        if kind == IDLE:
            self.inner.ingest(raw)
            return None
        if kind == END:
            if (sequence < self.closed_before or (self.pending and sequence <= max(self.pending))
                    or (self.base is not None and sequence <= self.base)
                    or (self.base is not None and self.count is not None
                        and sequence < self.base + self.count)):
                self.inner.malformed += 1
                return None
            output = self._flush(min(DATA_COUNT, sequence - self.base), "END") if self.base is not None else b""
            if sequence > self.closed_before:
                self.failed_groups += (sequence - self.closed_before + DATA_COUNT - 1) // DATA_COUNT
                self.inner.confirm_until(sequence)
                self.closed_before = sequence
            self.inner.ingest(raw)
            return output or None
        if kind == self.parity_kind:
            self.parity_frames += 1
        if self.inner.final_packet_count is not None:
            if kind == DATA:
                self.inner.duplicate_or_late += 1
            return None
        base = sequence if kind == self.parity_kind else sequence // DATA_COUNT * DATA_COUNT
        if base < self.closed_before:
            if kind == DATA:
                self.inner.duplicate_or_late += 1
            return None
        output = b""
        if self.base is not None and base != self.base:
            if base < self.base:
                self.inner.duplicate_or_late += 1
                return None
            output = self._flush(self.count or DATA_COUNT, "next_group")
        if self.base is None:
            if base > self.closed_before:
                self.failed_groups += (base - self.closed_before + DATA_COUNT - 1) // DATA_COUNT
                self.inner.confirm_until(base)
                self.closed_before = base
            self.base = base
        if kind == DATA:
            if self.count is not None and sequence >= base + self.count:
                self.inner.malformed += 1
                return output or None
            if sequence in self.pending:
                self.inner.duplicate_or_late += 1
            else:
                self.pending[sequence] = raw
                self.received_data += 1
                self.last_direct_sequence = sequence
        else:
            count, index = rs_parity_fields(stamp, self.parity_count)
            if ((self.count is not None and count != self.count)
                    or any(sequence >= base + count for sequence in self.pending)):
                self.inner.malformed += 1
                return output or None
            self.count = count
            if index not in self.parity:
                self.parity[index] = payload
        count = self.count or DATA_COUNT
        if len(self.pending) + len(self.parity) >= count:
            output += self._flush(count, "enough_shards")
        return output or None

    def snapshot(self):
        return {**self.inner.snapshot(), "valid_frames": self.raw_valid,
                "fec_mode": self.mode, "fec_data_packets_per_full_group": DATA_COUNT,
                "fec_parity_packets_per_group": self.parity_count,
                "data_packets_received_directly": self.received_data,
                "data_packets_recovered": self.recovered,
                "parity_packets_received": self.parity_frames,
                "fec_groups_unrecoverable": self.failed_groups,
                "fec_failed_group_details": list(self.failed_group_details),
                "fec_failed_group_details_truncated": self.failed_group_details_truncated,
                "fec_failed_group_detail_scope": "Observed groups closed with insufficient shards; fully erased groups counted in total",
                "fec_pending_data_packets": len(self.pending),
                "fec_pending_parity_packets": len(self.parity),
                "last_data_sequence_received_directly": self.last_direct_sequence,
                "last_data_sequence_delivered": self.inner.last_data_sequence,
                "data_packet_loss_fraction_scope": "After " + self.mode + " recovery; confirmed closed groups and END"}
