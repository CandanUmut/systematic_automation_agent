# ==================================================
# static_hash_store/const.py
# ==================================================
MAGIC = b"SHS1"          # 4‑byte magic + version major «1»
HEADER_FMT = "<4sHHLQ"    # magic, version_minor (H), key_size (H), segment_count (L), bloom_bits (Q)
HEADER_SIZE = 24          # bytes (4+2+2+4+8+padding)
BUCKET_FMT = "<Q"         # 8‑byte offset of first entry in segment (0 = empty)
BUCKET_SIZE = 8
ENTRY_HDR_FMT = "<QQL"    # next_offset, key_hash, value_size
ENTRY_HDR_SIZE = 8+8+4    # 20 bytes (pad to 24 for alignment)
VERSION_MINOR = 0
