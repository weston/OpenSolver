// Engine smoke test: exploitability must fall toward 0 on river / turn / flop trees.
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>

#include "../src/cards.hpp"
#include "../src/game.hpp"
#include "../src/parallel.hpp"
#include "../src/range.hpp"
#include "../src/solver.hpp"

using namespace pio;

template <class V>
static bool same_bytes(const V& a, const V& b) {
  return a.size() == b.size() &&
         (a.empty() || std::memcmp(a.data(), b.data(), a.size() * sizeof(a[0])) == 0);
}

static bool parallel_equivalence() {
  const int saved_threads = pool().threads();
  for (bool compressed : {false, true}) {
    for (bool iso : {false, true}) {
      GameConfig cfg;
      parse_cards("Kh9h4h", cfg.board);
      std::string err;
      RangeWeights weights;
      if (!parse_range("AA,KK,QQ,AKs,AQo", weights, err)) return false;
      cfg.range[0] = weights;
      if (!parse_range("AA,KK,JJ,AQs,KQo", weights, err)) return false;
      cfg.range[1] = weights;
      cfg.pot[2] = 60;
      // Keep bets below all-in: early all-in kernels intentionally partition
      // runout sums by thread count, which can change float rounding even with
      // the original scheduler. Ordinary chance reductions have a fixed order.
      cfg.eff_stack = 1000;
      cfg.iso_flop = iso;
      cfg.lines.add = {{20, 20, 60, 60, 200}, {0, 20, 20, 60, 60}, {20, 200}};
      GameTree g;
      g.compressed = compressed;
      if (!g.build(cfg, err)) return false;
      Solver s(g);
      pool().set_threads(1);
      for (int t = 1; t <= 10; ++t) s.iterate(t);
      const auto regrets = g.regrets, strat = g.strat;
      const auto regrets_q = g.regrets_q;
      const auto strat_q = g.strat_q;
      const auto rscale = g.rscale, sscale = g.sscale;
      const RootStats expected = s.root_stats();
      for (int threads : {2, 8}) {
        g.clear_storage();
        pool().set_threads(threads);
        for (int t = 1; t <= 10; ++t) s.iterate(t);
        const RootStats actual = s.root_stats();
        if (!same_bytes(regrets, g.regrets) || !same_bytes(strat, g.strat) ||
            !same_bytes(regrets_q, g.regrets_q) || !same_bytes(strat_q, g.strat_q) ||
            !same_bytes(rscale, g.rscale) || !same_bytes(sscale, g.sscale) ||
            actual.ev[0] != expected.ev[0] || actual.ev[1] != expected.ev[1] ||
            actual.mes[0] != expected.mes[0] || actual.mes[1] != expected.mes[1] ||
            actual.exploitability != expected.exploitability) {
          std::printf("Parallel result differs: threads=%d compressed=%d iso=%d\n", threads, compressed, iso);
          pool().set_threads(saved_threads);
          return false;
        }
      }
    }
  }
  pool().set_threads(saved_threads);
  std::printf("[parallel equivalence] strategies, regrets, scales and stats byte-identical\n");
  return true;
}

static bool run(const char* name, const char* board, const char* r0, const char* r1, int pot, int stack,
                std::vector<std::vector<int32_t>> lines, int iters, double target_frac, bool iso = true) {
  GameConfig cfg;
  parse_cards(board, cfg.board);
  std::string err;
  RangeWeights w;
  if (!parse_range(r0, w, err)) { std::printf("range0: %s\n", err.c_str()); return false; }
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
  if (!parse_range(r1, w, err)) { std::printf("range1: %s\n", err.c_str()); return false; }
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
  cfg.pot[2] = pot;
  cfg.eff_stack = stack;
  cfg.lines.add = lines;
  cfg.iso_flop = iso;
  GameTree g;
  auto t0 = std::chrono::steady_clock::now();
  if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
  double tb = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::printf("[%s] nodes=%zu chances=%zu showdown boards=%zu storage=%.1f MB hands=%d/%d build=%.2fs\n", name,
              g.nodes.size(), g.chances.size(), g.showdowns.size(), g.storage_floats * 8.0 / 1e6, g.num_hands(0),
              g.num_hands(1), tb);
  Solver s(g);
  auto t1 = std::chrono::steady_clock::now();
  RootStats st{};
  for (int t = 1; t <= iters; ++t) {
    s.iterate(t);
    if (t % (iters / 5 > 0 ? iters / 5 : 1) == 0 || t == iters) {
      st = s.root_stats();
      double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t1).count();
      std::printf("  iter %4d  EV OOP %.4f  EV IP %.4f  MES %.4f/%.4f  explo %.5f (%.4f%% pot)  %.2fs\n", t, st.ev[0],
                  st.ev[1], st.mes[0], st.mes[1], st.exploitability, 100 * st.exploitability / pot, el);
    }
  }
  bool ok = st.exploitability < target_frac * pot && std::abs(st.ev[0] + st.ev[1] - pot) < 1e-3 * pot;
  std::printf("  %s\n", ok ? "OK" : "NOT CONVERGED / EV SUM WRONG");
  return ok;
}

// Regret-based pruning (#51): two identical pruned runs must agree bit-for-bit (no parallelism
// nondeterminism via the prune decision), the EVs must still sum to the pot, and the pruned run
// must reach about the unpruned exploitability in the same number of iterations. The revisit
// schedule is exercised by using a small prune_period.
static bool run_pruning() {
  const char* board = "KhQs7d4c2s";
  const char* r0 = "AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s";
  const char* r1 = "AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s";
  const int pot = 100;
  auto build = [&](GameTree& g) {
    GameConfig cfg;
    parse_cards(board, cfg.board);
    std::string err;
    RangeWeights w;
    if (!parse_range(r0, w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
    if (!parse_range(r1, w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
    cfg.pot[2] = pot;
    cfg.eff_stack = 200;
    cfg.lines.add = {{75, 200}, {0, 75, 200}, {0, 50}, {75}};
    return g.build(cfg, err);
  };
  GameTree g_off, g_on1, g_on2;
  if (!build(g_off) || !build(g_on1) || !build(g_on2)) {
    std::printf("[pruning] build failed\n");
    return false;
  }
  Solver s_off(g_off), s_on1(g_on1), s_on2(g_on2);
  s_on1.pruning = s_on2.pruning = true;
  s_on1.prune_period = s_on2.prune_period = 7;
  const int iters = 400;
  for (int t = 1; t <= iters; ++t) {
    s_off.iterate(t);
    s_on1.iterate(t);
    s_on2.iterate(t);
  }
  RootStats off = s_off.root_stats(), on1 = s_on1.root_stats(), on2 = s_on2.root_stats();
  bool det = on1.ev[0] == on2.ev[0] && on1.ev[1] == on2.ev[1] && on1.exploitability == on2.exploitability;
  bool sum = std::abs(on1.ev[0] + on1.ev[1] - pot) < 1e-3 * pot;
  bool conv = on1.exploitability <= off.exploitability * 1.5 + 0.0005 * pot;
  std::printf("[pruning] explo off=%.5f on=%.5f  %s%s%s\n", off.exploitability, on1.exploitability,
              det ? "deterministic " : "NONDETERMINISTIC ", sum ? "ev-sum-ok " : "EV-SUM-WRONG ",
              conv ? "converged" : "DIVERGED");
  return det && sum && conv;
}

// #98: memory_bytes_for must price the set_predictive preds array (storage_floats * 4, never
// compressed) under with_preds, and follow the actual preds allocation state without it.
static bool mem_accounting() {
  GameConfig cfg;
  parse_cards("KhQs7d4c2s", cfg.board);
  std::string err;
  RangeWeights w;
  if (!parse_range("AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
  if (!parse_range("AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
  cfg.pot[2] = 100;
  cfg.eff_stack = 200;
  cfg.lines.add = {{75, 200}, {0, 75, 200}, {0, 50}, {75}};
  GameTree g;
  if (!g.build(cfg, err)) return false;
  const uint64_t preds_bytes = g.storage_floats * sizeof(float);
  bool ok = g.memory_bytes_for(false, true) - g.memory_bytes_for(false, false) == preds_bytes &&
            g.memory_bytes_for(true, true) - g.memory_bytes_for(true, false) == preds_bytes &&
            g.memory_bytes() == g.memory_bytes_for(g.compressed, false);  // preds still empty
  g.preds.resize(g.storage_floats);  // what set_predictive allocates
  ok = ok && g.memory_bytes() == g.memory_bytes_for(g.compressed, true);
  std::printf("[mem] preds accounting %s\n", ok ? "OK" : "WRONG");
  return ok;
}

// #89: the AVX-512 showdown kernel must reproduce the scalar one bit for bit. Every showdown board
// of a river, a turn and a flop tree, both perspectives, with random reaches (some exact zeros, so
// blocked and empty groups occur), random payoffs and a non-zero `out` to accumulate into.
static bool showdown_kernels_match() {
#if PIO_SHOWDOWN_AVX512
  struct Spot {
    const char *board, *r0, *r1;
  } spots[] = {
      {"KhQs7d4c2s", "AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s", "AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s"},
      {"KhQs7d4c", "22+,A2+,K2+,Q2+,J2+,T2+,92+,82+,72+,62+,52+,42+,32", "22+,A2s+,K5s+,Q8s+,J8s+,T7s+,A8o+,KTo+"},
      {"Kh9h4h", "AA,KK,QQ,JJ,TT,99,AK,AQ,AJ,KQ,KJ,QJ,JT,T9", "AA,KK,QQ,JJ,TT,99,88,AK,AQ,KQ,QJ,JT"},
  };
  uint64_t rng = 0x9e3779b97f4a7c15ull;
  auto next = [&] {
    rng ^= rng << 13, rng ^= rng >> 7, rng ^= rng << 17;
    return rng;
  };
  auto unit = [&] { return static_cast<float>(next() >> 40) / static_cast<float>(1 << 24); };
  long boards = 0, bad = 0;
  for (const Spot& sp : spots) {
    GameConfig cfg;
    parse_cards(sp.board, cfg.board);
    std::string err;
    RangeWeights w;
    if (!parse_range(sp.r0, w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
    if (!parse_range(sp.r1, w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
    cfg.pot[2] = 100;
    cfg.eff_stack = 200;
    cfg.lines.add = {{0, 50}};
    GameTree g;
    if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
    for (const ShowdownBoard& sb : g.showdowns) {
      for (int p = 0; p < 2; ++p) {
        const int Hp = g.num_hands(p), Ho = g.num_hands(1 - p);
        std::vector<float> ro(Ho), a(Hp), b;
        for (float& x : ro) x = next() % 5 == 0 ? 0.0f : unit();
        for (float& x : a) x = 100 * unit() - 50;
        b = a;
        const float W = 150 * unit(), L = -100 * unit(), T = 0.5f * (W + L);
        showdown_board_scalar(sb, W, L, T, p, ro.data(), a.data());
        showdown_board_avx512(sb, W, L, T, p, ro.data(), b.data());
        bad += std::memcmp(a.data(), b.data(), sizeof(float) * Hp) != 0;
        ++boards;
      }
    }
  }
  std::printf("[showdown] AVX-512 vs scalar kernel: %ld of %ld board/player pairs differ\n", bad, boards);
  return bad == 0 && boards > 0;
#else
  std::printf("[showdown] AVX-512 kernel not built (needs AVX-512F/BW/VBMI); nothing to compare\n");
  return true;
#endif
}

int main(int argc, char** argv) {
  init_cards();
  bool big = argc > 1 && std::string(argv[1]) == "big";
  int fails = 0;
  fails += !parallel_equivalence();
  // #71 convergence experiments still converge: predictive regret matching (PCFR+-style,
  // matching input R + last instantaneous regret) and a delayed strategy average.
  {
    auto pcfr = [&](bool predictive, int delay) {
      GameConfig cfg;
      parse_cards("KhQs7d4c2s", cfg.board);
      std::string err;
      RangeWeights w;
      if (!parse_range("AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s", w, err)) return false;
      for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
      if (!parse_range("AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s", w, err)) return false;
      for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
      cfg.pot[2] = 100;
      cfg.eff_stack = 200;
      cfg.lines.add = {{75, 200}, {0, 75, 200}, {0, 50}, {75}};
      GameTree g;
      if (!g.build(cfg, err)) return false;
      if (predictive) g.preds.resize(g.storage_floats);  // what set_predictive allocates
      Solver s(g);
      s.predictive = predictive;
      s.avg_delay = delay;
      for (int t = 1; t <= 300; ++t) s.iterate(t);
      return s.root_stats().exploitability < 0.003 * 100;
    };
    bool ok1 = pcfr(true, 0), ok2 = pcfr(false, 25), ok3 = pcfr(true, 25);
    std::printf("  %s\n", ok1 && ok2 && ok3 ? "ok: predictive / avg_delay converge" : "FAILED: pcfr knobs");
    fails += !(ok1 && ok2 && ok3);
  }
  // River: polar spot, 1 bet size + all-in.
  fails += !run("river", "KhQs7d4c2s", "AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s", "AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s", 100,
                200, {{75, 200}, {0, 75, 200}, {0, 50}, {75}}, 500, 0.003);
  // Turn tree
  fails += !run("turn", "KhQs7d4c", "AA,KK,QQ,77,44,AK,AQ,KQ,QJs,JTs,T9s,98s,A5s,A4s", "AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s,99,88", 100,
                300, {{66, 66, 66 + 132}, {66, 300}, {0, 66, 300}, {0, 0, 66}, {0, 0, 0, 66}}, 300, 0.005);
  // Flop tree, monotone board exercises isomorphism.
  fails += !run("flop-mono", "Kh9h4h", "AA,KK,QQ,JJ,TT,99,AK,AQ,AJ,KQ,KJ,QJ,JT,T9", "AA,KK,QQ,JJ,TT,99,88,AK,AQ,KQ,QJ,JT", 60, 200,
                {{20, 20, 60, 60, 200}, {0, 20, 20, 60, 60}, {0, 0, 40}, {20, 200}}, 200, 0.01);
  fails += !run("flop-mono-noiso", "Kh9h4h", "AA,KK,QQ,JJ,TT,99,AK,AQ,AJ,KQ,KJ,QJ,JT,T9", "AA,KK,QQ,JJ,TT,99,88,AK,AQ,KQ,QJ,JT", 60, 200,
                {{20, 20, 60, 60, 200}, {0, 20, 20, 60, 60}, {0, 0, 40}, {20, 200}}, 200, 0.01, false);
  fails += !run_pruning();
  fails += !mem_accounting();
  fails += !showdown_kernels_match();
  if (big) {
    fails += !run("flop-wide", "Qs8d7s",
                  "22+,A2s+,K2s+,Q4s+,J6s+,T6s+,96s+,85s+,75s+,64s+,54s,A2o+,K8o+,Q9o+,J9o+,T9o",
                  "22+,A2s+,K5s+,Q8s+,J8s+,T7s+,97s+,86s+,76s,65s,A8o+,KTo+,QTo+,JTo", 60, 940,
                  {{0, 20, 20, 60, 60, 160}, {20, 60, 60, 160, 160, 940}, {0, 40, 40, 120, 120, 940}, {0, 0, 40, 40, 120}, {20, 20, 20, 60}},
                  100, 0.02);
  }
  std::printf(fails ? "FAILED (%d)\n" : "ALL PASSED\n", fails);
  return fails ? 1 : 0;
}
