# Copied unchanged from OFDM_Video_N210 v1.23 (ofdmvideo). Wire format kept identical.
"""Shortened systematic RS(248,216), roots alpha**0..31 over GF(256).

Protect the complete 212-byte application packet plus its original PHY CRC.
The inner decoder corrects up to sixteen erroneous byte positions, checks all
32 syndromes again, then leaves the original CRC and outer RS checks in place.
There is no TX reference or CRC-guided candidate search in this decoder.
"""
from functools import lru_cache
import numpy as np
from .fec_rs import _EXP, _LOG

MESSAGE_BYTES = 216
PARITY_BYTES = 32
CODE_BYTES = MESSAGE_BYTES + PARITY_BYTES
MAX_ERRORS = PARITY_BYTES // 2


def _mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _div(a, b):
    if not b:
        raise ValueError("Division by zero in RS error evaluator")
    return _EXP[(_LOG[a] - _LOG[b]) % 255] if a else 0


def _eval_ascending(poly, x):
    value = 0
    for coefficient in reversed(poly):
        value = _mul(value, x) ^ coefficient
    return value


@lru_cache(maxsize=1)
def _tables():
    multiplication = np.asarray([[_mul(a, b) for b in range(256)]
                                 for a in range(256)], np.uint8)
    # Generator coefficients in descending degree order.
    generator = [1]
    for i in range(PARITY_BYTES):
        result = [0] * (len(generator) + 1)
        for j, coefficient in enumerate(generator):
            result[j] ^= coefficient
            result[j + 1] ^= _mul(coefficient, _EXP[i])
        generator = result
    updates = tuple(int.from_bytes(bytes(_mul(a, g) for g in generator[1:]), "big")
                    for a in range(256))
    powers = np.asarray([[_EXP[(root * (CODE_BYTES - 1 - j)) % 255]
                          for j in range(CODE_BYTES)] for root in range(PARITY_BYTES)], np.uint8)
    roots = np.asarray([[_EXP[(-power * degree) % 255]
                         for power in range(CODE_BYTES)] for degree in range(MAX_ERRORS + 1)], np.uint8)
    for value in (multiplication, powers, roots):
        value.setflags(write=False)
    return multiplication, updates, powers, roots


def encode(message):
    if len(message) != MESSAGE_BYTES:
        raise ValueError("Inner RS needs exactly 216 CRC-appended bytes")
    updates = _tables()[1]
    state = 0
    mask = (1 << (PARITY_BYTES * 8)) - 1
    for value in message:
        feedback = value ^ (state >> ((PARITY_BYTES - 1) * 8))
        state = ((state << 8) & mask) ^ updates[feedback]
    return bytes(message) + state.to_bytes(PARITY_BYTES, "big")


def syndromes(codeword):
    if len(codeword) != CODE_BYTES:
        raise ValueError("Inner RS codeword must contain 248 bytes")
    multiplication, _, powers, _ = _tables()
    data = np.frombuffer(bytes(codeword), np.uint8)
    return np.bitwise_xor.reduce(multiplication[powers, data[None, :]], axis=1).tolist()


class Uncorrectable(ValueError):
    pass


def decode(codeword):
    """Return (216 corrected bytes, number of corrected byte positions)."""
    if len(codeword) != CODE_BYTES:
        raise ValueError("Inner RS codeword must contain 248 bytes")
    syndrome = syndromes(codeword)
    if not any(syndrome):
        return bytes(codeword[:MESSAGE_BYTES]), 0
    # Berlekamp–Massey; locator coefficients are in ascending degree order.
    locator, previous = [1] + [0] * PARITY_BYTES, [1] + [0] * PARITY_BYTES
    degree, shift, discrepancy_before = 0, 1, 1
    for n in range(PARITY_BYTES):
        discrepancy = syndrome[n]
        for i in range(1, degree + 1):
            discrepancy ^= _mul(locator[i], syndrome[n - i])
        if not discrepancy:
            shift += 1
            continue
        saved = locator.copy()
        factor = _div(discrepancy, discrepancy_before)
        for i in range(PARITY_BYTES + 1 - shift):
            locator[i + shift] ^= _mul(factor, previous[i])
        if 2 * degree <= n:
            degree = n + 1 - degree
            previous, discrepancy_before, shift = saved, discrepancy, 1
        else:
            shift += 1
    if not 1 <= degree <= MAX_ERRORS:
        raise Uncorrectable("Inner RS error locator exceeds sixteen byte positions")
    locator = locator[:degree + 1]
    multiplication, _, _, roots = _tables()
    values = np.bitwise_xor.reduce(multiplication[np.asarray(locator, np.uint8)[:, None],
                                                roots[:degree + 1]], axis=0)
    powers = np.flatnonzero(values == 0).tolist()
    if len(powers) != degree:
        raise Uncorrectable("Inner RS locator roots do not match its degree")
    evaluator = []
    for n in range(degree):
        value = 0
        for i in range(min(n, degree) + 1):
            value ^= _mul(locator[i], syndrome[n - i])
        evaluator.append(value)
    derivative = [locator[i] if i % 2 else 0 for i in range(1, degree + 1)]
    corrected = bytearray(codeword)
    for power in powers:
        x, inverse_x = _EXP[power % 255], _EXP[(-power) % 255]
        numerator = _eval_ascending(evaluator, inverse_x)
        denominator = _eval_ascending(derivative, inverse_x)
        if not denominator:
            raise Uncorrectable("Inner RS error evaluator has a repeated root")
        magnitude = _mul(x, _div(numerator, denominator))
        if not magnitude:
            raise Uncorrectable("Inner RS locator contains a zero-magnitude error")
        corrected[CODE_BYTES - 1 - power] ^= magnitude
    if any(syndromes(corrected)):
        raise Uncorrectable("Inner RS corrected word failed its independent syndrome check")
    return bytes(corrected[:MESSAGE_BYTES]), degree
