// Vector-form Discounted CFR over a GameTree, plus best-response and evaluation traversals.
#pragma once
#include <atomic>
#include <cstdint>
#include <vector>

#include "game.hpp"

namespace pio {

enum class Walk { CFR, BestResponse, Evaluate };

struct DcfrParams {
  double alpha = 1.5;  // positive regret discount exponent
  double beta = 0.0;   // negative regret discount exponent
  // 3, not DCFR's published 2: a sweep over the benchmark suite (#52) found gamma 3 reaches
  // every target in fewer iterations than 2 on every tree, by up to 19% of wall time. See
  // set_dcfr to override, and bench/dcfr_sweep.md for the numbers.
  double gamma = 3.0;  // strategy-sum discount exponent
};

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
};

}  // namespace pio
