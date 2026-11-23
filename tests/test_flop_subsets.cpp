// Flop subsets (tools/flop_subsets.hpp, #486): the flop classes, the who-is-ahead tables behind the
// choice (the fast sweep against comparing every pair of hands), the weight solver, the search, and the
// shipped files (flops/wsolver_<N>.txt) against random subsets of the same size.
// Usage: build/test_flop_subsets [FLOPS_DIR]   (default: flops)
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <random>
#include <set>

#include "../src/parallel.hpp"
#include "../tools/flop_subsets.hpp"

using namespace pio;
using namespace pio::flops;

static int fails = 0;
#define CHECK(cond, ...)                \
  do {                                  \
    if (!(cond)) {                      \
      std::printf("FAIL: " __VA_ARGS__); \
      std::printf("\n");                \
      ++fails;                          \
    } else {                            \
      std::printf("  ok: " __VA_ARGS__); \
      std::printf("\n");                \
    }                                   \
  } while (0)

static std::array<uint8_t, 3> flop_of(const char* text) {
  std::vector<int> cs;
  parse_cards(text, cs);
  return {static_cast<uint8_t>(cs[0]), static_cast<uint8_t>(cs[1]), static_cast<uint8_t>(cs[2])};
}

// The rows of some flops on every street, and the Gram matrix of their sum: what flop_subsets.cpp does
// for all flops, here for the few a test needs.
static std::vector<double> gram_of(const std::vector<FlopClass>& fc, const std::vector<int>& flops, const Deck& deck) {
  const int m = static_cast<int>(flops.size());
  std::vector<double> G(static_cast<size_t>(m) * m, 0.0);
  std::vector<float> rows(static_cast<size_t>(m) * NROW);
  for (int s = 0; s < STREETS; ++s) {
    std::function<void(int)> work = [&](int i) {
      std::vector<uint32_t> e;
      flop_table(fc[flops[i]].cards, s, e);
      feature_row(e, s, deck, rows.data() + static_cast<size_t>(i) * NROW);
    };
    pool().parallel_for(m, work);
    for (int i = 0; i < m; ++i)
      for (int j = 0; j < m; ++j) {
        double d = 0;
        for (int k = 0; k < NROW; ++k) d += static_cast<double>(rows[static_cast<size_t>(i) * NROW + k]) * rows[static_cast<size_t>(j) * NROW + k];
        G[static_cast<size_t>(i) * m + j] += d;
      }
  }
  return G;
}

int main(int argc, char** argv) {
  init_cards();
  const std::string dir = argc > 1 ? argv[1] : "flops";
  std::printf("[flop subsets]\n");

  // ---- classes ----
  const std::vector<FlopClass> fc = flop_classes();
  int total = 0;
  bool sizes = true, fixed = true, ordered = true;
  for (size_t i = 0; i < fc.size(); ++i) {
    total += fc[i].count;
    sizes = sizes && (fc[i].count == 4 || fc[i].count == 12 || fc[i].count == 24);
    fixed = fixed && canonical(fc[i].cards) == fc[i].cards;
    ordered = ordered && (i == 0 || fc[i].cards < fc[i - 1].cards);
  }
  CHECK(fc.size() == 1755 && total == 22100 && sizes, "1,755 flop classes of 4, 12 or 24 flops, 22,100 in all (%zu, %d)", fc.size(), total);
  CHECK(fixed && ordered && flop_str(fc.front().cards) == "AsAhAd" && flop_str(fc.back().cards) == "2s2h2d",
        "each is its own representative, highest first (%s .. %s)", flop_str(fc.front().cards).c_str(), flop_str(fc.back().cards).c_str());
  CHECK(class_index(fc, "2c5dKc") == class_index(fc, "Ks5h2s") && class_index(fc, "Ks5h2s") >= 0 && class_index(fc, "KsKs2d") < 0 &&
            class_index(fc, "Ks5h") < 0,
        "a flop finds its class under any suits; text that is no flop finds none");

  // ---- tables ----
  const std::vector<double> pairs = class_pairs();
  std::vector<uint32_t> fast, direct;
  bool same = true, counted = true;
  for (const char* f : {"AsKs9s", "Ks8s7h", "QsTh2d", "6s5h5d", "7s7h7d", "4s3h2s"})
    for (int street = 0; street < STREETS; ++street) {
      if (street == 2 && std::string(f) != "Ks8s7h" && std::string(f) != "6s5h5d") continue;  // the river by pairs is slow
      const auto flop = flop_of(f);
      flop_table(flop, street, fast);
      flop_table_direct(flop, street, direct);
      same = same && fast == direct;
      // each ordered pair of hands that fits the flop is counted 2 * boards times between the two orders
      const uint64_t fm = card_bit(flop[0]) | card_bit(flop[1]) | card_bit(flop[2]);
      std::vector<uint64_t> fit(NFEAT, 0);
      for (int h = 0; h < NUM_COMBOS; ++h)
        for (int o = 0; o < NUM_COMBOS; ++o)
          if (!(combo_mask(h) & fm) && !(combo_mask(o) & fm) && !(combo_mask(h) & combo_mask(o)))
            ++fit[static_cast<size_t>(preflop_class(h)) * NCLS + preflop_class(o)];
      for (int a = 0; a < NCLS; ++a)
        for (int b = 0; b < NCLS; ++b)
          counted = counted && static_cast<uint64_t>(fast[a * NCLS + b]) + fast[b * NCLS + a] == 2ull * BOARDS_PER_PAIR[street] * fit[a * NCLS + b];
    }
  CHECK(same, "the sweep over sorted hands gives the table that comparing every pair of hands gives (6 flops; flop, turn, river)");
  CHECK(counted, "a class pair's two orders add up to its fitting hand pairs times the boards");

  // ---- the deck: every flop on the flop and the turn; the river is the exact preflop table ----
  Deck deck;
  deck.pairs = pairs;
  deck.combos = class_combos();
  const int n = static_cast<int>(fc.size());
  for (int street = 0; street < 2; ++street) {
    std::vector<uint32_t> all(static_cast<size_t>(n) * NFEAT);
    std::function<void(int)> work = [&](int v) {
      std::vector<uint32_t> e;
      flop_table(fc[v].cards, street, e);
      std::copy(e.begin(), e.end(), all.begin() + static_cast<size_t>(v) * NFEAT);
    };
    pool().parallel_for(n, work);
    std::vector<uint64_t> sum(NFEAT, 0);
    for (int v = 0; v < n; ++v)
      for (int k = 0; k < NFEAT; ++k) sum[k] += static_cast<uint64_t>(all[static_cast<size_t>(v) * NFEAT + k]) * fc[v].count;
    deck.ahead[street] = ahead_from_sum(sum, street);
    // every disjoint pair of hands sees C(48, 3) flops, so the two orders of a class pair add up to its pairs
    bool whole = true;
    for (int a = 0; a < NCLS; ++a)
      for (int b = 0; b < NCLS; ++b)
        whole = whole && sum[a * NCLS + b] + sum[b * NCLS + a] ==
                             static_cast<uint64_t>(2 * BOARDS_PER_PAIR[street] * FLOPS_PER_PAIR * pairs[a * NCLS + b]);
    CHECK(whole, "all flops at their class sizes cover every pair of hands C(48,3) times (%s)", street == 0 ? "flop" : "turn");
  }
  deck.ahead[2] = preflop_equity_sums();
  {
    // The whole deck against itself: every class is worth what the exact table says, half the pot overall.
    const auto eq = against_all(deck.ahead[2]);
    double mean = 0;
    for (int a = 0; a < NCLS; ++a) mean += eq[a] * deck.combos[a] / NUM_COMBOS;
    CHECK(std::abs(mean - 0.5) < 1e-12 && eq[preflop_class_of_name("AA")] > eq[preflop_class_of_name("KK")] &&
              eq[preflop_class_of_name("72o")] < eq[preflop_class_of_name("32s")],
          "equity against all hands from the exact table: mean %.12f, AA %.4f, 72o %.4f", mean, eq[preflop_class_of_name("AA")],
          eq[preflop_class_of_name("72o")]);
  }

  // ---- weights on the simplex ----
  {
    // Points in the plane: the weights that bring their mean closest to the origin. The origin is inside
    // the first three, at one set of weights; with the far point as well it is still reached (by many).
    const double pts[4][2] = {{1, 0}, {-1, 1}, {-1, -1}, {5, 5}};
    std::vector<double> G(16);
    for (int i = 0; i < 4; ++i)
      for (int j = 0; j < 4; ++j) G[i * 4 + j] = pts[i][0] * pts[j][0] + pts[i][1] * pts[j][1];
    const Subset in = fit(G, 4, {0, 1, 2}), all = fit(G, 4, {0, 1, 2, 3});
    // (the line searches close in on it: weights to 1e-9 here, so the squared error to about 1e-18)
    CHECK(in.err2 < 1e-15 && all.err2 < 1e-15 && std::abs(in.weight[0] - 0.5) < 1e-9 && std::abs(in.weight[1] - 0.25) < 1e-9,
          "weights: the origin inside the points is reached (%.3f %.3f %.3f; %.1e, %.1e)", in.weight[0], in.weight[1], in.weight[2],
          in.err2, all.err2);
    const Subset out = fit(G, 4, {0, 1, 3});
    // closest point of the segment (1,0)-(-1,1) to the origin: t = 3/5 of the way, at (-1/5, 3/5)... squared 1/5
    CHECK(std::abs(out.err2 - 0.2) < 1e-12 && out.weight[2] == 0, "weights: outside, the nearest point of the hull (%.6f)", out.err2);
    const Subset one = choose(G, 4, 1), three = choose(G, 4, 3);
    CHECK(one.flops == std::vector<int>{0} && three.err2 < 1e-15 && three.flops.size() == 3,
          "search: the best single point, then the three around the origin");
  }

  // ---- the shipped files against random subsets of their size ----
  std::mt19937_64 rng(486);
  std::vector<double> cw;
  for (const auto& f : fc) cw.push_back(f.count);
  std::discrete_distribution<int> deal(cw.begin(), cw.end());
  double previous = -1;
  for (int size : {10, 25, 50, 100, 200}) {
    const std::string path = dir + "/wsolver_" + std::to_string(size) + ".txt";
    std::ifstream in(path);
    std::vector<int> S;
    std::vector<double> w;
    std::string bad;
    const bool parsed = in && parse_flop_file(in, fc, S, w, bad);
    const double sum = std::accumulate(w.begin(), w.end(), 0.0);
    // weights are printed in percent with 4 decimals: the sum is 100 to within half a last digit per flop
    CHECK(parsed && static_cast<int>(S.size()) == size && std::set<int>(S.begin(), S.end()).size() == S.size() &&
              std::abs(sum - 100.0) <= 0.00005 * size,
          "%s: %d different flops, weights that sum to 100 (%.4f)", path.c_str(), size, sum);
    if (!parsed || S.empty()) continue;
    // Random flops as the deck deals them: 3 subsets of the same size for the small files (the larger
    // ones cost seconds each), with the best weights they can have.
    const int samples = size <= 25 ? 3 : size <= 50 ? 1 : 0;
    std::vector<int> flops = S;
    for (int k = 0; k < samples; ++k) {
      std::set<int> r;
      while (static_cast<int>(r.size()) < size) r.insert(deal(rng));
      flops.insert(flops.end(), r.begin(), r.end());
    }
    const int m = static_cast<int>(flops.size());
    const std::vector<double> G = gram_of(fc, flops, deck);
    std::vector<int> own(size);
    std::iota(own.begin(), own.end(), 0);
    const double err = error_of(G, m, own, w), best = fit(G, m, own).err2;
    double random_best = -1;
    for (int k = 0; k < samples; ++k) {
      std::vector<int> r(size);
      std::iota(r.begin(), r.end(), size * (k + 1));
      const double e = fit(G, m, r).err2;
      if (random_best < 0 || e < random_best) random_best = e;
    }
    if (samples)
      CHECK(err < random_best, "  error %.5f, the best of %d random subsets with solved weights %.5f", std::sqrt(err), samples,
            std::sqrt(std::max(0.0, random_best)));
    else
      std::printf("      error %.5f\n", std::sqrt(err));
    // The file's weights are the solved ones up to their 4 printed decimals. At the solved weights the
    // error has no slope along the simplex, so weights off by d cost (sum_i d_i x_i)^2, x_i being flop
    // i's own error: at most (1e-6 * sum_i |x_i|)^2 with every weight within one of its last digit.
    double reach = 0;
    for (int i = 0; i < size; ++i) reach += std::sqrt(G[static_cast<size_t>(i) * m + i]);
    const double rounding = 1e-6 * reach * 1e-6 * reach;
    CHECK(std::abs(err - best) <= rounding, "  its weights are the solved weights (%.6e against %.6e; rounding allows %.1e)", err, best, rounding);
    CHECK(previous < 0 || err < previous, "  and more flops are closer to the deck than fewer");
    previous = err;
  }

  std::printf(fails ? "\nFAILED (%d)\n" : "\nALL PASSED\n", fails);
  return fails ? 1 : 0;
}
