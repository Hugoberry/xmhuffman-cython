#ifndef XMHUFFMAN_KERNEL_H
#define XMHUFFMAN_KERNEL_H

#include <stddef.h>
#include <stdint.h>

#if defined(_MSC_VER)
#include <BaseTsd.h>
typedef SSIZE_T xmh_ssize_t;
#else
#include <sys/types.h>
typedef ssize_t xmh_ssize_t;
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define XMH_MAX_CODE_LEN 15
#define XMH_TABLE_MAX_SIZE (1u << XMH_MAX_CODE_LEN)  /* 32768 u16 entries */

/* Expand 128-byte nibble-packed code lengths into 256 plain bytes.
 * Low nibble of byte i is the length for symbol 2i, high nibble for 2i+1. */
void xmh_decompress_encode_array(const uint8_t *in128, uint8_t *out256);

/* Pair-swap: bytes 2k and 2k+1 swap; a trailing odd byte is copied as-is.
 * Safe to alias (in == out). */
void xmh_swap_pairs(const uint8_t *in_buf, uint8_t *out_buf, size_t n);

/* Build the flat canonical-Huffman decode table.
 *   lengths256 : 256 code lengths in [0, 15]
 *   table      : caller-provided u16 buffer, must hold 1 << *out_max_len
 *                entries. Safe upper bound: XMH_TABLE_MAX_SIZE.
 *                Each entry packs (symbol << 8) | code_len.
 *   out_max_len: receives the max non-zero code length (0 if alphabet empty).
 * Returns 0 on success, negative on invalid code (Kraft failure). */
int xmh_build_table(const uint8_t *lengths256,
                    uint16_t *table,
                    unsigned *out_max_len);

/* Decode one bit-slice [start_bit, end_bit) of a *swapped* bitstream into
 * out[0..out_cap). Returns bytes written, or -1 on overflow. */
xmh_ssize_t xmh_decode_one(const uint8_t *swapped, size_t swapped_len,
                           const uint16_t *table, unsigned max_len,
                           uint64_t start_bit, uint64_t end_bit,
                           uint8_t *out, size_t out_cap);

/* Decode every string on a page into a single contiguous output buffer.
 *
 *   offsets[i]         : per-string start bit (sorted ascending)
 *   total_bits         : end-of-stream sentinel for offsets[n_strings - 1]
 *   charset_mode       : 0 = general (one byte per symbol),
 *                        1 = single  (write [symbol, charset_byte] per
 *                                     symbol — UTF-16-LE-ready)
 *   out, out_cap       : caller-owned write buffer
 *   out_end_offsets    : caller-owned array of length n_strings; on
 *                        successful return, holds the cumulative byte
 *                        offset (one past the last written byte) of each
 *                        decoded string. String i occupies
 *                        out[out_end_offsets[i-1] .. out_end_offsets[i]],
 *                        with out_end_offsets[-1] treated as 0.
 *
 * Returns total bytes written, or a negative error code (matching
 * xmh_decode_one: -1 = overflow, -2 = corrupt stream).
 *
 * No heap allocations, no Python, fully nogil-safe. */
xmh_ssize_t xmh_decode_page(const uint8_t *swapped, size_t swapped_len,
                            const uint16_t *table, unsigned max_len,
                            const uint32_t *offsets, xmh_ssize_t n_strings,
                            uint64_t total_bits,
                            int charset_mode, uint8_t charset_byte,
                            uint8_t *out, size_t out_cap,
                            xmh_ssize_t *out_end_offsets);

#ifdef __cplusplus
}
#endif

#endif /* XMHUFFMAN_KERNEL_H */
