"""IEEE 802.11n QC-LDPC codes (n = 648 / 1296 / 1944, rates 1/2, 2/3, 3/4, 5/6), numpy.

Base (exponent) matrices: IEEE Std 802.11-2012 Annex F (HT LDPC), 24 block columns, lifting Z = n/24
(27 / 54 / 81); entry s = identity cyclically shifted right by s ("-" = zero block). Transcribed from
github.com/simgunz/802.11n-ldpc (protoH/*), checked against the standard's structure at import:
the parity part is the 802.11n quasi-lower-triangular one (first parity column with shifts a, 0, a;
the rest dual-diagonal), which the encoder below relies on.

Encoder: p0 = sum of all lambda_i (lambda = info part of each block row), then the dual diagonal
gives p1 .. p_{mb-1} recursively (no generator matrix).
Decoder: layered normalised min-sum (one block row = one layer, alpha 0.75), many codewords at once
(numpy over (codewords, edges, Z)), early stop per codeword when the syndrome is zero.

Packets (PacketCoder): one 212-byte OVN1 packet + CRC32 = 1728 info bits, split over m codewords of
one code (shortened: the unused info bits are known zeros, not sent) and, when a frame has less room,
punctured (parity bits spread over the dual-diagonal part are not sent).
"""
from functools import lru_cache

import numpy as np

RATES = ("1/2", "2/3", "3/4", "5/6")
LENGTHS = (648, 1296, 1944)
INFO_BITS = 216 * 8                      # 212-byte application packet + CRC32 (same as RS(248,216))

_BASE = {
    (648, "1/2"): """
        0 - - - 0 0 - - 0 - - 0 1 0 - - - - - - - - - -
        22 0 - - 17 - 0 0 12 - - - - 0 0 - - - - - - - - -
        6 - 0 - 10 - - - 24 - 0 - - - 0 0 - - - - - - - -
        2 - - 0 20 - - - 25 0 - - - - - 0 0 - - - - - - -
        23 - - - 3 - - - 0 - 9 11 - - - - 0 0 - - - - - -
        24 - 23 1 17 - 3 - 10 - - - - - - - - 0 0 - - - - -
        25 - - - 8 - - - 7 18 - - 0 - - - - - 0 0 - - - -
        13 24 - - 0 - 8 - 6 - - - - - - - - - - 0 0 - - -
        7 20 - 16 22 10 - - 23 - - - - - - - - - - - 0 0 - -
        11 - - - 19 - - - 13 - 3 17 - - - - - - - - - 0 0 -
        25 - 8 - 23 18 - 14 9 - - - - - - - - - - - - - 0 0
        3 - - - 16 - - 2 25 5 - - 1 - - - - - - - - - - 0
        """,
    (648, "2/3"): """
        25 26 14 - 20 - 2 - 4 - - 8 - 16 - 18 1 0 - - - - - -
        10 9 15 11 - 0 - 1 - - 18 - 8 - 10 - - 0 0 - - - - -
        16 2 20 26 21 - 6 - 1 26 - 7 - - - - - - 0 0 - - - -
        10 13 5 0 - 3 - 7 - - 26 - - 13 - 16 - - - 0 0 - - -
        23 14 24 - 12 - 19 - 17 - - - 20 - 21 - 0 - - - 0 0 - -
        6 22 9 20 - 25 - 17 - 8 - 14 - 18 - - - - - - - 0 0 -
        14 23 21 11 20 - 24 - 18 - 19 - - - - 22 - - - - - - 0 0
        17 11 11 20 - 21 - 26 - 3 - - 18 - 26 - 1 - - - - - - 0
        """,
    (648, "3/4"): """
        16 17 22 24 9 3 14 - 4 2 7 - 26 - 2 - 21 - 1 0 - - - -
        25 12 12 3 3 26 6 21 - 15 22 - 15 - 4 - - 16 - 0 0 - - -
        25 18 26 16 22 23 9 - 0 - 4 - 4 - 8 23 11 - - - 0 0 - -
        9 7 0 1 17 - - 7 3 - 3 23 - 16 - - 21 - 0 - - 0 0 -
        24 5 26 7 1 - - 15 24 15 - 8 - 13 - 13 - 11 - - - - 0 0
        2 2 19 14 24 1 15 19 - 21 - 2 - 24 - 3 - 2 1 - - - - 0
        """,
    (648, "5/6"): """
        17 13 8 21 9 3 18 12 10 0 4 15 19 2 5 10 26 19 13 13 1 0 - -
        3 12 11 14 11 25 5 18 0 9 2 26 26 10 24 7 14 20 4 2 - 0 0 -
        22 16 4 3 10 21 12 5 21 14 19 5 - 8 5 18 11 5 5 15 0 - 0 0
        7 7 14 14 4 16 16 24 24 10 1 7 15 6 10 26 8 18 21 14 1 - - 0
        """,
    (1296, "1/2"): """
        40 - - - 22 - 49 23 43 - - - 1 0 - - - - - - - - - -
        50 1 - - 48 35 - - 13 - 30 - - 0 0 - - - - - - - - -
        39 50 - - 4 - 2 - - - - 49 - - 0 0 - - - - - - - -
        33 - - 38 37 - - 4 1 - - - - - - 0 0 - - - - - - -
        45 - - - 0 22 - - 20 42 - - - - - - 0 0 - - - - - -
        51 - - 48 35 - - - 44 - 18 - - - - - - 0 0 - - - - -
        47 11 - - - 17 - - 51 - - - 0 - - - - - 0 0 - - - -
        5 - 25 - 6 - 45 - 13 40 - - - - - - - - - 0 0 - - -
        33 - - 34 24 - - - 23 - - 46 - - - - - - - - 0 0 - -
        1 - 27 - 1 - - - 38 - 44 - - - - - - - - - - 0 0 -
        - 18 - - 23 - - 8 0 35 - - - - - - - - - - - - 0 0
        49 - 17 - 30 - - - 34 - - 19 1 - - - - - - - - - - 0
        """,
    (1296, "2/3"): """
        39 31 22 43 - 40 4 - 11 - - 50 - - - 6 1 0 - - - - - -
        25 52 41 2 6 - 14 - 34 - - - 24 - 37 - - 0 0 - - - - -
        43 31 29 0 21 - 28 - - 2 - - 7 - 17 - - - 0 0 - - - -
        20 33 48 - 4 13 - 26 - - 22 - - 46 42 - - - - 0 0 - - -
        45 7 18 51 12 25 - - - 50 - - 5 - - - 0 - - - 0 0 - -
        35 40 32 16 5 - - 18 - - 43 51 - 32 - - - - - - - 0 0 -
        9 24 13 22 28 - - 37 - - 25 - - 52 - 13 - - - - - - 0 0
        32 22 4 21 16 - - - 27 28 - 38 - - - 8 1 - - - - - - 0
        """,
    (1296, "3/4"): """
        39 40 51 41 3 29 8 36 - 14 - 6 - 33 - 11 - 4 1 0 - - - -
        48 21 47 9 48 35 51 - 38 - 28 - 34 - 50 - 50 - - 0 0 - - -
        30 39 28 42 50 39 5 17 - 6 - 18 - 20 - 15 - 40 - - 0 0 - -
        29 0 1 43 36 30 47 - 49 - 47 - 3 - 35 - 34 - 0 - - 0 0 -
        1 32 11 23 10 44 12 7 - 48 - 4 - 9 - 17 - 16 - - - - 0 0
        13 7 15 47 23 16 47 - 43 - 29 - 52 - 2 - 53 - 1 - - - - 0
        """,
    (1296, "5/6"): """
        48 29 37 52 2 16 6 14 53 31 34 5 18 42 53 31 45 - 46 52 1 0 - -
        17 4 30 7 43 11 24 6 14 21 6 39 17 40 47 7 15 41 19 - - 0 0 -
        7 2 51 31 46 23 16 11 53 40 10 7 46 53 33 35 - 25 35 38 0 - 0 0
        19 48 41 1 10 7 36 47 5 29 52 52 31 10 26 6 3 2 - 51 1 - - 0
        """,
    (1944, "1/2"): """
        57 - - - 50 - 11 - 50 - 79 - 1 0 - - - - - - - - - -
        3 - 28 - 0 - - - 55 7 - - - 0 0 - - - - - - - - -
        30 - - - 24 37 - - 56 14 - - - - 0 0 - - - - - - - -
        62 53 - - 53 - - 3 35 - - - - - - 0 0 - - - - - - -
        40 - - 20 66 - - 22 28 - - - - - - - 0 0 - - - - - -
        0 - - - 8 - 42 - 50 - - 8 - - - - - 0 0 - - - - -
        69 79 79 - - - 56 - 52 - - - 0 - - - - - 0 0 - - - -
        65 - - - 38 57 - - 72 - 27 - - - - - - - - 0 0 - - -
        64 - - - 14 52 - - 30 - - 32 - - - - - - - - 0 0 - -
        - 45 - 70 0 - - - 77 9 - - - - - - - - - - - 0 0 -
        2 56 - 57 35 - - - - - 12 - - - - - - - - - - - 0 0
        24 - 61 - 60 - - 27 51 - - 16 1 - - - - - - - - - - 0
        """,
    (1944, "2/3"): """
        61 75 4 63 56 - - - - - - 8 - 2 17 25 1 0 - - - - - -
        56 74 77 20 - - - 64 24 4 67 - 7 - - - - 0 0 - - - - -
        28 21 68 10 7 14 65 - - - 23 - - - 75 - - - 0 0 - - - -
        48 38 43 78 76 - - - - 5 36 - 15 72 - - - - - 0 0 - - -
        40 2 53 25 - 52 62 - 20 - - 44 - - - - 0 - - - 0 0 - -
        69 23 64 10 22 - 21 - - - - - 68 23 29 - - - - - - 0 0 -
        12 0 68 20 55 61 - 40 - - - 52 - - - 44 - - - - - - 0 0
        58 8 34 64 78 - - 11 78 24 - - - - - 58 1 - - - - - - 0
        """,
    (1944, "3/4"): """
        48 29 28 39 9 61 - - - 63 45 80 - - - 37 32 22 1 0 - - - -
        4 49 42 48 11 30 - - - 49 17 41 37 15 - 54 - - - 0 0 - - -
        35 76 78 51 37 35 21 - 17 64 - - - 59 7 - - 32 - - 0 0 - -
        9 65 44 9 54 56 73 34 42 - - - 35 - - - 46 39 0 - - 0 0 -
        3 62 7 80 68 26 - 80 55 - 36 - 26 - 9 - 72 - - - - - 0 0
        26 75 33 21 69 59 3 38 - - - 35 - 62 36 26 - - 1 - - - - 0
        """,
    (1944, "5/6"): """
        13 48 80 66 4 74 7 30 76 52 37 60 - 49 73 31 74 73 23 - 1 0 - -
        69 63 74 56 64 77 57 65 6 16 51 - 64 - 68 9 48 62 54 27 - 0 0 -
        51 15 0 80 24 25 42 54 44 71 71 9 67 35 - 58 - 29 - 53 0 - 0 0
        16 29 36 41 44 56 59 37 50 24 - 65 4 65 52 - 4 - 73 52 1 - - 0
        """,
}


def base_matrix(n, rate):
    rows = [[-1 if v == "-" else int(v) for v in line.split()] for line in _BASE[(n, rate)].strip().splitlines()]
    return np.array(rows, int)


class LdpcCode:
    def __init__(self, n, rate):
        if (n, rate) not in _BASE:
            raise ValueError(f"no 802.11n LDPC code n={n} rate={rate}")
        B = base_matrix(n, rate)
        self.n, self.rate, self.Z = n, rate, n // 24
        self.mb, nb = B.shape
        self.kb = nb - self.mb
        self.k = self.kb * self.Z
        num, den = map(int, rate.split("/"))
        if nb != 24 or self.k * den != n * num or B.max() >= self.Z:
            raise ValueError(f"bad base matrix n={n} rate={rate}")
        # 802.11n parity structure: column kb has shifts a (row 0), 0 (one middle row), a (last row)
        col = B[:, self.kb]
        rows = np.flatnonzero(col >= 0)
        if len(rows) != 3 or rows[0] != 0 or rows[2] != self.mb - 1 or col[0] != col[-1] or col[rows[1]]:
            raise ValueError(f"parity column of n={n} rate={rate} is not 802.11n")
        self.a, self.mid = int(col[0]), int(rows[1])
        for j in range(1, self.mb):                     # dual diagonal, nothing else
            c = B[:, self.kb + j]
            if set(np.flatnonzero(c >= 0)) != {j - 1, j} or c[j - 1] or c[j]:
                raise ValueError(f"parity part of n={n} rate={rate} is not dual-diagonal")
        self.B = B
        Z, t = self.Z, np.arange(self.Z)
        # layer i: check t is connected to bit col*Z + (t + s) % Z of every non-empty block
        self.idx, self.info_idx = [], []
        for i in range(self.mb):
            cols = np.flatnonzero(B[i] >= 0)
            idx = cols[:, None] * Z + (t[None, :] + B[i, cols][:, None]) % Z
            self.idx.append(idx)
            self.info_idx.append(idx[cols < self.kb])

    # -- encoder ------------------------------------------------------
    def encode(self, u):
        """u: (P, k) bits -> (P, n) codewords."""
        u = np.asarray(u, np.uint8).reshape(-1, self.k)
        lam = np.stack([np.bitwise_xor.reduce(u[:, ix], axis=1) for ix in self.info_idx], axis=1)  # (P, mb, Z)
        p = np.empty_like(lam)
        p[:, 0] = np.bitwise_xor.reduce(lam, axis=1)
        p[:, 1] = lam[:, 0] ^ np.roll(p[:, 0], -self.a, axis=-1)
        for i in range(1, self.mb - 1):
            p[:, i + 1] = lam[:, i] ^ p[:, i] ^ (p[:, 0] if i == self.mid else 0)
        return np.concatenate([u, p.reshape(len(u), -1)], axis=1)

    def syndrome_ok(self, c):
        """c: (P, n) hard bits -> (P,) True where every parity check holds."""
        c = np.asarray(c, np.uint8)
        bad = np.zeros(len(c), bool)
        for ix in self.idx:
            bad |= np.bitwise_xor.reduce(c[:, ix], axis=1).any(axis=1)
        return ~bad

    # -- decoder ------------------------------------------------------
    def decode(self, llr, max_iter=20, alpha=0.75):
        """llr: (P, n), positive = bit 0 (+inf allowed for known bits). Returns (bits (P, n) uint8,
        ok (P,) syndrome zero, iterations (P,))."""
        L = np.array(llr, np.float32).reshape(-1, self.n)
        L = np.clip(L, -1e4, 1e4)
        P = len(L)
        bits = (L < 0).astype(np.uint8)
        ok = self.syndrome_ok(bits)
        iters = np.zeros(P, int)
        act = np.flatnonzero(~ok)
        if act.size == 0:
            return bits, ok, iters
        La = L[act]
        R = [np.zeros((act.size,) + ix.shape, np.float32) for ix in self.idx]
        for it in range(1, max_iter + 1):
            for i, ix in enumerate(self.idx):
                Q = La[:, ix] - R[i]                               # (A, deg, Z)
                mag = np.abs(Q)
                m1 = mag.min(axis=1, keepdims=True)                # smallest and second smallest |Q|
                at = mag <= m1
                m2 = np.where(at, np.float32(np.inf), mag).min(axis=1, keepdims=True)
                m2 = np.where(at.sum(axis=1, keepdims=True) > 1, m1, m2)
                neg = Q < 0
                par = np.logical_xor.reduce(neg, axis=1, keepdims=True)
                r = np.float32(alpha) * np.where(at, m2, m1)
                r = np.where(neg ^ par, -r, r)
                La[:, ix] = Q + r
                R[i] = r
            hb = (La < 0).astype(np.uint8)
            done = self.syndrome_ok(hb)
            if done.any() or it == max_iter:
                fin = done if it < max_iter else np.ones(act.size, bool)
                bits[act[fin]] = hb[fin]
                ok[act[fin]] = done[fin]
                iters[act[fin]] = it
                keep = ~fin
                act, La = act[keep], La[keep]
                R = [r_[keep] for r_ in R]
                if act.size == 0:
                    break
        return bits, ok, iters


@lru_cache(maxsize=None)
def code(n, rate):
    return LdpcCode(n, rate)


def _natural_layout(rate):
    """(n, m): the code length and number of codewords per packet with the fewest coded bits
    (ties: the longer code)."""
    best = None
    for n in sorted(LENGTHS, reverse=True):
        c = code(n, rate)
        m = -(-INFO_BITS // c.k)
        bits = m * (n - c.k) + INFO_BITS
        if best is None or bits < best[0]:
            best = (bits, n, m)
    return best[1], best[2]


class PacketCoder:
    """1728 info bits <-> `bits` coded bits (m shortened, possibly punctured codewords)."""

    def __init__(self, rate, bits=None):
        self.rate = rate
        n, m = _natural_layout(rate)
        self.code = c = code(n, rate)
        self.m = m
        self.info = [INFO_BITS // m + (i < INFO_BITS % m) for i in range(m)]        # info bits per codeword
        natural = INFO_BITS + m * (n - c.k)
        self.bits = natural if bits is None else int(bits)
        punct = natural - self.bits
        if punct < 0 or punct > m * (n - c.k - c.Z) // 2:
            raise ValueError(f"LDPC rate {rate}: cannot fit a packet in {self.bits} bits")
        # per codeword: positions sent = info[:kept] + parity minus punctured; punctured bits are spread
        # over the dual-diagonal parity blocks (never the first parity column p0)
        self.sent = []
        for i in range(m):
            pi = punct // m + (i < punct % m)
            par = np.arange(c.k + c.Z, n)
            drop = par[np.round(np.linspace(0, par.size - 1, pi)).astype(int)] if pi else np.array([], int)
            keep = np.setdiff1d(np.concatenate([np.arange(self.info[i]), np.arange(c.k, n)]), drop)
            self.sent.append(keep)
        assert sum(len(s) for s in self.sent) == self.bits
        # first sent position of each codeword (its info bits come first, in order)
        self._starts = np.cumsum([0] + [len(s) for s in self.sent[:-1]]).tolist()

    @property
    def efficiency(self):
        return INFO_BITS / self.bits

    def encode(self, info_bits):
        """info_bits: (P, 1728) -> (P, bits)."""
        info_bits = np.asarray(info_bits, np.uint8).reshape(-1, INFO_BITS)
        P, c = len(info_bits), self.code
        out, pos = [], 0
        for i in range(self.m):
            u = np.zeros((P, c.k), np.uint8)
            u[:, :self.info[i]] = info_bits[:, pos:pos + self.info[i]]
            pos += self.info[i]
            out.append(c.encode(u)[:, self.sent[i]])
        return np.concatenate(out, axis=1)

    def decode(self, llr, max_iter=20):
        """llr: (P, bits) -> (info bits (P, 1728), all codewords ok (P,), iterations (P, m)).

        First a re-encoding check: packets whose hard decisions already form codewords (punctured bits
        recomputed) need no iteration; only the others go through min-sum."""
        llr = np.asarray(llr, np.float32).reshape(-1, self.bits)
        P, c = len(llr), self.code
        hard = (llr < 0).astype(np.uint8)
        info_h = np.concatenate([hard[:, s0:s0 + k] for s0, k in zip(self._starts, self.info)], axis=1)
        clean = (self.encode(info_h) == hard).all(axis=1)
        if clean.all():
            return info_h, clean, np.zeros((P, self.m), int)
        info_out, ok_out, it_out = info_h.copy(), clean.copy(), np.zeros((P, self.m), int)
        todo = np.flatnonzero(~clean)
        info_t, ok_t, it_t = self._iterate(llr[todo], max_iter)
        info_out[todo], ok_out[todo], it_out[todo] = info_t, ok_t, it_t
        return info_out, ok_out, it_out

    def _iterate(self, llr, max_iter):
        P, c = len(llr), self.code
        full = np.zeros((P, self.m, c.n), np.float32)
        pos = 0
        for i in range(self.m):
            full[:, i, :c.k] = 1e4                         # shortened info bits: known zeros
            full[:, i, self.sent[i]] = llr[:, pos:pos + len(self.sent[i])]   # punctured bits stay 0
            pos += len(self.sent[i])
        bits, ok, it = c.decode(full.reshape(P * self.m, c.n), max_iter)
        bits, ok, it = bits.reshape(P, self.m, c.n), ok.reshape(P, self.m), it.reshape(P, self.m)
        info = np.concatenate([bits[:, i, :self.info[i]] for i in range(self.m)], axis=1)
        return info, ok.all(axis=1), it
