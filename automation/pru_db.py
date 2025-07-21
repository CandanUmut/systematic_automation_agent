# automation/pru_db.py   – thin helper around StaticHashStore
from __future__ import annotations
from pathlib import Path
import base64, binascii, xxhash          # pip install xxhash
from static_hash_store import StaticHashStore

PRU_PATH  = Path("pru.shs")
_store    = StaticHashStore(PRU_PATH, key_size=8, bloom_fp=0.01)

# ── small utils ──────────────────────────────────────────────
def _h(selector: str) -> bytes:                  # 8‑byte key
    return xxhash.xxh64(selector).digest()       # fast & stable

def _bytes(data: str | bytes) -> bytes:          # str → b64/utf‑8
    if isinstance(data, bytes):
        return data
    try:                     # first try base‑64 text
        return base64.b64decode(data, validate=True)
    except binascii.Error:   # fall back to utf‑8
        return data.encode()

# ── public api ───────────────────────────────────────────────
def put(selector: str, html: str | bytes):
    """Store raw HTML (or already‑encoded bytes) for `selector`."""
    _store.put(_h(selector), _bytes(html))
    _store.flush()                               # immediate durability

def get(selector: str) -> bytes | None:
    """Return bytes previously stored for `selector`; None if absent."""
    return _store.get(_h(selector))

def close():
    _store.close()
