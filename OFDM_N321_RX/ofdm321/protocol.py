# Copied unchanged from OFDM_Video_N210 v1.23 (ofdmvideo). Wire format kept identical.
"""Application framing. The GNU Radio OFDM blocks add/check the PHY CRC32."""

from collections import deque
from dataclasses import dataclass, field
import struct
import time

from .config import APP_HEADER_BYTES, PACKET_BYTES, TS_CHUNK_BYTES

MAGIC = b"OVN1"
VERSION = 1
IDLE, DATA, END, PARITY, RS_PARITY, RS8_PARITY = 0, 1, 2, 3, 4, 5
HEADER = struct.Struct("!4sBBHIIQ")
assert HEADER.size == APP_HEADER_BYTES


def rs_parity_stamp(count, index, parity_count=4):
    if parity_count not in (4, 8) or not 1 <= count <= 8 or not 0 <= index < parity_count:
        raise ValueError("Invalid RS group count, parity index or profile")
    return count << 8 | index


def rs_parity_fields(stamp, parity_count=4):
    count, index = stamp >> 8, stamp & 0xff
    rs_parity_stamp(count, index, parity_count)
    return count, index


def pack_packet(kind, session, sequence=0, payload=b"", timestamp_ns=0):
    if kind not in (IDLE, DATA, END, PARITY, RS_PARITY, RS8_PARITY):
        raise ValueError("Unknown packet kind")
    if len(payload) > TS_CHUNK_BYTES or (kind in (IDLE, END) and payload):
        raise ValueError("Invalid payload length")
    if kind in (DATA, PARITY, RS_PARITY, RS8_PARITY) and len(payload) != TS_CHUNK_BYTES:
        raise ValueError("DATA must contain complete 188-byte MPEG-TS packets")
    if kind == PARITY and (sequence % 8 or not 1 <= timestamp_ns <= 8):
        raise ValueError("PARITY needs an eight-aligned base sequence and group count in [1,8]")
    if kind in (RS_PARITY, RS8_PARITY):
        if sequence % 8:
            raise ValueError("RS parity base sequence must be eight-aligned")
        rs_parity_fields(timestamp_ns, 8 if kind == RS8_PARITY else 4)
    header = HEADER.pack(MAGIC, VERSION, kind, len(payload), session,
                         sequence, timestamp_ns)
    return header + payload + bytes(TS_CHUNK_BYTES - len(payload))


def unpack_packet(raw):
    if len(raw) != PACKET_BYTES:
        raise ValueError("Wrong packet size")
    magic, version, kind, length, session, sequence, stamp = HEADER.unpack_from(raw)
    if magic != MAGIC or version != VERSION or kind not in (IDLE, DATA, END, PARITY, RS_PARITY, RS8_PARITY):
        raise ValueError("Wrong magic/version/type")
    if length > TS_CHUNK_BYTES or (kind in (IDLE, END) and length):
        raise ValueError("Wrong payload length")
    if kind in (DATA, PARITY, RS_PARITY, RS8_PARITY) and length != TS_CHUNK_BYTES:
        raise ValueError("MPEG-TS alignment error")
    if kind == PARITY and (sequence % 8 or not 1 <= stamp <= 8):
        raise ValueError("Malformed parity group metadata")
    if kind in (RS_PARITY, RS8_PARITY):
        if sequence % 8:
            raise ValueError("Malformed RS parity base sequence")
        rs_parity_fields(stamp, 8 if kind == RS8_PARITY else 4)
    return kind, session, sequence, stamp, raw[APP_HEADER_BYTES:APP_HEADER_BYTES + length]


@dataclass
class ReceiverState:
    session: int
    valid_frames: int = 0
    idle_frames: int = 0
    data_packets: int = 0
    data_bytes: int = 0
    duplicate_or_late: int = 0
    malformed: int = 0
    foreign_session: int = 0
    expected_sequence: int = 0
    sequence_gaps: int = 0
    final_packet_count: int | None = None
    latency_ms: deque = field(default_factory=lambda: deque(maxlen=10_000))
    missing_ranges: list = field(default_factory=list)
    missing_ranges_truncated: bool = False
    last_data_sequence: int | None = None
    delivery_observer: object = field(default=None, repr=False, compare=False)
    delivery_observer_errors: int = 0

    def confirm_until(self, next_sequence):
        """Confirm a gap before a new DATA packet or a closed FEC boundary."""
        if next_sequence > self.expected_sequence:
            self.sequence_gaps += next_sequence - self.expected_sequence
            if len(self.missing_ranges) < 128:
                self.missing_ranges.append([self.expected_sequence, next_sequence - 1])
            else:
                self.missing_ranges_truncated = True
            self.expected_sequence = next_sequence

    def ingest(self, raw):
        try:
            kind, session, sequence, stamp, payload = unpack_packet(raw)
        except (ValueError, struct.error):
            self.malformed += 1
            return None
        if session != self.session:
            self.foreign_session += 1
            return None
        if kind in (PARITY, RS_PARITY, RS8_PARITY):
            self.malformed += 1  # A receiver without FEC must never emit parity as video.
            return None
        self.valid_frames += 1
        if kind == IDLE:
            self.idle_frames += 1
        elif kind == END:
            self.final_packet_count = sequence
        else:
            if sequence < self.expected_sequence:
                self.duplicate_or_late += 1
                return None
            self.confirm_until(sequence)
            self.expected_sequence = sequence + 1
            self.last_data_sequence = sequence
            self.data_packets += 1
            self.data_bytes += len(payload)
            if stamp:
                self.latency_ms.append((time.monotonic_ns() - stamp) / 1e6)
            if self.delivery_observer is not None:
                try:
                    self.delivery_observer(sequence, stamp)
                except Exception:
                    # Diagnostic failure must not remove valid video bytes.
                    self.delivery_observer_errors += 1
            return payload
        return None

    def snapshot(self):
        total = self.final_packet_count
        missing = max(0, total - self.data_packets) if total is not None else self.sequence_gaps
        ranges = [list(pair) for pair in self.missing_ranges]
        truncated = self.missing_ranges_truncated
        if total is not None and total > self.expected_sequence:
            if len(ranges) < 128:
                ranges.append([self.expected_sequence, total - 1])
            else:
                truncated = True
        values = sorted(self.latency_ms)
        return {
            "valid_frames": self.valid_frames,
            "idle_frames": self.idle_frames,
            "data_packets": self.data_packets,
            "data_bytes": self.data_bytes,
            "missing_data_packets": missing,
            "missing_sequence_ranges_inclusive": ranges,
            "missing_sequence_ranges_truncated": truncated,
            "sequence_numbering": "Zero-based DATA sequence numbers; parity is excluded",
            "last_data_sequence_delivered": self.last_data_sequence,
            "duplicate_or_late": self.duplicate_or_late,
            "malformed": self.malformed,
            "foreign_session": self.foreign_session,
            "end_received": total is not None,
            "final_packet_count": total,
            "data_packet_loss_fraction": missing / total if total else None,
            "same_host_queue_to_rx_latency_ms_median": values[len(values) // 2] if values else None,
            "same_host_queue_to_rx_latency_ms_p95": values[min(len(values)-1, int(len(values)*.95))] if values else None,
            "delivery_observer_errors": self.delivery_observer_errors,
        }
