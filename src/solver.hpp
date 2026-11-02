// Vector-form Discounted CFR over a GameTree, plus best-response and evaluation traversals.
#pragma once
#include <atomic>
#include <cstdint>
#include <vector>

#include "game.hpp"

namespace pio {

enum class Walk { CFR, BestResponse, Evaluate };

struct DcfrParams {
  // Tuned on the benchmark suite (#52; bench/dcfr_sweep.md), not DCFR's published 1.5 / 0 / 2:
  // alpha 1.3, gamma 4 reaches every target (1% .. 0.05% of the pot) in no more iterations than
  // 1.5 / 0 / 3 on every bench tree, and far fewer on deep and wide trees (0.05%: deep 260 -> 180,
  // flop_srp_4 105 -> 80, preflop 85 -> 60). alpha <= 1.2 is faster early but stalls at tight targets
  // on rivers. set_dcfr overrides.
  double alpha = 1.3;  // positive regret discount exponent
  double beta = 0.0;   // negative regret discount exponent
  double gamma = 4.0;  // strategy-sum discount exponent
};

// Showdown values against one river board: out[h] += value of p's hand h given opponent reach ro,
// with W / L / T the win / loss / tie payoffs. showdown_board_scalar is the reference. x86 builds
// with AVX-512F, BW and VBMI (-march=native on Zen 4+ / Ice Lake+) use showdown_board_avx512, which
// must give the same floats bit for bit (#89; tests/test_solver.cpp checks). -DPIO_NO_AVX512 opts out.
#if defined(__AVX512F__) && defined(__AVX512BW__) && defined(__AVX512VBMI__) && !defined(PIO_NO_AVX512)
#define PIO_SHOWDOWN_AVX512 1
#else
#define PIO_SHOWDOWN_AVX512 0
#endif
void showdown_board_scalar(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out);
#if PIO_SHOWDOWN_AVX512
void showdown_board_avx512(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out);
#endif

struct RootStats {
  double ev[2] = {0, 0};
  double mes[2] = {0, 0};
  double exploitability = 0;
};

class Solver {
 public:
  explicit Solver(GameTree& g) : g_(g) {}

  DcfrParams params;
  int first_player = 0;  // set_first_iteration_player
  bool parallel = true;
  bool write_best_response = false;  // BestResponse walks store the pure BR strategy (set_mes)
  // #71 convergence experiments (set_predictive / set_avg_delay):
  bool predictive = false;  // regret matching input is R + last instantaneous regret (PCFR+-style)
  int avg_delay = 0;        // strategies only enter the average after this iteration (0 = from t=1)

  // Regret-based pruning (set_pruning, #51; Brown & Sandholm, "Regret-Based Pruning in
  // Extensive-Form Games", NIPS 2015). During a CFR walk only, an action of the traverser whose
  // CURRENT strategy is exactly zero for every one of the traverser's hands is pruned: its subtree
  // is not re-measured this iteration and its regret row is left completely untouched (frozen, not
  // discounted). This changes nothing about this iteration's strategy, cfv aggregate or
  // strategy-sum rows (the zero-strategy condition already guarantees "this action contributes 0
  // everywhere"); the skipped instantaneous regret measurement is the bounded approximation RBP
  // theory covers. Every prune_period-th iteration (default 32) is measured in full (the RBP
  // revisiting schedule), which is when a frozen row can re-enter.
  //
  // Pruning activates only after two full periods (t > 2*prune_period). Bench: pruning from t=1
  // is ruinous - early on about half of all rows are all-nonpositive at any moment and re-enter
  // constantly, and frozen rows cannot re-enter until the next full iteration (flop_3bet to
  // 0.2% pot: 50 -> 230 iterations at period 32, 90 at period 8; the damage scales with the
  // period, and absolute chip margins do not fix it because early regret magnitudes are large).
  // After the strategy settles, exact-zero rows are stable and rarer, so gating reproduces the
  // unpruned iteration counts exactly on every bench tree while keeping the per-iteration savings
  // of the long tail (deep to 0.05% pot: 33.1s -> 30.5s).
  // Best-response, evaluate and strategy_only walks are never pruned.
  bool pruning = true;  // on by default (#51): inactive before t > 2*prune_period, then never
                        // slower and up to ~8% faster on long solves (bench data in the comment)
  int prune_period = 32;  // <= 1: never force a full iteration

  // CFR iterations restricted to the subtree at `ni` with the given (canonical) reaches.
  void iterate_subtree(uint32_t ni, int t, const float* r0, const float* r1);

  // One full iteration (both players). `t` is the 1-based iteration number.
  void iterate(int t);

  // Generic traversal from node `ni` for player p. reach vectors are in the canonical frame of the
  // node, of length H[p] and H[1-p]. out has length H[p] (counterfactual values).
  void walk(Walk mode, uint32_t ni, int p, const float* rp, const float* ro, float* out);

  // Stats from the tree root.
  RootStats root_stats();

  // Strategies at decision node (A * H[acting]): current (regret matching) / average.
  void current_strategy(uint32_t ni, float* out) const;
  void average_strategy(uint32_t ni, float* out) const;
  void apply_elimination(uint32_t ni, float* out) const;

  // Sum over opponent hands compatible with each of p's hands: out[h] = sum_o ro[o] * compat(h, o).
  void compat_mass(int p, const float* ro, float* out) const;
  // Total matchups at the root: sum_h r_p[h] * compat mass.
  double root_matchups() const;

 private:
  template <Walk M>
  void walk_t(uint32_t ni, int p, const float* rp, const float* ro, float* out);
  void strategy_only(uint32_t ni, int p, const float* rp);
  void flop_deal(const GNode& n, const ChanceInfo& ci, Walk mode, int p, const float* rp, const float* ro, float* out);
  void fold_value(const GNode& n, int p, const float* ro, float* out) const;
  void showdown_value(const GNode& n, int p, const float* ro, float* out) const;
  void showdown_board(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out) const;

  GameTree& g_;
  float pos_mult_ = 1, neg_mult_ = 1, strat_mult_ = 1;
  bool accumulate_ = true;  // t > avg_delay: whether this iteration feeds the strategy sum
  bool prune_this_iter_ = false;  // set by iterate/iterate_subtree; read-only during walks
};

}  // namespace pio
