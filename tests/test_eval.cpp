// Exhaustive evaluator test: category counts over all 7-card hands and
// the number of distinct 5-card hand values (must be 7462).
#include <chrono>
#include <cstdio>
#include <set>

#include "../src/cards.hpp"
#include "../src/eval.hpp"

using namespace pio;

int main() {
  init_cards();
  int fails = 0;
  // Spot checks.
  auto ev = [](const char* s) {
    std::vector<int> cs;
    parse_cards(s, cs);
    return evaluate(cs.data(), static_cast<int>(cs.size()));
  };
  struct Cmp { const char* a; const char* b; int expect; };  // expect sign(a-b)
  Cmp cmps[] = {
      {"AsKsQsJsTs", "9s8s7s6s5s", 1},
      {"5d4d3d2dAd", "6c5c4c3c2c", -1},          // wheel SF < 6-high SF
      {"AcAdAhAs2c", "KcKdKhKsAc", 1},
      {"AcAdAh2c2d", "KcKdKhAsAh", 1},
      {"2c3c4c5c7c", "AdKdQdJd9h", 1},           // flush > high card
      {"Ac2d3h4s5c", "2c3d4h5s6c", -1},          // wheel < 6-high straight
      {"AcAd2h2s3c4d5h", "AcAd2h2s3c", 1},       // 7 cards: wheel straight beats two pair
      {"KcKd7h7s2c", "KcKd7h7s3c", -1},
      {"QcQdQh2s2c3d3h", "QcQdQh3s3c2d4h", 0},   // both QQQ33
      {"AcKcQcJc9d8d7d", "AcKcQcJc9d", 0},
  };
  for (auto& c : cmps) {
    uint32_t a = ev(c.a), b = ev(c.b);
    int sign = (a > b) - (a < b);
    if (sign != c.expect) {
      std::printf("FAIL compare %s vs %s: got %d expected %d\n", c.a, c.b, sign, c.expect);
      ++fails;
    }
  }

  // Distinct 5-card values.
  std::set<uint32_t> vals;
  int cs[7];
  for (cs[0] = 0; cs[0] < 52; ++cs[0])
    for (cs[1] = cs[0] + 1; cs[1] < 52; ++cs[1])
      for (cs[2] = cs[1] + 1; cs[2] < 52; ++cs[2])
        for (cs[3] = cs[2] + 1; cs[3] < 52; ++cs[3])
          for (cs[4] = cs[3] + 1; cs[4] < 52; ++cs[4]) vals.insert(evaluate(cs, 5));
  std::printf("distinct 5-card values: %zu (expect 7462)\n", vals.size());
  if (vals.size() != 7462) ++fails;

  // 7-card category distribution.
  const long long expect[9] = {23294460, 58627800, 31433400, 6461620, 6180020,
                               4047644,  3473184,  224848,   41584};
  long long counts[9] = {0};
  auto t0 = std::chrono::steady_clock::now();
  for (int a = 0; a < 52; ++a)
    for (int b = a + 1; b < 52; ++b)
      for (int c = b + 1; c < 52; ++c)
        for (int d = c + 1; d < 52; ++d)
          for (int e = d + 1; e < 52; ++e)
            for (int f = e + 1; f < 52; ++f)
              for (int g = f + 1; g < 52; ++g) {
                int h[7] = {a, b, c, d, e, f, g};
                counts[strength_category(evaluate(h, 7))]++;
              }
  double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  for (int i = 0; i < 9; ++i) {
    std::printf("cat %d: %lld (expect %lld)%s\n", i, counts[i], expect[i], counts[i] == expect[i] ? "" : "  <-- FAIL");
    if (counts[i] != expect[i]) ++fails;
  }
  std::printf("133784560 evals in %.2fs (%.1f ns/eval)\n", secs, secs * 1e9 / 133784560.0);
  std::printf(fails ? "FAILED (%d)\n" : "ALL PASSED\n", fails);
  return fails ? 1 : 0;
}
