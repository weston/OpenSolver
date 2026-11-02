// Poker hand evaluator for 5, 6 or 7 cards.
// Returns a strength value: larger is better, equal means a tie.
// Layout: category << 20 | five 4-bit rank nibbles (most significant first).
#pragma once
#include <cstdint>

namespace pio {

enum HandCategory : int {
  HC_HIGH_CARD = 0,
  HC_PAIR = 1,
  HC_TWO_PAIR = 2,
  HC_TRIPS = 3,
  HC_STRAIGHT = 4,
  HC_FLUSH = 5,
  HC_FULL_HOUSE = 6,
  HC_QUADS = 7,
  HC_STRAIGHT_FLUSH = 8,
};

// cards: card indices (rank*4+suit), n in [5, 7], all distinct.
uint32_t evaluate(const int* cards, int n);
// Same, from a 52-bit card mask (popcount 5..7).
uint32_t evaluate_mask(uint64_t mask);

inline int strength_category(uint32_t v) { return static_cast<int>(v >> 20); }

}  // namespace pio
