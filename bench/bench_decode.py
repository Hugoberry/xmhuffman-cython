"""Benchmark xmhuffman.decode_page vs pbixray.huffman.decode_substrings.

Usage:
    python3 bench/bench_decode.py [path/to/file.pbix]

If no path is given, defaults to a small sample under
~/git/hub/pbixray/data.
"""
import os
import sys
import time

_PBIXRAY_PATH = os.path.expanduser("~/git/hub/pbixray")
if os.path.isdir(_PBIXRAY_PATH) and _PBIXRAY_PATH not in sys.path:
    sys.path.insert(0, _PBIXRAY_PATH)

import pbixray.huffman as pbix_huffman
import pbixray.vertipaq_decoder as vd
from pbixray.core import PBIXRay

import xmhuffman


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _PBIXRAY_PATH, "data", "Adventure Works DW 2020.pbix")
    if not os.path.isfile(path):
        sys.exit(f"file not found: {path}")
    print(f"file: {path}")

    captured = []
    orig = pbix_huffman.decode_substrings

    def spy(swapped, table, max_len, offsets, store_total_bits):
        captured.append((bytes(swapped), table, max_len, list(offsets),
                         int(store_total_bits)))
        return orig(swapped, table, max_len, offsets, store_total_bits)

    pbix_huffman.decode_substrings = spy
    vd.decode_substrings = spy
    try:
        px = PBIXRay(path)
        for tname in list(px.tables):
            try:
                px.get_table(tname)
            except Exception:
                pass
    finally:
        pbix_huffman.decode_substrings = orig
        vd.decode_substrings = orig

    if not captured:
        sys.exit("no compressed Huffman pages in this file")

    print(f"captured {len(captured)} pages, "
          f"total {sum(len(c[3]) for c in captured)} strings")

    # Time the Python reference.
    t0 = time.perf_counter()
    for swapped, table, max_len, offsets, total in captured:
        orig(swapped, table, max_len, offsets, total)
    t_py = time.perf_counter() - t0

    # Time the C kernel via decode_with_table (already-swapped path).
    import struct
    packed_tables = []
    for _, table, max_len, _, _ in captured:
        n = 1 << max_len
        buf = bytearray(n * 2)
        for idx in range(n):
            sym, L = table[idx]
            struct.pack_into("<H", buf, idx * 2, (sym << 8) | (L & 0xff))
        packed_tables.append(bytes(buf))

    t0 = time.perf_counter()
    for (swapped, _, max_len, offsets, total), packed in zip(captured, packed_tables):
        xmhuffman.decode_with_table(swapped, packed, max_len, offsets, total,
                                    swap=False)
    t_c = time.perf_counter() - t0

    print(f"python decode_substrings: {t_py * 1000:8.2f} ms")
    print(f"xmhuffman.decode_with_table: {t_c * 1000:8.2f} ms")
    print(f"speedup: {t_py / t_c:.1f}x")


if __name__ == "__main__":
    main()
