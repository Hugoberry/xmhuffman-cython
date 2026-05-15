from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t
from libc.stddef cimport size_t

cdef extern from "xmhuffman_kernel.h":
    ctypedef Py_ssize_t xmh_ssize_t

    void xmh_decompress_encode_array(const uint8_t *in128, uint8_t *out256) nogil
    void xmh_swap_pairs(const uint8_t *in_buf, uint8_t *out_buf, size_t n) nogil
    int xmh_build_table(const uint8_t *lengths256,
                        uint16_t *table,
                        unsigned *out_max_len) nogil
    xmh_ssize_t xmh_decode_one(const uint8_t *swapped, size_t swapped_len,
                               const uint16_t *table, unsigned max_len,
                               uint64_t start_bit, uint64_t end_bit,
                               uint8_t *out, size_t out_cap) nogil

    xmh_ssize_t xmh_decode_page(const uint8_t *swapped, size_t swapped_len,
                                const uint16_t *table, unsigned max_len,
                                const uint32_t *offsets,
                                xmh_ssize_t n_strings,
                                uint64_t total_bits,
                                int charset_mode, uint8_t charset_byte,
                                uint8_t *out, size_t out_cap,
                                xmh_ssize_t *out_end_offsets) nogil
