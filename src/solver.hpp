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
  //
  // The stall, quantified on flop_3bet at gamma 4 (independent check, #110 follow-up): alpha 1.2
  // wins at loose targets and loses at tight ones, crossing over between 0.02% and 0.01% of pot.
  //
  //   alpha   to 0.05%   to 0.02%   to 0.01%   iterations
  //   1.2       4.11 s     6.19 s    12.67 s      270
  //   1.3       4.29 s     6.85 s    10.07 s      220
  //
  // So a sweep that stops at 0.05% picks 1.2 and is wrong. Any retune here must go to at least
  // 0.01% of pot, and should prefer iteration counts (exact integers) over wall time.
  double alpha = 1.3;  // positive regret discount exponent
  double beta = 0.0;   // negative regret discount exponent
  double gamma = 4.0;  // strategy-sum discount exponent
  // beta schedule: from iteration beta_switch + 1 on, negative regrets use beta_late instead of beta
  // (0 = no switch). beta = 0 halves every negative regret each iteration, so discarded actions keep
  // re-entering; on large trees with many actions (a realistic 13 GB SRP tree: 3 flop sizes x 2
  // raises, 2 turn and 2 river sizes) the average then oscillates and stalls around 1% of pot.
  // Slower forgetting late in the solve fixes that: exploitability at iteration 125 / 150 / 200
  // 0.83 / 0.66 / 0.75 -> 0.67 / 0.44 / 0.24, and 1% of pot in 140 instead of 220 iterations (70 s,
  // level with PioSOLVER on the same PC). Constant beta 0.5 does as well there but costs the small
  // bench trees 25-40% more iterations; switching after 50 keeps them within 0-11% (deep 180 -> 200,
  // flop_3bet 95 -> 100, river 165 -> 175; flop_srp_4, flop_srp_2, turn unchanged). Constant
  // beta 0.25 oscillates too.
  double beta_late = 0.5;
  int beta_switch = 50;
  double beta_at(int t) const { return beta_switch > 0 && t > beta_switch ? beta_late : beta; }
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
  // 32, not 0. The first iterations are the noisiest, and skipping their contribution to the
  // strategy sum both saves the accumulation work and leaves a cleaner average. #80 swept this
  // and found nothing, but that predated the alpha/gamma retune (#72) and the prune_period fix
  // (#131), which changed where the time goes. Re-swept at the current defaults, to 0.05% of
  // pot, 1 thread:
  //
  //   tree          delay 0    delay 32     exploitability
  //   flop_srp_2     12.59 s    10.85 s     (+13.8%)   0.026 vs 0.026
  //   flop_srp_4     26.46 s    24.11 s     (+8.9%)    0.029 vs 0.028
  //   deep           43.18 s    41.21 s     (+4.6%)    0.020 vs 0.020
  //   flop_3bet       4.26 s     4.20 s     (+1.4%)    0.099 vs 0.089
  //   turn            0.24 s     0.23 s                0.045 vs 0.045
  //
  // Those iteration counts looked unchanged, which led me to call this a per-iteration saving
  // rather than better convergence. That was wrong: they were reported at set_info_freq 5, so
  // both rounded to 80. At info_freq 1 the exact counts are 79 (delay 0) against 76 (delay 32)
  // on flop_srp_4, i.e. fewer iterations. See the avg_reset note below for the real mechanism.
  //   preflop         0.09 s     0.08 s                0.007 vs 0.007
  //
  // Better or equal on every tree with exploitability equal or better.
  //
  // The old way to apply it (skip accumulation for t <= avg_delay) leaves the average EMPTY if a
  // solve stops before the delay, falling back to near-pure regret-derived strategies. #135 fixed
  // that with avg_reset: accumulate from t = 1 and zero the sums at t = avg_delay + 1, which is
  // bit-identical past the delay and keeps the full average before it. Measured then (1 thread,
  // time to 0.2% / 0.05% of pot): flop_srp_4 -11% / -4%, flop_srp_2 0 / -9%, deep -1% / -2%,
  // flop_3bet, turn, river, preflop unchanged. Accumulation itself is only 2.5-6% of an
  // iteration, so the gain is convergence (a cleaner average), not skipped work.
  // The UPI default is 32 (Upi::avg_delay_); this struct's 0 keeps library callers (tests) exact.
  int avg_delay = 0;       // strategies only enter the average after this iteration (0 = from t=1)
  // How the delay is applied (#135). false: iterations t <= avg_delay skip accumulation, so a solve
  // stopped before the delay has an EMPTY average. true (default): accumulate from t = 1 and zero
  // the strategy sums at the start of iteration avg_delay + 1, so the average is never empty and
  // every run past the delay is bit-identical to the skipping version.
  bool avg_reset = true;
  void reset_strategy_sums();  // zero every unlocked strategy sum (locked strategies are kept)

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
  // 8, not 32. The period trades activation delay (pruning is inactive until t > 2*period)
  // against how often a full measurement iteration is forced, and those pull opposite ways.
  // At the targets the suite actually reports (0.2% of pot, reached in 35-100 iterations) the
  // activation delay dominates: period 32 does not engage until t=64, so on flop_srp_2 - which
  // reaches target at t=45 - pruning never ran at all. Measured to 0.2% of pot, 1 thread:
  //
  //   tree          iters   period 8   period 32
  //   flop_srp_4      50     17.24 s    18.72 s   (+7.9%)
  //   flop_srp_2      45      7.78 s     8.25 s   (+5.7%)
  //   deep           100     24.92 s    25.49 s   (+2.2%)
  //   flop_3bet       50      2.41 s     2.43 s   (+0.8%)
  //   turn            35      0.10 s     0.10 s   (=)
  //
  // Better or equal on every tree, results bit-identical. The crossover is past ~100
  // iterations: at 180 on deep, period 32 wins back 1.3% (26.23 s vs 26.58 s), because by
  // then the activation delay is amortised and the forced full iterations dominate.
  //
  // CAREFUL if you raise it. The period sets BOTH the measurement frequency and the
  // activation delay (`t > 2 * prune_period`), so raising it pushes activation out too and
  // can switch pruning off entirely for a short solve. `set_pruning on 64` delays activation
  // to t=128, and every bench tree reaches 0.2% of pot by t=100, so pruning never runs:
  // flop_srp_2 goes 7.71 s -> 8.22 s, i.e. slower than the default despite asking for more
  // pruning. Only raise it for runs that will go well past 2x the new period.
  //
  // Decoupling the two (a separate warmup field) was tried and rejected: it removes the
  // footgun but is ~1.3% slower, because the extra int changes this struct's layout, and at
  // fixed warmup the period sweep showed no consistent winner across trees.
  int prune_period = 8;  // <= 1: never force a full iteration

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
  template <typename F>
  void run_actions(const GNode& n, int A, F& act);
  void strategy_only(uint32_t ni, int p, const float* rp);
  void flop_deal(const GNode& n, const ChanceInfo& ci, Walk mode, int p, const float* rp, const float* ro, float* out);
  // Evaluate and BestResponse in one traversal (root_stats): ev = on-policy values, br = best
  // response values, both for player p against the opponent's average strategy.
  void walk_evbr(uint32_t ni, int p, const float* rp, const float* ro, float* ev, float* br);
  void fold_value(const GNode& n, int p, const float* ro, float* out) const;
  void showdown_value(const GNode& n, int p, const float* ro, float* out) const;
  void showdown_board(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out) const;

  GameTree& g_;
  float pos_mult_ = 1, neg_mult_ = 1, strat_mult_ = 1;
  bool accumulate_ = true;  // t > avg_delay: whether this iteration feeds the strategy sum
  bool prune_this_iter_ = false;  // set by iterate/iterate_subtree; read-only during walks
};

}  // namespace pio
