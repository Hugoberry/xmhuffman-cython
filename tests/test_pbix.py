"""Decode every compressed dictionary page in a real PBIX and compare to
the pure-Python reference. Skips gracefully if pbixray isn't importable
or the data files aren't present.
"""
import os
import sys

import pytest

_PBIXRAY_PATH = os.path.expanduser("~/git/hub/pbixray")
if os.path.isdir(_PBIXRAY_PATH) and _PBIXRAY_PATH not in sys.path:
    sys.path.insert(0, _PBIXRAY_PATH)

pbix_huffman = pytest.importorskip("pbixray.huffman")

import xmhuffman

_DATA = os.path.join(_PBIXRAY_PATH, "data")

# Pick a few small/medium PBIX files. Heavy ones (5M.pbix, meta.pbix) take a
# long time on the Python reference side, so default to the lighter set.
_CANDIDATES = [
    "abc.pbix",
    "Excalidraw.pbix",
    "rls-sample-report.pbix",
    "old-Human-Resources-Sample-PBIX.pbix",
    "old-Retail-Analysis-Sample-PBIX.pbix",
    "old-Procurement-Analysis-Sample-PBIX.pbix",
    "old-Sales-and-Marketing-Sample-PBIX.pbix",
    "old-Supplier-Quality-Analysis-Sample-PBIX.pbix",
    "old-Customer-Profitability-Sample-PBIX.pbix",
    "Adventure Works, Internet Sales.pbix",
    "Adventure Works DW 2020.pbix",
    "Sales & Returns Sample v201912.pbix",
]


def _existing(*names):
    return [n for n in names if os.path.isfile(os.path.join(_DATA, n))]


@pytest.mark.parametrize("name", _existing(*_CANDIDATES))
def test_decode_page_matches_reference_on_pbix(name):
    """For every compressed dict page in the file, decode with both the
    pure-Python reference and our extension and assert byte-identity."""
    from pbixray.core import PBIXRay
    from pbixray.huffman import (
        decompress_encode_array,
        build_huffman_table,
        _swap_bitstream,
        decode_substrings,
    )
    import numpy as np
    from collections import defaultdict

    path = os.path.join(_DATA, name)
    px = PBIXRay(path)

    # Iterate over every column dictionary the loader exposes.
    table_names = list(px.tables)
    n_pages = 0
    for tname in table_names:
        meta = px.schema[px.schema["TableName"] == tname]
        for _, row in meta.iterrows():
            dict_buf = row.get("Dictionary")
            if dict_buf is None or (hasattr(dict_buf, "__len__") and not len(dict_buf)):
                continue
            # The dictionary parsing lives in pbixray internals; we lean on
            # vertipaq_decoder. Just call the higher-level path and trust
            # that any bug surface manifests as mismatched strings.
            # Instead of reproducing the iteration, hook decode_substrings.
            pass

    # The clean approach: monkey-patch decode_substrings in the reference,
    # capture each invocation's inputs, run both implementations against
    # the captured inputs, compare.
    captured = []
    orig = pbix_huffman.decode_substrings

    def spy(swapped, table, max_len, offsets, store_total_bits):
        captured.append((bytes(swapped), table, max_len, list(offsets),
                         int(store_total_bits)))
        return orig(swapped, table, max_len, offsets, store_total_bits)

    import pbixray.vertipaq_decoder as vd
    pbix_huffman.decode_substrings = spy
    vd.decode_substrings = spy
    try:
        # Touch every table so every page is decoded.
        for tname in table_names:
            try:
                px.get_table(tname)
            except Exception:
                # Skip tables that fail for unrelated reasons.
                pass
    finally:
        pbix_huffman.decode_substrings = orig
        vd.decode_substrings = orig

    # Some PBIX files have no compressed string pages — that's fine.
    if not captured:
        pytest.skip(f"{name}: no compressed Huffman pages exercised")

    # We have (swapped, table, max_len, offsets, total) tuples. We need
    # the original (unswapped) bitstream plus encode_array. Reconstruct
    # the encode_array from `table`? Easier: re-derive a length array
    # from the table directly and call decode_with_table.
    import struct
    for i, (swapped, table, max_len, offsets, total) in enumerate(captured):
        # Reference output:
        ref = orig(swapped, table, max_len, offsets, total)
        # Our output, using the already-swapped buffer and matching table.
        # We need to convert `table` (list of (sym, len) tuples) into our
        # packed u16 form.
        n = 1 << max_len
        packed = bytearray(n * 2)
        for idx in range(n):
            sym, L = table[idx]
            entry = (sym << 8) | (L & 0xff)
            struct.pack_into("<H", packed, idx * 2, entry)
        got = xmhuffman.decode_with_table(bytes(swapped), bytes(packed),
                                          max_len, offsets, total,
                                          swap=False)
        # The reference returns list[str] (one per record). Compare via
        # latin-1 round-trip since our output is bytes.
        ref_bytes = [s.encode("latin-1") for s in ref]
        assert got == ref_bytes, (
            f"{name}: page #{i} mismatch (n={len(ref)}); "
            f"first diff at {next((j for j in range(min(len(got), len(ref_bytes))) if got[j] != ref_bytes[j]), 'N/A')}"
        )
        n_pages += 1

    assert n_pages > 0
