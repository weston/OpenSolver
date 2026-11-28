// Vector-form Discounted CFR over a GameTree, plus best-response and evaluation traversals.
#pragma once
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <cstdint>
#include <unordered_map>
#include <vector>

#include "average_sampling.hpp"
#include "game.hpp"
#include "reach_digits.hpp"

namespace pio {

enum class Walk { CFR, BestResponse, Evaluate };

struct DcfrParams {
  bool operator==(const DcfrParams&) const = default;

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
// AArch64 (Apple silicon, Linux ARM): showdown_batch runs its jobs as NEON lanes (#219), the same
// design as the AVX-512 lanes kernel. -DPIO_NO_NEON_LANES opts out.
#if !PIO_SHOWDOWN_AVX512 && defined(__aarch64__) && defined(__ARM_NEON) && !defined(PIO_NO_NEON_LANES)
#define PIO_SHOWDOWN_NEON 1
#else
#define PIO_SHOWDOWN_NEON 0
#endif
// x86 with AVX2 + FMA but not the AVX-512 set above (most Intel desktops, Zen 2/3, -march=x86-64-v3
// builds): the lanes kernel is plain 256-bit code, so it runs there too (#225). -DPIO_NO_AVX2_LANES
// opts out.
#if !PIO_SHOWDOWN_AVX512 && defined(__AVX2__) && defined(__FMA__) && !defined(PIO_NO_AVX2_LANES)
#define PIO_SHOWDOWN_AVX2 1
#else
#define PIO_SHOWDOWN_AVX2 0
#endif
// compat (optional, hand-indexed, zeroed by the caller): also store each of p's hands' card-removal
// compatible opponent mass, sum of ro over opponent hands disjoint from h. It is a by-product of the
// kernel, and exactly what a fold child with the same opponent reach needs (Solver::call_and_fold).
// fold (optional): an opponent fold sibling of this showdown (opponent decision facing p's bet: fold or
// call). Its value pay * (compatible mass of xf) is added to out in the same kernel call: the fold mass
// is accumulated in the kernel's opponent pass and its card-removal lookups ride along in the final
// pass, instead of a separate pass over both hand lists (Solver::call_with_opp_fold). xf is the fold
// reach (opponent reach * fold probability), precomputed so the kernels only add: a product inside
// them could be FMA-contracted differently by the scalar and the AVX-512 code.
struct SdFold {
  const float* xf;  // opponent's reach into the fold, by opponent hand
  float pay;        // p's payoff when the opponent folds
};
void showdown_board_scalar(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                           float* compat = nullptr, const SdFold* fold = nullptr);
// One showdown terminal for showdown_batch (#186): out and compat need not be zeroed, showdown_batch
// zeroes what its kernel needs (zero_rows); compat set: also the compatible mass (call_and_fold); xf
// set: the opponent's fused fold (call_with_opp_fold).
struct SdJob {
  const float* ro;
  float W, L, T;
  float* out;
  float* compat;
  const float* xf;
  float pay;
};
// Jobs on one board, each bit-identical to its own showdown_board call. ho = the opponent's hand count
// (the length of every ro / xf row). On AVX-512, AVX2 and NEON builds the jobs run as SIMD lanes, up to
// eight lanes per sweep over the board (showdown_lanes_256 in solver.cpp; NEON: showdown_lanes_neon).
void showdown_batch(const ShowdownBoard& sb, int p, int n, const SdJob* jobs, int ho);
#if PIO_SHOWDOWN_AVX512
void showdown_board_avx512(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                           float* compat = nullptr, const SdFold* fold = nullptr);
#endif

struct RootStats {
  double ev[2] = {0, 0};
  double mes[2] = {0, 0};
  double exploitability = 0;
};

enum class FlopOpening : uint8_t { Ordinary, SquareRoot, Edited };
enum class AverageSampling : uint8_t { Dense, AwaitingFirstGo, Bounded };

class Solver;
// A hybrid solve (#582): a device (gpu_solve.cpp) solves some of a flop tree's turn subtrees in each pass
// while the CPU walks the rest. Each pass the CPU writes both players' reaches at the device's roots
// (Solver::device_reach), launches it, and takes its root values at the flop's turn deals.
class SubtreeDevice {
 public:
  virtual ~SubtreeDevice() = default;
  virtual int root(uint32_t node) const = 0;  // the node's index among the device's roots, or -1
  virtual float* reach(int player, int root) = 0;  // where the host writes that reach for the next pass
  virtual void launch(Solver& s, int t, int phase, int p) = 0;  // p's pass, from the reaches written
  // A report (Solver::root_stats): each player's EV and best response against the average strategy, from
  // the average-strategy reaches written; values waits for them.
  virtual void launch_check() = 0;
  virtual const float* check_values(int p, bool best_response, int root) = 0;
  virtual const float* values(int root) = 0;  // p's values there (waits for the pass)
  virtual void finish() = 0;                  // waits for the pass
  virtual void publish(GameTree& g) = 0;      // the device's rows into the tree, where it is behind
  virtual void upload(const GameTree& g) = 0;  // the tree's rows to the device
};

class Solver {
 public:
  explicit Solver(GameTree& g) : g_(g) {}

  DcfrParams params;
  int first_player = 0;  // set_first_iteration_player
  SubtreeDevice* subtrees = nullptr;  // a hybrid solve's device (#582), or none
  bool hybrid_reports = false;        // root_stats from the device's rows, not the tree's (the solve loop)
  // A hybrid iteration that times the device's passes and the CPU's turns one after the other (#582)
  bool hybrid_measure = false;
  double hybrid_device_seconds = 0, hybrid_cpu_seconds = 0;
  bool flop_ip_first = false;
  bool strict_opening = false;  // IP first on a rainbow flop with a forced check: bfloat16 regrets allowed (#591)
  bool uses_flop_ip_first() const { return flop_ip_first && !predictive; }  // float32 or 16-bit (#578)
  FlopOpening flop_opening() const { return opening_; }
  bool sqrt_regret_weights() const { return sqrt_weights_; }
  void set_flop_opening(FlopOpening opening) {
    opening_ = opening;
    if (opening != FlopOpening::SquareRoot) set_average_sampling(AverageSampling::Dense);
    update_flop_opening();
  }
  void update_flop_opening() {
    sqrt_weights_ = opening_ == FlopOpening::SquareRoot && uses_flop_ip_first() && g_.iterations < 8;
  }
  void cancel_flop_opening() {
    if (opening_ == FlopOpening::SquareRoot) set_flop_opening(FlopOpening::Edited);
  }
  void restart_flop_opening() {
    const bool previously_eligible = average_sampling_ != AverageSampling::Dense;
    const bool smart = g_.cfg.river_sizing == RiverSizing::Smart && strict_opening;
    const bool allow_fp16 = smart || fp16_sums_;
    const bool allow_bfloat = smart || bfloat_regrets_;
    set_average_sampling(AverageSampling::Dense);
    set_flop_opening(opening_ == FlopOpening::Edited ? FlopOpening::SquareRoot : opening_);
    if (std::any_of(g_.nodes.begin(), g_.nodes.end(), [](const GNode& n) { return n.flags != 0; })) return;
    if ((g_.cfg.river_sizing == RiverSizing::Smart || previously_eligible) && opening_ == FlopOpening::SquareRoot)
      prepare_sampled_averaging(allow_bfloat, allow_fp16);
    else if (sampled_smart_tree())
      prepare_sampled_averaging(allow_bfloat, allow_fp16);
  }
  void prepare_sampled_averaging(bool allow_bfloat = true, bool allow_fp16 = true) {
    constexpr uint64_t min_strategy_floats = (uint64_t{1} << 30) / sizeof(float);
    if (g_.storage_floats >= min_strategy_floats) {
      set_average_sampling(AverageSampling::AwaitingFirstGo);
      set_bfloat_regrets(allow_bfloat);
      set_fp16_sums(allow_fp16);
    }
  }
  bool is_bfloat_target(double target_chips) const {
    const double target = 0.003 * g_.cfg.start_pot();
    return target > 0 && target_chips >= std::nextafter(target, 0.) &&
           target_chips <= std::nextafter(target, std::numeric_limits<double>::infinity());
  }
  void select_sampled_averaging(double target_chips, bool metal = false) {
    if (average_sampling_ != AverageSampling::AwaitingFirstGo) return;
    const bool compact = bfloat_regrets_ && metal && is_bfloat_target(target_chips);
    const bool tight_target = target_chips > 0 && target_chips <= 0.003 * g_.cfg.start_pot();
    set_average_sampling(g_.iterations == 0 && (tight_target || compact) ? AverageSampling::Bounded : AverageSampling::Dense);
    set_bfloat_regrets(compact);
    late_pairs_ = uses_sampled_averaging() && is_bfloat_target(target_chips);
  }
  AverageStep strategy_average_step(int t) const {
    return average_step(t, params.gamma, avg_delay, avg_reset, uses_sampled_averaging(), uses_fp16_sums(t), late_pairs_);
  }
  bool late_pairs_ = false;  // a sampled solve to the default 0.3% of pot pairs its later iterations (#598)
  bool fp16_sums() const { return fp16_sums_; }
  bool uses_fp16_sums(int t) const { return fp16_sums_ && t <= 1024 && uses_bfloat_regrets(t); }
  void set_fp16_sums(bool enabled) {
    fp16_sums_ = enabled && bfloat_regrets_;
    if (fp16_sums_) {
      for (const auto& range : g_.init_reach)
        if (std::any_of(range.begin(), range.end(), [](float x) { return !(x >= 0.f && x <= 1.f); })) fp16_sums_ = false;
    }
  }
  bool bfloat_regrets() const { return bfloat_regrets_; }
  bool uses_bfloat_regrets(int t) const { return t > 8 && bfloat_regrets_ && uses_sampled_averaging(); }
  void set_bfloat_regrets(bool enabled) {
    bfloat_regrets_ = enabled && average_sampling_ != AverageSampling::Dense &&
                      g_.root_board_len() == 3 && g_.perm_all.size() <= 1;
    if (!bfloat_regrets_) fp16_sums_ = false;
  }
  AverageSampling average_sampling() const { return average_sampling_; }
  bool uses_sampled_averaging() const { return average_sampling_ == AverageSampling::Bounded; }
  // Sampled averaging (#556) on the app's IP-first rainbow flops with the square-root opening, where it
  // began, and on every other Smart tree (smart_rivers or single_river_size) with the ordinary opening:
  // HUNL 3BP to 0.3% of pot on 32 Zen 5 threads, 16-bit, 7.97 -> 7.12 s (#578). On the GPU, Medium
  // (smart_rivers) trees also take the bfloat16 regrets and fp16 sums of #559/#561 (#586).
  bool sampled_opening() const { return opening_ == FlopOpening::SquareRoot && uses_flop_ip_first(); }
  bool sampled_smart_tree() const {
    return opening_ == FlopOpening::Ordinary && g_.cfg.river_sizing != RiverSizing::Full;
  }
  void set_average_sampling(AverageSampling mode) {
    const bool eligible = (sampled_opening() || sampled_smart_tree()) &&
                          params == DcfrParams{} && avg_reset && avg_delay == 32 && !g_.payoff().icm.enabled;
    average_sampling_ = eligible ? mode : AverageSampling::Dense;
    if (average_sampling_ == AverageSampling::Dense) set_bfloat_regrets(false);
  }
  int whole_tree_first_player() const { return uses_flop_ip_first() ? 1 : first_player; }
  bool parallel = true;
  bool write_best_response = false;  // BestResponse walks store the pure BR strategy (set_mes)
  float br_regret_scale = 1.0f;      // ...with the best action's regret this large (set_strategy's, #423)
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
  // #300: average player p's regrets and strategy sums over each hand's orbit under the board's suit
  // symmetries (GameTree::perm_all), at the decisions on the root street.
  void symmetrize_root_street(int p, float* regrets = nullptr, float* strategy = nullptr);
  // Built on first use: the root-street decisions of each player, and each player's hand orbits of two
  // or more (flattened: sym_orbits_[p] holds the hands, sym_orbit_end_[p] where each orbit ends).
  bool sym_ready_ = false;
  std::vector<uint32_t> sym_nodes_[2];
  std::vector<uint16_t> sym_orbits_[2];
  std::vector<uint32_t> sym_orbit_end_[2];

  // Regret-based pruning (set_pruning, #51; Brown & Sandholm, "Regret-Based Pruning in
  // Extensive-Form Games", NIPS 2015). During a CFR walk only, an action of the traverser whose
  // CURRENT strategy is exactly zero for every one of the traverser's hands that can occur at the node
  // (hands holding a dealt turn or river card never get there and are ignored, #435) is pruned: its subtree
  // is not re-measured this iteration and its regret row is left completely untouched (frozen, not
  // discounted). This changes nothing about this iteration's strategy, cfv aggregate or
  // strategy-sum rows (the zero-strategy condition already guarantees "this action contributes 0
  // everywhere"); the skipped instantaneous regret measurement is the bounded approximation RBP
  // theory covers. Every prune_period-th iteration (default 8) is measured in full (the RBP
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
  // All-in terminals before the river from the precomputed outcome tables (AllinRunouts::e2) when
  // they exist; false evaluates every runout with the showdown kernel (the reference, for tests).
  bool allin_tables = true;
  // A compact byte table's sums on reaches held as whole numbers (reach_digits.hpp), where that is the
  // faster way. Tests set it either way and compare.
  bool allin_digits = PIO_ALLIN_DIGITS;
  // Values of p's hands at a showdown terminal against opponent reach ro (public for tests).
  void showdown_value(const GNode& n, int p, const float* ro, float* out) const;
  void traverser_update(const GNode& n, uint32_t ni, int p, const float* rp, const float* ro, const float* sigma,
                        const float* cfv, const uint8_t* pruned, const float* out);
  // #186 two-phase river walk (CFR mode): top-down reaches and strategies into buffers that live until
  // the river chance task returns, every showdown of the subtree evaluated in batches, then values and
  // updates bottom-up. Same operations in the same order as walk_t, so bit-identical.
  void river_cfr(uint32_t ni, int p, const float* rp, const float* ro, float* out);
  void river_down(uint32_t ni, int p, const float* rp, const float* ro, float* out);
  // The same for the exploitability check's fused walk (walk_evbr): on-policy values and best responses.
  void river_evbr(uint32_t ni, int p, const float* ro, float* ev, float* br);
  void river_evbr_down(uint32_t ni, int p, const float* ro, float* ev, float* br);
  template <class E>
  void allin_table_value(const AllinRunouts& ar, const E* e2, float W, float L, int p, const float* ro, float* out) const;
  template <class E>
  void allin_tri_value(const AllinRunouts& ar, const E* tri, float W, float L, int p, const float* ro, float* out) const;
  // #186: Solver::river_cfr for river subtrees in the CFR walk. AVX-512, AVX2 and NEON builds batch the
  // kernels; elsewhere the two-phase walk is overhead (M4 before the NEON kernel: +2-3.5%), so it is
  // off by default there.
  bool river_batch = PIO_SHOWDOWN_AVX512 != 0 || PIO_SHOWDOWN_NEON != 0 || PIO_SHOWDOWN_AVX2 != 0;
  // The default for a tree (Upi sets it when it creates the solver). AVX-512: always. NEON and AVX2: when the
  // action tree has at least 2.5 showdown terminals per river subtree (GameTree::river_showdowns_per_root),
  // since below that the batches are 1-2 jobs, the lanes kernel does not run, and only the two-phase
  // walk's overhead is left (M4, 10 threads: threebet_mono, 5.16 per river, -21%; flop_srp_2 1.40,
  // flop_srp_4 1.32, deep 1.17, flop_3bet 1.00, turn 1.83: +1.5-2.5% with it on).
  static bool river_batch_for(const GameTree& g) {
#if PIO_SHOWDOWN_AVX512
    (void)g;
    return true;
#elif PIO_SHOWDOWN_NEON || PIO_SHOWDOWN_AVX2
    return g.river_showdowns_per_root() >= 2.5;
#else
    (void)g;
    return false;
#endif
  }
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
  void iterate(int t) { iterate(t, first_player); }
  void iterate(int t, int first);

  // Generic traversal from node `ni` for player p. reach vectors are in the canonical frame of the
  // node, of length H[p] and H[1-p]. out has length H[p] (counterfactual values).
  void walk(Walk mode, uint32_t ni, int p, const float* rp, const float* ro, float* out);

  // Stats from the tree root.
  RootStats root_stats();
  RootStats root_stats_from_bfloat16(const uint16_t* committed_regrets, const uint16_t* committed_sums = nullptr) const;

  // Strategies at decision node (A * H[acting]): current (regret matching) / average.
  void current_strategy(uint32_t ni, float* out) const;
  void average_strategy(uint32_t ni, float* out) const;
  void apply_elimination(uint32_t ni, float* out) const;

  // Sum over opponent hands compatible with each of p's hands: out[h] = sum_o ro[o] * compat(h, o).
  void compat_mass(int p, const float* ro, float* out) const;
  // Evaluation walks: add node ni's incentives to p's per-action value rows (A x Hp) (#439).
  void add_incentives(uint32_t ni, int p, const float* ro, float* rows) const;
  // Pruning test for the traverser p's strategy row at decision ni: zero for every hand the board allows.
  bool prunable_row(uint32_t ni, int p, const float* row) const;
  // Total matchups at the root: sum_h r_p[h] * compat mass.
  double root_matchups() const;

 private:
  template <Walk M>
  void walk_t(uint32_t ni, int p, const float* rp, const float* ro, float* out);
  template <typename F>
  void run_actions(const GNode& n, int A, F& act);
  void strategy_only(uint32_t ni, int p, const float* rp);
  // A hybrid pass (#582): both players' reaches at every turn root of the flop's turn deals, from the
  // flop's current strategies (into the device's rows or the CPU's), then the CPU's turn subtrees,
  // walked while the device solves its own.
  void device_reach(uint32_t ni, const float* r0, const float* r1, bool average = false);
  bool hybrid_check_ = false;  // walk_evbr takes the turn roots' values from a hybrid report
  std::vector<float> cpu_ev_[2], cpu_br_[2];
  void hybrid_turns(int p);
  struct HybridRoot {
    uint32_t node;
    uint8_t card;
  };
  std::vector<HybridRoot> cpu_roots_;  // the turn roots the CPU walks in a hybrid solve
  std::unordered_map<uint32_t, int> cpu_root_index_;
  std::vector<float> cpu_root_reach_[2], cpu_root_values_;
  size_t cpu_root_stride_ = 0;
  const SubtreeDevice* cpu_roots_for_ = nullptr;  // the device cpu_roots_ were listed against
  void flop_deal(const GNode& n, const ChanceInfo& ci, Walk mode, int p, const float* rp, const float* ro, float* out);
  // Evaluate and BestResponse in one traversal (root_stats): ev = on-policy values, br = best
  // response values, both for player p against the opponent's average strategy.
  void walk_evbr(uint32_t ni, int p, const float* rp, const float* ro, float* ev, float* br);
  void fold_value(const GNode& n, int p, const float* ro, float* out) const;
  void showdown_board(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                      float* compat = nullptr, const SdFold* fold = nullptr) const;
  // River decision of the opponent facing p's bet: the call (showdown, reach xc = ro * sigma_call) and
  // fold (reach ro * sigma_fold) contributions in one kernel call, summed into out. False if the pair
  // does not qualify (both terminals at a 5-card board); then nothing is written.
  // xf: scratch for the fold reach (num_hands(opponent) floats).
  bool call_with_opp_fold(const GNode& sd, const GNode& fold, int p, const float* xc, const float* ro,
                          const float* sfold, float* xf, float* out) const;
  // River decision of the traverser facing a bet: its fold and call (showdown) children see the same
  // opponent reach, so one showdown kernel call gives both rows (the fold from the kernel's compatible
  // mass instead of a separate compat_mass pass). Returns false (nothing written) if the pair does not
  // qualify: both children must be terminals at a 5-card board.
  bool call_and_fold(const GNode& sd, const GNode& fold, int p, const float* ro, float* sd_out, float* fold_out) const;

  GameTree& g_;
  const uint16_t* report_bfloat16_regrets_ = nullptr;
  const uint16_t* report_fp16_sums_ = nullptr;
  bool bfloat_regrets_ = false, fp16_sums_ = false;
  float pos_mult_ = 1, neg_mult_ = 1, strat_mult_ = 1;
  bool accumulate_ = true;  // t > avg_delay: whether this iteration feeds the strategy sum
  FlopOpening opening_ = FlopOpening::Ordinary;
  AverageSampling average_sampling_ = AverageSampling::Dense;
  bool sqrt_weights_ = false;
  bool prune_this_iter_ = false;  // set by iterate/iterate_subtree; read-only during walks
};

}  // namespace pio
