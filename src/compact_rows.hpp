#pragma once
// Compact 16-bit row moves (#239, see GameTree::keep_sets). AMD Zen 4 / 5 run the memory forms of
// vpexpandw / vpcompressw as slow microcode, so values move with plain masked loads and stores of exactly
// popcount(mask) elements (masked-off lanes never touch memory, so nothing reads or writes past a row)
// and expand / compress in registers.
#include "game.hpp"

#if PIO_COMPACT_ROWS_AVX512
#include <immintrin.h>

namespace pio {

inline __mmask32 low_bits32(int k) { return k >= 32 ? 0xffffffffu : static_cast<__mmask32>((1u << k) - 1u); }
inline __mmask16 low_bits16(int k) { return k >= 16 ? static_cast<__mmask16>(0xffff) : static_cast<__mmask16>((1u << k) - 1u); }

// The 32 hands of `keep` (bit set = kept) from the compact values at src; dropped hands' lanes are 0.
inline __m512i expand32(__mmask32 keep, const void* src) {
  return _mm512_maskz_expand_epi16(keep, _mm512_maskz_loadu_epi16(low_bits32(__builtin_popcount(keep)), src));
}
inline __m256i expand16(__mmask16 keep, const void* src) {
  return _mm256_maskz_expand_epi16(keep, _mm256_maskz_loadu_epi16(low_bits16(__builtin_popcount(keep)), src));
}
// The kept lanes of v, packed, to dst; returns how many.
inline int compress32(__mmask32 keep, __m512i v, void* dst) {
  const int k = __builtin_popcount(keep);
  _mm512_mask_storeu_epi16(dst, low_bits32(k), _mm512_maskz_compress_epi16(keep, v));
  return k;
}

}  // namespace pio
#endif

// NEON (#568): 8 hands at a time, one byte of the kept bits. A table lookup moves 16-bit values between
// their packed positions and their hands' lanes; lanes of dropped hands read 0. Values move, nothing is
// computed, so the results are the portable loops' to the bit.
#if !PIO_COMPACT_ROWS_AVX512 && PIO_COMPACT_ROWS && defined(__aarch64__) && defined(__ARM_NEON)  // vqtbl1q_u8: A64 only
#define PIO_COMPACT_ROWS_NEON 1
#include <arm_neon.h>
#include <array>
#include <cstdint>

namespace pio {

struct CompactTables {
  // expand[m]: for lane i (a hand) the bytes of its packed value, 0xff (reads 0) where bit i of m is clear.
  // compress[m]: for packed slot j the bytes of the lane of the j-th set bit, 0xff past popcount(m).
  alignas(16) uint8_t expand[256][16];
  alignas(16) uint8_t compress[256][16];
  constexpr CompactTables() : expand{}, compress{} {
    for (int m = 0; m < 256; ++m) {
      int k = 0;
      for (int i = 0; i < 8; ++i) {
        const bool kept = (m >> i) & 1;
        expand[m][2 * i] = kept ? static_cast<uint8_t>(2 * k) : 0xff;
        expand[m][2 * i + 1] = kept ? static_cast<uint8_t>(2 * k + 1) : 0xff;
        compress[m][2 * i] = compress[m][2 * i + 1] = 0xff;
        k += kept;
      }
      k = 0;
      for (int i = 0; i < 8; ++i)
        if ((m >> i) & 1) {
          compress[m][2 * k] = static_cast<uint8_t>(2 * i);
          compress[m][2 * k + 1] = static_cast<uint8_t>(2 * i + 1);
          ++k;
        }
    }
  }
};
inline constexpr CompactTables kCompactTables{};

// The 8 hands of byte m from the packed values at src (16 bytes are read: the caller makes sure they are the
// node's own).
inline uint16x8_t expand8(unsigned m, const void* src) {
  return vreinterpretq_u16_u8(vqtbl1q_u8(vld1q_u8(static_cast<const uint8_t*>(src)), vld1q_u8(kCompactTables.expand[m])));
}
// The kept lanes of v, packed into the low lanes (the rest 0).
inline uint16x8_t compress8(unsigned m, uint16x8_t v) {
  return vreinterpretq_u16_u8(vqtbl1q_u8(vreinterpretq_u8_u16(v), vld1q_u8(kCompactTables.compress[m])));
}

}  // namespace pio
#else
#define PIO_COMPACT_ROWS_NEON 0
#endif

