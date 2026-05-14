"""Correctness tests for xmhuffman against the pbixray pure-Python reference.

Run with: pytest tests/test_basic.py -v
"""
import os
import random
import struct
import sys

import pytest

import xmhuffman

# Allow running without pbixray on PYTHONPATH by adding its repo.
_PBIXRAY_PATH = os.path.expanduser("~/git/hub/pbixray")
if os.path.isdir(_PBIXRAY_PATH) and _PBIXRAY_PATH not in sys.path:
    sys.path.insert(0, _PBIXRAY_PATH)

pbix_huffman = pytest.importorskip("pbixray.huffman")


# ---------------------------------------------------------------------------
# swap_bytes

@pytest.mark.parametrize("n", [0, 1, 2, 3, 7, 8, 9, 1024, 1025])
def test_swap_bytes_matches_reference(n):
    random.seed(n)
    buf = bytes(random.randint(0, 255) for _ in range(n))
    expected = pbix_huffman._swap_bitstream(buf)
    got = xmhuffman.swap_bytes(buf)
    assert got == expected


# ---------------------------------------------------------------------------
# decompress_encode_array

def test_decompress_encode_array_matches_reference():
    random.seed(0)
    for _ in range(20):
        buf = bytes(random.randint(0, 255) for _ in range(128))
        ref = pbix_huffman.decompress_encode_array(buf)
        got = list(xmhuffman.decompress_encode_array(buf))
        assert got == ref


def test_decompress_encode_array_size_validation():
    with pytest.raises(ValueError):
        xmhuffman.decompress_encode_array(b"\x00" * 127)


# ---------------------------------------------------------------------------
# build_table

def _ref_table(encode_array_128):
    lengths = pbix_huffman.decompress_encode_array(encode_array_128)
    table, max_len = pbix_huffman.build_huffman_table(lengths)
    return table, max_len


def _pack_encode_array(lengths256):
    """Inverse of decompress_encode_array — pack 256 lengths into 128 bytes."""
    assert len(lengths256) == 256
    out = bytearray(128)
    for i in range(128):
        lo = lengths256[2 * i] & 0x0F
        hi = lengths256[2 * i + 1] & 0x0F
        out[i] = lo | (hi << 4)
    return bytes(out)


def _make_valid_lengths(n_symbols, max_len=8, seed=0):
    """Generate a valid canonical Huffman length set for n_symbols active
    symbols (Kraft-equal-1). Strategy: random tree depths reduced if
    Kraft > 1."""
    random.seed(seed)
    lengths = [0] * 256
    # Pick which symbols are active.
    active = random.sample(range(256), n_symbols)
    # Pure binary tree: 2 symbols get the deepest leaves, walk up.
    # Easier: assign lengths via a greedy Huffman on random frequencies.
    freq = {s: random.randint(1, 1000) for s in active}
    # Build a Huffman tree.
    import heapq
    heap = [(f, i, s) for i, (s, f) in enumerate(freq.items())]
    heapq.heapify(heap)
    if len(heap) == 1:
        lengths[heap[0][2]] = 1
        return lengths
    parents = {}
    counter = 1000  # safely above any symbol id (0-255)
    while len(heap) > 1:
        a = heapq.heappop(heap)
        b = heapq.heappop(heap)
        parents[counter] = (a[2], b[2])
        heapq.heappush(heap, (a[0] + b[0], counter, counter))
        counter += 1
    # Walk back down to assign depths.
    root = heap[0][2]
    def walk(node, depth):
        if node in parents:
            l, r = parents[node]
            walk(l, depth + 1)
            walk(r, depth + 1)
        else:
            lengths[node] = depth
    walk(root, 0)
    # Cap and ensure ≤ max_len; this may break Kraft so iterate.
    # Simpler: if any depth > max_len, reject this seed.
    if any(L > max_len for L in lengths):
        return None
    return lengths


def test_build_table_matches_reference():
    seeds_tried = 0
    runs = 0
    while runs < 10 and seeds_tried < 200:
        seeds_tried += 1
        lengths = _make_valid_lengths(n_symbols=random.randint(2, 64),
                                      max_len=12, seed=seeds_tried)
        if lengths is None:
            continue
        runs += 1
        ea = _pack_encode_array(lengths)
        ref_table, ref_max = _ref_table(ea)
        got_bytes, got_max = xmhuffman.build_table(ea)
        assert got_max == ref_max
        # Unpack our u16 table and compare per-index.
        n = 1 << got_max
        got_table = list(struct.unpack("<%dH" % n, got_bytes))
        for idx in range(n):
            entry = got_table[idx]
            sym = entry >> 8
            length = entry & 0xff
            assert (sym, length) == ref_table[idx], \
                f"mismatch at idx={idx}: got ({sym}, {length}) vs ref {ref_table[idx]}"
    assert runs >= 5


# ---------------------------------------------------------------------------
# decode_page — end-to-end roundtrip via hand-encoded stream

def _encode_to_bitstream(strings, codes, max_len):
    """Encode each byte string using `codes` dict {sym: (code, length)}.
    Returns (bitstream_bytes, offsets, total_bits) in *logical* form."""
    bits = []
    offsets = []
    bit_count = 0
    for s in strings:
        offsets.append(bit_count)
        for byte in s:
            code, L = codes[byte]
            bits.append(bin(code)[2:].zfill(L))
            bit_count += L
    bitstr = "".join(bits)
    total = len(bitstr)
    assert total == bit_count
    # Pad to byte boundary with zeros (high bits of last byte).
    pad = (-total) % 8
    padded = bitstr + "0" * pad
    out = bytearray(len(padded) // 8)
    for i in range(len(out)):
        chunk = padded[i * 8:(i + 1) * 8]
        out[i] = int(chunk, 2)
    return bytes(out), offsets, total


def test_decode_page_roundtrip():
    # 4-symbol alphabet with lengths [1, 2, 3, 3] (codes: 0, 10, 110, 111).
    lengths256 = [0] * 256
    lengths256[ord('a')] = 1
    lengths256[ord('b')] = 2
    lengths256[ord('c')] = 3
    lengths256[ord('d')] = 3
    ea = _pack_encode_array(lengths256)
    ref_table, ref_max = _ref_table(ea)
    # Look up canonical codes from ref_table by scanning.
    seen = {}
    for idx in range(1 << ref_max):
        sym, L = ref_table[idx]
        if sym in seen:
            continue
        code = idx >> (ref_max - L)
        seen[sym] = (code, L)

    strings = [b"abcd", b"aabd", b"d", b"abc"]
    raw, offsets, total = _encode_to_bitstream(strings, seen, ref_max)
    # `raw` is the logical (MSB-first) bit stream. The on-disk form has
    # adjacent bytes swapped; pre-swap to simulate storage, then ask the
    # decoder to swap back.
    storage = xmhuffman.swap_bytes(raw)

    out = xmhuffman.decode_page(storage, ea, offsets, total, swap=True)
    assert out == strings


def test_decode_page_swap_false():
    # Same but pre-swap manually.
    lengths256 = [0] * 256
    lengths256[ord('a')] = 1
    lengths256[ord('b')] = 2
    lengths256[ord('c')] = 3
    lengths256[ord('d')] = 3
    ea = _pack_encode_array(lengths256)
    ref_table, ref_max = _ref_table(ea)
    seen = {}
    for idx in range(1 << ref_max):
        sym, L = ref_table[idx]
        if sym in seen:
            continue
        code = idx >> (ref_max - L)
        seen[sym] = (code, L)

    strings = [b"abcd", b"d"]
    raw, offsets, total = _encode_to_bitstream(strings, seen, ref_max)
    # `raw` is the logical stream. With swap=False the decoder expects the
    # logical form directly.
    out = xmhuffman.decode_page(raw, ea, offsets, total, swap=False)
    assert out == strings


def test_decode_with_table_matches_decode_page():
    lengths256 = [0] * 256
    lengths256[ord('a')] = 1
    lengths256[ord('b')] = 2
    lengths256[ord('c')] = 3
    lengths256[ord('d')] = 3
    ea = _pack_encode_array(lengths256)
    ref_table, ref_max = _ref_table(ea)
    seen = {}
    for idx in range(1 << ref_max):
        sym, L = ref_table[idx]
        if sym in seen:
            continue
        code = idx >> (ref_max - L)
        seen[sym] = (code, L)

    strings = [b"abcd", b"aabd", b"d", b"abc"]
    raw, offsets, total = _encode_to_bitstream(strings, seen, ref_max)
    storage = xmhuffman.swap_bytes(raw)

    table_bytes, max_len = xmhuffman.build_table(ea)
    out = xmhuffman.decode_with_table(storage, table_bytes, max_len, offsets,
                                      total, swap=True)
    assert out == strings
