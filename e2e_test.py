"""End-to-end smoke test for installed wheels. No external dependencies.

Encodes a small fixture by hand against a 4-symbol canonical Huffman
code and checks that decode_page, decode_with_table, build_table,
swap_bytes, and decompress_encode_array all agree.
"""
import struct
import sys

import xmhuffman


def _pack_encode_array(lengths256):
    out = bytearray(128)
    for i in range(128):
        out[i] = (lengths256[2 * i] & 0x0F) | ((lengths256[2 * i + 1] & 0x0F) << 4)
    return bytes(out)


def _encode(strings, codes):
    bits = []
    offsets = []
    n = 0
    for s in strings:
        offsets.append(n)
        for byte in s:
            code, L = codes[byte]
            bits.append(bin(code)[2:].zfill(L))
            n += L
    bitstr = "".join(bits)
    pad = (-len(bitstr)) % 8
    padded = bitstr + "0" * pad
    out = bytes(int(padded[i:i + 8], 2) for i in range(0, len(padded), 8))
    return out, offsets, len(bitstr)


def main():
    # 4-symbol alphabet with lengths [1, 2, 3, 3] → codes a=0, b=10, c=110, d=111.
    lengths = [0] * 256
    lengths[ord("a")] = 1
    lengths[ord("b")] = 2
    lengths[ord("c")] = 3
    lengths[ord("d")] = 3
    ea = _pack_encode_array(lengths)

    # decompress_encode_array roundtrip
    got_lengths = list(xmhuffman.decompress_encode_array(ea))
    assert got_lengths == lengths, "decompress_encode_array mismatch"

    # build_table sanity
    table_bytes, max_len = xmhuffman.build_table(ea)
    assert max_len == 3, f"unexpected max_len={max_len}"
    assert len(table_bytes) == 8 * 2

    # Decode round-trip
    codes = {ord("a"): (0, 1), ord("b"): (2, 2), ord("c"): (6, 3), ord("d"): (7, 3)}
    strings = [b"abcd", b"aabd", b"d", b"abc"]
    raw, offsets, total = _encode(strings, codes)

    # swap_bytes is its own inverse on the even prefix
    storage = xmhuffman.swap_bytes(raw)
    assert xmhuffman.swap_bytes(storage)[: len(raw) & ~1] == raw[: len(raw) & ~1]

    # decode_page with swap=True (input is storage form)
    out = xmhuffman.decode_page(storage, ea, offsets, total, swap=True)
    assert out == strings, f"decode_page mismatch: {out!r}"

    # decode_with_table with swap=False on logical bytes
    out2 = xmhuffman.decode_with_table(raw, table_bytes, max_len, offsets,
                                       total, swap=False)
    assert out2 == strings, f"decode_with_table mismatch: {out2!r}"

    print("xmhuffman e2e: OK")


if __name__ == "__main__":
    main()
