# xmhuffman

[![PyPI version](https://img.shields.io/pypi/v/xmhuffman.svg)](https://pypi.org/project/xmhuffman/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small, fast Cython extension that decodes the canonical-Huffman string
dictionary pages used by xVelocity / Vertipaq column stores — the storage
format inside Power BI `.pbix` files (the `DataModel` part) and Excel
Power Pivot workbooks (`xl/model/item.data`).

Provides a tight C kernel for what would otherwise be a per-symbol Python
loop. On real `.pbix` files this is **30–50× faster** than an equivalent
pure-Python implementation and removes a hot path that dominates table
extraction time in tools like [pbixray](https://github.com/Hugoberry/pbixray).

## Installation

```bash
pip install xmhuffman
```

Building from source requires a C compiler and Cython ≥ 3.0:

```bash
git clone https://github.com/Hugoberry/xmhuffman-cython
cd xmhuffman-cython
pip install -e .
```

## Usage

The library exposes a tiny surface — one entry point for the common case
plus a few lower-level helpers.

### Decode a dictionary page

```python
import xmhuffman

# All inputs come straight from the Vertipaq dictionary page metadata:
#   bitstream  — compressed_string_buffer (bytes)
#   encode_array_128 — 128-byte nibble-packed code-length array
#   offsets    — per-string start bit offsets (sequence of u32)
#   total_bits — store_total_bits (end of last string)
strings: list[bytes] = xmhuffman.decode_page(
    bitstream,
    encode_array_128,
    offsets,
    total_bits,
    swap=True,            # apply the byte-pair swap inside the extension
)
```

Output is `list[bytes]`. Charset interpretation is the caller's choice:
Vertipaq pages flag themselves as either single-charset (latin-1 / ANSI,
one Python `str` per record) or general (the byte stream is UTF-16LE).

### Lower-level building blocks

For callers that want to amortize table construction across pages, or
just to unit-test pieces:

```python
# Expand the 128-byte nibble-packed array to 256 plain bytes of lengths.
lengths = xmhuffman.decompress_encode_array(encode_array_128)

# Pair-swap a buffer (bytes 2k and 2k+1 swap; trailing odd byte left as-is).
swapped = xmhuffman.swap_bytes(raw)

# Build the flat decode table once, reuse it across decode calls.
table_bytes, max_len = xmhuffman.build_table(encode_array_128)
strings = xmhuffman.decode_with_table(
    bitstream, table_bytes, max_len, offsets, total_bits, swap=True,
)
```

## Format notes

The on-disk format is documented publicly in Microsoft's open
specification [\[MS-XLDM\] §2.7.4 — Huffman
Compression](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/f70b41f2-ca64-44a1-9e6f-53e63f6a5ee9).
Each dictionary page is, schematically:

| Field | Description |
|---|---|
| `encode_array` | 128 bytes, two 4-bit code lengths per byte (low nibble = symbol `2i`, high = `2i+1`). Value 0 means "symbol unused". Per [\[MS-XLDM\]](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/f70b41f2-ca64-44a1-9e6f-53e63f6a5ee9) codeword lengths are between 2 and 15 bits. |
| `uiDecodeBits` | Width of the on-disk primary lookup table (≤ 12). This decoder uses a single flat `2^max_len` table instead and ignores `uiDecodeBits`. |
| `compressed_string_buffer` | The bitstream itself, with adjacent bytes pair-swapped on disk. No padding between strings. |
| `store_total_bits` | Total logical bit length; end sentinel for the last string. |
| `vector_of_record_handle_structures` | Per-record `(bit_offset, page_id)`; sorted offsets per page give the per-string start boundaries. |

Codes are classical Huffman, encoded canonically by sorting
`(length, symbol)` ascending and incrementing the code with a left-shift
on length changes — exactly the reconstruction described in
[\[MS-XLDM\] §2.7.4.1.5](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/f70b41f2-ca64-44a1-9e6f-53e63f6a5ee9).

### Character-set modes

[\[MS-XLDM\] §2.7.4.1.4](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/f70b41f2-ca64-44a1-9e6f-53e63f6a5ee9)
distinguishes two modes per page:

- **Single character set** (`character_set_type_identifier = 0x000aba91`)
  — only the low byte of each character is Huffman-encoded; the upper
  (charset) byte is stored once on the page and must be reinserted by
  the caller to recover the original 2-byte character stream.
- **Multiple character sets** (`0x000aba92`) — both bytes are encoded;
  the output byte stream is consumed directly as UTF-16LE.

This decoder emits raw `bytes` either way; reassembly of UTF-16 characters
(including reinserting the single-charset upper byte) is the caller's
responsibility.

## Performance

Apples-to-apples against an equivalent pure-Python decoder on a few real
`.pbix` files:

| File | Strings | Python ref | xmhuffman | Speedup |
|---|---:|---:|---:|---:|
| Adventure Works DW 2020 | 191,489 | 449 ms | 10.0 ms | 45× |
| Sales & Marketing sample | 103,290 | 160 ms | 5.3 ms | 30× |
| Retail Analysis sample | 9 | 144 ms | 2.9 ms | 50× |

The kernel does one unaligned 64-bit big-endian load, one shift, one
mask, one table lookup and one byte store per output symbol. The decode
table is a flat `2^max_len` array of `uint16_t` (≤ 64 KB; usually 1–8 KB)
that fits comfortably in L1/L2.

The GIL is released around the inner work, so callers can decode
multiple pages or columns from worker threads without contention.

## Project layout

```
xmhuffman-cython/
├── xmhuffman.pyx         # Cython surface
├── xmhuffman.pxd         # C declarations
├── src/xmhuffman_kernel.c    # C kernel
├── include/xmhuffman_kernel.h
├── tests/                # correctness tests
└── bench/                # micro-benchmark
```

## Testing

```bash
pip install -e .
pip install pytest
pytest tests/ -v
```

The basic test suite checks each helper against a pure-Python reference
implementation. An additional integration test (`tests/test_pbix.py`)
decodes pages out of real `.pbix` files and asserts byte-identity with
the reference; it is skipped automatically when fixtures aren't
available.

## Scope and non-goals

- **Not** a general-purpose Huffman library. Alphabets are fixed at 256
  symbols, codeword lengths are capped at 15 bits, and the bitstream
  convention is the one used by Vertipaq pages.
- **Not** an encoder. Round-tripping pages is out of scope.
- **No** charset conversion inside the extension. The decoder returns
  raw `bytes`; the caller picks between `latin-1` and paired UTF-16LE
  based on the page's character-set identifier.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

This package is the third in a family of thin Cython wrappers around
Microsoft column-store / compression formats, alongside
[xpress8-python](https://github.com/Hugoberry/xpress8-python) and
[xpress9-python](https://github.com/Hugoberry/xpress9-python).

The format itself is documented publicly in Microsoft's open
specification [\[MS-XLDM\] — Spreadsheet Data Model File
Format](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/),
which the implementation here follows. The Python reference and
end-to-end test fixtures come from the
[pbixray](https://github.com/Hugoberry/pbixray) project.
