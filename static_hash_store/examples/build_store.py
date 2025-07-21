# ==================================================
# examples/build_store.py
# ==================================================
import struct, argparse, random
from static_hash_store import StaticHashStore

def main():
    p = argparse.ArgumentParser()
    p.add_argument("store", help="path to shs file")
    p.add_argument("count", type=int)
    args = p.parse_args()

    shs = StaticHashStore(args.store, key_size=8)
    pack = struct.Struct("<Q").pack
    for i in range(args.count):
        k = pack(i)
        v = f"value_{i}".encode()
        shs.put(k, v)
    shs.close()

if __name__ == "__main__":
    main()