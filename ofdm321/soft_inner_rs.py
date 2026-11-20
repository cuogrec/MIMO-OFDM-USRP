# Copied unchanged from OFDM_Video_N210 v1.23 (ofdmvideo). Errors-and-erasures RS core.
"""Bounded reliability-assisted decoding of the existing RS(248,216) wire word.

An errors-and-erasures trial must satisfy 2*unknown_errors + erasures <= 32,
all 32 RS syndromes, and the original PHY CRC. The separate GNU Radio CRC
and two-extra-equation outer RS verifier remain mandatory after this block.
No transmitted bytes, video contents, session value or timestamps are inputs.
"""
from functools import lru_cache
import time
import zlib
import numpy as np
from .inner_rs import (CODE_BYTES, MESSAGE_BYTES, PARITY_BYTES, Uncorrectable,
                       _EXP, _mul, _div, _tables, syndromes, decode, encode)

ERASURE_COUNTS = tuple(range(2, PARITY_BYTES + 1, 2))
MAX_TRIALS = 1 + len(ERASURE_COUNTS)


@lru_cache(maxsize=1)
def _roots():
    roots = np.asarray([[_EXP[(-power * degree) % 255] for power in range(CODE_BYTES)]
                        for degree in range(PARITY_BYTES + 1)], np.uint8)
    roots.setflags(write=False)
    return roots


@lru_cache(maxsize=1)
def _convolution_indices():
    n, i = np.indices((PARITY_BYTES, PARITY_BYTES + 1))
    return np.maximum(n - i, 0), i <= n


def _add_erasure(filtered, locator, position):
    x = _EXP[CODE_BYTES - 1 - position]
    updated = locator + [0]
    for i, coefficient in enumerate(locator):
        updated[i + 1] ^= _mul(coefficient, x)
    return ([filtered[i + 1] ^ _mul(x, filtered[i])
             for i in range(len(filtered) - 1)], updated)


def byte_reliability(symbols):
    """Estimate byte reliability from QPSK margins and per-carrier residual noise."""
    x = np.asarray(symbols)
    if x.shape != (21, 48) or not np.all(np.isfinite(x)):
        raise ValueError('Expected a finite 21 x 48 QPSK payload')
    valid = np.arange(x.size).reshape(x.shape) < CODE_BYTES * 4
    decisions = (np.where(x.real >= 0, 1., -1.)
                 + 1j * np.where(x.imag >= 0, 1., -1.)) * np.sqrt(2)
    # The final sixteen padding positions carry no bytes or noise evidence.
    variance = np.sum(valid * abs(x - decisions) ** 2, axis=0) / np.sum(valid, axis=0)
    margins = np.stack((abs(x.real), abs(x.imag)), axis=-1)
    margins /= np.maximum(variance[None, :, None], 1e-6)
    bits = margins.reshape(-1)[:CODE_BYTES * 8].reshape(CODE_BYTES, 8)
    return (-np.sum(np.exp(-2 * np.sqrt(2) * bits), axis=1)).astype(np.float32)


def decode_errors_erasures(word, erasures, syndrome=None):
    """Return corrected message/error count, or reject without producing bytes."""
    if (len(word) != CODE_BYTES or len(erasures) > PARITY_BYTES
            or len(set(erasures)) != len(erasures)
            or any(type(i) is not int or not 0 <= i < CODE_BYTES for i in erasures)):
        raise ValueError('Invalid inner RS word or erasure positions')
    original = syndromes(word) if syndrome is None else list(syndrome)
    if len(original) != PARITY_BYTES:
        raise ValueError('Expected 32 RS syndromes')
    if not any(original):
        return bytes(word[:MESSAGE_BYTES]), 0
    # Filter out the known erasure exponentials from the syndrome sequence.
    filtered, erased_locator = original.copy(), [1]
    for position in erasures:
        filtered, erased_locator = _add_erasure(filtered, erased_locator, position)
    return _decode_filtered(word, erasures, original, filtered, erased_locator)


def _decode_filtered(word, erasures, original, filtered, erased_locator):
    size = len(filtered)
    locator, previous = [1] + [0] * size, [1] + [0] * size
    degree, shift, previous_discrepancy = 0, 1, 1
    for n in range(size):
        discrepancy = filtered[n]
        for i in range(1, degree + 1):
            discrepancy ^= _mul(locator[i], filtered[n - i])
        if not discrepancy:
            shift += 1
            continue
        saved = locator.copy()
        factor = _div(discrepancy, previous_discrepancy)
        for i in range(size + 1 - shift):
            locator[i + shift] ^= _mul(factor, previous[i])
        if 2 * degree <= n:
            degree = n + 1 - degree
            previous, previous_discrepancy, shift = saved, discrepancy, 1
        else:
            shift += 1
    if 2 * degree + len(erasures) > PARITY_BYTES:
        raise Uncorrectable('Inner RS errors/erasures exceed its parity budget')
    combined = [0] * (degree + len(erasures) + 1)
    for i, a in enumerate(locator[:degree + 1]):
        for j, b in enumerate(erased_locator):
            combined[i + j] ^= _mul(a, b)
    locator, degree = combined, len(combined) - 1
    if not 1 <= degree <= PARITY_BYTES:
        raise Uncorrectable('Invalid inner RS combined locator')
    multiplication = _tables()[0]
    values = np.bitwise_xor.reduce(
        multiplication[np.asarray(locator, np.uint8)[:, None], _roots()[:degree + 1]], axis=0)
    powers = np.flatnonzero(values == 0).tolist()
    if len(powers) != degree:
        raise Uncorrectable('Inner RS locator root count does not match its degree')
    # Evaluate all error magnitudes together. The cached triangular indices
    # express (syndrome * locator) modulo z**degree without Python GF loops.
    indices, valid = _convolution_indices()
    products = multiplication[np.asarray(original, np.uint8)[indices[:degree, :degree + 1]],
                              np.asarray(locator, np.uint8)[None, :]]
    evaluator = np.bitwise_xor.reduce(products * valid[:degree, :degree + 1], axis=1)
    derivative = np.asarray([locator[i] if i % 2 else 0 for i in range(1, degree + 1)], np.uint8)
    selected_roots = _roots()[:degree, powers]
    numerators = np.bitwise_xor.reduce(multiplication[evaluator[:, None], selected_roots], axis=0)
    denominators = np.bitwise_xor.reduce(multiplication[derivative[:, None], selected_roots], axis=0)
    if np.any(denominators == 0):
        raise Uncorrectable('Inner RS combined locator has a repeated root')
    corrected, changed = bytearray(word), 0
    for power, numerator, denominator in zip(powers, numerators.tolist(), denominators.tolist()):
        magnitude = _mul(_EXP[power], _div(numerator, denominator))
        if not magnitude and CODE_BYTES - 1 - power not in erasures:
            raise Uncorrectable('Inner RS unknown error has zero magnitude')
        corrected[CODE_BYTES - 1 - power] ^= magnitude
        changed += magnitude != 0
    if any(syndromes(corrected)):
        raise Uncorrectable('Inner RS errors/erasures candidate failed all-syndrome verification')
    return bytes(corrected[:MESSAGE_BYTES]), changed


class ReliabilityRSDecoder:
    """One ordinary RS trial, then at most sixteen ranked erasure trials."""
    def __init__(self):
        self.soft_frames = self.soft_decoded = self.patterns = self.invalid_reliability = 0
        self.crc_rejected = self.maximum_erasures = 0

    def decode(self, word, reliability):
        try:
            return decode(word)
        except Uncorrectable:
            pass
        self.soft_frames += 1
        confidence = np.asarray(reliability() if callable(reliability) else reliability, dtype=float)
        if confidence.shape != (CODE_BYTES,) or not np.all(np.isfinite(confidence)):
            self.invalid_reliability += 1
            raise Uncorrectable('No valid byte reliability for soft inner RS')
        order = np.argsort(confidence, kind='stable')
        syndrome = syndromes(word)
        filtered, locator, erased = syndrome.copy(), [1], []
        # Reuse the previous trial's erasure filtering instead of rebuilding
        # the same prefixes sixteen times. Trial bounds and results are equal.
        for position in order[:PARITY_BYTES].tolist():
            erased.append(position)
            filtered, locator = _add_erasure(filtered, locator, position)
            count = len(erased)
            if count not in ERASURE_COUNTS:
                continue
            self.patterns += 1
            try:
                message, changed = _decode_filtered(word, erased, syndrome, filtered, locator)
            except Uncorrectable:
                continue
            if zlib.crc32(message[:212]) != int.from_bytes(message[212:], 'little'):
                self.crc_rejected += 1
                continue
            self.soft_decoded += 1
            self.maximum_erasures = max(self.maximum_erasures, count)
            return message, changed
        raise Uncorrectable('No bounded soft inner RS candidate passed the original CRC')

    def snapshot(self):
        return {'inner_rs_soft_enabled': True,
                'inner_rs_max_erasure_bytes': PARITY_BYTES,
                'inner_rs_max_trials_per_frame': MAX_TRIALS,
                'inner_rs_soft_attempt_frames': self.soft_frames,
                'inner_rs_soft_decoded_frames': self.soft_decoded,
                'inner_rs_erasure_trial_attempts': self.patterns,
                'inner_rs_invalid_reliability_frames': self.invalid_reliability,
                'inner_rs_soft_candidate_crc_rejections': self.crc_rejected,
                'inner_rs_max_erasures_used': self.maximum_erasures,
                'inner_rs_soft_candidate_original_crc_required': True}


class SoftInnerRSFrame:
    """Correct QPSK decisions in the equalizer's own worker, before stock RX.

The serializer, descrambler, independent hard RS syndrome check and original
GNU Radio CRC still process the selected symbols. Rejected observations are
left untouched; no synthetic packet or zero-length frame is inserted.
"""
    def __init__(self, scrambler_mask, constellation_points):
        self.mask = np.asarray(scrambler_mask, np.uint8)
        self.points = np.asarray(constellation_points, np.complex64)
        if self.mask.shape != (248,) or self.points.shape != (4,):
            raise ValueError('Expected fixed inner RS mask and four QPSK points')
        signs = (self.points.real > 0).astype(int) | ((self.points.imag > 0).astype(int) << 1)
        if (not np.array_equal(signs, np.arange(4)) or not np.all(np.isfinite(self.points))
                or np.any(self.points.real == 0) or np.any(self.points.imag == 0)):
            raise ValueError('Unsupported GNU Radio QPSK point/bit mapping')
        self.decoder = ReliabilityRSDecoder()
        self.corrected_frames = self.corrected_bytes = self.maximum_corrected = 0
        self.decode_ns_total = self.decode_ns_max = 0

    def decode(self, symbols):
        x = np.asarray(symbols)
        if x.shape != (21, 48) or not np.all(np.isfinite(x)):
            raise ValueError('Expected one finite coded QPSK frame')
        before = time.perf_counter_ns()
        try:
            flat = x.reshape(-1)[:248 * 4]
            indices = (flat.real >= 0).astype(np.uint8) | ((flat.imag >= 0).astype(np.uint8) << 1)
            grouped = indices.reshape(248, 4)
            raw = bytes((grouped[:, 0] | grouped[:, 1] << 2 | grouped[:, 2] << 4 | grouped[:, 3] << 6) ^ self.mask)
            try:
                message, changed = self.decoder.decode(raw, lambda: byte_reliability(x))
            except Uncorrectable:
                return x
            if not changed:
                return x
            selected = np.frombuffer(encode(message), np.uint8) ^ self.mask
            selected_indices = ((selected[:, None] >> np.arange(0, 8, 2)) & 3).reshape(-1)
            affected = np.flatnonzero(selected_indices != indices)
            result = x.copy().reshape(-1)
            result[affected] = self.points[selected_indices[affected]]
            self.corrected_frames += 1
            self.corrected_bytes += changed
            self.maximum_corrected = max(self.maximum_corrected, changed)
            return result.reshape(21, 48)
        finally:
            elapsed = time.perf_counter_ns() - before
            self.decode_ns_total += elapsed
            self.decode_ns_max = max(self.decode_ns_max, elapsed)

    def snapshot(self):
        return {**self.decoder.snapshot(),
                'inner_rs_corrected_frames': self.corrected_frames,
                'inner_rs_corrected_bytes': self.corrected_bytes,
                'inner_rs_max_corrected_bytes_observed': self.maximum_corrected,
                'inner_rs_candidate_decode_wall_seconds': self.decode_ns_total / 1e9,
                'inner_rs_candidate_decode_max_ms': self.decode_ns_max / 1e6,
                'inner_rs_soft_location': 'equalizer_before_independent_RS_and_CRC'}
