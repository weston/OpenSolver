#pragma once
#include <bit>
#include <cmath>
#include <cstdint>

namespace pio {
inline constexpr float fp16_sum_scale = 32.f;

inline uint16_t fp16_sum_word(float value) {
  const float scaled = value * fp16_sum_scale;
#if defined(__FLT16_MANT_DIG__)
  return std::bit_cast<uint16_t>(static_cast<_Float16>(scaled));
#else
  const uint32_t bits = std::bit_cast<uint32_t>(scaled);
  if (scaled == 0.f) return static_cast<uint16_t>(bits >> 16);
  if (bits < 0x38800000u) return static_cast<uint16_t>(std::nearbyint(std::ldexp(scaled, 24)));
  return static_cast<uint16_t>((bits + 0xfffu + ((bits >> 13) & 1u) - 0x38000000u) >> 13);
#endif
}

inline float scaled_fp16_sum_value(uint16_t word) {
#if defined(__FLT16_MANT_DIG__)
  return float(std::bit_cast<_Float16>(word));
#else
  if (word == 0x8000u) return -0.f;
  return word < 1024 ? std::ldexp(float(word), -24) :
      std::bit_cast<float>((uint32_t(word) << 13) + 0x38000000u);
#endif
}

inline float fp16_sum_value(uint16_t word) {
  return scaled_fp16_sum_value(word) / fp16_sum_scale;
}
}  // namespace pio
