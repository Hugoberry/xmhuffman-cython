# AGENTS.md

Instructions for AI coding assistants working in this repository.

## What this project is

`xmhuffman` is a Cython extension that decodes canonical-Huffman string
dictionary pages from xVelocity / Vertipaq column stores (`.pbix`
DataModel, Power Pivot `item.data`). It is a focused, single-purpose
package — not a general Huffman library.

The on-disk format is documented in Microsoft's open specification
[\[MS-XLDM\] §2.7.4 — Huffman
Compression](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xldm/f70b41f2-ca64-44a1-9e6f-53e63f6a5ee9).
The Python reference used by the test suite comes from the open-source
[pbixray](https://github.com/Hugoberry/pbixray) reader. All
documentation in this repo describes the published file format only.

## Project conventions

- **Hot path stays in C.** Anything per-symbol or per-string in
  performance-critical code belongs in `src/xmhuffman_kernel.c`. Cython
  is the glue, not the algorithm.
- **No Python objects in the inner loop.** The kernel takes typed
  memoryviews and raw pointers. The Cython surface releases the GIL
  around any work that takes more than a few microseconds.
- **One GIL release per page, not per string.** `decode_page` /
  `decode_with_table` must wrap their entire kernel call in a single
  `with nogil:` block. Allocate Python objects (the `list[bytes]`
  result) only after the kernel returns. This is what lets callers
  fan out across `ThreadPoolExecutor` and actually scale — the
  pre-`xmh_decode_page` per-string release/reacquire pattern
  anti-scaled at 4+ workers.
- **Tiny public API.** Five module-level functions: `swap_bytes`,
  `decompress_encode_array`, `build_table`, `decode_with_table`,
  `decode_page`. Resist adding more — the helpers exist mainly for
  testability and for callers that want to amortize work across pages.
- **`bytes` in, `bytes` out.** No `str` / `unicode` returns. The
  extension emits raw `bytes`; the caller picks between `latin-1` and
  paired UTF-16LE based on the page's `character_set_type_identifier`.
  The one charset-aware option, `charset_mode='single', charset_byte=cb`,
  performs the spec-defined `CharacterSetUsed` reinsertion as a byte
  interleave (still `bytes` out) so the caller can `b.decode('utf-16-le')`
  directly. That is the only place charset state lives in the extension.
- **Decode table is flat.** A single `2^max_len` array of `uint16_t`
  packed as `(symbol << 8) | code_len`. `max_len ≤ 15`, so the worst
  case is 64 KB. Don't add a two-level table without a measured reason.

## Where the lines are

- The kernel is small enough to read in one sitting. Don't refactor it
  into more files without a measured win.
- `decompress_encode_array`, `swap_bytes`, and `build_table` exist for
  parity with the pure-Python reference and to make testing easy.
  Removing them would silently break downstream code that builds tables
  out-of-band.
- `decode_page` is the single entry point most callers should use.
  Optimizations live behind it; the API stays stable.

## Build / test workflow

```bash
pip install -e .
pytest tests/ -v
python3 bench/bench_decode.py            # optional micro-bench
```

`tests/test_basic.py` cross-checks every helper against a pure-Python
reference (pbixray). `tests/test_pbix.py` decodes pages out of real
`.pbix` files; it auto-skips when fixtures aren't present.

After any change to the C kernel or Cython surface, rebuild and re-run
the full test suite. Build warnings (`-Wall -Wextra`) should stay at
zero.

## Things to avoid

- **Don't expand scope.** No encoder, no other alphabets, no other
  bitstream conventions, no Python-level charset conversion (`str`
  outputs, `encoding=` knobs) inside the extension. The
  `CharacterSetUsed` interleave for single-charset pages is spec-defined
  and stays; everything beyond that belongs in the caller. See "Scope
  and non-goals" in [README.md](README.md).
- **Don't add dependencies.** The runtime dependency set is empty by
  design. Build-time needs only Cython and a C compiler.
- **Don't reintroduce per-symbol Python.** Any change that puts Python
  bytecode inside the per-symbol loop is a regression.
- **Don't paper over correctness with `errors='ignore'` or similar.**
  Decode failures should raise; the caller decides how to recover.

## Sibling packages

`xmhuffman` is one of three thin Cython wrappers around Microsoft
column-store formats. The other two follow the same conventions:

- [xpress8-python](https://github.com/Hugoberry/xpress8-python)
- [xpress9-python](https://github.com/Hugoberry/xpress9-python)

Match their style (`setup.py`, build flags, GIL handling) when in doubt.
