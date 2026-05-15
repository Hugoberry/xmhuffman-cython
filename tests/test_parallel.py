"""Verify ``decode_page`` runs concurrently across threads.

This is a regression guard for the GIL-per-page contract: every call to
``xmhuffman.decode_page`` must release the GIL for the duration of the
kernel work, not reacquire it per-string. We assert two things:

1. **Byte-identity**: parallel results equal the serial baseline.
2. **Wall-clock speedup**: on a multi-page workload, ``ThreadPoolExecutor``
   with multiple workers is at least 2× faster than n=1. Skipped on
   single-core machines where the assertion is moot.
"""
from __future__ import annotations

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import xmhuffman


# --- fixture: synthesize a workload that exercises the multi-page path -----

def _pack_encode_array(lengths256):
    out = bytearray(128)
    for i in range(128):
        out[i] = (lengths256[2 * i] & 0x0F) | ((lengths256[2 * i + 1] & 0x0F) << 4)
    return bytes(out)


def _build_codes(ea):
    """Return {symbol: (code, length)} from the canonical table."""
    import struct
    tbl, max_len = xmhuffman.build_table(ea)
    entries = struct.unpack("<%dH" % (1 << max_len), tbl)
    codes = {}
    for idx in range(1 << max_len):
        sym = entries[idx] >> 8
        L = entries[idx] & 0xFF
        if sym in codes:
            continue
        codes[sym] = (idx >> (max_len - L), L)
    return codes, max_len


def _encode_strings(strings, codes):
    bits = []
    offsets = []
    bc = 0
    for s in strings:
        offsets.append(bc)
        for byte in s:
            code, L = codes[byte]
            bits.append(bin(code)[2:].zfill(L))
            bc += L
    bitstr = "".join(bits)
    total = len(bitstr)
    bitstr += "0" * ((-total) % 8)
    return (bytes(int(bitstr[i:i + 8], 2) for i in range(0, len(bitstr), 8)),
            offsets, total)


def _make_workload(n_pages=128, strings_per_page=200, alphabet_size=16,
                   string_len=24, seed=0):
    """Build ``n_pages`` independently-encoded dictionary pages."""
    rng = random.Random(seed)
    # A small, balanced alphabet so build_table gives short codewords and
    # the per-page kernel work isn't degenerate.
    symbols = list(range(ord("a"), ord("a") + alphabet_size))
    lengths = [0] * 256
    # Equal-length codes (log2 alphabet) → uniform table, fast decoder.
    import math
    L = max(2, int(math.ceil(math.log2(alphabet_size))))
    for s in symbols:
        lengths[s] = L
    ea = _pack_encode_array(lengths)
    codes, _ = _build_codes(ea)

    pages = []
    for _ in range(n_pages):
        strs = [
            bytes(rng.choice(symbols) for _ in range(string_len))
            for _ in range(strings_per_page)
        ]
        raw, offsets, total = _encode_strings(strs, codes)
        storage = xmhuffman.swap_bytes(raw)
        pages.append((storage, ea, offsets, total, strs))
    return pages


def _run(pages, n_workers):
    def one(p):
        storage, ea, offsets, total, _ = p
        return xmhuffman.decode_page(storage, ea, offsets, total, swap=True)
    if n_workers == 1:
        return [one(p) for p in pages]
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        return list(ex.map(one, pages))


# ---------------------------------------------------------------------------

def test_parallel_byte_identity():
    """Multi-thread output must equal single-thread output, byte-for-byte."""
    pages = _make_workload(n_pages=64, strings_per_page=128)
    serial = _run(pages, 1)
    threaded = _run(pages, 8)
    assert threaded == serial


def test_parallel_speedup():
    """8 workers should beat 1 by at least 2× on a multi-page workload."""
    cpu = os.cpu_count() or 1
    if cpu < 4:
        pytest.skip(f"need >= 4 cores to assert speedup, have {cpu}")

    pages = _make_workload(n_pages=256, strings_per_page=512, string_len=64)
    n_workers = min(8, cpu)

    # Best-of-3 to keep this stable across CI hosts.
    def best(fn, n=3):
        return min(_time_once(fn) for _ in range(n))

    def _time_once(fn):
        t0 = time.perf_counter()
        fn()
        return time.perf_counter() - t0

    t_serial = best(lambda: _run(pages, 1))
    t_parallel = best(lambda: _run(pages, n_workers))
    speedup = t_serial / t_parallel
    assert speedup >= 2.0, (
        f"parallel decode did not scale: n=1 took {t_serial * 1000:.1f} ms, "
        f"n={n_workers} took {t_parallel * 1000:.1f} ms "
        f"(ratio {speedup:.2f}x; expected >= 2.0x)")
