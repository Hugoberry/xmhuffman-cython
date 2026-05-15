# distutils: language = c
# cython: boundscheck=False, wraparound=False, initializedcheck=False, language_level=3

"""Cython wrapper around the canonical-Huffman decoder used by Vertipaq /
xVelocity dictionary pages (PBIX DataModel, Power Pivot item.data).

See xmhuffman-python.md for format details."""

from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy

from cpython.bytes cimport PyBytes_FromStringAndSize
from cpython.list cimport PyList_New, PyList_SET_ITEM
from cpython.ref cimport Py_INCREF

from xmhuffman cimport (
    xmh_decompress_encode_array,
    xmh_swap_pairs,
    xmh_build_table,
    xmh_decode_one,
    xmh_decode_page,
)

DEF _TABLE_MAX = 1 << 15  # 32768 u16 entries = 64 KB

DEF _CHARSET_GENERAL = 0
DEF _CHARSET_SINGLE = 1


cdef int _parse_charset_mode(object mode) except -1:
    """Map ``'general'``/``'multi'`` and ``'single'`` to internal flags.

    Mirrors MS-XLDM ``XM_HUFFMAN_MULTICHARSET`` (0xABA92) vs
    ``XM_HUFFMAN_SINGLECHARSET`` (0xABA91).
    """
    if mode is None or mode == 'general' or mode == 'multi':
        return _CHARSET_GENERAL
    if mode == 'single':
        return _CHARSET_SINGLE
    raise ValueError("charset_mode must be 'general' or 'single'")


def swap_bytes(buffer):
    """Pair-swap a bytes-like buffer; trailing odd byte left as-is."""
    cdef const unsigned char[::1] inv = buffer
    cdef Py_ssize_t n = inv.shape[0]
    cdef bytes out = PyBytes_FromStringAndSize(NULL, n)
    cdef unsigned char *op
    if n == 0:
        return out
    op = <unsigned char *>(<char *>out)
    with nogil:
        xmh_swap_pairs(&inv[0], op, <size_t>n)
    return out


def decompress_encode_array(buffer):
    """Expand the 128-byte nibble-packed code-length array to 256 bytes."""
    cdef const unsigned char[::1] inv = buffer
    cdef bytes out
    cdef unsigned char *op
    if inv.shape[0] != 128:
        raise ValueError("encode_array must be exactly 128 bytes")
    out = PyBytes_FromStringAndSize(NULL, 256)
    op = <unsigned char *>(<char *>out)
    with nogil:
        xmh_decompress_encode_array(&inv[0], op)
    return out


def build_table(encode_array_128):
    """Build the canonical-Huffman decode table.

    Returns ``(table_bytes, max_len)``. ``table_bytes`` is a packed
    ``2**max_len`` array of u16 entries (native endian), each encoding
    ``(symbol << 8) | code_len``.
    """
    cdef const unsigned char[::1] inv = encode_array_128
    cdef uint8_t lengths[256]
    cdef uint16_t *tbl
    cdef unsigned max_len = 0
    cdef int rc
    cdef Py_ssize_t nbytes
    cdef bytes out

    if inv.shape[0] != 128:
        raise ValueError("encode_array must be exactly 128 bytes")
    tbl = <uint16_t *>malloc(_TABLE_MAX * sizeof(uint16_t))
    if tbl is NULL:
        raise MemoryError()
    try:
        with nogil:
            xmh_decompress_encode_array(&inv[0], lengths)
            rc = xmh_build_table(lengths, tbl, &max_len)
        if rc != 0:
            raise ValueError("invalid Huffman code lengths (rc=%d)" % rc)
        if max_len == 0:
            return b"", 0
        nbytes = (1 << max_len) * sizeof(uint16_t)
        out = PyBytes_FromStringAndSize(<char *>tbl, nbytes)
        return out, int(max_len)
    finally:
        free(tbl)


cdef list _decode_loop(const uint8_t *swapped, size_t swapped_len,
                       const uint16_t *table, unsigned max_len,
                       const uint32_t *offsets, Py_ssize_t n_strings,
                       uint64_t total_bits,
                       int charset_mode, uint8_t charset_byte):
    """Decode all strings on a page into a ``list[bytes]``.

    Holds the GIL once per page: the entire bit-stream walk runs inside
    a single ``with nogil:`` block via ``xmh_decode_page``. Python
    objects are allocated only afterwards, in a tight slicing pass. This
    is what makes the function safe to call from a thread pool.

    When ``charset_mode == _CHARSET_SINGLE`` each output element is
    ``2 * nwritten`` bytes long: the page-level ``CharacterSetUsed``
    byte is interleaved as the UTF-16-LE high byte of every character,
    so the caller can ``b.decode('utf-16-le')`` directly.
    """
    cdef list result = PyList_New(n_strings)
    cdef Py_ssize_t i, prev, end
    cdef size_t out_cap
    cdef uint8_t *out = NULL
    cdef Py_ssize_t *ends = NULL
    cdef Py_ssize_t rc
    cdef bytes item

    if n_strings == 0:
        return result

    # Upper bound: each bit can produce at most one decoded symbol (the
    # kernel accepts codeword lengths down to 1; the spec mandates >= 2,
    # so this is loose but safe). Double for single-charset interleave.
    out_cap = <size_t>total_bits
    if charset_mode == _CHARSET_SINGLE:
        out_cap *= 2
    if out_cap == 0:
        out_cap = 1

    out = <uint8_t *>malloc(out_cap)
    if out is NULL:
        raise MemoryError()
    ends = <Py_ssize_t *>malloc(<size_t>n_strings * sizeof(Py_ssize_t))
    if ends is NULL:
        free(out)
        raise MemoryError()

    try:
        with nogil:
            rc = xmh_decode_page(swapped, swapped_len,
                                 table, max_len,
                                 offsets, <xmh_ssize_t>n_strings,
                                 total_bits,
                                 charset_mode, charset_byte,
                                 out, out_cap, ends)
        if rc < 0:
            raise ValueError("decode failure (rc=%d)" % rc)

        prev = 0
        for i in range(n_strings):
            end = ends[i]
            item = PyBytes_FromStringAndSize(<char *>(out + prev),
                                             end - prev)
            Py_INCREF(item)
            PyList_SET_ITEM(result, i, item)
            prev = end
    finally:
        free(out)
        free(ends)
    return result


cdef object _coerce_offsets_to_u32(offsets, uint32_t **out_ptr,
                                   Py_ssize_t *out_n, int *owns):
    """Return either a keepalive object (no copy) or None (owned buffer).

    On return, ``*out_ptr`` points at n_strings u32 offsets, ``*owns``
    tells the caller whether to ``free()`` it.
    """
    cdef const uint32_t[::1] mv
    cdef uint32_t *buf
    cdef Py_ssize_t n, i
    cdef list lst

    try:
        mv = offsets
        out_ptr[0] = <uint32_t *>&mv[0] if mv.shape[0] > 0 else NULL
        out_n[0] = mv.shape[0]
        owns[0] = 0
        return mv  # keep alive
    except (TypeError, ValueError):
        pass

    lst = list(offsets)
    n = len(lst)
    buf = <uint32_t *>malloc((n if n > 0 else 1) * sizeof(uint32_t))
    if buf is NULL:
        raise MemoryError()
    for i in range(n):
        buf[i] = <uint32_t>(<unsigned long long>lst[i])
    out_ptr[0] = buf
    out_n[0] = n
    owns[0] = 1
    return None


def decode_with_table(bitstream, table, max_len, offsets, store_total_bits,
                      swap=True, charset_mode='general', charset_byte=0):
    """Decode a page given an already-built table.

    ``table`` must be the bytes object returned by :func:`build_table`
    (length ``(2**max_len) * 2``).

    Parameters
    ----------
    charset_mode : {'general', 'single'}, default 'general'
        Per MS-XLDM. In ``'single'`` mode (``XM_HUFFMAN_SINGLECHARSET``,
        0xABA91) the page-level ``CharacterSetUsed`` byte is reinserted
        as the UTF-16-LE high byte of every decoded character, so each
        output element is exactly ``2 * decoded_length`` bytes and can
        be ``b.decode('utf-16-le')``-ed directly. In ``'general'`` mode
        (``XM_HUFFMAN_MULTICHARSET``, 0xABA92) the raw decoded byte
        stream is returned unchanged.
    charset_byte : int, default 0
        The page's ``CharacterSetUsed`` byte. Ignored unless
        ``charset_mode`` is ``'single'``.
    """
    cdef unsigned ml = <unsigned>max_len
    cdef const unsigned char[::1] bsv
    cdef const unsigned char[::1] tblv
    cdef Py_ssize_t n_buf, expected
    cdef uint32_t *off_ptr = NULL
    cdef Py_ssize_t n_strings = 0
    cdef int owns = 0
    cdef object keep
    cdef uint8_t *swapped_owned = NULL
    cdef const uint8_t *swapped_view
    cdef int cset = _parse_charset_mode(charset_mode)
    cdef uint8_t cb = <uint8_t>(<unsigned int>charset_byte & 0xff)

    if ml == 0:
        return [b""] * len(offsets)
    if ml > 15:
        raise ValueError("max_len must be in 0..15")

    bsv = bitstream
    tblv = table
    n_buf = bsv.shape[0]
    expected = (1 << ml) * 2
    if tblv.shape[0] != expected:
        raise ValueError("table size mismatch: got %d bytes, expected %d"
                         % (tblv.shape[0], expected))

    keep = _coerce_offsets_to_u32(offsets, &off_ptr, &n_strings, &owns)

    try:
        if swap and n_buf > 0:
            swapped_owned = <uint8_t *>malloc(<size_t>n_buf)
            if swapped_owned is NULL:
                raise MemoryError()
            with nogil:
                xmh_swap_pairs(&bsv[0], swapped_owned, <size_t>n_buf)
            swapped_view = swapped_owned
        elif n_buf > 0:
            swapped_view = &bsv[0]
        else:
            swapped_view = NULL

        return _decode_loop(swapped_view, <size_t>n_buf,
                            <const uint16_t *>&tblv[0], ml,
                            off_ptr, n_strings,
                            <uint64_t>store_total_bits,
                            cset, cb)
    finally:
        if swapped_owned is not NULL:
            free(swapped_owned)
        if owns:
            free(off_ptr)


def decode_page(bitstream, encode_array_128, offsets, store_total_bits,
                swap=True, charset_mode='general', charset_byte=0):
    """Decode every string on a Vertipaq dictionary page.

    Parameters
    ----------
    bitstream : bytes-like
        The raw (or pre-swapped) ``compressed_string_buffer``.
    encode_array_128 : bytes-like
        The 128-byte nibble-packed ``encode_array``.
    offsets : sequence of u32
        Per-string start bit offsets (sorted ascending).
    store_total_bits : int
        End-of-stream sentinel for the last string.
    swap : bool, default True
        If True, byte-pair-swap ``bitstream`` inside the extension.
    charset_mode : {'general', 'single'}, default 'general'
        See :func:`decode_with_table`.
    charset_byte : int, default 0
        See :func:`decode_with_table`.
    """
    cdef const unsigned char[::1] eav = encode_array_128
    cdef const unsigned char[::1] bsv
    cdef Py_ssize_t n_buf
    cdef uint8_t lengths[256]
    cdef uint16_t *tbl
    cdef unsigned max_len = 0
    cdef int rc
    cdef uint32_t *off_ptr = NULL
    cdef Py_ssize_t n_strings = 0
    cdef int owns = 0
    cdef object keep
    cdef uint8_t *swapped_owned = NULL
    cdef const uint8_t *swapped_view
    cdef int cset = _parse_charset_mode(charset_mode)
    cdef uint8_t cb = <uint8_t>(<unsigned int>charset_byte & 0xff)

    if eav.shape[0] != 128:
        raise ValueError("encode_array must be exactly 128 bytes")
    bsv = bitstream
    n_buf = bsv.shape[0]

    tbl = <uint16_t *>malloc(_TABLE_MAX * sizeof(uint16_t))
    if tbl is NULL:
        raise MemoryError()

    keep = _coerce_offsets_to_u32(offsets, &off_ptr, &n_strings, &owns)

    try:
        with nogil:
            xmh_decompress_encode_array(&eav[0], lengths)
            rc = xmh_build_table(lengths, tbl, &max_len)
        if rc != 0:
            raise ValueError("invalid Huffman code lengths (rc=%d)" % rc)
        if max_len == 0:
            return [b""] * n_strings

        if swap and n_buf > 0:
            swapped_owned = <uint8_t *>malloc(<size_t>n_buf)
            if swapped_owned is NULL:
                raise MemoryError()
            with nogil:
                xmh_swap_pairs(&bsv[0], swapped_owned, <size_t>n_buf)
            swapped_view = swapped_owned
        elif n_buf > 0:
            swapped_view = &bsv[0]
        else:
            swapped_view = NULL

        return _decode_loop(swapped_view, <size_t>n_buf,
                            tbl, max_len,
                            off_ptr, n_strings,
                            <uint64_t>store_total_bits,
                            cset, cb)
    finally:
        free(tbl)
        if swapped_owned is not NULL:
            free(swapped_owned)
        if owns:
            free(off_ptr)
