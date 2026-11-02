#include "eval.hpp"

namespace pio {

namespace {

inline int top_bit(uint32_t x) { return 31 - __builtin_clz(x); }

// Highest rank (0..12) of a straight inside a 13-bit rank mask, or -1.
inline int straight_high(uint32_t m) {
  // x bit (r+1) = rank r; bit 0 = ace playing low.
  uint32_t x = (m << 1) | ((m >> 12) & 1u);
  uint32_t r = x & (x >> 1) & (x >> 2) & (x >> 3) & (x >> 4);
  if (!r) return -1;
  return top_bit(r) + 4 - 1;  // top card bit index in x, minus 1 -> rank
}

// Pack the top `k` ranks of mask m into nibbles starting at the given shift (descending).
inline uint32_t pack_top(uint32_t m, int k, int shift) {
  uint32_t v = 0;
  for (int i = 0; i < k && m; ++i) {
    int r = top_bit(m);
    m &= ~(1u << r);
    v |= static_cast<uint32_t>(r) << shift;
    shift -= 4;
  }
  return v;
}

inline uint32_t eval_from_masks(const uint32_t sm[4], const uint8_t cnt[13]) {
  for (int s = 0; s < 4; ++s) {
    if (__builtin_popcount(sm[s]) >= 5) {
      int st = straight_high(sm[s]);
      if (st >= 0) return (HC_STRAIGHT_FLUSH << 20) | (static_cast<uint32_t>(st) << 16);
      return (HC_FLUSH << 20) | pack_top(sm[s], 5, 16);
    }
  }
  uint32_t all = sm[0] | sm[1] | sm[2] | sm[3];
  uint32_t quads = 0, trips = 0, pairs = 0;
  for (int r = 0; r < 13; ++r) {
    switch (cnt[r]) {
      case 4: quads |= 1u << r; break;
      case 3: trips |= 1u << r; break;
      case 2: pairs |= 1u << r; break;
      default: break;
    }
  }
  if (quads) {
    int q = top_bit(quads);
    uint32_t rest = all & ~(1u << q);
    return (HC_QUADS << 20) | (static_cast<uint32_t>(q) << 16) | pack_top(rest, 1, 12);
  }
  if (trips) {
    int t = top_bit(trips);
    uint32_t pair_cands = (trips & ~(1u << t)) | pairs;
    if (pair_cands) {
      int p = top_bit(pair_cands);
      return (HC_FULL_HOUSE << 20) | (static_cast<uint32_t>(t) << 16) | (static_cast<uint32_t>(p) << 12);
    }
  }
  int st = straight_high(all);
  if (st >= 0) return (HC_STRAIGHT << 20) | (static_cast<uint32_t>(st) << 16);
  if (trips) {
    int t = top_bit(trips);
    uint32_t rest = all & ~(1u << t);
    return (HC_TRIPS << 20) | (static_cast<uint32_t>(t) << 16) | pack_top(rest, 2, 12);
  }
  if (pairs) {
    int p1 = top_bit(pairs);
    uint32_t rem = pairs & ~(1u << p1);
    if (rem) {
      int p2 = top_bit(rem);
      uint32_t rest = all & ~(1u << p1) & ~(1u << p2);
      return (HC_TWO_PAIR << 20) | (static_cast<uint32_t>(p1) << 16) | (static_cast<uint32_t>(p2) << 12) |
             pack_top(rest, 1, 8);
    }
    uint32_t rest = all & ~(1u << p1);
    return (HC_PAIR << 20) | (static_cast<uint32_t>(p1) << 16) | pack_top(rest, 3, 12);
  }
  return (HC_HIGH_CARD << 20) | pack_top(all, 5, 16);
}

}  // namespace

uint32_t evaluate(const int* cards, int n) {
  uint32_t sm[4] = {0, 0, 0, 0};
  uint8_t cnt[13] = {0};
  for (int i = 0; i < n; ++i) {
    int c = cards[i];
    sm[c & 3] |= 1u << (c >> 2);
    cnt[c >> 2]++;
  }
  return eval_from_masks(sm, cnt);
}

uint32_t evaluate_mask(uint64_t mask) {
  uint32_t sm[4] = {0, 0, 0, 0};
  uint8_t cnt[13] = {0};
  while (mask) {
    int c = __builtin_ctzll(mask);
    mask &= mask - 1;
    sm[c & 3] |= 1u << (c >> 2);
    cnt[c >> 2]++;
  }
  return eval_from_masks(sm, cnt);
}

}  // namespace pio
