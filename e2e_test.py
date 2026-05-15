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

    # Single-charset interleave: charset_byte == 0 must be byte-equivalent to
    # the default (raw) output zero-padded for UTF-16-LE.
    out_single_zero = xmhuffman.decode_page(
        storage, ea, offsets, total, swap=True,
        charset_mode="single", charset_byte=0)
    for raw_item, interleaved in zip(strings, out_single_zero):
        expected = bytes(b for byte in raw_item for b in (byte, 0))
        assert interleaved == expected, (
            f"single/cb=0 mismatch: got {interleaved!r}, want {expected!r}")
    # And decoding as UTF-16-LE recovers the original ASCII text.
    assert [b.decode("utf-16-le") for b in out_single_zero] == [
        s.decode("latin-1") for s in strings]

    # Single-charset with a non-zero charset byte: each emitted byte is the
    # UTF-16-LE low byte, charset_byte is the high byte (matches xmsrv).
    out_single_nz = xmhuffman.decode_page(
        storage, ea, offsets, total, swap=True,
        charset_mode="single", charset_byte=0x04)
    decoded = [b.decode("utf-16-le") for b in out_single_nz]
    expected_nz = ["".join(chr((0x04 << 8) | b) for b in s) for s in strings]
    assert decoded == expected_nz, (
        f"single/cb=0x04 mismatch: got {decoded!r}, want {expected_nz!r}")

    # decode_with_table accepts the same charset kwargs.
    out_single_nz2 = xmhuffman.decode_with_table(
        raw, table_bytes, max_len, offsets, total, swap=False,
        charset_mode="single", charset_byte=0x04)
    assert out_single_nz2 == out_single_nz, "decode_with_table charset path mismatch"

    print("xmhuffman e2e: OK")


if __name__ == "__main__":
    main()
