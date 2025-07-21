(# ==================================================
# static_hash_store/compression.py
# ==================================================
import zstandard as zstd
import struct

# -------- Delta‑encoding helpers -----------------------------------------

def delta_encode(sorted_ints:list[int]) -> bytes:

    out = bytearray()
    prev = 0
    for n in sorted_ints:
        delta = n - prev
        prev = n
        while True:
            byte = delta & 0x7F
            delta >>= 7
            if delta:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                break
    return bytes(out)

# -------- zstd wrappers ---------------------------------------------------

cctx = zstd.ZstdCompressor(level=3)
dctx = zstd.ZstdDecompressor()

def compress(data:bytes) -> bytes:
    return cctx.compress(data)

def decompress(data:bytes) -> bytes:
    return dctx.decompress(data)