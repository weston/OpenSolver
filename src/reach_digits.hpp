#pragma once
// Reaches as whole numbers, for the sums of a compact byte table (#507, Solver::allin_tri_value).
//
// A float times a byte has to be widened and converted, sixteen bytes to four vectors, before it is a
// product; a byte times a byte is summed sixteen to an instruction where the machine has one for it (ARM
// SDOT), and the sum is exact. So the reaches of one value are held as fixed point: in units of the
// largest of them over kReachUnits, and each whole number q <= kReachUnits as three signed digits of base
// 256, q = d[0] + 256 * d[1] + 65536 * d[2] with every digit in [-128, 127] (0x7f7f7f is the largest
// number that has such digits).
//
// What it costs in exactness: a reach is off by at most half a unit, 2^-24 of the largest reach, and the
// sum of the products by nothing. So sum_j reach[j] * row[j] is off by at most unit / 2 * sum_j |row[j]|.
// The float sum it stands in for rounds every product to 2^-24 of itself and every partial sum to 2^-24
// of the sum so far.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>

#include "cards.hpp"

// Where each of the three sums is one instruction for sixteen bytes. Elsewhere (x86 before VNNI has no such
// instruction) the float sum stays; -DPIO_NO_ALLIN_DIGITS keeps it here too.
#if defined(__aarch64__) && defined(__ARM_FEATURE_DOTPROD) && !defined(PIO_NO_ALLIN_DIGITS)
#define PIO_ALLIN_DIGITS 1
#include <arm_neon.h>
#else
#define PIO_ALLIN_DIGITS 0
#endif

namespace pio {

constexpr int32_t kReachUnits = 0x7f7f7f;

struct ReachDigits {
  alignas(64) int8_t d[3][NUM_COMBOS + 16];  // 16 to spare: the last block of a row is read whole
  double unit = 0.0;                         // one unit of q, as a reach
  // The digits of r[0..n), n <= NUM_COMBOS. False when the reaches have no such form: none of them is
  // positive, one is negative or not a number, or their float sum has no end. The caller then sums in
  // float.
  bool set(const float* r, int n) {
    float mx = 0.0f, mn = 0.0f, all = 0.0f;
    for (int i = 0; i < n; ++i) mx = std::max(mx, r[i]), mn = std::min(mn, r[i]), all += r[i];
    if (!(mx > 0.0f) || mn < 0.0f || !std::isfinite(all)) return false;
    const double up = static_cast<double>(kReachUnits) / mx;
    unit = static_cast<double>(mx) / kReachUnits;
    int32_t q[NUM_COMBOS];
    for (int i = 0; i < n; ++i) q[i] = static_cast<int32_t>(r[i] * up + 0.5);
    for (int k = 0; k < 3; ++k) {
      for (int i = 0; i < n; ++i) {
        const int8_t digit = static_cast<int8_t>(static_cast<uint8_t>(q[i] & 0xff));
        d[k][i] = digit;
        q[i] = (q[i] - digit) >> 8;
      }
      std::memset(d[k] + n, 0, 16);
    }
    return true;
  }
  int32_t whole(int i) const { return d[0][i] + 256 * d[1][i] + 65536 * d[2][i]; }
};

// sum_j q[j] * row[j] over j < n, exactly. (No sum of digit * byte passes 1,326 * 128 * 128 < 2^25.)
inline int64_t tri_dot_digits_plain(const ReachDigits& q, const int8_t* row, int n) {
  int32_t a = 0, b = 0, c = 0;
  for (int j = 0; j < n; ++j) {
    const int32_t e = row[j];
    a += q.d[0][j] * e;
    b += q.d[1][j] * e;
    c += q.d[2][j] * e;
  }
  return a + 256 * static_cast<int64_t>(b) + 65536 * static_cast<int64_t>(c);
}

#if PIO_ALLIN_DIGITS
// In a function of its own, as tri_dot is (solver.cpp).
[[gnu::noinline]] inline int64_t tri_dot_digits(const ReachDigits& q, const int8_t* row, int n) {
  int32x4_t a0 = vdupq_n_s32(0), a1 = a0, a2 = a0, b0 = a0, b1 = a0, b2 = a0;
  int j = 0;
  for (; j + 32 <= n; j += 32) {
    const int8x16_t e = vld1q_s8(row + j), f = vld1q_s8(row + j + 16);
    a0 = vdotq_s32(a0, vld1q_s8(q.d[0] + j), e);
    a1 = vdotq_s32(a1, vld1q_s8(q.d[1] + j), e);
    a2 = vdotq_s32(a2, vld1q_s8(q.d[2] + j), e);
    b0 = vdotq_s32(b0, vld1q_s8(q.d[0] + j + 16), f);
    b1 = vdotq_s32(b1, vld1q_s8(q.d[1] + j + 16), f);
    b2 = vdotq_s32(b2, vld1q_s8(q.d[2] + j + 16), f);
  }
  for (; j < n; j += 16) {
    int8x16_t e;
    if (j + 16 <= n) {
      e = vld1q_s8(row + j);
    } else {
      // The end of the row, filled up with zeros: nothing is read past the row, and the digits past n
      // (the other hands', or the sixteen to spare) meet a zero.
      int8_t rest[16] = {};
      std::memcpy(rest, row + j, static_cast<size_t>(n - j));
      e = vld1q_s8(rest);
    }
    a0 = vdotq_s32(a0, vld1q_s8(q.d[0] + j), e);
    a1 = vdotq_s32(a1, vld1q_s8(q.d[1] + j), e);
    a2 = vdotq_s32(a2, vld1q_s8(q.d[2] + j), e);
  }
  return vaddlvq_s32(vaddq_s32(a0, b0)) + 256 * vaddlvq_s32(vaddq_s32(a1, b1)) + 65536 * vaddlvq_s32(vaddq_s32(a2, b2));
}
#else
inline int64_t tri_dot_digits(const ReachDigits& q, const int8_t* row, int n) { return tri_dot_digits_plain(q, row, n); }
#endif

}  // namespace pio
