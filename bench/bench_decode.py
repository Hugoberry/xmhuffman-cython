"""Benchmark xmhuffman decode + UTF-16 stringification against a real pbix.

Captures every ``xmhuffman.decode_page`` call pbixray makes when loading
a file, then re-runs each captured page through two strategies:

  1. ``decode_page(...)`` + Python ``.decode()`` per pbixray's current
     glue: ``b.decode('latin-1')`` for single, ``b.decode('utf-16-le')``
     for general.
  2. ``decode_page(..., charset_mode=..., charset_byte=...)`` + a
     uniform ``b.decode('utf-16-le')`` for both modes. This is the
     spec-faithful path; behavior matches (1) when ``charset_byte == 0``
     and is correct (1 is silently wrong) when ``charset_byte != 0``.

Usage:
    python3 bench/bench_decode.py [path/to/file.pbix]
"""
import os
import sys
import time

import numpy as np

_PBIXRAY_PATH = os.path.expanduser("~/git/hub/pbixray")
if os.path.isdir(_PBIXRAY_PATH) and _PBIXRAY_PATH not in sys.path:
    sys.path.insert(0, _PBIXRAY_PATH)

import xmhuffman
from pbixray.core import PBIXRay
import pbixray.vertipaq_decoder as vd


_HUFFMAN_GENERAL = getattr(vd, "_HUFFMAN_GENERAL", 0xABA92)


def _capture_pages(path):
    """Return a list of dicts describing every compressed page pbixray hit.

    Recovers per-page ``charset_type`` / ``charset_used`` by peeking at
    the caller frame's ``compressed_store`` local (pbixray's
    ``_read_dictionary`` exposes it under that name).
    """
    captured = []
    orig_decode_page = xmhuffman.decode_page

    def spy(bitstream, encode_array, offsets, total_bits,
            swap=True, charset_mode='general', charset_byte=0):
        caller_locals = sys._getframe(1).f_locals
        store = caller_locals.get("compressed_store")
        if store is not None:
            cs_type = int(getattr(store, "character_set_type_identifier",
                                  _HUFFMAN_GENERAL))
            cs_byte = int(getattr(store, "character_set_used", 0)) & 0xFF
        else:
            cs_type = _HUFFMAN_GENERAL
            cs_byte = 0
        mode = "general" if cs_type == _HUFFMAN_GENERAL else "single"
        captured.append({
            "bitstream": bytes(bitstream),
            "encode_array": bytes(encode_array),
            "offsets": np.asarray(list(offsets), dtype=np.uint32),
            "total_bits": int(total_bits),
            "swap": bool(swap),
            "charset_mode": mode,
            "charset_byte": cs_byte,
        })
        return orig_decode_page(bitstream, encode_array, offsets, total_bits,
                                swap=swap, charset_mode=charset_mode,
                                charset_byte=charset_byte)

    xmhuffman.decode_page = spy
    vd.xmhuffman.decode_page = spy
    try:
        px = PBIXRay(path)
        for tname in list(px.tables):
            try:
                px.get_table(tname)
            except Exception:
                pass
    finally:
        xmhuffman.decode_page = orig_decode_page
        vd.xmhuffman.decode_page = orig_decode_page

    return captured


def _bench(label, fn, n_runs=3):
    times = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    best = min(times)
    print(f"  {label:54s} {best * 1000:8.2f} ms  "
          f"(of {n_runs}: {[f'{t*1000:.1f}' for t in times]})")
    return best


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _PBIXRAY_PATH, "data", "Adventure Works DW 2020.pbix")
    if not os.path.isfile(path):
        sys.exit(f"file not found: {path}")
    print(f"file: {path}")

    captured = _capture_pages(path)
    if not captured:
        sys.exit("no compressed Huffman pages in this file")

    n_strings = sum(len(c["offsets"]) for c in captured)
    n_single = sum(1 for c in captured if c["charset_mode"] == "single")
    n_general = len(captured) - n_single
    nonzero_cb = sum(1 for c in captured
                     if c["charset_mode"] == "single"
                     and c["charset_byte"] != 0)
    print(f"captured {len(captured)} pages "
          f"({n_single} single, {n_general} general; "
          f"{nonzero_cb} non-zero charset_byte), "
          f"{n_strings} strings total")
    print()

    def run_pbixray_glue():
        for p in captured:
            decoded = xmhuffman.decode_page(
                p["bitstream"], p["encode_array"], p["offsets"],
                p["total_bits"], swap=p["swap"])
            if p["charset_mode"] == "general":
                for b in decoded:
                    b[:len(b) & ~1].decode("utf-16-le", errors="ignore")
            else:
                for b in decoded:
                    b.decode("latin-1")

    def run_charset_aware():
        # Spec-faithful: use the interleave path ONLY when charset_byte != 0;
        # take the fast latin-1 path when charset_byte == 0. This mirrors
        # the recommended pbixray glue shape.
        for p in captured:
            if p["charset_mode"] == "single" and p["charset_byte"] != 0:
                decoded = xmhuffman.decode_page(
                    p["bitstream"], p["encode_array"], p["offsets"],
                    p["total_bits"], swap=p["swap"],
                    charset_mode="single", charset_byte=p["charset_byte"])
                for b in decoded:
                    b.decode("utf-16-le", errors="ignore")
            else:
                decoded = xmhuffman.decode_page(
                    p["bitstream"], p["encode_array"], p["offsets"],
                    p["total_bits"], swap=p["swap"])
                if p["charset_mode"] == "general":
                    for b in decoded:
                        b[:len(b) & ~1].decode("utf-16-le", errors="ignore")
                else:
                    for b in decoded:
                        b.decode("latin-1")

    print("decode + stringify, wall clock:")
    t_old = _bench("1. current pbixray glue (latin-1 / utf-16-le branches)",
                   run_pbixray_glue)
    t_new = _bench("2. charset-aware (slow path only when charset_byte != 0)",
                   run_charset_aware)
    print()
    print(f"ratio (2) vs (1): {t_old / t_new:.2f}x  "
          "(should be ~1.0x on files with charset_byte == 0)")


if __name__ == "__main__":
    main()
