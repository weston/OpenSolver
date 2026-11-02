// Exact preflop hand-vs-hand table (pfh2h.*): invariants and agreement with direct board
// enumeration for a sample of matchups. NOTE: the first run on a machine generates the table
// (multithreaded, tens of seconds; cached under ~/.cache/wsolver afterwards).
#include <cstdint>
#include <cstdio>

#include "../src/cards.hpp"
#include "../src/pfh2h.hpp"

using namespace pio;

static int fails = 0;
#define CHECK(cond, msg)                              \
  do {                                                \
    if (!(cond)) {                                    \
      std::printf("FAIL: %s\n", msg);                 \
      ++fails;                                        \
    }                                                 \
  } while (0)

int main() {
  init_cards();
  const PfTable& t = pf_table();
  const size_t n = NUM_COMBOS;
  CHECK(t.win.size() == n * n && t.tie.size() == n * n, "table dimensions");

  // Diagonal is never used.
  for (int c = 0; c < NUM_COMBOS; ++c)
    CHECK(t.win[c * n + c] == 0 && t.tie[c * n + c] == 0, "diagonal zero");

  // Deterministic sample of compatible pairs: win+tie+lose = C(48,5), tie and counts consistent
  // across the two orientations.
  for (int k = 1; k < 20000; ++k) {
    int h = (k * 613) % NUM_COMBOS;
    int o = (k * 1237 + 77) % NUM_COMBOS;
    if (h == o || (combo_mask(h) & combo_mask(o))) {
      CHECK(t.win[h * n + o] == 0 && t.tie[h * n + o] == 0, "blocked pairs unused");
      continue;
    }
    uint32_t w = t.win[h * n + o], ti = t.tie[h * n + o];
    uint32_t w2 = t.win[o * n + h], ti2 = t.tie[o * n + h];
    CHECK(ti == ti2, "tie symmetric");
    CHECK(w + ti + w2 == PF_BOARDS_PER_PAIR, "win+tie+lose cover all boards");
  }

  // Agreement with direct enumeration for representative matchups (pair vs pair with 0/1/2
  // shared suits, suited vs suited same/different suits, offsuit vs offsuit, connected lows).
  for (auto& [hs, os] : {std::pair<const char*, const char*>{"AsAh", "KcKd"},
                         {"AsAh", "KsKh"},
                         {"AsAd", "KhKs"},
                         {"AcKd", "QhJs"},
                         {"AsKs", "QsJs"},
                         {"AsKs", "QhJh"},
                         {"2c3d", "AhKs"}}) {
    int h = parse_combo(hs), o = parse_combo(os);
    auto [dw, dt] = pf_pair_direct(h, o);
    CHECK(dw == t.win[h * n + o] && dt == t.tie[h * n + o], hs);
    auto [rw, rt] = pf_pair_direct(o, h);
    CHECK(rw == t.win[o * n + h] && rt == t.tie[o * n + h], os);
  }

  // Plausibility: AA vs KK equity ~0.81 (the exact recorded PioSOLVER range-vs-range numbers
  // are checked in tests/test_upi.py).
  int aa = parse_combo("AsAh"), kk = parse_combo("KcKd");
  double eq = (t.win[aa * n + kk] + 0.5 * t.tie[aa * n + kk]) / PF_BOARDS_PER_PAIR;
  CHECK(eq > 0.80 && eq < 0.83, "AA vs KK equity plausible");

  if (fails) {
    std::printf("test_pfh2h: %d FAILURES\n", fails);
    return 1;
  }
  std::printf("test_pfh2h: OK\n");
  return 0;
}
