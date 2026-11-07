#pragma once
// Compact 16-bit row moves (#239, see GameTree::keep_sets). AMD Zen 4 / 5 run the memory forms of
// vpexpandw / vpcompressw as slow microcode, so values move with plain masked loads and stores of exactly
// popcount(mask) elements (masked-off lanes never touch memory, so nothing reads or writes past a row)
// and expand / compress in registers.
#include "game.hpp"

#if PIO_COMPACT_ROWS
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
