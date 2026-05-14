# xmhuffman-cython — design spec

A small Cython extension wrapping a native canonical-Huffman decoder for the
string-dictionary pages emitted by Microsoft's xVelocity / Vertipaq engine
(PBIX `DataModel`, XLSX Power Pivot `xl/model/item.data`).

Sits alongside `xpress8-python` and `xpress9-python` in the same "thin
Cython wrapper around a Microsoft-format codec" family: one repo, one
codec, no Python-level loops on the hot path.

## Why a separate package

The benchmark in `bench_get_table.py` shows `decode_substrings`
(pure-Python per-symbol loop in [`pbixray/huffman.py`](../pbixray/huffman.py))
accounting for **97.7% of `PBIXRay.get_table()` on meta.pbix** (529 s of
542 s) and 85.8% on 5M.pbix. It is by far the largest hotspot in the
entire library.

No public Python library exposes a "raw canonical Huffman decoder with
arbitrary code-length array + caller-supplied bitstream layout":

- `numpy` / `scipy` — nothing.
- `dahuffman`, `huffman` — pure Python, slower than the existing loop.
- `constriction` — Rust-backed but ANS/range-coder oriented; its symbol
  table + bitstream layout don't match Vertipaq's pair-swapped stream
  and don't support random-access substring decode.
- `zlib` / `libdeflate` / `brotli` / Xpress9's internal Huffman — all
  `static`-scoped inside their containing codec, tangled with that
  codec's framing (DEFLATE blocks, Xpress9 LZ77+Huffman frames,
  end-of-block markers). Not extractable as a primitive.

Vertipaq's dictionary-page Huffman has two quirks that rule out reusing
any existing decoder unchanged:

1. **Byte-pair-swapped bitstream** — bytes are paired then swapped
   in-pair before bit-reading.
2. **Random-access substring decode** — input is an array of per-string
   start bit offsets, not a single linear stream with end markers.
   Each `[offsets[i], offsets[i+1])` slice is an independent string.

A small dedicated wrapper is the lowest-overhead path.

## Naming

`xmhuffman-cython`. "XM" is the format-internal prefix used throughout
Vertipaq metadata (`XMColumnSegment`, `xm_type_string`, `xm_type_long`,
the `xmserializer` companion project). It signals "the Huffman variant
used by xVelocity/AS Tabular column store" without claiming generality.

- Repo: `github.com/<user>/xmhuffman-cython`
- Distribution name (PyPI): `xmhuffman`
- Import name: `xmhuffman`
- Cython unit: `xmhuffman.pyx` → `xmhuffman.c` (same layout as
  `xpress8.pyx` / `xpress9.pyx` in the sibling packages).

## Format reference (Vertipaq dictionary page Huffman)

Encoded inside a `Dictionary.DictionaryPages[i].string_store` whose
`character_set_type_identifier` is one of:

| ID | Meaning |
|---|---|
| `0x000aba91` | Charset-based Huffman — symbols are single bytes; output is the raw byte stream interpreted under the page's `character_set_used` (commonly latin-1 / ANSI). One Python `str` per record. |
| `0x000aba92` | General Huffman — symbols are bytes; output bytes are a UTF-16LE stream. Caller pairs adjacent symbols and decodes as UTF-16LE. |

Fields used:

- `encode_array` — 128 bytes. Two 4-bit code lengths per byte
  (low nibble = symbol 2i, high nibble = symbol 2i+1) for the
  256-symbol alphabet. Value 0 means "symbol unused". Max value 15 →
  **max codeword length ≤ 15 bits**.
- `compressed_string_buffer` — bitstream, **byte-pair swapped**
  (bytes 2k and 2k+1 are swapped; a trailing odd byte is left as-is).
- `store_total_bits` — total bit length of the unswapped logical
  stream; needed as the end bound for the last string.
- `vector_of_record_handle_structures` — `(bit_or_byte_offset:u32,
  page_id:u32)` per record. For a given page, the sorted
  `bit_or_byte_offset` values give the per-string start offsets.

Canonical Huffman codes are assigned by sorting `(length, symbol)`
ascending and incrementing `code` with a left-shift on length changes
(see `generate_codes` in [`pbixray/huffman.py`](../pbixray/huffman.py)).

## Public API

Keep it tiny. One function does the hot work; two helpers expose the
table build for callers that want it (and for tests).

```python
import xmhuffman

# 1. The hot path. Decode N strings out of a single page.
#    Caller is responsible for byte-pair-swapping the buffer once
#    (cheap, vectorisable in numpy — leave it in Python) OR pass the
#    raw buffer and request swap_in_place=True.
out: list[bytes] = xmhuffman.decode_page(
    bitstream,                # bytes or bytes-like; pair-swapped if swap=False
    encode_array_128,         # bytes, length 128 (the nibble-packed form)
    offsets,                  # array of u32, len = n_strings
    store_total_bits,         # u64
    swap=True,                # do the byte-pair swap inside the extension
)

# 2. Lower-level entry points for callers that want to amortise table
#    construction across calls or unit-test pieces.
table, max_len = xmhuffman.build_table(encode_array_128)  # table: bytes (or np.ndarray u16)
out = xmhuffman.decode_with_table(
    bitstream, table, max_len, offsets, store_total_bits, swap=True,
)
```

Return type is **`list[bytes]`**, not `list[str]`. Charset interpretation
(`latin-1` → `str`, or pair-and-decode-as-`utf-16-le`) is the caller's
business — the decoder emits the raw symbol stream. Keeping it as bytes
saves a per-symbol Python-string construction and lets pbixray pick the
right decode based on `character_set_type_identifier`.

For convenience and parity with the current Python helper, also expose:

```python
xmhuffman.swap_bytes(buffer)        # the standalone pair-swap, returns bytes
xmhuffman.decompress_encode_array(b128)  # 128-byte nibble-packed -> 256-u8 lengths
```

so callers can mix and match without re-implementing the trivia.

## Decode kernel (the actual point of the library)

```c
// Inputs:
//   p             : pointer to *already-swapped* bitstream
//   p_len         : length of swapped buffer in bytes
//   table         : table[1<<max_len] of u16 packing (symbol<<8) | code_len
//   max_len       : max codeword length (≤ 15)
//   offsets[k]    : start bit of string k
//   n_strings     : number of strings on this page
//   total_bits    : end-of-stream sentinel for the last string

for (size_t s = 0; s < n_strings; ++s) {
    uint64_t bit = offsets[s];
    uint64_t end = (s + 1 < n_strings) ? offsets[s + 1] : total_bits;

    // Emit into a per-string scratch buffer; finalize as a Python bytes
    // object once (PyBytes_FromStringAndSize) per string.
    uint8_t* out = scratch;
    while (bit < end) {
        size_t byte = bit >> 3;
        unsigned off = bit & 7;

        // Load up to 8 bytes big-endian. Guard the tail: if byte+8 > p_len,
        // copy into a zero-padded local first. The tail is hit at most
        // once per string so the branch is cheap.
        uint64_t w = load_be64_safe(p, p_len, byte);

        unsigned idx = (unsigned)((w >> (64 - max_len - off)) & ((1u << max_len) - 1));
        uint16_t e = table[idx];

        *out++ = (uint8_t)(e >> 8);
        bit += (e & 0xff);
    }
    results[s] = PyBytes_FromStringAndSize((char*)scratch, out - scratch);
}
```

Properties:

- **One unaligned 64-bit load, one shift, one mask, one table hit,
  one byte store per output symbol.** No branches in the hot path
  except the loop condition and the per-string tail guard.
- 15-bit codeword limit → 64-bit window always holds a full codeword
  regardless of bit offset (`64 ≥ 15 + 7`).
- Table is 2¹⁵ × 2 B = 64 KB worst case → fits in L2. Typical pages
  have `max_len` 9–12 → 1–8 KB, fits in L1.
- Scratch buffer is sized once per page to `(end_of_last_string -
  offsets[0]) / shortest_code_len_bits` upper bound, or grown
  amortized. Allocation is per-page, not per-string.

`load_be64_safe`: on x86_64/aarch64 do `__builtin_bswap64` of an
unaligned 64-bit read when `byte + 8 <= p_len`, else copy
`min(8, p_len - byte)` bytes into a zero-padded `uint64_t` and bswap.

## Cython surface

Match the `xpress8.pyx` / `xpress9.pyx` style: declare a `cdef class
XMHuffman` with the kernel as a `cpdef` method, plus module-level
function wrappers for the static helpers. No Python objects in the
inner loop — `decode_page` takes a `const unsigned char[::1]`
memoryview for the bitstream and a `const uint32_t[::1]` memoryview for
offsets, and returns a `list` that's appended to with
`PyList_SET_ITEM` after each `PyBytes_FromStringAndSize`.

```cython
# xmhuffman.pxd
cdef extern from "xmhuffman_kernel.h":
    int xmh_build_table(const unsigned char* lengths256,
                        unsigned short* table, unsigned* out_max_len) nogil
    void xmh_swap_pairs(const unsigned char* in_buf, unsigned char* out_buf,
                        Py_ssize_t n) nogil
    Py_ssize_t xmh_decode_one(const unsigned char* swapped, Py_ssize_t swapped_len,
                              const unsigned short* table, unsigned max_len,
                              unsigned long long start_bit,
                              unsigned long long end_bit,
                              unsigned char* out, Py_ssize_t out_cap) nogil
```

Releasing the GIL across the per-page decode means a future
multi-threaded `get_table` (one thread per column, or one per page on
huge dictionaries) is cheap to wire up.

## Source layout

Mirrors `xpress8-python` / `xpress9-python`:

```
xmhuffman-cython/
├── pyproject.toml
├── setup.py                  # Cython + cibuildwheel-friendly
├── README.md
├── xmhuffman.pyx
├── xmhuffman.pxd
├── xmhuffman.c               # generated, committed for sdists w/o Cython
├── include/
│   └── xmhuffman_kernel.h
├── src/
│   └── xmhuffman_kernel.c    # the kernel above
├── tests/
│   ├── test_roundtrip.py     # build_table + decode against hand-crafted streams
│   ├── test_pbix_pages.py    # parametrised over saved Vertipaq pages from data/
│   └── fixtures/             # a handful of real dictionary pages dumped as .bin + expected .json
└── bench/
    └── bench_decode.py       # apples-to-apples vs the pbixray Python loop
```

## Integration in pbixray

Replace [`pbixray/huffman.py`](../pbixray/huffman.py) `decode_substrings`
call site in [`pbixray/vertipaq_decoder.py:176`](../pbixray/vertipaq_decoder.py:176):

```python
# before
swapped = _swap_bitstream(compressed_string_buffer)
decoded = decode_substrings(swapped, table, max_len, page_offsets, store_total_bits)

# after
decoded = xmhuffman.decode_page(
    compressed_string_buffer,        # raw, unswapped
    encode_array,                    # 128-byte nibble form
    np.asarray(page_offsets, dtype=np.uint32),
    store_total_bits,
    swap=True,
)
if is_general:
    for i, b in enumerate(decoded):
        hashtable[index + i] = b[:len(b) & ~1].decode('utf-16-le', errors='ignore')
else:
    cs = page.string_store.character_set_used  # e.g. 'latin-1'
    for i, b in enumerate(decoded):
        hashtable[index + i] = b.decode(cs, errors='replace')
    index += len(decoded)
```

The current pure-Python fallback stays in the repo behind a
`try: import xmhuffman` so pbixray remains installable when the wheel
is unavailable.

## Performance target

From the benchmark:

| File | rows × cols | get_table now | decode_substrings now | get_table target |
|---|---:|---:|---:|---:|
| meta.pbix | 1.06 M × 19 | 541.6 s | 529.3 s (97.7%) | ≈10–15 s |
| meta2.pbix | 372 k × 19 | 244.2 s | 239.3 s (98.0%) | ≈5 s |
| Contoso.pbix | 12.5 M × 19 | 10.2 s | 4.66 s (45.5%) | ≈6 s |
| 5M.pbix | 2.10 M × 1 | 3.29 s | 2.82 s (85.8%) | ≈0.5 s |

Conservative 50× speedup on the decode kernel (heap-tree Python loop →
flat-table C kernel is typically 50–200×). After the kernel, the next
hotspot is `pd.Series(...).map(dictionary)` on multi-million-row fact
tables — a separate numpy-take optimisation, out of scope here.

## Non-goals

- **Not** a general-purpose Huffman library. No support for adaptive
  Huffman, alphabets ≠ 256, codeword lengths > 15 bits, or arbitrary
  bitstream conventions.
- **Not** an encoder. Round-trip writing of Vertipaq dictionary pages
  is not on the roadmap — pbixray is read-only.
- **No** charset conversion inside the extension. Returns `bytes`; the
  caller decides between `latin-1` and `utf-16-le` paired decoding.
