# ==================================================
# static_hash_store/store.py   (cross‑platform)
# ==================================================
import mmap, os, struct, hashlib
from pathlib import Path
from typing import Optional

from .const  import *
from .bloom  import Bloom
# ── cross‑platform byte‑range locks ───────────────────────────
import sys
if sys.platform.startswith("win"):
    import msvcrt
    _LKLOCK, _LKUNLOCK = msvcrt.LK_NBLCK, msvcrt.LK_UNLCK
    def _lock(f, n, offset):   msvcrt.locking(f.fileno(), _LKLOCK, n)
    def _unlock(f, n, offset): msvcrt.locking(f.fileno(), _LKUNLOCK, n)
else:
    import fcntl
    def _lock(f, n, offset):   fcntl.lockf(f.fileno(), fcntl.LOCK_EX, n, offset)
    def _unlock(f, n, offset): fcntl.lockf(f.fileno(), fcntl.LOCK_UN, n, offset)
# --------------------------------------------------------------

# ── cross‑platform advisory‑lock helpers ───────────
try:
    import fcntl                                      # Unix / WSL / macOS
    def _lock(f, size, offset):
        fcntl.lockf(f.fileno(), fcntl.LOCK_EX, size, offset)
    def _unlock(f, size, offset):
        fcntl.lockf(f.fileno(), fcntl.LOCK_UN, size, offset)
except ImportError:                                   # native Windows
    # Single‑writer desktop; we can safely NO‑OP.
    def _lock(f, size, offset):   ...
    def _unlock(f, size, offset): ...

# ───────────────────────────────────────────────────

class StaticHashStore:
    """Read/append‑only persistent hash map with mmap sharing."""
    def __init__(self, path: str | os.PathLike,
                 key_size: int = 8,
                 segments: int = 256,
                 bloom_fp: float = 0.01):
        self.path       = Path(path)
        self.key_size   = key_size
        self.segments   = segments
        self.bloom_fp   = bloom_fp

        if self.path.exists():
            self._open_existing()
        else:
            self._create_new()

    # ------------------------------------------------------------------
    def _create_new(self):
        with open(self.path, "wb") as f:
            bloom_bits = 1                                       # 1 byte
            header = struct.pack(HEADER_FMT, MAGIC, VERSION_MINOR,
                                 self.key_size, self.segments, bloom_bits)
            f.write(header.ljust(HEADER_SIZE, b"\0"))
            f.write(b"\0" * (BUCKET_SIZE * self.segments))
            f.write(b"\0" * bloom_bits)                          # empty bloom
        self.file = open(self.path, "r+b")
        self.mm   = mmap.mmap(self.file.fileno(), 0)
        self.bloom = Bloom(1, self.bloom_fp)  # start with 1 dummy item

        self._header_dirty  = True

    def _open_existing(self):
        self.file = open(self.path, "r+b")
        self.mm   = mmap.mmap(self.file.fileno(), 0)
        magic, ver, key_sz, seg_cnt, bloom_bits = struct.unpack_from(
            HEADER_FMT, self.mm, 0)
        if magic != MAGIC:
            raise ValueError("Invalid store file")
        if key_sz != self.key_size:
            raise ValueError("Key size mismatch")
        self.segments = seg_cnt
        bloom_off     = HEADER_SIZE + BUCKET_SIZE * self.segments
        bloom_raw     = self.mm[bloom_off:bloom_off + bloom_bits]
        self.bloom    = Bloom.from_bytes(1, self.bloom_fp, bloom_raw)
        self._header_dirty = False

    # ------------------------------------------------------------------
    def _segment_of(self, h: int) -> int:
        return h % self.segments

    def _bucket_offset(self, segment: int) -> int:
        return HEADER_SIZE + segment * BUCKET_SIZE

    # ------------------------------------------------------------------
    def get(self, key: bytes) -> Optional[bytes]:
        h_int = struct.unpack("<Q", hashlib.blake2b(key, digest_size=8).digest())[0]
        if key not in self.bloom:
            return None
        seg           = self._segment_of(h_int)
        bucket_ptr_of = self._bucket_offset(seg)
        entry_of      = struct.unpack_from(BUCKET_FMT, self.mm, bucket_ptr_of)[0]
        while entry_of:
            nxt, e_hash, val_sz = struct.unpack_from(ENTRY_HDR_FMT, self.mm, entry_of)
            key_off = entry_of + ENTRY_HDR_SIZE
            if e_hash == h_int and self.mm[key_off:key_off + self.key_size] == key:
                val_off = key_off + self.key_size
                return bytes(self.mm[val_off: val_off + val_sz])
            entry_of = nxt
        return None

    # ------------------------------------------------------------------
    def put(self, key: bytes, value):
        """
        Append‑only insert.
        • key must already be exactly `self.key_size` bytes.
        • value can be either bytes *or* str (str is UTF‑8‑encoded here).
        Thread‑/process‑safe: bucket region is locked during the write.
        """
        if len(key) != self.key_size:
            raise ValueError("Key length mismatch")

        # ← NEW: tolerate str payloads
        if isinstance(value, str):
            value = value.encode("utf‑8")

        h_int = struct.unpack("<Q", hashlib.blake2b(key, digest_size=8).digest())[0]
        seg = self._segment_of(h_int)
        bucket_ptr_off = self._bucket_offset(seg)

        # ── platform‑neutral advisory lock on the bucket pointer ─────────
        _lock(self.file, BUCKET_SIZE, bucket_ptr_off)
        try:
            bucket_head = struct.unpack_from(BUCKET_FMT, self.mm, bucket_ptr_off)[0]

            eof = self.mm.size()
            entry = (
                    struct.pack(ENTRY_HDR_FMT, bucket_head, h_int, len(value))
                    + key + value
            )

            self.mm.resize(eof + len(entry))
            self.mm[eof: eof + len(entry)] = entry

            # update bucket head to new entry
            struct.pack_into(BUCKET_FMT, self.mm, bucket_ptr_off, eof)

            # bloom
            self.bloom.add(key)
            self._header_dirty = True
        finally:
            _unlock(self.file, BUCKET_SIZE, bucket_ptr_off)

    # ------------------------------------------------------------------
    def flush(self):
        if self._header_dirty:
            bloom_bits = len(self.bloom.bits)
            bloom_off  = HEADER_SIZE + BUCKET_SIZE * self.segments
            if self.mm.size() < bloom_off + bloom_bits:
                self.mm.resize(bloom_off + bloom_bits)
            self.mm[bloom_off:bloom_off + bloom_bits] = self.bloom.bits
            header = struct.pack(HEADER_FMT, MAGIC, VERSION_MINOR,
                                 self.key_size, self.segments, bloom_bits)
            self.mm[0:len(header)] = header
            self._header_dirty = False
        self.mm.flush()

    # ------------------------------------------------------------------
    def close(self):
        self.flush()
        self.mm.close()
        self.file.close()
