#include "xmhuffman_kernel.h"

#include <string.h>

#if defined(_MSC_VER)
#include <stdlib.h>
#define XMH_BSWAP64(x) _byteswap_uint64(x)
#else
#define XMH_BSWAP64(x) __builtin_bswap64(x)
#endif

void xmh_decompress_encode_array(const uint8_t *in128, uint8_t *out256)
{
    for (size_t i = 0; i < 128; ++i) {
        uint8_t b = in128[i];
        out256[2 * i]     = (uint8_t)(b & 0x0fu);
        out256[2 * i + 1] = (uint8_t)((b >> 4) & 0x0fu);
    }
}

void xmh_swap_pairs(const uint8_t *in_buf, uint8_t *out_buf, size_t n)
{
    size_t even = n & ~(size_t)1;
    for (size_t i = 0; i < even; i += 2) {
        uint8_t a = in_buf[i];
        uint8_t b = in_buf[i + 1];
        out_buf[i]     = b;
        out_buf[i + 1] = a;
    }
    if (n & 1) {
        out_buf[n - 1] = in_buf[n - 1];
    }
}

int xmh_build_table(const uint8_t *lengths256,
                    uint16_t *table,
                    unsigned *out_max_len)
{
    /* Count symbols by length. Per [MS-XLDM] §2.7.4.1.2 valid codeword
     * lengths in this format are 2..15; we accept 1..15 here because a
     * length-1 code is still a well-formed canonical Huffman code and
     * rejecting it would offer no real safety. 0 means "unused". */
    uint32_t count[XMH_MAX_CODE_LEN + 1] = {0};
    unsigned max_len = 0;
    for (unsigned s = 0; s < 256; ++s) {
        uint8_t L = lengths256[s];
        if (L == 0) continue;
        if (L > XMH_MAX_CODE_LEN) return -1;
        count[L]++;
        if (L > max_len) max_len = L;
    }

    if (max_len == 0) {
        *out_max_len = 0;
        return 0;
    }

    /* Kraft inequality: sum_L count[L] * 2^(max_len - L) == 2^max_len.
     * Equivalently, the canonical "code" counter must end exactly at 1
     * shifted into the (max_len+1)'th bit. Use the classic count-based
     * check. */
    {
        uint32_t left = 1;
        for (unsigned L = 1; L <= max_len; ++L) {
            left <<= 1;
            if (count[L] > left) return -2;
            left -= count[L];
        }
        if (left != 0) return -3;
    }

    /* first_code[L] = first canonical code value at length L. */
    uint32_t first_code[XMH_MAX_CODE_LEN + 2] = {0};
    {
        uint32_t code = 0;
        for (unsigned L = 1; L <= max_len; ++L) {
            code = (code + count[L - 1]) << 1;
            first_code[L] = code;
        }
    }

    /* Assign codes in symbol order and splat into the table. */
    uint32_t next_code[XMH_MAX_CODE_LEN + 2];
    memcpy(next_code, first_code, sizeof(next_code));

    for (unsigned s = 0; s < 256; ++s) {
        uint8_t L = lengths256[s];
        if (L == 0) continue;
        uint32_t code = next_code[L]++;
        unsigned pad = max_len - L;
        uint32_t base = code << pad;
        uint32_t span = 1u << pad;
        uint16_t entry = (uint16_t)((s << 8) | L);
        for (uint32_t k = 0; k < span; ++k) {
            table[base + k] = entry;
        }
    }

    *out_max_len = max_len;
    return 0;
}

/* Load 8 big-endian bytes starting at byte offset `byte`. Pads with zero
 * past the buffer end. */
static inline uint64_t xmh_load_be64_safe(const uint8_t *p, size_t p_len,
                                          size_t byte)
{
    uint64_t w;
    if (byte + 8 <= p_len) {
        memcpy(&w, p + byte, 8);
    } else {
        uint8_t tmp[8] = {0};
        size_t avail = (byte < p_len) ? (p_len - byte) : 0;
        if (avail > 8) avail = 8;
        if (avail) memcpy(tmp, p + byte, avail);
        memcpy(&w, tmp, 8);
    }
    return XMH_BSWAP64(w);
}

xmh_ssize_t xmh_decode_one(const uint8_t *swapped, size_t swapped_len,
                           const uint16_t *table, unsigned max_len,
                           uint64_t start_bit, uint64_t end_bit,
                           uint8_t *out, size_t out_cap)
{
    if (max_len == 0 || start_bit >= end_bit) return 0;

    uint64_t bit = start_bit;
    uint8_t *op = out;
    uint8_t *op_end = out + out_cap;
    unsigned shift_const = 64u - max_len;
    uint64_t mask = ((uint64_t)1 << max_len) - 1;

    while (bit < end_bit) {
        if (op >= op_end) return -1;
        size_t byte = (size_t)(bit >> 3);
        unsigned off = (unsigned)(bit & 7u);
        uint64_t w = xmh_load_be64_safe(swapped, swapped_len, byte);
        unsigned idx = (unsigned)((w >> (shift_const - off)) & mask);
        uint16_t e = table[idx];
        unsigned code_len = e & 0xffu;
        if (code_len == 0) return -2; /* corrupt stream: hit unused slot */
        *op++ = (uint8_t)(e >> 8);
        bit += code_len;
    }
    return (xmh_ssize_t)(op - out);
}

xmh_ssize_t xmh_decode_page(const uint8_t *swapped, size_t swapped_len,
                            const uint16_t *table, unsigned max_len,
                            const uint32_t *offsets, xmh_ssize_t n_strings,
                            uint64_t total_bits,
                            int charset_mode, uint8_t charset_byte,
                            uint8_t *out, size_t out_cap,
                            xmh_ssize_t *out_end_offsets)
{
    if (n_strings <= 0) return 0;
    if (max_len == 0) {
        /* All strings are empty. */
        for (xmh_ssize_t i = 0; i < n_strings; ++i) out_end_offsets[i] = 0;
        return 0;
    }

    unsigned shift_const = 64u - max_len;
    uint64_t mask = ((uint64_t)1 << max_len) - 1;
    size_t cursor = 0;
    const int single = (charset_mode != 0);

    for (xmh_ssize_t i = 0; i < n_strings; ++i) {
        uint64_t start_bit = (uint64_t)offsets[i];
        uint64_t end_bit = (i + 1 == n_strings)
            ? total_bits
            : (uint64_t)offsets[i + 1];
        uint64_t bit = start_bit;

        while (bit < end_bit) {
            size_t byte = (size_t)(bit >> 3);
            unsigned off = (unsigned)(bit & 7u);
            uint64_t w = xmh_load_be64_safe(swapped, swapped_len, byte);
            unsigned idx = (unsigned)((w >> (shift_const - off)) & mask);
            uint16_t e = table[idx];
            unsigned code_len = e & 0xffu;
            if (code_len == 0) return -2; /* corrupt stream */
            uint8_t sym = (uint8_t)(e >> 8);
            if (single) {
                if (cursor + 2 > out_cap) return -1;
                out[cursor++] = sym;
                out[cursor++] = charset_byte;
            } else {
                if (cursor >= out_cap) return -1;
                out[cursor++] = sym;
            }
            bit += code_len;
        }
        out_end_offsets[i] = (xmh_ssize_t)cursor;
    }
    return (xmh_ssize_t)cursor;
}
