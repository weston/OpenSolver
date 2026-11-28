// The postflop solve on a discrete GPU, OpenCL or CUDA (gpu_backend.hpp): the same compiled traversal
// as metal.mm, from the plan in gpu_pack.hpp and the kernels in postflop.cl.
//
// Unlike Metal's shared memory, the GPU's own memory holds the regrets and strategy sums. They are
// updated there in place, and the tree gets them only when the CPU reads it (publish_for_cpu): for an
// exploitability report, a command between iterations (TreePause), a fallback to the CPU, the end.
#include "gpu.hpp"

#if !(defined(__APPLE__) && defined(__aarch64__))

#include <algorithm>
#include <atomic>
#include <bit>
#include <cmath>
#include <cstring>
#include <unordered_map>

#include "bfloat_regrets.hpp"
#include "fp16_sums.hpp"
#include "gpu_backend.hpp"
#include "gpu_pack.hpp"
#include "state_transfer.hpp"

namespace pio {
namespace gpu {

const char opencl_kernel_source[] =
#include "../build/opencl_kernels.inc"
    ;
const char cuda_kernel_source[] =
#include "../build/cuda_kernels.inc"
    ;

Backend* backend(std::string* error) {
  static std::string why;
  static std::unique_ptr<Backend> chosen = [] {
    const char* env = std::getenv("WSOLVER_GPU_BACKEND");
    const std::string want = env ? env : "";
    std::unique_ptr<Backend> b;
    std::string reasons;
    if (want.empty() || want == "cuda") {
      std::string e;
      b = make_cuda_backend(e);
      if (!b) reasons = "CUDA: " + e;
    }
    if (!b && (want.empty() || want == "opencl")) {
      std::string e;
      b = make_opencl_backend(e);
      if (!b) reasons += (reasons.empty() ? "" : "; ") + std::string("OpenCL: ") + e;
    }
    if (!b) why = reasons.empty() ? "no GPU backend " + want : reasons;
    return b;
  }();
  if (error) *error = why;
  return chosen.get();
}

namespace {

enum KernelId {
  kPropagate, kUpdate, kTerminal, kCallFold, kAllin, kBetCheck, kTwoBets, kTwoBetsWide,
  kRounds, kRoundsWide, kLeadCheck, kLeadCheckWide, kKernelCount
};
const char* const kKernelNames[kKernelCount] = {
    "propagate_reach",   "update_values",       "terminal",       "river_call_fold",
    "allin",             "river_bet_check",     "river_two_bets", "river_two_bets_wide",
    "river_betting_round", "river_betting_round_wide", "river_lead_check", "river_lead_check_wide"};

// A device allocation, released with its owner.
struct DeviceBuffer {
  Backend* b = nullptr;
  Buf buf = 0;
  size_t bytes = 0;
  DeviceBuffer() = default;
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  ~DeviceBuffer() { reset(); }
  void reset() {
    if (buf) b->release(buf);
    buf = 0;
    bytes = 0;
  }
  void create(Backend& backend, size_t n) {
    reset();
    b = &backend;
    bytes = std::max<size_t>(n, 16);
    if (bytes > b->max_alloc) throw std::runtime_error("GPU buffer exceeds the device's allocation limit");
    buf = b->alloc(bytes);
  }
  template <class V>
  void upload(Backend& backend, const V& x) {
    const size_t n = x.size() * sizeof(x[0]);
    create(backend, n);
    if (n) b->write(buf, 0, x.data(), n, false);
  }
};

class Solve final : public MetalSolve {
 public:
  Backend& b;
  PostflopPlan plan;
  void* kernels[kVariants][kKernelCount] = {};
  size_t limits[kKernelCount] = {};
  DeviceBuffer nodes, ids, r[2], v, cards, iso, perms[2], boards[2], indices[2], heroes[2], tables, counts[2];
  DeviceBuffer rounds, bet_checks, two_bets, lead_checks, batches, regrets, sums;
  size_t state_floats = 0, width = 128;
  // The device's state format, and where the accepted iterations are: the tree is current, or not
  // (the device has iterations the tree lacks); with a prepared iteration on the device, staging holds
  // the accepted bfloat16 / fp16 words that evaluate_and_prepare measured.
  bool compact_regrets = false, compact_sums = false, allocated = false;
  bool tree_regrets_current = true, tree_sums_current = true;
  bool staged_regrets = false, staged_sums = false;
  bool device_behind = true;  // the tree is newer: uploaded before the next iteration
  bool first_submission = true, early_replay = false, write_sums = true;
  int prepared_iteration = 0;
  std::vector<uint16_t> regret_words, sum_words;
  // Each player's root-street rows (float offset, count) and room for them, for the symmetrization.
  std::vector<std::pair<size_t, size_t>> root_rows[2];
  ZVec<float> staging_regrets, staging_sums;

  // roots: the whole tree, or a hybrid solve's turn subtrees (gpu_pack.hpp, #582)
  Solve(Backend& backend, GameTree& g, uint64_t available,
        const std::vector<std::pair<unsigned, unsigned>>* roots = nullptr, const DeviceLayout* layout = nullptr)
      : b(backend) {
    PostflopPlan::check_tree(g);
    state_floats = layout ? layout->floats : g.storage_floats;
    if (2 * g.nodes.size() * sizeof(TraversalNode) > available) throw std::runtime_error("GPU workspace exceeds available memory");
    std::lock_guard<std::mutex> lock(b.mu);
    size_t river_limit = SIZE_MAX;
    for (int k = 0; k < kKernelCount; ++k) {
      kernels[kFloatState][k] = b.kernel(kFloatState, kKernelNames[k]);
      limits[k] = b.local_limit(kernels[kFloatState][k]);
      if (b.max_width(kernels[kFloatState][k]) < 128) throw std::runtime_error("GPU work-groups are too small");
      if (k >= kBetCheck) river_limit = std::min(river_limit, limits[k]);
    }
    {
      ProfileScope ps(b, "setup: pack nodes");
      plan.pack_nodes(g, river_limit, roots);
      if (layout) plan.remap_storage(*layout);
    }
    width = plan.stride >= 256 ? 256 : 128;
    {
      ProfileScope ps(b, "setup: pack hands");
      plan.pack_hands(g);
      plan.pack_allins(g);
    }
    for (unsigned p = 0; p < 2; ++p) {  // what each pass needs fits
      const size_t O = g.num_hands(1 - p), channel = plan.round_bytes[p], prefix = channel - O * sizeof(float);
      if (plan.terminal_range.count && std::max(prefix, (O + 53) * sizeof(float)) > limits[kTerminal])
        throw std::runtime_error("range too wide for GPU work-group memory");
      if (plan.pair_range.count && prefix + (2 * O + 53) * sizeof(float) > limits[kCallFold])
        throw std::runtime_error("range too wide for GPU work-group memory");
      if (std::max({plan.rounds.size(), plan.lead_checks.size()}) && channel > limits[kRoundsWide])
        throw std::runtime_error("range too wide for GPU work-group memory");
      if (std::max({plan.bet_checks.size(), plan.two_bets.size()}) && 2 * channel > limits[kBetCheck])
        throw std::runtime_error("range too wide for GPU work-group memory");
    }
    const uint64_t state_bytes = uint64_t(state_floats) * sizeof(float);
    const uint64_t need = plan.workspace_bytes(g) + 2 * state_bytes;
    // A hybrid device takes less of the GPU: where its share needs more, a smaller share fits (#582).
    if (need > (roots ? b.memory / 100 * 80 : b.memory / 10 * 9)) throw std::runtime_error("GPU memory too small for this tree");
    if (plan.workspace_bytes(g) / 2 + state_bytes > available) throw std::runtime_error("GPU workspace exceeds available memory");
    {
      ProfileScope ps(b, "setup: upload");
      upload_plan(g);
    }
    if (profile().on)
      std::fprintf(stderr,
                   "GPU profile: %s, hands %d/%d, stride %zu, state %llu MB, %zu levels; terminals %zu, river call/fold "
                   "%zu, rounds %zu, bet/check %zu, two bets %zu, lead/check %zu, all-in batches %zu; value rows %u\n",
                   b.api(), g.num_hands(0), g.num_hands(1), plan.stride, (unsigned long long)(state_bytes >> 20),
                   plan.levels.size(), plan.terminal_range.count, plan.pair_range.count, plan.rounds.size(),
                   plan.bet_checks.size(), plan.two_bets.size(), plan.lead_checks.size(), plan.allin_batches.size(),
                   plan.value_rows);
    if (g.perm_all.size() > 1) {
      const int root_street = g.nodes[0].board_len;
      for (const auto& n : g.nodes)
        if (n.kind == ANodeKind::Decision && n.board_len == root_street) {
          auto& rows = root_rows[n.player];
          const size_t count = size_t(n.num_children) * g.num_hands(n.player);
          if (!rows.empty() && rows.back().first + rows.back().second == n.storage)
            rows.back().second += count;
          else
            rows.push_back({n.storage, count});
        }
      // only the root street's pages are ever touched
      staging_regrets.resize(g.regrets.size());
      staging_sums.resize(g.strat.size());
    }
  }

  ~Solve() override {
    std::lock_guard<std::mutex> lock(b.mu);
    b.finish();
  }

  void upload_plan(const GameTree& g) {
    nodes.upload(b, plan.nodes);
    ids.upload(b, plan.list);
    iso.upload(b, plan.iso);
    cards.upload(b, plan.cards);
    rounds.upload(b, plan.rounds);
    bet_checks.upload(b, plan.bet_checks);
    two_bets.upload(b, plan.two_bets);
    lead_checks.upload(b, plan.lead_checks);
    batches.upload(b, plan.allin_batches);
    tables.upload(b, plan.tables);
    for (int p = 0; p < 2; ++p) {
      perms[p].upload(b, plan.perms[p]);
      boards[p].upload(b, plan.boards[p]);
      indices[p].upload(b, plan.indices[p]);
      heroes[p].upload(b, plan.heroes[p]);
      counts[p].upload(b, plan.counts[p]);
      r[p].create(b, size_t(plan.reach_floats[p]) * sizeof(float));
      b.zero(r[p].buf, r[p].bytes);
      b.write(r[p].buf, 0, g.init_reach[p].data(), g.num_hands(p) * sizeof(float), false);
    }
    v.create(b, size_t(plan.value_rows) * plan.stride * sizeof(float));
    b.zero(v.buf, v.bytes);
    b.finish();
    // the device has its copies now
    for (int p = 0; p < 2; ++p) {
      std::vector<int16_t>().swap(plan.counts[p]);
      std::vector<HandPrefixes>().swap(plan.heroes[p]);
      std::vector<PrefixEntry>().swap(plan.indices[p]);
      std::vector<BoardPrefixes>().swap(plan.boards[p]);
    }
  }

  // ----- the state: device <-> tree ----------------------------------------------------------------
  void allocate_state() {
    regrets.create(b, state_floats * (compact_regrets ? sizeof(uint16_t) : sizeof(float)));
    sums.create(b, state_floats * (compact_sums ? sizeof(uint16_t) : sizeof(float)));
    allocated = true;
  }

  // The tree is current; the device gets it.
  void upload_state(const GameTree& g) {
    ProfileScope ps(b, "copy to the GPU");
    if (compact_regrets) {
      regret_words.resize(state_floats);
      transfer_state(g.regrets.data(), regret_words.data(), state_floats,
                     [](float x) { return bfloat_regret_word(std::bit_cast<uint32_t>(x)); });
      b.write(regrets.buf, 0, regret_words.data(), state_floats * sizeof(uint16_t), true);
    } else {
      b.write(regrets.buf, 0, g.regrets.data(), state_floats * sizeof(float), false);
    }
    if (compact_sums) {
      sum_words.resize(state_floats);
      transfer_state(g.strat.data(), sum_words.data(), state_floats, [](float x) { return fp16_sum_word(x); });
      b.write(sums.buf, 0, sum_words.data(), state_floats * sizeof(uint16_t), true);
    } else {
      b.write(sums.buf, 0, g.strat.data(), state_floats * sizeof(float), false);
    }
    staged_regrets = staged_sums = false;
    device_behind = false;
  }

  // The accepted state's words into staging (without a prepared iteration, the device holds it).
  void stage_regrets() {
    if (staged_regrets) return;
    regret_words.resize(state_floats);
    b.read(regrets.buf, 0, regret_words.data(), state_floats * sizeof(uint16_t), true);
    staged_regrets = true;
  }
  void stage_sums() {
    if (staged_sums) return;
    sum_words.resize(state_floats);
    b.read(sums.buf, 0, sum_words.data(), state_floats * sizeof(uint16_t), true);
    staged_sums = true;
  }

  void publish(GameTree& g) {
    if (tree_regrets_current && tree_sums_current) return;
    ProfileScope ps(b, "copy to the tree");
    if (!tree_regrets_current) {
      if (compact_regrets) {
        stage_regrets();
        transfer_state(regret_words.data(), g.regrets.data(), state_floats,
                       [](uint16_t w) { return std::bit_cast<float>(bfloat_regret_bits(w)); });
      } else {
        b.read(regrets.buf, 0, g.regrets.data(), state_floats * sizeof(float), false);
      }
    }
    if (!tree_sums_current) {
      if (compact_sums) {
        stage_sums();
        transfer_state(sum_words.data(), g.strat.data(), state_floats, [](uint16_t w) { return fp16_sum_value(w); });
      } else {
        b.read(sums.buf, 0, g.strat.data(), state_floats * sizeof(float), false);
      }
    }
    b.finish();
    tree_regrets_current = tree_sums_current = true;
  }

  void publish_for_cpu(GameTree& g) override {
    std::lock_guard<std::mutex> lock(b.mu);
    publish(g);
  }

  // ----- an iteration -------------------------------------------------------------------------------
  void dispatch(Variant variant, KernelId id, Range span, std::vector<Arg>& args, size_t scratch = 0,
                size_t groups_y = 1, size_t group_width = 0) {
    if (!span.count) return;
    void*& k = kernels[variant][id];
    if (!k) k = b.kernel(variant, kKernelNames[id]);
    args[9] = value(uint32_t(span.start));
    b.launch(k, kKernelNames[id], span.count, groups_y, group_width ? group_width : width, scratch, args);
  }

  // One player's update: reach down the tree, terminal values, then values and regrets back up.
  // strategy: the rows the strategies are read from, the regrets unless given (a hybrid report: the sums)
  void enqueue_phase(const GameTree& g, unsigned p, bool refresh_reach, const IterationParams& params, Buf strategy = 0) {
    const Variant variant = compact_sums ? (compact_regrets ? kBfloatRegretsHalfSums : kFloatRegretsHalfSums)
                                         : (compact_regrets ? kBfloatRegrets : kFloatState);
    std::vector<Arg> args = {arg(nodes.buf), arg(ids.buf), value(params), arg(r[0].buf), arg(r[1].buf), arg(v.buf),
                             arg(strategy ? strategy : regrets.buf), arg(sums.buf), arg(cards.buf), value(uint32_t(0))};
    for (const auto& reach : plan.reach_levels) {
      Range span = reach[1 - p];
      if (refresh_reach) span = {reach[0].start, reach[0].count + reach[1].count};
      dispatch(variant, kPropagate, span, args);
    }
    const size_t O = g.num_hands(1 - p), channel = plan.round_bytes[p], prefix = channel - O * sizeof(float);
    args.resize(10);
    args.push_back(arg(boards[p].buf));
    args.push_back(arg(indices[p].buf));
    args.push_back(arg(heroes[p].buf));
    dispatch(variant, kTerminal, plan.terminal_range, args, std::max(prefix, (O + 53) * sizeof(float)));
    dispatch(variant, kCallFold, plan.pair_range, args, prefix + (2 * O + 53) * sizeof(float));
    auto records = [&](const DeviceBuffer& buffer, size_t count, KernelId wide, KernelId narrow, size_t wide_bytes,
                       size_t narrow_bytes) {
      args.resize(13);
      args.push_back(arg(buffer.buf));
      if (wide_bytes <= limits[wide]) dispatch(variant, wide, {0, count}, args, wide_bytes);
      else dispatch(variant, narrow, {0, count}, args, narrow_bytes);
    };
    records(rounds, plan.rounds.size(), kRounds, kRoundsWide, 3 * channel, channel);
    records(bet_checks, plan.bet_checks.size(), kBetCheck, kBetCheck, 2 * channel, 2 * channel);
    records(two_bets, plan.two_bets.size(), kTwoBets, kTwoBetsWide, 4 * channel, 2 * channel);
    records(lead_checks, plan.lead_checks.size(), kLeadCheck, kLeadCheckWide, 3 * channel, channel);
    args.resize(10);
    args.push_back(arg(tables.buf));
    args.push_back(arg(counts[p].buf));
    args.push_back(arg(batches.buf));
    dispatch(variant, kAllin, {0, plan.allin_batches.size()}, args, 0, g.num_hands(p), 64);
    args.resize(10);
    args.push_back(arg(iso.buf));
    args.push_back(arg(perms[p].buf));
    for (auto it = plan.levels.rbegin(); it != plan.levels.rend(); ++it) dispatch(variant, kUpdate, *it, args);
  }

  // Suit symmetrization of p's root street (#300): only those rows come down, are averaged over their
  // orbits on the CPU, and go back up. (Compact state is never symmetrized: Solver::set_bfloat_regrets.)
  void symmetrize(Solver& s, unsigned p) {
    for (auto [offset, count] : root_rows[p]) {
      b.read(regrets.buf, offset * sizeof(float), staging_regrets.data() + offset, count * sizeof(float), false);
      b.read(sums.buf, offset * sizeof(float), staging_sums.data() + offset, count * sizeof(float), false);
    }
    b.finish();
    s.symmetrize_root_street(int(p), staging_regrets.data(), staging_sums.data());
    for (auto [offset, count] : root_rows[p]) {
      b.write(regrets.buf, offset * sizeof(float), staging_regrets.data() + offset, count * sizeof(float), false);
      b.write(sums.buf, offset * sizeof(float), staging_sums.data() + offset, count * sizeof(float), false);
    }
  }

  // Iteration t's arguments for player p's pass (the phase-th of the iteration).
  IterationParams params_for(const GameTree& g, const Solver& s, int t, unsigned phase, unsigned p) const {
    const auto average = s.strategy_average_step(t);
    const AverageMode mode = average.weight == 1.f ? DenseAverage : average.weight == 0.f ? SkipAverage : WeightedAverage;
    auto disc = [](double z) { return float(z / (1 + z)); };
    return {unsigned(g.num_hands(0)),
            unsigned(g.num_hands(1)),
            unsigned(plan.stride),
            p,
            disc(std::pow(t, s.params.alpha)),
            disc(std::pow(t, s.params.beta_at(t))),
            average.reset ? 0.f : average.discount,
            unsigned(s.avg_reset || t > s.avg_delay),
            phase,
            average.weight,
            unsigned(s.sqrt_regret_weights()),
            unsigned(mode),
            0u};
  }

  // Queue iteration t, both players, on the device's state.
  void submit(GameTree& g, Solver& s, int t, bool refresh_reach) {
    refresh_reach |= first_submission;
    const bool compact = s.uses_bfloat_regrets(t), half_sums = s.uses_fp16_sums(t);
    if (!allocated || compact != compact_regrets || half_sums != compact_sums) {
      if (allocated) publish(g);
      regrets.reset();
      sums.reset();
      compact_regrets = compact;
      compact_sums = half_sums;
      allocate_state();
      device_behind = true;
      refresh_reach = true;
    }
    if (device_behind) upload_state(g);
    const bool early = s.sqrt_regret_weights();
    const bool policy_changed = early != early_replay;
    early_replay = early;
    const auto average = s.strategy_average_step(t);
    const AverageMode mode = average.weight == 1.f ? DenseAverage : average.weight == 0.f ? SkipAverage : WeightedAverage;
    write_sums = mode != SkipAverage;
    first_submission = false;
    auto disc = [](double z) { return float(z / (1 + z)); };
    const bool symmetric = t % 4 == 0 && g.perm_all.size() > 1;
    for (unsigned phase = 0; phase < 2; ++phase) {
      const unsigned p = phase ^ s.whole_tree_first_player();
      IterationParams args = {unsigned(g.num_hands(0)),
                              unsigned(g.num_hands(1)),
                              unsigned(plan.stride),
                              p,
                              disc(std::pow(t, s.params.alpha)),
                              disc(std::pow(t, s.params.beta_at(t))),
                              average.reset ? 0.f : average.discount,
                              unsigned(s.avg_reset || t > s.avg_delay),
                              phase,
                              average.weight,
                              unsigned(early),
                              unsigned(mode),
                              0u};
      enqueue_phase(g, p, phase == 0 && (refresh_reach || policy_changed), args);
      // the first player's root street is averaged before the second player's walk reads it, as on
      // the CPU and Metal
      if (phase == 0 && symmetric) symmetrize(s, p);
    }
    b.flush();
  }

  void finish_iteration(GameTree& g, Solver& s, int t) {
    b.finish();
    if (t % 4 == 0 && g.perm_all.size() > 1) symmetrize(s, 1 - s.whole_tree_first_player());
  }

  RootStats evaluate_and_prepare(GameTree& g, Solver& s, int next) override {
    std::lock_guard<std::mutex> lock(b.mu);
    // The CPU measures the accepted iterations while the GPU already runs the next one: from the tree,
    // or, when the state stays compact, from its words (half the bytes to copy, nothing to convert).
    const uint16_t* committed = nullptr;
    const uint16_t* committed_sums = nullptr;
    if (allocated && compact_regrets && s.uses_bfloat_regrets(next) && compact_sums == s.uses_fp16_sums(next) &&
        !tree_regrets_current) {
      ProfileScope ps(b, "copy to the host");
      stage_regrets();
      committed = regret_words.data();
      if (compact_sums && !tree_sums_current) {
        stage_sums();
        committed_sums = sum_words.data();
      } else if (!tree_sums_current) {
        b.read(sums.buf, 0, g.strat.data(), state_floats * sizeof(float), true);
        tree_sums_current = true;
      }
    } else {
      publish(g);
    }
    submit(g, s, next, false);
    prepared_iteration = next;  // on the device only: the tree (or staging) keeps the measured state
    RootStats stats;
    try {
      stats = committed ? s.root_stats_from_packed(committed, committed_sums) : s.root_stats();
    } catch (...) {
      b.finish();
      throw;
    }
    finish_iteration(g, s, next);
    return stats;
  }

  void iterate(GameTree& g, Solver& s, int t, bool refresh_reach) override {
    std::lock_guard<std::mutex> lock(b.mu);
    if (refresh_reach || prepared_iteration != t) {
      // A command may have changed the tree (a pause brought it up to date first), or the device holds a
      // prepared iteration that is not taken: start again from the accepted state.
      if (refresh_reach || prepared_iteration) {
        publish(g);
        device_behind = true;
      }
      prepared_iteration = 0;
      submit(g, s, t, refresh_reach);
      finish_iteration(g, s, t);
    }
    prepared_iteration = 0;
    tree_regrets_current = false;
    if (write_sums) tree_sums_current = false;
    staged_regrets = staged_sums = false;
  }
};

}  // namespace
}  // namespace gpu

bool gpu_available() { return gpu::backend() != nullptr; }

std::string gpu_name() {
  gpu::Backend* b = gpu::backend();
  return b ? b->device_name + " (" + b->api() + ")" : "";
}

std::string gpu_unavailable_reason() {
  std::string error;
  gpu::backend(&error);
  return error;
}

// Not by default on a discrete GPU: on the one measured (an RX 9060 XT against a 16-core Zen 5) the CPU
// was faster on most trees (README "GPU solving"). Choosing it also costs nothing until it is used.
bool gpu_preferred() { return false; }

uint64_t gpu_memory_bytes() {
  gpu::Backend* b = gpu::backend();
  return b ? b->memory : 0;
}

std::unique_ptr<MetalSolve> make_gpu_solve(GameTree& tree, uint64_t available) {
  std::string error;
  gpu::Backend* b = gpu::backend(&error);
  if (!b) throw std::runtime_error(error);
  return std::make_unique<gpu::Solve>(*b, tree, available);
}

namespace gpu {
namespace {
// A hybrid solve's device (#582): the turn subtrees of a share of the turn cards, solved on the GPU in
// every pass while the CPU walks the others. Its state is the tree's float32 rows of those subtrees
// (each one range of storage: the tree is laid out depth first), uploaded at the start and read back
// when the CPU reads the tree.
class Subtrees final : public SubtreeDevice {
 public:
  Subtrees(Backend& backend, GameTree& tree, uint64_t available, double share) : b(backend), g(tree) {
    for (uint32_t i = 0; i < g.nodes.size(); ++i) {
      const auto& n = g.nodes[i];
      if (n.kind != ANodeKind::Chance || n.board_len != 3 || g.chances[n.aux].flop_deal) continue;
      const auto& ci = g.chances[n.aux];
      const int K = n.num_children;
      for (int k = 0; k < K; ++k) {
        // an even spread of the cards: k is the device's when floor((k + 1) * share) passes an integer
        if (std::floor((k + 1) * share) == std::floor(k * share)) continue;
        const uint32_t child = n.first_child + uint32_t(k);
        if (g.nodes[child].kind != ANodeKind::Decision) continue;
        index[child] = int(roots.size());
        roots.push_back({child, ci.cards[k]});
      }
    }
    if (roots.empty()) throw std::runtime_error("no turn subtrees for the GPU");
    // Each subtree's rows are one range of storage (the tree is filled depth first); its decisions are
    // what a 16-bit tree converts.
    subtrees.resize(roots.size());
    size_t row = 0;
    for (size_t j = 0; j < roots.size(); ++j) {
      auto& st = subtrees[j];
      size_t lo = SIZE_MAX, hi = 0;
      std::vector<uint32_t> stack{roots[j].first};
      while (!stack.empty()) {
        const uint32_t i = stack.back();
        const auto& n = g.nodes[i];
        stack.pop_back();
        if (n.kind == ANodeKind::Decision && !GameTree::shares_row(n)) {
          lo = std::min<size_t>(lo, n.storage);
          hi = std::max<size_t>(hi, n.storage + size_t(n.num_children) * g.num_hands(n.player));
          st.decisions.push_back(i);
        }
        if (n.kind == ANodeKind::Decision) row = std::max<size_t>(row, g.num_hands(n.player));
        if (n.kind == ANodeKind::Decision || n.kind == ANodeKind::Chance)
          for (uint32_t c = 0; c < n.num_children; ++c) stack.push_back(n.first_child + c);
      }
      st.tree = lo < hi ? lo : 0;
      st.floats = lo < hi ? hi - lo : 0;
    }
    std::vector<size_t> order(roots.size());
    for (size_t j = 0; j < order.size(); ++j) order[j] = j;
    std::sort(order.begin(), order.end(), [&](size_t a, size_t c) { return subtrees[a].tree < subtrees[c].tree; });
    for (size_t j : order) {
      auto& st = subtrees[j];
      if (!st.floats) continue;
      if (!layout.ranges.empty() && layout.ranges.back().tree + layout.ranges.back().floats > st.tree)
        throw std::runtime_error("turn subtrees overlap in storage");
      st.device = layout.floats;
      layout.ranges.push_back({st.tree, st.device, st.floats});
      layout.floats += st.floats;
    }
    layout.zero = layout.floats;
    layout.floats += std::max<size_t>(row, 1) * 8;  // zeros for any row outside them
    list_chunks(order);
    s = std::make_unique<Solve>(b, g, available, &roots, &layout);
    {
      std::lock_guard<std::mutex> lock(b.mu);
      s->compact_regrets = s->compact_sums = false;
      s->allocate_state();
      b.zero(s->regrets.buf, s->regrets.bytes);
      b.zero(s->sums.buf, s->sums.bytes);
    }
    for (int p = 0; p < 2; ++p) {
      reach_stride[p] = (g.num_hands(p) + 31) & ~31u;
      host_reach[p].assign(size_t(reach_stride[p]) * roots.size(), 0.f);
    }
    host_values.assign(s->plan.stride * roots.size(), 0.f);
    upload(g);
    if (profile().on)
      std::fprintf(stderr, "GPU profile: hybrid, %zu turn subtrees, %.1f%% of the rows, %s tree\n", roots.size(),
                   100.0 * layout.floats / std::max<size_t>(1, g.storage_floats), g.compressed ? "16-bit" : "32-bit");
  }
  ~Subtrees() override { finish(); }

  int root(uint32_t node) const override {
    auto it = index.find(node);
    return it == index.end() ? -1 : it->second;
  }
  float* reach(int player, int j) override { return host_reach[player].data() + size_t(j) * reach_stride[player]; }

  void launch(Solver& solver, int t, int phase, int p) override {
    std::lock_guard<std::mutex> lock(b.mu);
    for (int q = 0; q < 2; ++q)
      b.write(s->r[q].buf, 0, host_reach[q].data(), host_reach[q].size() * sizeof(float), false);
    // p's own reach below the roots is the last pass's (p's strategy has not changed since), as in a whole-tree
    // solve; both after the device was set up, filled, or used for a report's average-strategy reaches
    s->enqueue_phase(g, unsigned(p), refresh_reach, s->params_for(g, solver, t, unsigned(phase), unsigned(p)));
    refresh_reach = false;
    b.read(s->v.buf, 0, host_values.data(), host_values.size() * sizeof(float), false);
    b.flush();
    pending = true;
    device_newer = true;
    device_zero = false;
  }
  const float* values(int j) override {
    finish();
    return host_values.data() + size_t(j) * s->plan.stride;
  }
  void launch_check() override {
    std::lock_guard<std::mutex> lock(b.mu);
    for (int q = 0; q < 2; ++q)
      b.write(s->r[q].buf, 0, host_reach[q].data(), host_reach[q].size() * sizeof(float), false);
    for (unsigned p = 0; p < 2; ++p)
      for (unsigned mode = 1; mode <= 2; ++mode) {
        IterationParams q{};
        q.hands0 = unsigned(g.num_hands(0));
        q.hands1 = unsigned(g.num_hands(1));
        q.stride = unsigned(s->plan.stride);
        q.p = p;
        q.positive_discount = q.negative_discount = q.average_discount = q.sample_weight = 1.f;
        q.average_mode = SkipAverage;
        q.mode = mode;
        s->enqueue_phase(g, p, true, q, s->sums.buf);
        refresh_reach = true;
        auto& out = check[p][mode - 1];
        out.resize(host_values.size());
        b.read(s->v.buf, 0, out.data(), out.size() * sizeof(float), false);
      }
    b.flush();
    pending = true;
  }
  const float* check_values(int p, bool best_response, int j) override {
    finish();
    return check[p][best_response].data() + size_t(j) * s->plan.stride;
  }
  void finish() override {
    std::lock_guard<std::mutex> wait(wait_mu);
    if (!pending) return;
    std::lock_guard<std::mutex> lock(b.mu);
    b.finish();
    pending = false;
  }

  void publish(GameTree& tree) override {
    finish();
    if (!device_newer) return;
    std::lock_guard<std::mutex> lock(b.mu);
    ProfileScope ps(b, "hybrid: copy to the tree");
    if (!tree.compressed) {
      for (const auto& st : subtrees) {
        if (!st.floats) continue;
        b.read(s->regrets.buf, st.device * sizeof(float), tree.regrets.data() + st.tree, st.floats * sizeof(float), false);
        b.read(s->sums.buf, st.device * sizeof(float), tree.strat.data() + st.tree, st.floats * sizeof(float), false);
      }
      b.finish();
    } else {
      // 16-bit rows: a chunk of rows at a time, the next one read while this one's nodes are stored as the
      // CPU's walks store them
      staging(2);
      auto read = [&](size_t c) {
        const Chunk& k = chunks[c];
        b.read(s->regrets.buf, k.device * sizeof(float), stage[c & 1][0].data(), k.floats * sizeof(float), false);
        b.read(s->sums.buf, k.device * sizeof(float), stage[c & 1][1].data(), k.floats * sizeof(float), false);
      };
      if (!chunks.empty()) read(0);
      for (size_t c = 0; c < chunks.size(); ++c) {
        b.finish();
        if (c + 1 < chunks.size()) {
          read(c + 1);
          b.flush();
        }
        const float* rg = stage[c & 1][0].data();
        const float* sm = stage[c & 1][1].data();
        for_nodes(chunks[c], [&](const GNode& n, size_t at) {
          tree.regrets_commit(n, rg + at);
          tree.strat_commit(n, sm + at);
        });
      }
    }
    device_newer = false;
  }

  void upload(const GameTree& tree) override {
    finish();
    std::lock_guard<std::mutex> lock(b.mu);
    ProfileScope ps(b, "hybrid: copy to the GPU");
    refresh_reach = true;
    if (!tree.compressed) {
      for (const auto& st : subtrees) {
        if (!st.floats) continue;
        if (device_zero && all_zero(tree.regrets.data() + st.tree, st.floats) &&
            all_zero(tree.strat.data() + st.tree, st.floats))
          continue;  // a fresh tree: the device's zeros already
        b.write(s->regrets.buf, st.device * sizeof(float), tree.regrets.data() + st.tree, st.floats * sizeof(float), false);
        b.write(s->sums.buf, st.device * sizeof(float), tree.strat.data() + st.tree, st.floats * sizeof(float), false);
      }
      b.finish();
    } else {
      // 16-bit rows: a chunk at a time, decoded in parallel, then written
      staging(1);
      float* rg = stage[0][0].data();
      float* sm = stage[0][1].data();
      for (const Chunk& k : chunks) {
        if (device_zero) {  // a fresh tree's rows are zeros, which the device has already
          std::atomic<bool> rows{false};
          for_nodes(k, [&](const GNode& n, size_t) {
            if (rows.load(std::memory_order_relaxed)) return;
            const size_t count = tree.stored_size(n);
            const int16_t* q = tree.regrets_q.data() + tree.qo(n);
            const uint16_t* sq = tree.strat_q.data() + tree.qo(n);
            for (size_t i = 0; i < count; ++i)
              if (q[i] | sq[i]) {
                rows = true;
                return;
              }
          });
          if (!rows) continue;
        }
        for_nodes(k, [&](const GNode& n, size_t at) {
          const size_t count = size_t(n.num_children) * tree.num_hands(n.player);
          const float* r = tree.regrets_ro(n, rg + at);
          if (r != rg + at) std::memcpy(rg + at, r, count * sizeof(float));
          const float* m = tree.strat_ro(n, sm + at);
          if (m != sm + at) std::memcpy(sm + at, m, count * sizeof(float));
        });
        b.write(s->regrets.buf, k.device * sizeof(float), rg, k.floats * sizeof(float), false);
        b.write(s->sums.buf, k.device * sizeof(float), sm, k.floats * sizeof(float), false);
        b.finish();  // the staging is reused by the next chunk
      }
    }
    device_newer = false;
  }

 private:
  struct Subtree {
    size_t tree = 0, device = 0, floats = 0;  // its rows: in the tree's storage, on the device
    std::vector<uint32_t> decisions;           // its decisions with rows of their own
  };
  // A 16-bit tree's rows go between the device and the tree a chunk at a time (consecutive subtrees, in
  // device order): the staging is a fraction of the rows, and each chunk is one copy each way.
  struct Chunk {
    size_t device = 0, floats = 0;
    std::vector<std::pair<uint32_t, size_t>> nodes;  // a decision, and where its rows start in the chunk
  };
  static constexpr size_t kChunkFloats = size_t(16) << 20;
  std::vector<Chunk> chunks;
  std::vector<float> stage[2][2];  // [slot][regrets, sums]
  void list_chunks(const std::vector<size_t>& order) {
    for (size_t j : order) {
      const Subtree& st = subtrees[j];
      if (!st.floats) continue;
      if (chunks.empty() || chunks.back().floats + st.floats > kChunkFloats ||
          chunks.back().device + chunks.back().floats != st.device)
        chunks.push_back({st.device, 0, {}});
      Chunk& k = chunks.back();
      for (uint32_t i : st.decisions) k.nodes.push_back({i, st.device - k.device + (g.nodes[i].storage - st.tree)});
      k.floats += st.floats;
    }
  }
  void staging(int slots) {
    size_t most = 0;
    for (const Chunk& k : chunks) most = std::max(most, k.floats);
    for (int slot = 0; slot < slots; ++slot)
      for (auto& v : stage[slot])
        if (v.size() < most) v.resize(most);
  }
  // f(node, offset in the chunk) for each of a chunk's decisions, in parallel
  template <class F>
  void for_nodes(const Chunk& k, F f) {
    const int tasks = int(std::min<size_t>(k.nodes.size(), size_t(4 * pool().threads())));
    std::function<void(int)> fn = [&](int t) {
      const size_t lo = k.nodes.size() * size_t(t) / size_t(tasks), hi = k.nodes.size() * size_t(t + 1) / size_t(tasks);
      for (size_t i = lo; i < hi; ++i) f(g.nodes[k.nodes[i].first], k.nodes[i].second);
    };
    if (tasks > 0) pool().parallel_for(tasks, fn);
  }

  Backend& b;
  GameTree& g;
  std::unique_ptr<Solve> s;
  std::vector<std::pair<unsigned, unsigned>> roots;  // node, the turn card dealt to it
  std::vector<Subtree> subtrees;                      // by root
  std::unordered_map<uint32_t, int> index;
  DeviceLayout layout;
  unsigned reach_stride[2] = {0, 0};
  std::vector<float> host_reach[2], host_values, check[2][2];  // check: [player][EV, best response]
  std::mutex wait_mu;
  bool pending = false, device_newer = false;
  bool device_zero = true;  // the device's rows are the zeros they were allocated with
  bool refresh_reach = true;  // the next pass propagates both players' reaches
  // a float range of the tree, in parallel
  static bool all_zero(const float* x, size_t n) {
    std::atomic<bool> nonzero{false};
    const int tasks = int(std::min<size_t>(64, std::max<size_t>(1, n >> 16)));
    std::function<void(int)> fn = [&](int t) {
      const size_t lo = n * size_t(t) / size_t(tasks), hi = n * size_t(t + 1) / size_t(tasks);
      for (size_t i = lo; i < hi && !nonzero.load(std::memory_order_relaxed); ++i)
        if (x[i] != 0.f) nonzero = true;
    };
    pool().parallel_for(tasks, fn);
    return !nonzero;
  }
};
}  // namespace
}  // namespace gpu

std::unique_ptr<SubtreeDevice> make_gpu_subtrees(GameTree& tree, uint64_t available, double share) {
  std::string error;
  gpu::Backend* b = gpu::backend(&error);
  if (!b) throw std::runtime_error(error);
  return std::make_unique<gpu::Subtrees>(*b, tree, available, share);
}

// The all-in outcome table of a flop on the GPU (see metal_flop_table): the compact triangle of one
// range (preflop trees) or a full hero x opponent table.
void gpu_flop_table(const GameTree& g, AllinRunouts& ar, const std::vector<std::array<uint32_t, 4>>& runout_boards) {
  std::string error;
  gpu::Backend* bp = gpu::backend(&error);
  if (!bp) throw std::runtime_error(error);
  gpu::Backend& b = *bp;
  const bool compact = g.compact_tables();
  const unsigned H = compact ? ar.live.size() : g.num_hands(0);
  const unsigned O = compact ? H : g.num_hands(1);
  const unsigned B = runout_boards.size(), Bp = (B + 63) & ~63u;
  const unsigned ip_offset = compact ? 0 : (H + 3) & ~3u;
  if (!H || !O || (compact && H < 2)) return;
  const size_t entries = compact ? size_t(H) * (H - 1) / 2 : size_t(H) * O;
  const unsigned hand_rows = ip_offset + O;
  std::vector<std::array<uint8_t, 2>> hand_cards(hand_rows);
  for (unsigned i = 0; i < H; ++i) {
    const unsigned h = compact ? ar.live[i] : i;
    hand_cards[i] = {g.hcard[0][0][h], g.hcard[0][1][h]};
  }
  if (!compact) {
    for (unsigned i = H; i < ip_offset; ++i) hand_cards[i] = hand_cards[0];
    for (unsigned o = 0; o < O; ++o) hand_cards[ip_offset + o] = {g.hcard[1][0][o], g.hcard[1][1][o]};
  }
  const unsigned padded_rows = (hand_rows + 3) & ~3u;
  std::vector<uint16_t> result(entries);
  {
    std::lock_guard<std::mutex> lock(b.mu);
    void* rank = b.kernel(gpu::kFloatState, "rankhands");
    void* count = b.kernel(gpu::kFloatState, "countpairs");
    gpu::DeviceBuffer boards, hands, ranks, table;
    boards.upload(b, runout_boards);
    hands.upload(b, hand_cards);
    ranks.create(b, size_t(padded_rows) * Bp * sizeof(uint32_t));
    table.create(b, entries * sizeof(uint16_t));
    const uint32_t rank_dims[4] = {hand_rows, B, Bp, 0};
    b.launch(rank, "rankhands", Bp / 64, padded_rows, 64, 0,
             {gpu::arg(boards.buf), gpu::arg(hands.buf), gpu::arg(ranks.buf), gpu::value(rank_dims)});
    const uint32_t pair_dims[4] = {H, O, Bp, ip_offset};
    const int center = compact ? ar.valid : 0;
    b.launch(count, "countpairs", (O + 3) / 4, (H + 3) / 4, 64, 0,
             {gpu::arg(ranks.buf), gpu::arg(hands.buf), gpu::arg(table.buf), gpu::value(pair_dims), gpu::value(center)});
    b.read(table.buf, 0, result.data(), entries * sizeof(uint16_t), true);
  }
  if (compact) {
    ar.tri16.resize(entries);
    std::memcpy(ar.tri16.data(), result.data(), entries * sizeof(uint16_t));
  } else {
    ar.e2 = std::move(result);
  }
}

}  // namespace pio
#endif
