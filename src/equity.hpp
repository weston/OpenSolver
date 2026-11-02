// Range-vs-range equity with exact runout enumeration (postflop) or Monte Carlo (preflop).
#pragma once
#include <array>
#include <string>
#include <vector>

#include "cards.hpp"

namespace pio {

struct EquityResult {
  std::array<float, NUM_COMBOS> eq{};        // equity per hand (0..1), 0 for dead hands
  std::array<float, NUM_COMBOS> matchups{};  // weighted opponent combos compatible on the board
  double total = 0;                          // range equity of the hero range
};

// board: 3..5 cards. Ranges: weights in Pio hand order.
EquityResult range_equity(const std::vector<int>& board, const std::array<float, NUM_COMBOS>& hero,
                          const std::array<float, NUM_COMBOS>& villain);

// Preflop: 169 classes in show_preflop_order order. Monte Carlo over `boards` random boards.
struct PreflopEquity {
  std::vector<float> eq, matchups;  // 169 each
  double total = 0;
};
PreflopEquity preflop_equity(const std::array<float, NUM_COMBOS>& hero, const std::array<float, NUM_COMBOS>& villain,
                             int boards = 20000);
// Index (0..168) of a combo's preflop class (13x13 grid order; see preflop_class_of_name).
int preflop_class(int combo);
// Class index for a name like "AKs", "AKo", "AA"; -1 if invalid.
int preflop_class_of_name(const std::string& name);

}  // namespace pio
