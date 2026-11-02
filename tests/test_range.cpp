// Standalone tests for src/range.cpp: range text parsing, formatting, round-trips.
#include <cmath>
#include <cstdio>
#include <string>
#include <string_view>

#include "../src/cards.hpp"
#include "../src/range.hpp"

using namespace pio;

static int failures = 0;

#define CHECK(cond)                                              \
  do {                                                           \
    if (!(cond)) {                                               \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      ++failures;                                                \
    }                                                            \
  } while (0)

static RangeWeights parse_ok(std::string_view text) {
  RangeWeights w;
  std::string err;
  if (!parse_range(text, w, err)) {
    std::printf("FAIL: parse_range(\"%s\") unexpectedly failed: %s\n",
                std::string(text).c_str(), err.c_str());
    ++failures;
  }
  return w;
}

static void parse_must_fail(std::string_view text) {
  RangeWeights w;
  std::string err;
  if (parse_range(text, w, err)) {
    std::printf("FAIL: parse_range(\"%s\") unexpectedly succeeded\n",
                std::string(text).c_str());
    ++failures;
  } else if (err.empty()) {
    std::printf("FAIL: parse_range(\"%s\") failed with empty err\n",
                std::string(text).c_str());
    ++failures;
  }
}

static int count_nonzero(const RangeWeights& w) {
  int n = 0;
  for (float f : w) n += (f != 0.0f);
  return n;
}

// weight of the combo holding cards (rank r1, suit s1) and (rank r2, suit s2)
static float wt(const RangeWeights& w, int r1, int s1, int r2, int s2) {
  return w[static_cast<size_t>(combo_index(make_card(r1, s1), make_card(r2, s2)))];
}

static constexpr int A = 12, K = 11, Q = 10, J = 9, T = 8;

static std::string join_1326(const RangeWeights& w) {
  std::string s;
  for (int i = 0; i < 1326; ++i) {
    if (i) s += ' ';
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.9g", static_cast<double>(w[static_cast<size_t>(i)]));
    s += buf;
  }
  return s;
}

static void roundtrip_exact(const RangeWeights& x, const char* what) {
  std::string f1 = format_range(x);
  RangeWeights y;
  std::string err;
  if (!parse_range(f1, y, err)) {
    std::printf("FAIL: round-trip parse failed for %s ('%s'): %s\n", what, f1.c_str(),
                err.c_str());
    ++failures;
    return;
  }
  if (y != x) {
    std::printf("FAIL: round-trip mismatch for %s ('%s')\n", what, f1.c_str());
    ++failures;
  }
  // Formatting must also be a fixed point.
  std::string f2 = format_range(y);
  CHECK(f2 == f1);
}

int main() {
  pio::init_cards();

  // --- Basic class forms ---
  {
    RangeWeights w = parse_ok("AA");
    CHECK(count_nonzero(w) == 6);
    CHECK(wt(w, A, 0, A, 1) == 1.0f);  // AcAd
    CHECK(wt(w, A, 2, A, 3) == 1.0f);  // AhAs
    CHECK(wt(w, A, 0, K, 0) == 0.0f);
  }
  {
    RangeWeights w = parse_ok("kk:0.5");  // case-insensitive + weight
    CHECK(count_nonzero(w) == 6);
    CHECK(wt(w, K, 2, K, 0) == 0.5f);
  }
  {
    RangeWeights w = parse_ok("AKs");
    CHECK(count_nonzero(w) == 4);
    for (int s = 0; s < 4; ++s) CHECK(wt(w, A, s, K, s) == 1.0f);
    CHECK(wt(w, A, 0, K, 1) == 0.0f);
  }
  {
    RangeWeights w = parse_ok("AKo");
    CHECK(count_nonzero(w) == 12);
    CHECK(wt(w, A, 0, K, 1) == 1.0f);
    CHECK(wt(w, A, 0, K, 0) == 0.0f);
  }
  {
    RangeWeights w = parse_ok("AK");
    CHECK(count_nonzero(w) == 16);
  }

  // --- Plus forms ---
  {
    RangeWeights w = parse_ok("22+");
    CHECK(count_nonzero(w) == 13 * 6);
    CHECK(wt(w, 0, 0, 0, 1) == 1.0f);
    CHECK(wt(w, A, 0, A, 1) == 1.0f);
    CHECK(wt(w, A, 0, K, 0) == 0.0f);
  }
  {
    RangeWeights w = parse_ok("A2s+");
    CHECK(count_nonzero(w) == 12 * 4);
    CHECK(wt(w, A, 0, 0, 0) == 1.0f);   // Ac2c
    CHECK(wt(w, A, 3, K, 3) == 1.0f);   // AsKs
    CHECK(wt(w, A, 0, K, 1) == 0.0f);   // AKo not included
  }
  {
    RangeWeights w = parse_ok("K9o+");  // K9o,KTo,KJo,KQo
    CHECK(count_nonzero(w) == 4 * 12);
    CHECK(wt(w, K, 0, Q, 1) == 1.0f);
    CHECK(wt(w, K, 0, 6, 1) == 0.0f);  // K8o not included
  }
  {
    RangeWeights w = parse_ok("T8+");  // T8,T9 both suitednesses
    CHECK(count_nonzero(w) == 2 * 16);
    CHECK(wt(w, T, 0, 6, 0) == 1.0f);   // T8 suited
    CHECK(wt(w, T, 0, 7, 1) == 1.0f);   // T9 offsuit
    CHECK(wt(w, T, 0, J, 1) == 0.0f);   // notation can't raise past one below the high rank
  }

  {
    RangeWeights w = parse_ok("AKs+");  // degenerate plus: only AKs itself
    CHECK(count_nonzero(w) == 4);
  }

  // --- Dash ranges ---
  {
    RangeWeights w = parse_ok("TT-77");  // TT,99,88,77
    CHECK(count_nonzero(w) == 4 * 6);
    RangeWeights rev = parse_ok("77-TT");  // endpoint order irrelevant
    CHECK(w == rev);
  }
  {
    RangeWeights w = parse_ok("A5s-A2s");
    CHECK(count_nonzero(w) == 4 * 4);
    CHECK(wt(w, A, 3, 3, 3) == 1.0f);  // As5s
    CHECK(w == parse_ok("A2s-A5s"));
  }
  {
    RangeWeights w = parse_ok("KTo-K8o");  // KTo,K9o,K8o
    CHECK(count_nonzero(w) == 3 * 12);
    CHECK(wt(w, K, 0, T, 1) == 1.0f);
    CHECK(wt(w, K, 0, J, 1) == 0.0f);
  }
  {
    RangeWeights w = parse_ok("J9-J6");  // J9..J6, both suitednesses
    CHECK(count_nonzero(w) == 4 * 16);
  }

  // --- Specific combos ---
  {
    RangeWeights w = parse_ok("AhKd");
    CHECK(count_nonzero(w) == 1);
    CHECK(wt(w, A, 2, K, 1) == 1.0f);
    CHECK(w == parse_ok("KdAh"));  // card order irrelevant
  }
  {
    RangeWeights w = parse_ok("AcAh");  // pair combo via specific-combo syntax
    CHECK(count_nonzero(w) == 1);
    CHECK(w[static_cast<size_t>(parse_combo("AhAc"))] == 1.0f);
  }

  // --- Overriding: later items win ---
  {
    RangeWeights w = parse_ok("AA,AhAc:0.5");
    CHECK(count_nonzero(w) == 6);
    CHECK(wt(w, A, 2, A, 0) == 0.5f);
    CHECK(wt(w, A, 1, A, 0) == 1.0f);
  }
  {
    RangeWeights w = parse_ok("AKs,AcKc:0");
    CHECK(count_nonzero(w) == 3);
  }
  {
    RangeWeights w = parse_ok("KK:0.5,KK");
    CHECK(wt(w, K, 0, K, 1) == 1.0f);
  }
  {
    RangeWeights w = parse_ok("AA:1,AA:0.25");
    CHECK(wt(w, A, 0, A, 3) == 0.25f);
  }

  // --- Whitespace handling ---
  {
    RangeWeights w = parse_ok("  AA ,\tKK:0.5 ,\n AKs  , QQ : 0.25 ");
    CHECK(count_nonzero(w) == 6 + 6 + 4 + 6);
    CHECK(wt(w, K, 0, K, 2) == 0.5f);
    CHECK(wt(w, Q, 0, Q, 1) == 0.25f);
  }

  // --- Empty input = empty range ---
  {
    RangeWeights w = parse_ok("");
    CHECK(count_nonzero(w) == 0);
    RangeWeights w2 = parse_ok("   \n\t ");
    CHECK(count_nonzero(w2) == 0);
  }

  // --- Error cases ---
  parse_must_fail("XX");
  parse_must_fail("AAs");   // pair with s
  parse_must_fail("AAo");   // pair with o
  parse_must_fail("QQs");
  parse_must_fail("AKx");   // bad suffix
  parse_must_fail("AKs:abc");
  parse_must_fail("AKs:-1");     // negative
  parse_must_fail("AKs:-0.0001");
  parse_must_fail("AKs:nan");    // NaN
  parse_must_fail("AKs:inf");    // Inf
  parse_must_fail("AKs:");       // missing weight
  parse_must_fail(":0.5");       // missing body
  parse_must_fail("AA,,KK");     // empty item
  parse_must_fail("AhAh");       // duplicate card
  parse_must_fail("A5s-K2s");    // dash with different high ranks
  parse_must_fail("A5s-A2o");    // dash with mismatched suitedness
  parse_must_fail("AA-AKs");     // dash mixing pair and non-pair
  parse_must_fail("A5s-A2s-A3s");// two dashes
  parse_must_fail("+");          // bare plus
  parse_must_fail("2s+");        // bad base for plus
  parse_must_fail("AhKd-AcKs");  // dash only between classes, not combos
  parse_must_fail("123");        // lone number token is not a class
  parse_must_fail("5 1");        // numbers but not 1326 of them
  parse_must_fail("AKs:0.5:0.2");
  parse_must_fail("A K s");

  // --- 1326-number form ---
  {
    RangeWeights ref;
    for (size_t i = 0; i < 1326; ++i)
      ref[i] = (i % 3 == 0) ? 0.9f : ((i % 3 == 1) ? 1.0f : 0.0f);
    std::string s = join_1326(ref);
    RangeWeights w = parse_ok(s);
    CHECK(w == ref);
    // Leading/trailing/extra whitespace tolerated.
    RangeWeights w2 = parse_ok("  \n" + s + "  \t");
    CHECK(w2 == ref);
    // 1325 numbers -> error
    std::string short_str = s.substr(0, s.find_last_of(' '));
    parse_must_fail(short_str);
    // negative weight among 1326 -> error
    std::string neg = std::string("-0.5") + s.substr(s.find(' '));
    parse_must_fail(neg);
  }

  // --- format_range basics ---
  {
    RangeWeights w = parse_ok("AA,KK:0.5,AKs,AQo:0.25");
    std::string s = format_range(w);
    CHECK(s == "AA,KK:0.5,AKs,AQo:0.25");  // canonical order matches spec example
    RangeWeights all0{};
    all0.fill(0.0f);
    CHECK(format_range(all0).empty());
  }
  {
    // Mixed weights inside a class -> individual combos, ":1" omitted.
    RangeWeights x{};
    x.fill(0.0f);
    x[static_cast<size_t>(parse_combo("AcKc"))] = 0.3f;
    CHECK(format_range(x) == "AcKc:0.3");
  }

  // --- Round-trips ---
  roundtrip_exact(parse_ok("AA,KK:0.5,AKs,AQo:0.25"), "spec example");
  roundtrip_exact(parse_ok("22+,A2s+:0.5,AhKd:0.75,QQ-88"), "mixed forms");
  roundtrip_exact(parse_ok("T8+,J6-J9,KQo"), "dash/plus");
  {
    // Mixed per-combo weights inside AKs; full 77 class at 0.25; stray combos.
    RangeWeights x{};
    x.fill(0.0f);
    x[static_cast<size_t>(parse_combo("AcKc"))] = 0.5f;
    x[static_cast<size_t>(parse_combo("AdKd"))] = 0.75f;
    x[static_cast<size_t>(parse_combo("AhKh"))] = 1.0f;
    for (int s1 = 0; s1 < 4; ++s1)
      for (int s2 = s1 + 1; s2 < 4; ++s2)
        x[static_cast<size_t>(combo_index(make_card(5, s1), make_card(5, s2)))] = 0.25f;
    x[static_cast<size_t>(parse_combo("JsTc"))] = 0.3f;
    roundtrip_exact(x, "mixed per-combo");
  }
  {
    // Every combo gets a varied weight from a small decimal set.
    RangeWeights x;
    static const float kW[] = {0.f, 0.25f, 0.5f, 0.75f, 1.f, 0.3f, 0.7f};
    for (size_t i = 0; i < 1326; ++i) x[i] = kW[i % 7];
    roundtrip_exact(x, "varied all-combos");
  }
  {
    // Arbitrary floats need not survive 6-significant-digit printing exactly,
    // but formatting must reach a fixed point after one parse.
    RangeWeights x;
    for (size_t i = 0; i < 1326; ++i) x[i] = (i % 1000) / 999.0f;
    std::string f1 = format_range(x);
    RangeWeights y;
    std::string err;
    CHECK(parse_range(f1, y, err));
    CHECK(format_range(y) == f1);
  }

  // SCRATCH: deliberate failure to prove the CI jobs fail the check (not for merge).
  CHECK(1 == 2);

  if (failures) {
    std::printf("%d FAILURE(S)\n", failures);
    return 1;
  }
  std::printf("ALL PASSED\n");
  return 0;
}
