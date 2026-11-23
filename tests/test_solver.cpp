// Engine smoke test: exploitability must fall toward 0 on river / turn / flop trees.
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>

#include "../src/cards.hpp"
#include "../src/game.hpp"
#include "../src/parallel.hpp"
#include "../src/range.hpp"
#include "../src/solver.hpp"
#include "../src/upi.hpp"

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
// Pruning below the flop: hands that share a card with a node's board (the dealt turn / river) never
// occur there, so they must not keep an action alive (they play every action uniformly on zero regrets).
// Checked on a river node directly, then a flop tree solved with pruning must stay deterministic and
// converge like the unpruned solve.
static bool pruning_ignores_blocked_hands() {
  GameConfig cfg;
  parse_cards("Kh9d4h", cfg.board);
  std::string err;
  RangeWeights w;
  if (!parse_range("AA,KK,QQ,JJ,TT,99,AK,AQ,AJ,KQ,KJ,QJ,JT,T9,A5s,65s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
  if (!parse_range("AA,KK,QQ,JJ,TT,99,88,AK,AQ:0.5,KQ,QJ,JT,98s,76s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
  cfg.pot[2] = 60;
  cfg.eff_stack = 200;
  cfg.lines.add = {{20, 20, 60, 60, 200}, {0, 20, 20, 60, 60}, {0, 0, 40}, {20, 200}, {0, 0, 0, 0, 30, 30}, {0, 0, 0, 0, 30, 90}};
  GameTree g;
  if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
  // a river decision node of OOP where some of OOP's hands are blocked by the turn / river
  Solver s0(g);
  const int H = g.num_hands(0);
  uint32_t ni = UINT32_MAX;
  int blocked = -1, live = -1;
  for (uint32_t i = 0; i < g.nodes.size() && ni == UINT32_MAX; ++i) {
    const GNode& n = g.nodes[i];
    if (n.kind != ANodeKind::Decision || n.board_len != 5 || n.player != 0) continue;
    blocked = live = -1;
    for (int h = 0; h < H; ++h) {
      if (combo_mask(g.hands[0][h]) & g.dec_board[n.aux]) blocked = h;
      else live = h;
    }
    if (blocked >= 0 && live >= 0) ni = i;
  }
  if (ni == UINT32_MAX) { std::printf("[pruning, blocked hands] no river node with blocked hands\n"); return false; }
  std::vector<float> row(H, 0.0f);
  row[blocked] = 0.5f;
  const bool skip_blocked = s0.prunable_row(ni, 0, row.data());
  row[live] = 0.25f;
  const bool keep_live = !s0.prunable_row(ni, 0, row.data());
  // the solve: pruning (twice, for determinism) against no pruning
  GameTree g1, g2, g3;
  if (!g1.build(cfg, err) || !g2.build(cfg, err) || !g3.build(cfg, err)) { std::printf("[pruning, blocked hands] build\n"); return false; }
  Solver a(g1), b(g2), off(g3);
  off.pruning = false;
  for (int t = 1; t <= 200; ++t) {
    a.iterate(t);
    b.iterate(t);
    off.iterate(t);
  }
  const RootStats ra = a.root_stats(), rb = b.root_stats(), ro = off.root_stats();
  const bool det = ra.ev[0] == rb.ev[0] && ra.exploitability == rb.exploitability && g1.regrets == g2.regrets;
  const bool conv = ra.exploitability <= ro.exploitability * 1.5 + 0.0005 * 60;
  std::printf("[pruning, blocked hands] river row: blocked-only %s, live %s; flop tree explo on=%.5f off=%.5f %s%s\n",
              skip_blocked ? "prunable" : "KEPT", keep_live ? "kept" : "PRUNED", ra.exploitability, ro.exploitability,
              det ? "deterministic " : "NONDETERMINISTIC ", conv ? "converged" : "DIVERGED");
  return skip_blocked && keep_live && det && conv;
}

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
  long boards = 0, bad = 0, bad_compat = 0;
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
        std::vector<float> ca(Hp, 0.0f), cb(Hp, 0.0f);
        showdown_board_scalar(sb, W, L, T, p, ro.data(), a.data(), ca.data());
#if PIO_SHOWDOWN_AVX512
        showdown_board_avx512(sb, W, L, T, p, ro.data(), b.data(), cb.data());
        bad += std::memcmp(a.data(), b.data(), sizeof(float) * Hp) != 0;
        bad += std::memcmp(ca.data(), cb.data(), sizeof(float) * Hp) != 0;
#else
        (void)b;
        (void)cb;
#endif
        // fused opponent fold (call_with_opp_fold): out_fold = plain showdown + pay * compat(fro * fsig)
        {
          std::vector<float> xf(Ho), fa(Hp, 0.0f), fb(Hp, 0.0f), plain(Hp, 0.0f);
          for (float& x : xf) x = next() % 4 == 0 ? 0.0f : unit();
          const SdFold fd{xf.data(), 80 * unit() - 20};
          showdown_board_scalar(sb, W, L, T, p, ro.data(), fa.data(), nullptr, &fd);
#if PIO_SHOWDOWN_AVX512
          showdown_board_avx512(sb, W, L, T, p, ro.data(), fb.data(), nullptr, &fd);
          bad += std::memcmp(fa.data(), fb.data(), sizeof(float) * Hp) != 0;
#endif
          showdown_board_scalar(sb, W, L, T, p, ro.data(), plain.data());
          for (const SdEntry& e : sb.ent[p]) {
            double m = 0;
            for (const SdEntry& q : sb.ent[1 - p])
              if (q.c1 != e.c1 && q.c1 != e.c2 && q.c2 != e.c1 && q.c2 != e.c2) m += xf[q.idx];
            const double want = plain[e.idx] + fd.pay * m;
            // float rounding of the two terms (they can cancel): relative to their magnitudes
            if (std::fabs(fa[e.idx] - want) > 1e-5 * (1 + std::fabs(plain[e.idx]) + std::fabs(fd.pay * m))) {
              ++bad_compat;
            }
          }
        }
        // compat must be the card-removal compatible mass (call_and_fold's fold row)
        for (const SdEntry& e : sb.ent[p]) {
          double want = 0;
          for (const SdEntry& q : sb.ent[1 - p])
            if (q.c1 != e.c1 && q.c1 != e.c2 && q.c2 != e.c1 && q.c2 != e.c2) want += ro[q.idx];
          if (std::fabs(ca[e.idx] - want) > 1e-4 * (1 + std::fabs(want))) ++bad_compat;
        }
        ++boards;
      }
    }
  }
  std::printf("[showdown] AVX-512 vs scalar kernel: %ld of %ld board/player pairs differ%s; %ld wrong compat values\n",
              bad, boards, PIO_SHOWDOWN_AVX512 ? "" : " (AVX-512 kernel not built: scalar only)", bad_compat);
  return bad == 0 && bad_compat == 0 && boards > 0;
}

// #182: decompressing a 16-bit tree mid-solve keeps every value (same decode as regrets_ro /
// strat_ro), and the solve continues on float32 storage.
static bool decompress_midsolve() {
  GameConfig cfg;
  parse_cards("KhQs7d", cfg.board);
  std::string err;
  RangeWeights w;
  if (!parse_range("AA,KK,QQ,JJ,TT,AK,AQ,KQ,QJs,JTs", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = cfg.range[1][i] = w[i];
  cfg.pot[2] = 100;
  cfg.eff_stack = 150;
  cfg.lines.add = {{0, 50}, {50, 150}};
  GameTree g;
  if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
  g.compressed = true;
  g.allocate_storage();
  Solver s(g);
  for (int t = 1; t <= 40; ++t) s.iterate(t);
  std::vector<float> before;
  for (uint32_t ni = 0; ni < g.nodes.size(); ++ni) {
    const GNode& n = g.nodes[ni];
    if (n.kind != ANodeKind::Decision) continue;
    std::vector<float> a(g.node_size(n)), b(g.node_size(n));
    const float* r = g.regrets_ro(n, a.data());
    const float* st = g.strat_ro(n, b.data());
    before.insert(before.end(), r, r + a.size());
    before.insert(before.end(), st, st + b.size());
  }
  const double e0 = s.root_stats().exploitability;
  g.decompress();
  std::vector<float> after;
  for (uint32_t ni = 0; ni < g.nodes.size(); ++ni) {
    const GNode& n = g.nodes[ni];
    if (n.kind != ANodeKind::Decision) continue;
    const float* r = g.regrets_ro(n, nullptr);
    const float* st = g.strat_ro(n, nullptr);
    after.insert(after.end(), r, r + g.node_size(n));
    after.insert(after.end(), st, st + g.node_size(n));
  }
  const bool same = !g.compressed && g.regrets_q.empty() && before == after;
  const double e1 = s.root_stats().exploitability;
  for (int t = 41; t <= 120; ++t) s.iterate(t);
  const double e2 = s.root_stats().exploitability;
  std::printf("[decompress] values %s, exploitability %.5f == %.5f, then %.5f after 80 more float32 iterations\n",
              same ? "identical" : "DIFFER", e0, e1, e2);
  return same && e0 == e1 && e2 < e1;
}

// #272: 16-bit strategy sums are committed with stochastic rounding. A slot that gains 0.1 of its node's
// 16-bit step per iteration (while another slot holds the node's maximum, so the scale stays put) must
// grow by about 0.1 step per iteration on average; rounded to nearest it would stay at 0 forever. The
// rounding depends only on the slot and the seed, so a replay gives the same bytes.
static bool stochastic_sums() {
  GameConfig cfg;
  parse_cards("KhQs7d", cfg.board);
  std::string err;
  RangeWeights w;
  // A wide range, so the mean is over ~1,200 slots: with 2000 draws each slot's count has a standard
  // deviation of ~13 steps, which a 12-slot node (AA,KK,QQ) only averages down to ~4.
  if (!parse_range("22+,A2s+,K2s+,Q2s+,J2s+,T2s+,92s+,82s+,72s+,62s+,52s+,42s+,32s,A2o+,K2o+,Q2o+,J2o+,T2o+,"
                   "92o+,82o+,72o+,62o+,52o+,42o+,32o", w, err))
    return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = cfg.range[1][i] = w[i];
  cfg.pot[2] = 100;
  cfg.eff_stack = 150;
  cfg.lines.add = {{0, 50}};
  GameTree g;
  g.compressed = true;
  if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
  const GNode& n = g.nodes[0];
  const size_t len = g.node_size(n);
  std::vector<float> buf(len);
  auto run = [&](int iters) {
    std::vector<float> s(len, 0.0f);
    s[0] = 65535.0f;  // the node's maximum: one step = 1.0
    g.round_seed = 0;
    g.strat_commit(n, s.data());
    for (int t = 1; t <= iters; ++t) {
      g.round_seed = static_cast<uint32_t>(t) * 0x27D4EB2Fu + 0x165667B1u;
      const float* cur = g.strat_ro(n, buf.data());
      std::vector<float> nx(cur, cur + len);
      for (size_t i = 1; i < len; ++i) nx[i] += 0.1f;
      nx[0] = 65535.0f;
      g.strat_commit(n, nx.data());
    }
    const float* cur = g.strat_ro(n, buf.data());
    return std::vector<float>(cur, cur + len);
  };
  const auto a = run(2000), b = run(2000);
  double mean = 0;
  for (size_t i = 1; i < len; ++i) mean += a[i];
  mean /= static_cast<double>(len - 1);
  // Expected 200 steps; the spread per slot is ~sqrt(2000 * 0.09) = 13, far less on the mean.
  const bool ok = a == b && len > 2 && std::fabs(mean - 200.0) < 2.0;
  std::printf("[stochastic sums] 2000 increments of 0.1 step: mean %.2f steps (expect 200, nearest rounding: 0), "
              "replay %s\n", mean, a == b ? "identical" : "DIFFERS");
  return ok;
}

// All-in terminals before the river: the outcome-table path must match evaluating every runout with
// the showdown kernel (flop all-in: 1176 runouts; turn all-in: 48), both players, random reaches.
static bool allin_tables_match() {
  struct Spot {
    const char* board;
    int stack;
    std::vector<std::vector<int>> lines;
  } spots[] = {{"KhQs7d", 150, {{0, 50}, {50, 150}}}, {"KhQs7d4c", 150, {{0, 150}}}};
  uint64_t rng = 0x243f6a8885a308d3ull;
  auto unit = [&] {
    rng ^= rng << 13, rng ^= rng >> 7, rng ^= rng << 17;
    return static_cast<float>(rng >> 40) / static_cast<float>(1 << 24);
  };
  long terminals = 0, bad = 0;
  double worst = 0;
  for (const Spot& sp : spots) {
    GameConfig cfg;
    parse_cards(sp.board, cfg.board);
    std::string err;
    RangeWeights w;
    if (!parse_range("AA,KK,QQ,JJ,TT,99,77,44,AK,AQ,AJ,KQ,KJ,QJ,JT,T9s,98s,87s,A5s", w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
    if (!parse_range("AA,KK,QQ,JJ,TT,88,77,AK,AQ,KQ,KT,QJ,QT,JT,J9s,T9s,65s", w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
    cfg.pot[2] = 100;
    cfg.eff_stack = sp.stack;
    cfg.lines.add = sp.lines;
    GameTree g;
    if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
    Solver s(g);
    for (const GNode& n : g.nodes) {
      if (n.kind != ANodeKind::Showdown || n.board_len == 5) continue;
      if (!g.allins[n.aux].built()) { ++bad; continue; }
      for (int p = 0; p < 2; ++p) {
        const int Hp = g.num_hands(p), Ho = g.num_hands(1 - p);
        std::vector<float> ro(Ho), a(Hp), b(Hp);
        for (float& x : ro) x = unit() < 0.3f ? 0.0f : unit();
        s.allin_tables = true;
        s.showdown_value(n, p, ro.data(), a.data());
        s.allin_tables = false;
        s.showdown_value(n, p, ro.data(), b.data());
        double mag = 0;
        for (int h = 0; h < Hp; ++h) mag = std::max(mag, std::fabs(static_cast<double>(b[h])));
        for (int h = 0; h < Hp; ++h) {
          const double d = std::fabs(static_cast<double>(a[h]) - b[h]) / (1.0 + mag);
          worst = std::max(worst, d);
          if (d > 2e-5) ++bad;
        }
        ++terminals;
      }
    }
  }
  std::printf("[allin tables] %ld terminal/player pairs, worst relative difference %.2e, %ld bad\n", terminals, worst, bad);
  return bad == 0 && terminals > 0;
}

// #494: a preflop tree's exits to its flops. A level of runout sets (flop all-ins and check-downs, turn
// ones) gets the outcome tables when they are smaller than the boards the tree can then do without, and
// those boards are never built. Three trees, one for each outcome: betting through the flop with every
// hand (flop tables, no boards), through the turn with every hand (boards: 49 turn tables per flop would
// be five times their size), through the turn with narrow ranges (tables at both levels). In each, every
// pre-river showdown is valued as built and then on its boards, restored for the comparison; a table
// filled without boards has to hold the very counts the boards give; the memory count has to be the
// boards and tables there are; and under ICM, which the tables cannot serve, the tree has to answer as
// one built under ICM does.
static bool preflop_runout_tables() {
  struct Spot {
    const char* name;
    const char* range;   // empty: every hand
    const char* range2;  // the second player's, null: the same (then a table's two sides list the same hands)
    std::vector<std::vector<int>> flop_lines;  // bets from an exit with 250 in from each player
    int want_tables;                           // bit 0: flop sets, bit 1: turn sets
    int ask = -1;                              // GameTree::allin_plan: the tables asked for, not chosen
    int room = -1;                             // GameTree::table_limit: what the tree with these tables takes,
    int64_t slack = 0;                         // and this many bytes more
  } spots[] = {
      {"flop bets, all hands", "", nullptr, {{250, 500, 1100}, {500, 1100}}, 1},
      {"turn bets, all hands", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 0},
      {"turn bets, narrow", "TT+,AJs+,KQs,AQo+,76s,65s", nullptr,
       {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 3},
      // #496: the same hands on both sides make the tables compact ones (a triangle over the hands the
      // board leaves), which the three spots above have where they have tables; here they are asked for
      // at both levels with every hand, and at the turn level only
      {"turn bets, all, asked", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 3, 3},
      {"turn bets, all, turn only", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 2, 2},
      // with a memory limit: tables at both levels when the tree with them is within it, to the byte;
      // one byte short or with no room, the smallest tree
      {"turn bets, all, room", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 3, -1, 3, 0},
      {"turn bets, all, 1 short", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 0, -1, 3, -1},
      {"turn bets, all, no room", "", nullptr, {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 0, -1, 3,
       INT64_MIN / 2},
      // a limit takes no table from a tree that is smallest with them
      {"turn bets, narrow, no room", "TT+,AJs+,KQs,AQo+,76s,65s", nullptr,
       {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 3, -1, 3, INT64_MIN / 2},
      // unequal ranges: the second player holds hands the first does not (and the other way round in
      // the last), so its side of a table is ranked for itself and not copied from the first's
      {"flop bets, narrow v all", "TT+,AJs+,KQs,AQo+,76s,65s", "", {{250, 500, 1100}, {500, 1100}}, 1},
      {"turn bets, two ranges", "22+,A2s+,KTs+,AKo", "TT+,AJs+,KQs,AQo+,76s,65s",
       {{250, 500, 500, 1000}, {250, 250, 500, 10000}, {250, 10000}}, 3},
  };
  const int flops[3][3] = {{parse_card("Qs"), parse_card("4h"), parse_card("2s")},
                           {parse_card("Js"), parse_card("8h"), parse_card("4d")},
                           {parse_card("7s"), parse_card("7h"), parse_card("3d")}};
  uint64_t rng = 0x9e3779b97f4a7c15ull;
  auto unit = [&] {
    rng ^= rng << 13, rng ^= rng >> 7, rng ^= rng << 17;
    return static_cast<float>(rng >> 40) / static_cast<float>(1 << 24);
  };
  bool ok = true;
  for (const Spot& sp : spots) {
    GameConfig cfg;
    cfg.preflop = true;
    cfg.collapse_checkdowns = true;
    std::string err;
    for (int p = 0; p < 2; ++p) {
      const char* text = p && sp.range2 ? sp.range2 : sp.range;
      RangeWeights w;
      if (*text) {
        if (!parse_range(text, w, err)) return false;
      } else {
        w.fill(1.0f);
      }
      for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[p][i] = w[i];
    }
    cfg.pot[0] = 100, cfg.pot[1] = 50;
    cfg.eff_stack = 10000;
    cfg.lines.add = {{250, 250}, {10000, 10000}};
    {  // the exit after the call of 250 gets the flop tree
      GameTree pf;
      if (!pf.build(cfg, err, false)) { std::printf("preflop build: %s\n", err.c_str()); return false; }
      for (const ActionNode* an : pf.anodes) {
        if (!an->preflop_exit || an->line != std::vector<int32_t>{250, 250}) continue;
        Schematic sc;
        sc.exit_line = an->line;
        sc.pot[0] = sc.pot[1] = 250;
        sc.eff_stack = 10000;
        sc.lines.add = sp.flop_lines;
        cfg.schematics.push_back(sc);
      }
    }
    if (cfg.schematics.size() != 1) { std::printf("no exit found\n"); return false; }
    for (auto& f : flops) cfg.subset.push_back({1.0f, {f[0], f[1], f[2]}});
    uint64_t limit = 0;
    if (sp.room >= 0) {
      GameTree m;
      m.allin_plan = sp.room;
      if (!m.build(cfg, err, false)) return false;
      limit = static_cast<uint64_t>(std::max<int64_t>(1, static_cast<int64_t>(m.memory_bytes_for(true)) + sp.slack));
    }
    GameTree g;
    g.allin_plan = sp.ask;
    g.table_limit = limit;
    if (!g.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
    // what was planned, and that nothing is kept that nobody reads
    int sets[2] = {0, 0}, tabled[2] = {0, 0}, boards[2] = {0, 0}, unbuilt = 0, compact = 0;
    uint64_t table_bytes = 0;
    for (const AllinRunouts& ar : g.allins) {
      if (ar.exact) continue;
      const int lvl = ar.cards.size() == 4;
      ++sets[lvl];
      tabled[lvl] += ar.table;
      boards[lvl] += !ar.boards.empty();
      unbuilt += ar.table && !ar.built();
      compact += !ar.live.empty();
      // a compact table: the pairs of the hands its cards leave, each pair once
      const size_t n = ar.live.size(), pairs = n * (n - (n > 0)) / 2;
      unbuilt += !ar.live.empty() && (ar.tri8.size() + ar.tri16.size() != pairs || !ar.e2.empty() || !ar.e2b.empty());
      table_bytes += ar.e2.size() * 2 + ar.e2b.size() + ar.tri16.size() * 2 + ar.tri8.size() + ar.live.size() * 2;
    }
    bool plan = unbuilt == 0;
    for (int lvl = 0; lvl < 2; ++lvl) {
      const bool t = (sp.want_tables >> lvl) & 1;
      plan = plan && tabled[lvl] == (t ? sets[lvl] : 0) && boards[lvl] == (t ? 0 : sets[lvl]);
    }
    plan = plan && sets[0] == 3 && (sp.flop_lines.size() == 2 ? sets[1] == 0 : sets[1] > 3 * 20);  // flop bets: two lines
    // compact where the two players hold the same hands, and only there
    plan = plan && compact == (sp.range2 ? 0 : tabled[0] + tabled[1]) && g.compact_tables() == !sp.range2;
    const size_t kept = g.showdowns.size();
    if (sp.want_tables & 1 && (sets[1] == 0 || sp.want_tables & 2)) plan = plan && kept == 0;
    // the memory count: the tables built are the tables counted, before the storage as after it
    GameTree dry;
    dry.allin_plan = sp.ask;
    dry.table_limit = limit;
    if (!dry.build(cfg, err, false)) return false;
    const bool counted = dry.memory_bytes_for(true) == g.memory_bytes_for(true) && dry.showdowns.size() == kept;
    uint64_t dry_tables = 0;
    for (const AllinRunouts& ar : dry.allins)
      if (ar.table) dry_tables += dry.allin_table_bytes(ar);
    // values as built
    Solver s(g);
    struct Val {
      uint32_t node;
      int p;
      std::vector<float> ro, v;
    };
    std::vector<Val> vals;
    for (uint32_t ni = 0; ni < g.nodes.size(); ++ni) {
      const GNode& n = g.nodes[ni];
      if (n.kind != ANodeKind::Showdown || n.board_len == 5 || g.allins[n.aux].exact) continue;
      if (vals.size() >= 40 && unit() < 0.7f) continue;  // every hand: 4 ms a board-by-board value
      for (int p = 0; p < 2; ++p) {
        // as the deal hands them down: no reach on a hand holding a card of the board, whose own
        // value nobody reads
        Val v{ni, p, std::vector<float>(g.num_hands(1 - p)), std::vector<float>(g.num_hands(p))};
        for (float& x : v.ro) x = unit() < 0.3f ? 0.0f : unit();
        for (uint8_t c : g.allins[n.aux].cards)
          for (uint16_t h : g.hands_with_card[1 - p][c]) v.ro[h] = 0.0f;
        s.showdown_value(n, p, v.ro.data(), v.v.data());
        vals.push_back(std::move(v));
      }
    }
    // the same on the boards
    struct Tables {
      std::vector<uint16_t> e2, live;
      std::vector<uint8_t> e2b;
      std::vector<int16_t> tri16;
      std::vector<int8_t> tri8;
      bool operator==(const Tables&) const = default;
    };
    auto take = [](AllinRunouts& ar) {
      Tables t{std::move(ar.e2), std::move(ar.live), std::move(ar.e2b), std::move(ar.tri16), std::move(ar.tri8)};
      ar.e2.clear(), ar.live.clear(), ar.e2b.clear(), ar.tri16.clear(), ar.tri8.clear();
      return t;
    };
    std::vector<Tables> first;
    for (AllinRunouts& ar : g.allins) first.push_back(take(ar));
    // what the boards will add is known before they are built (the memory check of set_icm): never
    // more than they do add, and most of it (the strength groups are what the count leaves out)
    const uint64_t missing = g.allin_boards_missing_bytes(), before_boards = g.memory_bytes_for(true);
    g.restore_allin_boards();
    const uint64_t added = g.memory_bytes_for(true) - before_boards;
    // (turn tables alone leave no board out: the flop sets of these flops read every one of them)
    const bool foreseen = g.allin_boards_missing_bytes() == 0 && missing <= added && added <= missing + missing / 2 &&
                          (missing > 0) == (tabled[0] > 0);
    g.build_allin_tables();  // from the boards now
    long same_counts = 0, sets_tabled = 0;
    for (size_t i = 0; i < g.allins.size(); ++i) {
      if (!g.allins[i].table) continue;
      ++sets_tabled;
      const bool built = g.allins[i].built();
      Tables again = take(g.allins[i]);
      same_counts += built && again == first[i];
      g.allins[i].e2 = std::move(again.e2), g.allins[i].live = std::move(again.live), g.allins[i].e2b = std::move(again.e2b);
      g.allins[i].tri16 = std::move(again.tri16), g.allins[i].tri8 = std::move(again.tri8);
    }
    s.allin_tables = false;
    long bad = 0;
    double worst = 0;
    for (const Val& v : vals) {
      std::vector<float> b(v.v.size());
      const GNode& n = g.nodes[v.node];
      s.showdown_value(n, v.p, v.ro.data(), b.data());
      uint64_t bm = 0;
      for (uint8_t c : g.allins[n.aux].cards) bm |= card_bit(c);
      double mag = 0;
      for (float x : b) mag = std::max(mag, std::fabs(static_cast<double>(x)));
      for (size_t h = 0; h < b.size(); ++h) {
        if (combo_mask(g.hands[v.p][h]) & bm) continue;
        const double d = std::fabs(static_cast<double>(v.v[h]) - b[h]) / (1.0 + mag);
        worst = std::max(worst, d);
        if (d > 2e-5) ++bad;  // float32 sums in two orders, as in allin_tables_match
      }
    }
    // ICM: turned on after the build, the tree answers as one built under it (boards, the same sums)
    PayoffModel pm;
    pm.icm.enabled = true;
    pm.icm.stacks[0] = pm.icm.stacks[1] = 10000;
    for (int p = 0; p < 2; ++p) pm.icm.points[p] = {{0.0, 0.0}, {10000.0, 50.0}, {20000.0, 80.0}};
    // The one built under it is asked for tables everywhere, with all the room: it gets none (a table
    // cannot say what a runout pays under ICM, and a set with a table and no boards would be worth 0).
    GameTree gi, gj;
    gi.set_payoff(pm);
    gi.allin_plan = 3;
    gi.table_limit = ~uint64_t(0);
    if (!gi.build(cfg, err) || !gj.build(cfg, err)) return false;
    gj.set_payoff(pm);
    Solver si(gi), sj(gj);
    long icm_bad = 0, icm_n = 0;
    bool icm_boards = gi.showdowns.size() == gj.showdowns.size() && gi.nodes.size() == gj.nodes.size();
    for (const AllinRunouts& ar : gj.allins) icm_boards = icm_boards && (ar.exact || !ar.boards.empty());
    for (const AllinRunouts& ar : gi.allins) icm_boards = icm_boards && (ar.exact || (!ar.table && !ar.built() && !ar.boards.empty()));
    for (size_t k = 0; k < vals.size() && k < 12 && icm_boards; ++k) {
      const Val& v = vals[k];
      std::vector<float> a(v.v.size()), b(v.v.size());
      si.showdown_value(gi.nodes[v.node], v.p, v.ro.data(), a.data());
      sj.showdown_value(gj.nodes[v.node], v.p, v.ro.data(), b.data());
      icm_bad += a != b;
      ++icm_n;
    }
    const bool good = plan && counted && foreseen && dry_tables == table_bytes && same_counts == sets_tabled && bad == 0 &&
                      !vals.empty() && icm_boards && icm_bad == 0 && icm_n > 0;
    std::printf("[preflop runouts] %-24s %d+%d sets, %d+%d with tables (%.1f MB), %zu boards kept: plan %s, memory count %s, boards to come %.1f MB counted / %.1f MB added %s, "
                "%ld/%ld tables equal the boards' counts, %zu values worst relative difference %.2e (%ld bad), ICM %ld/%ld equal\n",
                sp.name, sets[0], sets[1], tabled[0], tabled[1], static_cast<double>(table_bytes) / 1e6, kept,
                plan ? "ok" : "WRONG", counted && dry_tables == table_bytes ? "ok" : "WRONG", static_cast<double>(missing) / 1e6,
                static_cast<double>(added) / 1e6, foreseen ? "ok" : "WRONG", same_counts, sets_tabled,
                vals.size(), worst, bad, icm_n - icm_bad, icm_n);
    ok = ok && good;
  }
  return ok;
}

// #484: with collapse_checkdowns a subtree in which nobody has a choice left is one showdown node on the
// runouts. It has to be worth what the one-action nodes were: every collapsed node is compared with the
// subtree it replaces under random opponent reaches, the roots under the uniform start, and then both
// trees are solved. The two solves do not follow the same path (a hand that wins the same pot by betting
// into folds as by checking has a regret of exactly zero, and rounding then decides its next strategy),
// so after solving they are compared through what the theory guarantees: each solve brackets the value
// of the game between what its two best responses get, and the brackets of two solves of the same game
// have to overlap. A tree without such a subtree must not change at all.
static bool checkdowns_collapse() {
  struct Spot {
    const char* name;
    const char* board;
    bool iso;
    std::vector<std::vector<int>> lines;
  } spots[] = {
      {"flop tree, flop bets only", "KhQs7d", true, {{0, 50, 150, 150}, {50, 150, 400}, {0, 0}}},
      {"flop tree, no isomorphism", "Kh9h4h", false, {{0, 50, 150, 150}, {50, 50}, {0, 0}}},
      {"flop tree, flop and turn bets", "KhQs7d", true, {{0, 50, 50, 50, 150, 400, 400}, {50, 150, 150, 250, 250}, {0, 0, 0, 100, 100}}},
      {"turn tree, turn bets only", "KhQs7d4c", false, {{0, 50, 150, 150}, {50, 150, 400}, {0, 0}}},
      {"river tree, IP can only check back", "KhQs7d4c2s", false, {{50, 150, 150}, {0, 0}}},
      {"no bets at all", "KhQs7d", true, {{0, 0}}},
  };
  auto config = [](const Spot& sp, bool collapse, GameConfig& cfg) {
    parse_cards(sp.board, cfg.board);
    std::string err;
    RangeWeights w;
    if (!parse_range("AA,KK,QQ,JJ,TT,99,77,44,AK,AQ,AJ,KQ,KJ,QJ,JT,T9s,98s,87s,A5s,K5s", w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
    if (!parse_range("AA,KK,QQ,JJ,TT,88,77,55,AK,AQ,KQ,KT,QJ,QT,JT,J9s,T9s,65s,A4s", w, err)) return false;
    for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
    cfg.pot[2] = 100;
    cfg.eff_stack = 400;
    cfg.iso_flop = sp.iso;
    cfg.lines.add = sp.lines;
    cfg.collapse_checkdowns = collapse;
    return true;
  };
  bool ok = true;
  uint64_t rng = 0x13198a2e03707344ull;
  auto unit = [&] {
    rng ^= rng << 13, rng ^= rng >> 7, rng ^= rng << 17;
    return static_cast<float>(rng >> 40) / static_cast<float>(1 << 24);
  };
  for (const Spot& sp : spots) {
    GameConfig c1, c2;
    if (!config(sp, false, c1) || !config(sp, true, c2)) return false;
    GameTree g1, g2;
    std::string err;
    if (!g1.build(c1, err) || !g2.build(c2, err)) { std::printf("build: %s\n", err.c_str()); return false; }
    const bool shape = g1.checkdowns == 0 && g2.checkdowns > 0 && g2.nodes.size() < g1.nodes.size() &&
                       g2.storage_floats < g1.storage_floats;
    Solver s1(g1), s2(g2);
    // Node by node: the two trees are the same down to each collapsed node.
    long compared = 0, bad = 0;
    double worst = 0;
    {
      Solver w1(g1);
      w1.parallel = false;
      // (node in g1, node in g2, cards dealt on the way: no hand holding one of them reaches the node)
      std::vector<std::tuple<uint32_t, uint32_t, std::vector<uint8_t>>> todo;
      todo.push_back({0, 0, {}});
      while (!todo.empty()) {
        const auto [i1, i2, dealt] = todo.back();
        todo.pop_back();
        const GNode& n1 = g1.nodes[i1];
        const GNode& n2 = g2.nodes[i2];
        if (n1.kind == n2.kind) {
          if (n1.kind != ANodeKind::Decision && n1.kind != ANodeKind::Chance) continue;
          if (n1.num_children != n2.num_children) { ++bad; continue; }
          for (uint32_t k = 0; k < n1.num_children; ++k) {
            std::vector<uint8_t> d = dealt;
            if (n1.kind == ANodeKind::Chance) d.push_back(g1.chances[n1.aux].cards[k]);
            todo.push_back({n1.first_child + k, n2.first_child + k, d});
          }
          continue;
        }
        if (n2.kind != ANodeKind::Showdown) { ++bad; continue; }
        for (int p = 0; p < 2; ++p) {
          const int Hp = g1.num_hands(p), Ho = g1.num_hands(1 - p);
          std::vector<float> rp(Hp, 1.0f), ro(Ho), x(Hp), y(Hp);
          for (float& v : ro) v = unit() < 0.3f ? 0.0f : unit();
          for (uint8_t c : dealt)
            for (uint16_t h : g1.hands_with_card[1 - p][c]) ro[h] = 0.0f;
          w1.walk(Walk::Evaluate, i1, p, rp.data(), ro.data(), x.data());
          s2.showdown_value(n2, p, ro.data(), y.data());
          std::vector<char> live(Hp, 1);  // the same for the player's own hands: their values are not read
          for (uint8_t c : dealt)
            for (uint16_t h : g1.hands_with_card[p][c]) live[h] = 0;
          double mag = 0;
          for (int h = 0; h < Hp; ++h)
            if (live[h]) mag = std::max(mag, std::fabs(static_cast<double>(x[h])));
          for (int h = 0; h < Hp; ++h) {
            if (!live[h]) continue;
            const double d = std::fabs(static_cast<double>(x[h]) - y[h]) / (1.0 + mag);
            worst = std::max(worst, d);
            if (d > 2e-5) ++bad;  // float32 sums in two orders, as in allin_tables_match
          }
          ++compared;
        }
      }
    }
    // pot 100: 1e-3 chips is 1e-5 of the pot
    const RootStats a0 = s1.root_stats(), b0 = s2.root_stats();
    double d0 = std::fabs(a0.exploitability - b0.exploitability);
    for (int p = 0; p < 2; ++p) d0 = std::max({d0, std::fabs(a0.ev[p] - b0.ev[p]), std::fabs(a0.mes[p] - b0.mes[p])});
    for (int t = 1; t <= 60; ++t) {
      s1.iterate(t);
      s2.iterate(t);
    }
    // OOP's value of the game is at most what OOP's best response gets and at least the pot minus what
    // IP's best response gets, for either solve.
    const RootStats a = s1.root_stats(), b = s2.root_stats();
    const double pot = a.ev[0] + a.ev[1];
    const double lo = std::max(pot - a.mes[1], pot - b.mes[1]), hi = std::min(a.mes[0], b.mes[0]);
    const bool good = shape && compared > 0 && bad == 0 && d0 <= 1e-3 && lo <= hi + 1e-3;
    ok &= good;
    std::printf("[checkdowns] %s: nodes %zu -> %zu, rows %llu -> %llu floats, %ld collapsed node/player pairs worst "
                "relative difference %.2e, roots differ by %.1e at the start, value of the game in [%.4f, %.4f] and "
                "[%.4f, %.4f] after 60 iterations: %s\n",
                sp.name, g1.nodes.size(), g2.nodes.size(), static_cast<unsigned long long>(g1.storage_floats),
                static_cast<unsigned long long>(g2.storage_floats), compared, worst, d0, pot - a.mes[1], a.mes[0],
                pot - b.mes[1], b.mes[0], good ? "ok" : "FAILED");
    if (!good) std::printf("  shape %d, bad %ld\n", shape, bad);
  }
  // Every decision down to the river showdowns has two actions or more here: nothing to collapse, and
  // nothing may change.
  {
    Spot sp{"", "KhQs7d4c", false,
            {{0, 0, 0, 0}, {0, 0, 50, 50}, {0, 0, 0, 50, 50},
             {50, 50, 50, 50}, {50, 50, 150, 150}, {50, 50, 50, 150, 150},
             {0, 50, 50, 50, 50}, {0, 50, 50, 150, 150}, {0, 50, 50, 50, 150, 150}}};
    GameConfig c1, c2;
    if (!config(sp, false, c1) || !config(sp, true, c2)) return false;
    GameTree g1, g2;
    std::string err;
    if (!g1.build(c1, err) || !g2.build(c2, err)) { std::printf("build: %s\n", err.c_str()); return false; }
    Solver s1(g1), s2(g2);
    for (int t = 1; t <= 30; ++t) {
      s1.iterate(t);
      s2.iterate(t);
    }
    const bool same = g2.checkdowns == 0 && g1.nodes.size() == g2.nodes.size() && g1.regrets == g2.regrets &&
                      g1.strat == g2.strat && !g1.regrets.empty();
    ok &= same;
    std::printf("[checkdowns] a tree without one is untouched (%zu nodes, %llu collapsed, regrets and sums after 30 "
                "iterations): %s\n",
                g1.nodes.size(), static_cast<unsigned long long>(g2.checkdowns), same ? "identical" : "DIFFER");
  }
  return ok;
}

// #186: the two-phase river walk (river_cfr) must reproduce walk_t exactly: same regrets and
// strategy sums, bit for bit, after many iterations with pruning, fold pairing, the opponent's fused
// fold and all-in tables in play. Forced on here so scalar builds test the walk logic too.
static bool river_batch_matches() {
  GameConfig cfg;
  parse_cards("KhQs7d4c", cfg.board);
  std::string err;
  RangeWeights w;
  if (!parse_range("AA,KK,QQ,JJ,TT,99,77,44,AK,AQ,AJ,KQ,KJ,QJ,JT,T9s,98s,87s,A5s,K5s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[0][i] = w[i];
  if (!parse_range("AA,KK,QQ,JJ,TT,88,77,55,AK,AQ,KQ,KT,QJ,QT,JT,J9s,T9s,65s,A4s", w, err)) return false;
  for (int i = 0; i < NUM_COMBOS; ++i) cfg.range[1][i] = w[i];
  cfg.pot[2] = 100;
  cfg.eff_stack = 400;
  cfg.lines.add = {{0, 50, 50, 150, 150}, {0, 0, 50, 150, 400}, {50, 150, 150, 400}, {0, 0, 0, 100, 100}, {0, 100, 400}};
  GameTree g1, g2;
  if (!g1.build(cfg, err) || !g2.build(cfg, err)) { std::printf("build: %s\n", err.c_str()); return false; }
  Solver s1(g1), s2(g2);
  s1.river_batch = false;
  s2.river_batch = true;
  for (int t = 1; t <= 60; ++t) {
    s1.iterate(t);
    s2.iterate(t);
  }
  const bool same = g1.regrets == g2.regrets && g1.strat == g2.strat;
  std::printf("[river batch] two-phase river walk vs walk_t after 60 iterations: %s (%zu floats)\n",
              same ? "identical" : "DIFFER", g1.regrets.size());
  // The exploitability check's river walk (river_evbr) against walk_evbr, on the same strategies.
  const RootStats a = s1.root_stats(), b = s2.root_stats();
  const bool same_stats = a.ev[0] == b.ev[0] && a.ev[1] == b.ev[1] && a.mes[0] == b.mes[0] && a.mes[1] == b.mes[1] &&
                          a.exploitability == b.exploitability && a.exploitability > 0;
  std::printf("[river batch] exploitability check river walk vs walk_evbr: %s (%.9g vs %.9g)\n",
              same_stats ? "identical" : "DIFFER", a.exploitability, b.exploitability);
  return same && same_stats && !g1.regrets.empty();
}

// set_info_freq auto across the average's restart (set_avg_delay). Replays the reports of a real
// 115K-node flop solve (the viewer's fast-preview sketch, target 0.165 chips, delay 32): the restart
// at iteration 33 lifts exploitability from 2.2 to 4.9, just under the previous report's 5.25. A
// trend fitted across that reads as no progress and waited the longest gap (to 49, at 0.82 chips);
// starting afresh reports at 41 (1.23 chips), which is when the preview's first threshold is met.
static bool auto_report_schedule() {
  AutoReportSchedule s;
  const double target = 0.165;
  const long long reset = 33;
  long long a = s.next(10, 29.38, target, reset), b = s.next(a, 11.74, target, reset);
  long long c = s.next(b, 5.25, target, reset), d = s.next(c, 4.88, target, reset);
  AutoReportSchedule old;   // no restart passed: the trend runs across it
  long long od = old.next(old.next(old.next(old.next(10, 29.38, target, 0), 11.74, target, 0), 5.25, target, 0), 4.88, target, 0);
  // Without set_avg_delay the restart test never fires; a restart before the first report neither.
  AutoReportSchedule n1, n2;
  long long e1 = n1.next(n1.next(40, 2.0, target, 0), 1.0, target, 0);
  long long e2 = n2.next(n2.next(40, 2.0, target, 33), 1.0, target, 33);
  bool ok = a == 15 && b == 22 && c == 33 && d == 41 && od == 49 && e1 == e2;
  std::printf("[auto reports] after the average restarts: next report %lld (old schedule %lld), %s\n", d, od,
              ok ? "ok" : "WRONG");
  return ok;
}

int main(int argc, char** argv) {
  init_cards();
  bool big = argc > 1 && std::string(argv[1]) == "big";
  int fails = 0;
  fails += !parallel_equivalence();
  fails += !auto_report_schedule();
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
  fails += !pruning_ignores_blocked_hands();
  fails += !mem_accounting();
  fails += !showdown_kernels_match();
  fails += !decompress_midsolve();
  fails += !stochastic_sums();
  fails += !allin_tables_match();
  fails += !preflop_runout_tables();
  fails += !river_batch_matches();
  fails += !checkdowns_collapse();
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
