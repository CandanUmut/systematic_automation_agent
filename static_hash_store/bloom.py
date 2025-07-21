# ==================================================
# static_hash_store/bloom.py
# ==================================================
from math import log, ceil
from hashlib import blake2b
import struct

class Bloom:
    def __init__(self, n_items: int, fp_rate: float = 0.01, bits: bytearray | None = None):
        n_items = max(1, n_items)  # ← prevent ÷0
        m = ceil(-(n_items * log(fp_rate)) / (log(2) ** 2))
        k = ceil((m / n_items) * log(2))

        self.m = m
        self.k = k
        self.bits = bits if bits is not None else bytearray((m+7)//8)
    # -- hashing helpers ---------------------------------------------------
    def _hashes(self, key:bytes):
        h = blake2b(key, digest_size=16).digest()
        h1, h2 = struct.unpack("<QQ", h)
        for i in range(self.k):
            yield (h1 + i * h2) % self.m
    # ----------------------------------------------------------------------
    def add(self, key: bytes):
        for bit in self._hashes(key):
            byte_i  = bit // 8
            bit_mask = 1 << (bit & 7)

            # ── NEW: grow the bytearray on demand ──────────────
            if byte_i >= len(self.bits):           # happens when store grows
                need = byte_i + 1 - len(self.bits)
                self.bits.extend(b"\0" * need)
                # keep internal m in‑sync
                self.m = len(self.bits) * 8

            self.bits[byte_i] |= bit_mask
    def __contains__(self, key:bytes):
        return all(self.bits[bit//8] & (1 << (bit & 7)) for bit in self._hashes(key))
    @classmethod
    def from_bytes(cls, n_items:int, fp_rate:float, data:bytes):
        bloom = cls(n_items, fp_rate, bytearray(data))
        return bloom