# AGENTS.md

Instructions for AI coding assistants working in this repository.

## What this project is

`xmhuffman` is a Cython extension that decodes canonical-Huffman string
dictionary pages from xVelocity / Vertipaq column stores (`.pbix`
DataModel, Power Pivot `item.data`). It is a focused, single-purpose
package — not a general Huffman library.

The format itself is described in [xmhuffman-python.md](xmhuffman-python.md)
and follows what the open-source [pbixray](https://github.com/Hugoberry/pbixray)
reader uses. All documentation in this repo describes the on-disk file
format only.

## Project conventions

- **Hot path stays in C.** Anything per-symbol or per-string in
  performance-critical code belongs in `src/xmhuffman_kernel.c`. Cython
  is the glue, not the algorithm.
- **No Python objects in the inner loop.** The kernel takes typed
  memoryviews and raw pointers. The Cython surface releases the GIL
  around any work that takes more than a few microseconds.
- **Tiny public API.** Five module-level functions: `swap_bytes`,
  `decompress_encode_array`, `build_table`, `decode_with_table`,
  `decode_page`. Resist adding more — the helpers exist mainly for
  testability and for callers that want to amortize work across pages.
- **`bytes` in, `bytes` out.** No charset interpretation inside the
  extension. The decoder emits the raw symbol stream; the caller picks
  between `latin-1` and paired UTF-16LE based on the page's
  `character_set_type_identifier`.
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
  bitstream conventions, no charset conversion inside the extension.
  See "Non-goals" in [xmhuffman-python.md](xmhuffman-python.md).
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
