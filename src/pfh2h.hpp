// Exact preflop hand-vs-hand table: for every ordered pair of hole-card combos (h, o) with no
// shared card, the number of 5-card boards on which h wins / ties against o. Generated with our
// own evaluator using suit-isomorphism reduction (multithreaded) and cached on disk
// (~/.cache/wsolver/pf_h2h_v1.bin) so later runs load it instantly.
#pragma once
#include <cstdint>
#include <utility>
#include <vector>

#include "cards.hpp"

namespace pio {

// Boards disjoint from two disjoint hole-card pairs: C(48, 5).
constexpr uint32_t PF_BOARDS_PER_PAIR = 1712304;

struct PfTable {
  // win/tie[hero * NUM_COMBOS + villain]; 0 for pairs sharing a card (never used).
  std::vector<uint32_t> win, tie;  // NUM_COMBOS^2 each (~14 MB total)
};

// Global table: loaded from the disk cache or generated on first use (thread-safe; generation
// is a one-time cost of under a minute on ~10 cores, progress goes to stderr).
const PfTable& pf_table();

// Direct independent enumeration of all C(48,5) boards for one disjoint pair (slow; for tests
// and for verifying a generated table before it is cached).
std::pair<uint32_t, uint32_t> pf_pair_direct(int h, int o);  // (wins of h, ties)

}  // namespace pio
