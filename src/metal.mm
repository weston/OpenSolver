#include "metal.hpp"
#include "gpu.hpp"
#include "bfloat_regrets.hpp"
#include "fp16_sums.hpp"
#include "state_transfer.hpp"
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <malloc/malloc.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <mutex>
#include <vector>
#include <unordered_map>
#include "solver.hpp"
#include "parallel.hpp"
namespace pio {
namespace {
using std::array;
using std::vector;
#include "metal_layout.h"
const char kernel_source[] =
#include "metal_kernels.inc"
    ;

struct Metal {
  id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
  id<MTLCommandQueue> queue = [dev newCommandQueue];
  id<MTLLibrary> library, compact_library[2][2];
  std::once_flag compact_once[2][2];
  id<MTLLibrary> compile(bool compact, bool half_sums = false) {
    NSError* error = nil;
    auto options = [MTLCompileOptions new];
#if __MAC_OS_X_VERSION_MAX_ALLOWED >= 150000
    if (@available(macOS 15.0, *)) options.mathMode = MTLMathModeSafe;
#if __MAC_OS_X_VERSION_MIN_REQUIRED < 150000
    else
      options.fastMathEnabled = NO;
#endif
#else
    options.fastMathEnabled = NO;
#endif
    if (compact || half_sums)
      options.preprocessorMacros = @{@"WSOLVER_BFLOAT": @(compact), @"WSOLVER_FP16_SUMS": @(half_sums)};
    auto result = [dev newLibraryWithSource:[NSString stringWithUTF8String:kernel_source] options:options error:&error];
    if (!result) fail(error);
    return result;
  }
  Metal() {
    if (!queue) throw std::runtime_error("Metal command queue unavailable");
    library = compile(false);
  }
  [[noreturn]] static void fail(NSError* error) {
    throw std::runtime_error(error ? error.localizedDescription.UTF8String : "Metal command failed");
  }
  id<MTLComputePipelineState> pipeline(NSString* name, bool replay = false, bool early = false,
                                       AverageMode average_mode = DenseAverage, bool compact = false, bool half_sums = false,
                                       unsigned pass_mode = 0) {
    auto selected_library = library;
    if (compact || half_sums) {
      auto& cached = compact_library[compact][half_sums];
      std::call_once(compact_once[compact][half_sums], [&] { cached = compile(compact, half_sums); });
      selected_library = cached;
    }
    NSError* error = nil;
    auto descriptor = [MTLComputePipelineDescriptor new];
    descriptor.label = name;
    if (replay) {
      auto constants = [MTLFunctionConstantValues new];
      [constants setConstantValue:&early type:MTLDataTypeBool atIndex:0];
      [constants setConstantValue:&average_mode type:MTLDataTypeUInt atIndex:1];
      [constants setConstantValue:&pass_mode type:MTLDataTypeUInt atIndex:2];
      descriptor.computeFunction = [selected_library newFunctionWithName:name constantValues:constants error:&error];
      if (!descriptor.computeFunction) fail(error);
    } else
      descriptor.computeFunction = [selected_library newFunctionWithName:name];
    descriptor.supportIndirectCommandBuffers = replay;
    auto state = [dev newComputePipelineStateWithDescriptor:descriptor
                                                    options:MTLPipelineOptionNone
                                                 reflection:nil
                                                      error:&error];
    if (!state) fail(error);
    if (state.threadExecutionWidth != 32 || state.maxTotalThreadsPerThreadgroup < 128)
      throw std::runtime_error("unsupported Metal threadgroup size");
    return state;
  }
  id<MTLBuffer> buffer(size_t bytes) {
    if (bytes > dev.maxBufferLength) throw std::bad_alloc();
    auto buffer = [dev newBufferWithLength:std::max(bytes, size_t(1)) options:MTLResourceStorageModeShared];
    if (!buffer) throw std::bad_alloc();
    return buffer;
  }
  id<MTLCommandBuffer> command() {
    auto cmd = [queue commandBuffer];
    if (!cmd) throw std::bad_alloc();
    return cmd;
  }
  id<MTLComputeCommandEncoder> encoder(id<MTLCommandBuffer> cmd) {
    auto enc = [cmd computeCommandEncoder];
    if (!enc) throw std::bad_alloc();
    return enc;
  }
  void wait(id<MTLCommandBuffer> cmd) {
    [cmd waitUntilCompleted];
    if (cmd.status != MTLCommandBufferStatusCompleted) {
      NSError* error = cmd.error;
      if ([error.domain isEqualToString:MTLCommandBufferErrorDomain] && error.code == MTLCommandBufferErrorOutOfMemory)
        throw std::bad_alloc();
      fail(error);
    }
  }
  void finish(id<MTLCommandBuffer> cmd) {
    [cmd commit];
    wait(cmd);
  }
};
Metal& metal() {
  static Metal result;
  return result;
}
struct MetalRows {
  unsigned slot = 0;
  bool copied = false;
  ZVec<float> staging;
  id<MTLBuffer> input, output;

  static id<MTLBuffer> shared(Metal& metal, ZVec<float>& rows) {
    const size_t page = getpagesize();
    const size_t bytes = (rows.size() * sizeof(float) + page - 1) & ~(page - 1);
    if (!bytes || uintptr_t(rows.data()) % page || bytes > metal.dev.maxBufferLength ||
        malloc_size(rows.data()) < bytes)
      return nil;
    return [metal.dev newBufferWithBytesNoCopy:rows.data()
                                        length:bytes
                                       options:MTLResourceStorageModeShared
                                   deallocator:nil];
  }

  void bind(Metal& metal, ZVec<float>& current, bool staged) {
    input = staged ? nil : shared(metal, current);
    if (input) {
      staging.resize(current.size());
      output = shared(metal, staging);
      if (output) return;
      ZVec<float>().swap(staging);
    }
    input = output = metal.buffer(current.size() * sizeof(float));
  }

  void prepare(const ZVec<float>& current, bool refresh) {
    if (input == output && (!copied || refresh) && !current.empty()) {
      memcpy(input.contents, current.data(), current.size() * sizeof(float));
      copied = true;
    }
  }

  void publish(ZVec<float>& current) {
    if (input != output) {
      current.swap(staging);
      std::swap(input, output);
      slot ^= 1;
    } else if (!current.empty()) {
      memcpy(current.data(), output.contents, current.size() * sizeof(float));
    }
  }
};

struct BoardTables {
  vector<BoardPrefixes> boards;
  vector<PrefixEntry> indices;
  vector<HandPrefixes> heroes;
  BoardTables(const GameTree& g, const ShowdownBoard& fold, int p) {
    const unsigned H = g.num_hands(p);
    boards.resize(g.showdowns.size() + 1);
    auto board_at = [&](size_t i) -> const ShowdownBoard& { return i < g.showdowns.size() ? g.showdowns[i] : fold; };
    unsigned total = 0;
    for (size_t i = 0; i < boards.size(); ++i) {
      boards[i].index_offset = total;
      boards[i].hand_offset = i * H;
      total += 53 + 3 * board_at(i).ent[1 - p].size();
    }
    indices.resize(total);
    HandPrefixes blocked{};
    blocked.same = -1;
    heroes.resize(boards.size() * H, blocked);
    pool().parallel_for(int(boards.size()), [&](int index) {
      auto& b = boards[index];
      const auto& sd = board_at(index);
      const auto& opponents = sd.ent[1 - p];
      unsigned count[53]{}, position[53];
      count[0] = opponents.size();
      for (const auto& o : opponents) {
        ++count[1 + o.c1];
        ++count[1 + o.c2];
      }
      for (int c = 0; c < 53; ++c) {
        position[c] = b.start[c];
        b.start[c + 1] = b.start[c] + 1 + count[c];
      }
      auto* entries = indices.data() + b.index_offset;
      vector<uint16_t> slots(b.start[53]);
      unsigned hero_start = 0, opp_start = 0;
      for (auto [hero_end, opp_end] : sd.groups[p]) {
        for (unsigned j = hero_start; j < hero_end; ++j) {
          auto h = sd.ent[p][j];
          auto& m = heroes[b.hand_offset + h.idx];
          m.same = h.same;
          m.live = 1;
          unsigned c[3] = {0, 1u + h.c1, 1u + h.c2};
          for (int k = 0; k < 3; ++k) {
            m.lo[k] = position[c[k]];
            slots[m.lo[k]] = 1;
          }
        }
        for (unsigned j = opp_start; j < opp_end; ++j) {
          auto o = opponents[j];
          entries[++position[0]].hand = o.idx;
          entries[++position[1 + o.c1]].hand = o.idx;
          entries[++position[1 + o.c2]].hand = o.idx;
        }
        for (unsigned j = hero_start; j < hero_end; ++j) {
          auto h = sd.ent[p][j];
          auto& m = heroes[b.hand_offset + h.idx];
          m.hi[0] = position[0];
          m.hi[1] = position[1 + h.c1];
          m.hi[2] = position[1 + h.c2];
          for (auto j : m.hi) slots[j] = 1;
        }
        hero_start = hero_end;
        opp_start = opp_end;
      }
      b.prefix_count = first_rank_prefix;
      for (unsigned c = 0; c < 53; ++c) {
        const unsigned first = b.start[c], last = b.start[c + 1] - 1;
        slots[first] = empty_prefix;
        if (first == last) continue;
        slots[last] = total_prefix + c;
        entries[last].slot = slots[last];
        for (unsigned j = first + 1; j < last; ++j) {
          if (slots[j]) {
            slots[j] = b.prefix_count++;
            entries[j].slot = slots[j];
          }
        }
      }
      for (const auto& h : sd.ent[p]) {
        auto& m = heroes[b.hand_offset + h.idx];
        for (int c = 0; c < 3; ++c) {
          m.lo[c] = slots[m.lo[c]];
          m.hi[c] = slots[m.hi[c]];
        }
      }
    });
  }
};
class PostflopSolve final : public MetalSolve {
 public:
  Metal& m = metal();
  id<MTLComputePipelineState> propagate_reach, update_values, terminals, river_pairs, river_rounds,
      allins, wide_rounds, bet_checks, two_bets, wide_two_bets, lead_checks, wide_lead_checks;
  id<MTLBuffer> nodes, node_ids, r[2], v, cards, iso, perms[2], boards[2], indices[2], heroes[2], rounds;
  id<MTLBuffer> equity[2], equity_tables, allin_batches, bet_check_nodes, two_bet_nodes, lead_check_nodes;
  MetalRows regrets, strategy_sums;
  bool compact_regrets = false, regrets_current_on_cpu = true;
  bool compact_sums = false, sums_current_on_cpu = true;
  void publish_sums(GameTree& g) {
    if (sums_current_on_cpu) return;
    const auto* source = static_cast<const uint16_t*>(strategy_sums.input.contents);
    transfer_state(source, g.strat.data(), g.strat.size(), [](uint16_t word) { return fp16_sum_value(word); });
    sums_current_on_cpu = true;
  }

  void publish_regrets(GameTree& g) {
    if (regrets_current_on_cpu) return;
    const auto* source = static_cast<const uint16_t*>(regrets.input.contents);
    transfer_state(source, g.regrets.data(), g.regrets.size(),
                   [](uint16_t word) { return std::bit_cast<float>(bfloat_regret_bits(word)); });
    regrets_current_on_cpu = true;
  }

  void publish_for_cpu(GameTree& g) override {
    publish_sums(g);
    publish_regrets(g);
  }

  void prepare_regrets(const GameTree& g) {
    auto* dest = static_cast<uint16_t*>(regrets.input.contents);
    transfer_state(g.regrets.data(), dest, g.regrets.size(),
                   [](float value) { return bfloat_regret_word(std::bit_cast<uint32_t>(value)); });
  }
  struct Range {
    size_t start, count;
  };
  vector<Range> levels;
  vector<array<Range, 2>> reach_levels;
  Range terminal_range, pair_range, round_range, bet_check_range, two_bet_range, lead_check_range, allin_range;
  size_t stride, state_bytes, round_bytes[2]{};
  bool stage_regrets = false, stage_sums = false;
  int prepared_iteration = 0;
  struct Replay {
    id<MTLIndirectCommandBuffer> commands;
    NSUInteger count = 0;
  };
  struct {
    Replay plans[3][2][2][2][2];  // average mode, regret slot, strategy slot, updating player, refreshed reach
  } replays;
  bool write_sums = true;
  bool early_replay = false;
  NSMutableDictionary<NSString*,id<MTLComputePipelineState>>* specialized_pipelines[4][2][3][3]{};  // + pass mode
  id<MTLBuffer> params[2];
  template <class V>
  id<MTLBuffer> copy_buffer(const V& x) {
    auto n = x.size() * sizeof(x[0]);
    auto b = m.buffer(std::max(size_t(16), n));
    if (n) memcpy(b.contents, x.data(), n);
    return b;
  }
  void check_workspace(const GameTree& g, uint64_t available, uint64_t value_rows, uint64_t reach_floats,
                       uint64_t mappings, uint64_t round_storage, uint64_t batches, uint64_t equity_bytes) {
    const size_t N = g.nodes.size();
    uint64_t hands = g.num_hands(0) + g.num_hands(1);
    uint64_t boardBytes =
        (g.showdowns.size() + 1) *
        (2 * sizeof(BoardPrefixes) + hands * (sizeof(HandPrefixes) + 3 * sizeof(PrefixEntry)) +
         106 * sizeof(PrefixEntry));
    uint64_t workspace = (value_rows * stride + reach_floats) * sizeof(float) + 2 * state_bytes;
    uint64_t peak = workspace + equity_bytes +
                    2 * (boardBytes + N * (sizeof(TraversalNode) + 4 * sizeof(unsigned)) +
                         mappings * 2 * sizeof(unsigned) + round_storage + batches * sizeof(AllinBatch)) +
                    128 * stride;
    if (mappings > UINT32_MAX || (g.showdowns.size() + 1) * (3 * stride + 53) > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal board index limits");
    const uint64_t limit = m.dev.recommendedMaxWorkingSetSize;
    if (peak > available || peak > limit)
      throw std::runtime_error("Metal workspace exceeds available memory");
    // Zero-copy rows also bind the published CPU arrays for the duration of each command.
    stage_sums = peak + 2 * state_bytes > limit;
    stage_regrets = peak + state_bytes > limit;
  }
  // roots: the whole tree, or a hybrid solve's turn subtrees (MetalSubtrees, #589), whose rows are the tree's
  // own: bound in place by their owner, so no state is allocated or counted here.
  explicit PostflopSolve(GameTree& g, uint64_t available, const vector<std::pair<unsigned, unsigned>>* roots = nullptr) {
    stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    state_bytes = roots ? 0 : g.storage_floats * sizeof(float);
    const size_t pairs = size_t(g.num_hands(0)) * g.num_hands(1);
    if (!g.num_hands(0) || !g.num_hands(1) || g.nodes.size() * stride > UINT32_MAX || g.storage_floats > UINT32_MAX ||
        pairs * g.allins.size() > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal index limits");
    if (2 * g.nodes.size() * sizeof(TraversalNode) > available)
      throw std::runtime_error("Metal workspace exceeds available memory");
    vector<AllinTable> tables;
    size_t table_words = 0;
    for (const auto& ar : g.allins) {
      const unsigned width = ar.e2b.empty() ? sizeof(int16_t) : sizeof(int8_t);
      tables.push_back({unsigned(table_words), width, .5f * ar.scale});
      table_words += (pairs * width + 1) / 2;
    }
    const size_t equity_bytes = 2 * table_words * sizeof(int16_t) + tables.size() * sizeof(AllinTable);
    pack_nodes(g, available, equity_bytes, roots);
    pack_hands(g);
    if (!levels.empty()) {
      propagate_reach = m.pipeline(@"propagate_reach", true);
      update_values = m.pipeline(@"update_values", true);
    }
    if (terminal_range.count) terminals = m.pipeline(@"terminal", true);
    if (pair_range.count) river_pairs = m.pipeline(@"river_call_fold", true);
    if (allin_range.count) allins = m.pipeline(@"allin", true);
    if (bet_check_range.count) bet_checks = m.pipeline(@"river_bet_check", true);
    if (two_bet_range.count) {
      if (4 * std::min(round_bytes[0], round_bytes[1]) <= m.dev.maxThreadgroupMemoryLength)
        two_bets = m.pipeline(@"river_two_bets", true);
      if (4 * std::max(round_bytes[0], round_bytes[1]) > m.dev.maxThreadgroupMemoryLength)
        wide_two_bets = m.pipeline(@"river_two_bets_wide", true);
    }
    if (lead_check_range.count) {
      if (3 * std::min(round_bytes[0], round_bytes[1]) <= m.dev.maxThreadgroupMemoryLength)
        lead_checks = m.pipeline(@"river_lead_check", true);
      if (3 * std::max(round_bytes[0], round_bytes[1]) > m.dev.maxThreadgroupMemoryLength)
        wide_lead_checks = m.pipeline(@"river_lead_check_wide", true);
    }
    if (round_range.count) {
      const auto limit = m.dev.maxThreadgroupMemoryLength;
      if (3 * std::min(round_bytes[0], round_bytes[1]) <= limit)
        river_rounds = m.pipeline(@"river_betting_round", true);
      if (3 * std::max(round_bytes[0], round_bytes[1]) > limit)
        wide_rounds = m.pipeline(@"river_betting_round_wide", true);
    }

    pack_allins(g, tables, table_words);
    for (auto& b : params) b = m.buffer(sizeof(IterationParams));
  }

  void pack_hands(const GameTree& g) {
    vector<array<uint8_t, 2>> hand_cards(2 * stride, {255, 255});
    ShowdownBoard fold;
    for (int p = 0; p < 2; ++p) {
      for (int h = 0; h < g.num_hands(p); ++h) {
        hand_cards[p * stride + h] = {g.hcard[p][0][h], g.hcard[p][1][h]};
        fold.ent[p].push_back(
            {uint16_t(h), hand_cards[p * stride + h][0], hand_cards[p * stride + h][1], int16_t(g.same_hand[p][h])});
      }
      vector<uint16_t> permutations(24 * stride);
      for (uint8_t j = 0; j < 24; ++j)
        if (g.perm_hand[p][j].size())
          std::copy(g.perm_hand[p][j].begin(), g.perm_hand[p][j].end(), permutations.begin() + j * stride);
      perms[p] = copy_buffer(permutations);
    }
    cards = copy_buffer(hand_cards);
    for (int p = 0; p < 2; ++p) {
      fold.groups[p].push_back({uint16_t(g.num_hands(p)), uint16_t(g.num_hands(1 - p))});
      BoardTables b(g, fold, p);
      size_t prefixes = 0;
      for (size_t i = 0; i < g.showdowns.size(); ++i)
        prefixes = std::max(prefixes, size_t(b.boards[i].prefix_count));
      round_bytes[p] = (prefixes + g.num_hands(1 - p)) * sizeof(float);
      boards[p] = copy_buffer(b.boards);
      indices[p] = copy_buffer(b.indices);
      heroes[p] = copy_buffer(b.heroes);
    }
  }

  template <typename Count, typename Outcomes>
  static void pack_counts(const GameTree& g, uint64_t board, const Outcomes& outcomes, Count* oop, Count* ip) {
    const int H = g.num_hands(0), O = g.num_hands(1);
    for (int h = 0; h < H; ++h) {
      const uint64_t hero = combo_mask(g.hands[0][h]);
      for (int o = 0; o < O; ++o) {
        const uint64_t opp = combo_mask(g.hands[1][o]);
        const size_t pair = size_t(h) * O + o;
        const Count count = (hero & opp) || ((hero | opp) & board) ? -1 : outcomes[pair];
        oop[pair] = count;
        ip[size_t(o) * H + h] = count;
      }
    }
  }

  void pack_allins(const GameTree& g, const vector<AllinTable>& tables, size_t table_words) {
    equity_tables = copy_buffer(tables);
    for (auto& buffer : equity) buffer = m.buffer(table_words * sizeof(int16_t));
    pool().parallel_for(int(g.allins.size()), [&](int k) {
      const auto& ar = g.allins[k];
      auto* oop = static_cast<int16_t*>(equity[0].contents) + tables[k].word_offset;
      auto* ip = static_cast<int16_t*>(equity[1].contents) + tables[k].word_offset;
      uint64_t board = 0;
      for (auto card : ar.cards) board |= card_bit(card);
      if (ar.e2b.empty()) pack_counts(g, board, ar.e2, oop, ip);
      else pack_counts(g, board, ar.e2b, reinterpret_cast<int8_t*>(oop), reinterpret_cast<int8_t*>(ip));
    });
  }

  template <typename Round>
  static void pack_river_payoff(const GameTree& g, Round& r, unsigned channel, unsigned showdown, bool has_fold) {
    const auto& pay = g.term_pay(g.nodes[showdown]);
    std::copy_n(pay.w, 2, r.win[channel]);
    std::copy_n(pay.l, 2, r.loss[channel]);
    std::copy_n(pay.t, 2, r.tie[channel]);
    if (has_fold) std::copy_n(g.term_pay(g.nodes[showdown + 1]).w, 2, r.fold[channel]);
  }

  static bool read_river_round(const GameTree& g, unsigned i, RiverRound& r) {
    const auto& n = g.nodes[i];
    if (n.kind != ANodeKind::Decision || n.board_len != 5) return false;
    r.root = i;
    auto call_fold = [&](const GNode& node, unsigned actor) {
      return node.kind == ANodeKind::Decision && node.player == actor && node.num_children == 2 &&
             g.nodes[node.first_child].kind == ANodeKind::Showdown &&
             g.nodes[node.first_child + 1].kind == ANodeKind::Fold;
    };
    const bool final_raise = n.num_children == 3;
    if (final_raise || (n.num_children == 2 && g.nodes[n.first_child + 1].kind == ANodeKind::Showdown)) {
      const unsigned last = n.first_child + 1;
      const auto& response = g.nodes[n.first_child];
      if (g.nodes[last].kind != ANodeKind::Showdown || (final_raise && g.nodes[last + 1].kind != ANodeKind::Fold))
        return false;
      r.board = g.nodes[last].aux;
      r.storage[0] = n.storage;
      r.storage[1] = response.storage;
      if (call_fold(response, 1 - n.player)) {
        r.kind = final_raise ? FinalRaise : BetThenCheck;
        pack_river_payoff(g, r, 0, response.first_child, true);
        pack_river_payoff(g, r, 1, last, final_raise);
        return true;
      }
      if (final_raise || response.kind != ANodeKind::Decision || response.player == n.player ||
          response.num_children != 3)
        return false;
      const auto& raised = g.nodes[response.first_child];
      const unsigned called = response.first_child + 1;
      if (!call_fold(raised, n.player) || g.nodes[called].kind != ANodeKind::Showdown ||
          g.nodes[called + 1].kind != ANodeKind::Fold)
        return false;
      r.kind = BetWithRaise;
      r.storage[2] = raised.storage;
      pack_river_payoff(g, r, 0, raised.first_child, true);
      pack_river_payoff(g, r, 1, called, true);
      pack_river_payoff(g, r, 2, last, false);
      return true;
    }
    if (n.num_children != 2) return false;
    auto decision = [&](unsigned index, int actor) {
      const auto& d = g.nodes[index];
      return d.kind == ANodeKind::Decision && d.num_children == 2 && d.player == actor;
    };
    unsigned lead = n.first_child, check = lead + 1;
    if (!decision(lead, 1 - n.player) || !decision(check, 1 - n.player)) return false;
    unsigned bet = g.nodes[check].first_child, last = bet + 1;
    if (!decision(bet, n.player) || g.nodes[last].kind != ANodeKind::Showdown) return false;
    unsigned call0 = g.nodes[lead].first_child, call1 = g.nodes[bet].first_child;
    if (g.nodes[call0].kind != ANodeKind::Showdown || g.nodes[call0 + 1].kind != ANodeKind::Fold ||
        g.nodes[call1].kind != ANodeKind::Showdown || g.nodes[call1 + 1].kind != ANodeKind::Fold)
      return false;
    r.board = g.nodes[last].aux;
    unsigned decisions[] = {i, lead, check, bet};
    for (int j = 0; j < 4; ++j) r.storage[j] = g.nodes[decisions[j]].storage;
    pack_river_payoff(g, r, 0, call0, true);
    pack_river_payoff(g, r, 1, call1, true);
    pack_river_payoff(g, r, 2, last, false);
    r.kind = BetBothSides;
    return true;
  }

  static bool read_paired_river_round(const GameTree& g, unsigned i, PairedRiverRound& pair) {
    const auto& n = g.nodes[i];
    if (n.num_children != 2) return false;
    const unsigned lead = n.first_child, check = lead + 1;
    if (g.nodes[lead].player == n.player || g.nodes[check].player != g.nodes[lead].player) return false;
    pair.root = i;
    return read_river_round(g, lead, pair.branches[0]) && pair.branches[0].kind == FinalRaise &&
           read_river_round(g, check, pair.branches[1]) && pair.branches[1].kind == BetWithRaise;
  }

  static bool pack_two_bet_river(const GameTree& g, unsigned index, vector<TwoBetRiver>& out) {
    const auto& n = g.nodes[index];
    if (n.kind != ANodeKind::Decision || n.board_len != 5 || (n.num_children != 3 && n.num_children != 4))
      return false;
    const unsigned big = n.first_child;
    const bool has_big_bet = n.num_children != 3 || g.nodes[big + 1].kind != ANodeKind::Showdown;
    const unsigned small = has_big_bet ? big + 1 : big, passive = small + 1;
    auto decision = [&](unsigned id, int actor, int count) {
      const auto& node = g.nodes[id];
      return node.kind == ANodeKind::Decision && node.player == actor && node.num_children == count;
    };
    if ((has_big_bet && !decision(big, 1 - n.player, 2)) || !decision(small, 1 - n.player, 3) ||
        g.nodes[passive].kind != ANodeKind::Showdown)
      return false;
    if ((!has_big_bet || n.num_children == 4) && g.nodes[passive + 1].kind != ANodeKind::Fold) return false;
    const unsigned raised = g.nodes[small].first_child;
    if (!decision(raised, n.player, 2)) return false;
    unsigned showdowns[] = {g.nodes[big].first_child, g.nodes[raised].first_child, raised + 1, passive};
    for (unsigned k = has_big_bet ? 0 : 1; k < 3; ++k)
      if (g.nodes[showdowns[k]].kind != ANodeKind::Showdown || g.nodes[showdowns[k] + 1].kind != ANodeKind::Fold)
        return false;
    unsigned decisions[] = {index, big, small, raised};
    TwoBetRiver r{};
    r.root = index;
    r.board = g.nodes[passive].aux;
    for (unsigned k = 0; k < 4; ++k) {
      r.storage[k] = g.nodes[decisions[k]].storage;
      if (k > 0 || has_big_bet) pack_river_payoff(g, r, k, showdowns[k], k < 3 || !has_big_bet || n.num_children == 4);
    }
    r.has_big_bet = has_big_bet;
    out.push_back(r);
    return true;
  }

  static size_t round_channel_bytes(unsigned hands) { return (4 * hands + 53) * sizeof(float); }

  // roots (a hybrid solve, #589, as gpu_pack.hpp's for #582): the turn subtrees at these nodes, each dealt the
  // card beside it; root j's reaches are rows j of each player's reach buffer and its values row j of the values.
  void pack_nodes(const GameTree& g, uint64_t available, uint64_t equity_bytes,
                  const vector<std::pair<unsigned, unsigned>>* roots = nullptr) {
    const size_t N = g.nodes.size();
    vector<TraversalNode> packed_nodes(N);
    vector<RiverRound> betting_rounds, final_bets;
    vector<PairedRiverRound> lead_check_rounds;
    vector<TwoBetRiver> two_bet_rivers;
    const size_t channel_bytes = round_channel_bytes(std::max(g.num_hands(0), g.num_hands(1)));
    const bool rounds_fit = channel_bytes <= m.dev.maxThreadgroupMemoryLength;
    const bool bet_checks_fit = 2 * channel_bytes <= m.dev.maxThreadgroupMemoryLength;
    vector<unsigned> list, frontier{0}, terminal_ids, paired_ids, allin_ids;
    vector<array<unsigned, 2>> deals_by_node;
    std::unordered_map<uint64_t, unsigned> outcomes;
    for (unsigned j = 0; j < g.allins.size(); ++j) {
      uint64_t mask = 0;
      for (auto c : g.allins[j].cards) mask |= card_bit(c);
      outcomes[mask] = j;
    }
    vector<unsigned> showdown(N, UINT32_MAX);
    for (size_t i = N; i-- > 0;) {
      const auto& n = g.nodes[i];
      if (n.kind == ANodeKind::Showdown)
        showdown[i] = i;
      else if (n.num_children == 1 && n.kind == ANodeKind::Decision)
        showdown[i] = showdown[n.first_child];
      else if (n.kind == ANodeKind::Chance && g.chances[n.aux].cards.size() == size_t(52 - n.board_len) &&
               std::all_of(showdown.begin() + n.first_child, showdown.begin() + n.first_child + n.num_children,
                           [](unsigned end) { return end != UINT32_MAX; }))
        showdown[i] = showdown[n.first_child];
    }
    const unsigned reach_stride[] = {unsigned(g.num_hands(0) + 31) & ~31u, unsigned(g.num_hands(1) + 31) & ~31u};
    unsigned reach_floats[] = {reach_stride[0], reach_stride[1]};
    unsigned value_rows = 1;
    if (roots) {
      frontier.clear();
      for (unsigned j = 0; j < roots->size(); ++j) {
        const auto [root, card] = (*roots)[j];
        auto& r = packed_nodes[root];
        r.value_row = j;
        r.card = card;
        r.public_cards = card + 1;  // below a flop root's deal, as Deal gives its children
        for (int p = 0; p < 2; ++p) r.reach_offset[p] = j * reach_stride[p];
        frontier.push_back(root);
      }
      reach_floats[0] = std::max<unsigned>(1, roots->size()) * reach_stride[0];
      reach_floats[1] = std::max<unsigned>(1, roots->size()) * reach_stride[1];
      value_rows = std::max<unsigned>(1, roots->size());
    }
    while (!frontier.empty()) {
      size_t start = list.size();
      vector<unsigned> next;
      for (auto i : frontier) {
        const auto& n = g.nodes[i];
        auto& a = packed_nodes[i];
        a.child = n.first_child;
        a.count = n.num_children;
        a.actor = n.player;
        a.storage = n.storage;
        a.aux = n.aux;
        if (bet_checks_fit && pack_two_bet_river(g, i, two_bet_rivers)) continue;
        if (rounds_fit && n.kind == ANodeKind::Decision && n.board_len == 5) {
          PairedRiverRound pair{};
          if (read_paired_river_round(g, i, pair)) {
            lead_check_rounds.push_back(pair);
            continue;
          }
          RiverRound round{};
          if (read_river_round(g, i, round) && (round.kind != BetThenCheck || bet_checks_fit)) {
            (round.kind == BetThenCheck ? final_bets : betting_rounds).push_back(round);
            continue;
          }
        }
        if (n.kind == ANodeKind::Decision && n.board_len == 5 && n.num_children == 2 &&
            g.nodes[n.first_child].kind == ANodeKind::Showdown && g.nodes[n.first_child + 1].kind == ANodeKind::Fold) {
          paired_ids.push_back(i);
          for (unsigned k = 0; k < 2; ++k) {
            auto& child = packed_nodes[a.child + k];
            const auto& original = g.nodes[a.child + k];
            const auto& pay = g.term_pay(original);
            child.aux = original.aux;
            std::copy_n(pay.w, 2, child.win);
            std::copy_n(pay.l, 2, child.loss);
            std::copy_n(pay.t, 2, child.tie);
          }
          continue;
        }
        if (n.kind == ANodeKind::Chance && showdown[i] != UINT32_MAX) {
          auto found = outcomes.find(g.chances[n.aux].board_mask);
          if (found != outcomes.end()) {
            a.kind = AllIn;
            a.aux = found->second;
            auto pay = g.term_pay(g.nodes[showdown[i]]);
            std::copy_n(pay.w, 2, a.win);
            std::copy_n(pay.l, 2, a.loss);
            allin_ids.push_back(i);
            continue;
          }
        }
        if (n.kind == ANodeKind::Decision || n.kind == ANodeKind::Chance) {
          a.kind = n.kind == ANodeKind::Chance ? Deal : Decision;
          if (a.kind != Decision || a.count > 1) list.push_back(i);
          if (a.kind == Deal) {
            const auto& ci = g.chances[n.aux];
            a.deals = deals_by_node.size();
            a.chance_weight = 1.f / ci.num_possible;
            for (unsigned k = 0; k < a.count; ++k) {
              packed_nodes[a.child + k].card = ci.cards[k];
              deals_by_node.push_back({a.child + k, 0});
              for (auto cp : ci.iso[k]) deals_by_node.push_back({a.child + k, PERM_INV[cp.second]});
            }
            a.deal_count = deals_by_node.size() - a.deals;
          }
          // A decision reads each hand independently, so its first child can reduce into the parent row.
          const bool reuse_first = a.kind == Decision;
          a.child_value = value_rows;
          value_rows += a.count - reuse_first;
          for (unsigned k = 0; k < a.count; ++k) {
            packed_nodes[a.child + k].public_cards =
                a.kind == Deal ? (a.public_cards << 8) | (packed_nodes[a.child + k].card + 1) : a.public_cards;
            packed_nodes[a.child + k].value_row = reuse_first && k == 0 ? a.value_row : a.child_value + k - reuse_first;
            for (int p = 0; p < 2; ++p) {
              auto& offset = packed_nodes[a.child + k].reach_offset[p];
              offset = a.reach_offset[p];
              if (a.kind == Decision && a.actor == unsigned(p) && a.count > 1) {
                offset = reach_floats[p];
                reach_floats[p] += reach_stride[p];
              }
            }
            next.push_back(a.child + k);
          }
        } else {
          auto pay = g.term_pay(n);
          std::copy_n(pay.w, 2, a.win);
          std::copy_n(pay.l, 2, a.loss);
          std::copy_n(pay.t, 2, a.tie);
          if (n.kind == ANodeKind::Fold) {
            a.kind = Fold;
            a.aux = g.showdowns.size();
            terminal_ids.push_back(i);
          } else if (n.board_len == 5) {
            a.kind = RiverShowdown;
            terminal_ids.push_back(i);
          } else {
            a.kind = AllIn;
            allin_ids.push_back(i);
          }
        }
      }
      if (list.size() > start) levels.push_back({start, list.size() - start});
      frontier = std::move(next);
    }
    for (Range level : levels) {
      array<Range, 2> by_player;
      for (unsigned p = 0; p < 2; ++p) {
        size_t start = list.size();
        for (size_t j = level.start; j < level.start + level.count; ++j) {
          unsigned index = list[j];
          const auto& node = packed_nodes[index];
          if (node.kind == Decision && node.actor == p && node.count > 1) list.push_back(2 * index + p);
        }
        by_player[p] = {start, list.size() - start};
      }
      reach_levels.push_back(by_player);
    }
    terminal_range = {list.size(), terminal_ids.size()};
    list.insert(list.end(), terminal_ids.begin(), terminal_ids.end());
    pair_range = {list.size(), paired_ids.size()};
    list.insert(list.end(), paired_ids.begin(), paired_ids.end());
    std::stable_sort(allin_ids.begin(), allin_ids.end(), [&](unsigned a, unsigned b) {
      return packed_nodes[a].aux < packed_nodes[b].aux;
    });
    vector<AllinBatch> batches;
    for (unsigned i : allin_ids) {
      const auto& n = packed_nodes[i];
      if (batches.empty() || batches.back().table != n.aux || batches.back().count == 4)
        batches.emplace_back().table = n.aux;
      auto& batch = batches.back();
      const unsigned k = batch.count++;
      batch.value_offset[k] = n.value_row * stride;
      for (unsigned p = 0; p < 2; ++p) {
        batch.reach_offset[p][k] = n.reach_offset[1 - p];
        batch.win_minus_loss[p][k] = n.win[p] - n.loss[p];
        batch.loss[p][k] = n.loss[p];
      }
    }
    allin_range = {0, batches.size()};
    size_t round_storage = (betting_rounds.size() + final_bets.size()) * sizeof(RiverRound) +
                           lead_check_rounds.size() * sizeof(PairedRiverRound) +
                           two_bet_rivers.size() * sizeof(TwoBetRiver);
    check_workspace(g, available, value_rows, size_t(reach_floats[0]) + reach_floats[1], deals_by_node.size(),
                    round_storage, batches.size(), equity_bytes);
    for (int p = 0; p < 2; ++p) {
      r[p] = m.buffer(size_t(reach_floats[p]) * sizeof(float));
      memcpy(r[p].contents, g.init_reach[p].data(), g.num_hands(p) * 4);
    }
    v = m.buffer(size_t(value_rows) * stride * sizeof(float));
    round_range = {0, betting_rounds.size()};
    rounds = copy_buffer(betting_rounds);
    two_bet_range = {0, two_bet_rivers.size()};
    two_bet_nodes = copy_buffer(two_bet_rivers);
    lead_check_nodes = copy_buffer(lead_check_rounds);
    lead_check_range = {0, lead_check_rounds.size()};
    bet_check_range = {0, final_bets.size()};
    bet_check_nodes = copy_buffer(final_bets);
    allin_batches = copy_buffer(batches);
    nodes = copy_buffer(packed_nodes);
    node_ids = copy_buffer(list);
    for (auto& entry : deals_by_node) {
      const auto& child = packed_nodes[entry[0]];
      entry = {child.value_row, entry[1] | (child.card << 8)};
    }
    iso = copy_buffer(deals_by_node);
  }

  // pass_mode (a hybrid report, #589): the average strategy, read from the sums bound as the regrets; EV (1) or
  // best response (2), nothing written.
  Replay record(const GameTree& g, unsigned p, bool refresh_reach, AverageMode mode, unsigned pass_mode = 0) {
    auto descriptor = [MTLIndirectCommandBufferDescriptor new];
    descriptor.commandTypes = MTLIndirectCommandTypeConcurrentDispatch;
    descriptor.inheritPipelineState = NO;
    descriptor.inheritBuffers = NO;
    descriptor.maxKernelBufferBindCount = 15;
    Replay plan;
    plan.commands = [m.dev newIndirectCommandBufferWithDescriptor:descriptor
                                                  maxCommandCount:reach_levels.size() + levels.size() + 8
                                                          options:MTLResourceStorageModeShared];
    if (!plan.commands) throw std::bad_alloc();
    id<MTLBuffer> buffers[15] = {
        nodes, node_ids,      params[p],           r[0], r[1], v, regrets.output, strategy_sums.output, cards, nil, nil,
        nil,   regrets.input, strategy_sums.input, nil};
    if (pass_mode) buffers[6] = buffers[12] = strategy_sums.input;
    auto dispatch = [&](id<MTLComputePipelineState> pipe, Range span, size_t scratch = 0, unsigned y = 1) {
      if (!span.count) return;
      auto c = [plan.commands indirectComputeCommandAtIndex:plan.count++];
      id<MTLComputePipelineState> selected = pipe;
      const bool policy = early_replay && pipe != terminals && pipe != allins;
      const bool writes_strategy = pipe != terminals && pipe != allins && pipe != propagate_reach;
      const AverageMode average = writes_strategy ? mode : DenseAverage;
      const bool compact = compact_regrets && pipe != terminals && pipe != allins;
      const bool half_sums = writes_strategy && compact_sums;
      const unsigned pass = writes_strategy ? pass_mode : 0;
      if (compact || half_sums || policy || average != DenseAverage || pass) {
        auto& variants = specialized_pipelines[unsigned(compact) + 2 * unsigned(half_sums)][policy][average][pass];
        if (!variants) variants = [NSMutableDictionary new];
        selected = variants[pipe.label];
        if (!selected) {
          selected = m.pipeline(pipe.label, true, policy, average, compact, half_sums, pass);
          variants[pipe.label] = selected;
        }
      }
      [c setComputePipelineState:selected];
      for (unsigned i = 0; i < 15; ++i)
        if (buffers[i]) [c setKernelBuffer:buffers[i] offset:i == 1 ? span.start * 4 : 0 atIndex:i];
      if (scratch) [c setThreadgroupMemoryLength:(scratch + 15) & ~size_t(15) atIndex:0];
      unsigned width = pipe != allins && stride >= 256 ? 256 : 128;
      [c setBarrier];
      [c concurrentDispatchThreadgroups:MTLSizeMake(span.count, y, 1)
                  threadsPerThreadgroup:MTLSizeMake(std::min<NSUInteger>(pipe.maxTotalThreadsPerThreadgroup, width), 1,
                                                    1)];
    };
    for (const auto& reach : reach_levels) {
      Range span = reach[1 - p];
      if (refresh_reach) span = {reach[0].start, reach[0].count + reach[1].count};
      dispatch(propagate_reach, span);
    }
    buffers[9] = boards[p];
    buffers[10] = indices[p];
    buffers[11] = heroes[p];
    auto O = g.num_hands(1 - p);
    const size_t prefix_bytes = round_bytes[p] - O * sizeof(float);
    dispatch(terminals, terminal_range, std::max(prefix_bytes, (O + 53) * sizeof(float)));
    dispatch(river_pairs, pair_range, prefix_bytes + (2 * O + 53) * sizeof(float));
    buffers[14] = rounds;
    const size_t channel_bytes = round_bytes[p];
    if (channel_bytes * 3 <= m.dev.maxThreadgroupMemoryLength)
      dispatch(river_rounds, round_range, channel_bytes * 3);
    else
      dispatch(wide_rounds, round_range, channel_bytes);
    buffers[14] = bet_check_nodes;
    dispatch(bet_checks, bet_check_range, 2 * channel_bytes);
    buffers[14] = two_bet_nodes;
    if (4 * channel_bytes <= m.dev.maxThreadgroupMemoryLength)
      dispatch(two_bets, two_bet_range, 4 * channel_bytes);
    else
      dispatch(wide_two_bets, two_bet_range, 2 * channel_bytes);
    buffers[14] = lead_check_nodes;
    if (3 * channel_bytes <= m.dev.maxThreadgroupMemoryLength)
      dispatch(lead_checks, lead_check_range, 3 * channel_bytes);
    else
      dispatch(wide_lead_checks, lead_check_range, channel_bytes);
    buffers[14] = allin_batches;
    buffers[9] = equity_tables;
    buffers[10] = equity[p];
    dispatch(allins, allin_range, 0, (g.num_hands(p) + 3) / 4);
    buffers[9] = iso;
    buffers[10] = perms[p];
    for (auto it = levels.rbegin(); it != levels.rend(); ++it) dispatch(update_values, *it);
    return plan;
  }

  void seed_forced_rows(const GameTree& g, const MetalRows& rows, size_t word_size) {
    if (rows.input == rows.output) return;
    const auto* input = static_cast<const uint8_t*>(rows.input.contents);
    auto* output = static_cast<uint8_t*>(rows.output.contents);
    // Every one-action decision of a player reads that player's one shared row (#567): two rows to seed.
    bool seeded[2] = {false, false};
    for (const auto& n : g.nodes) {
      if (n.kind != ANodeKind::Decision || !GameTree::shares_row(n) || seeded[n.player]) continue;
      seeded[n.player] = true;
      const size_t offset = word_size * n.storage;
      const size_t bytes = word_size * g.num_hands(n.player);
      if (memcmp(output + offset, input + offset, bytes) != 0) memcpy(output + offset, input + offset, bytes);
      if (seeded[0] && seeded[1]) break;
    }
  }

  id<MTLCommandBuffer> submit(GameTree& g, Solver& s, int t, bool refresh_reach) {
    const bool compact = s.uses_bfloat_regrets(t);
    const bool half_sums = s.uses_fp16_sums(t);
    const bool bind_regrets = !regrets.input || compact != compact_regrets;
    const bool bind_sums = !strategy_sums.input || half_sums != compact_sums;
    const bool upload_regrets = refresh_reach || bind_regrets;
    const bool upload_sums = refresh_reach || bind_sums;
    if (bind_regrets || bind_sums) {
      replays = {};
      if (bind_regrets) {
        publish_regrets(g);
        regrets = {};
        compact_regrets = compact;
        if (compact) {
          regrets.input = m.buffer(state_bytes / 2);
          regrets.output = m.buffer(state_bytes / 2);
        } else {
          regrets.bind(m, g.regrets, stage_regrets);
        }
      }
      if (bind_sums) {
        publish_sums(g);
        strategy_sums = {};
        compact_sums = half_sums;
        if (half_sums) {
          strategy_sums.input = m.buffer(state_bytes / 2);
          strategy_sums.output = m.buffer(state_bytes / 2);
        } else {
          strategy_sums.bind(m, g.strat, stage_sums);
        }
      }
      refresh_reach = true;
    }
    const bool early = s.sqrt_regret_weights();
    const bool policy_changed = early != early_replay;
    if (policy_changed) {
      early_replay = early;
      replays = {};
    }
    const auto average = s.strategy_average_step(t);
    const AverageMode mode = average.weight == 1.f ? DenseAverage : average.weight == 0.f ? SkipAverage : WeightedAverage;
    write_sums = mode != SkipAverage;
    if (compact_regrets) {
      if (upload_regrets) prepare_regrets(g);
    } else {
      regrets.prepare(g.regrets, upload_regrets);
    }
    if (compact_sums) {
      if (upload_sums) {
        auto* destination = static_cast<uint16_t*>(strategy_sums.input.contents);
        transfer_state(g.strat.data(), destination, g.strat.size(), [](float value) { return fp16_sum_word(value); });
      }
    } else {
      strategy_sums.prepare(g.strat, upload_sums);
    }
    if (upload_regrets) seed_forced_rows(g, regrets, compact_regrets ? sizeof(uint16_t) : sizeof(float));
    if (upload_sums) seed_forced_rows(g, strategy_sums, compact_sums ? sizeof(uint16_t) : sizeof(float));
    auto disc = [](double z) { return float(z / (1 + z)); };
    @autoreleasepool {
      auto cmd = m.command();
      const bool symmetrize = t % 4 == 0 && g.perm_all.size() > 1;
      for (unsigned phase = 0; phase < 2; ++phase) {
        unsigned p = phase ^ s.whole_tree_first_player();
        IterationParams args = {unsigned(g.num_hands(0)),
                                unsigned(g.num_hands(1)),
                                unsigned(stride),
                                p,
                                disc(pow(t, s.params.alpha)),
                                disc(pow(t, s.params.beta_at(t))),
                                average.reset ? 0.f : average.discount,
                                unsigned(s.avg_reset || t > s.avg_delay),
                                phase,
                                average.weight};
        memcpy(params[p].contents, &args, sizeof(args));
        const bool refresh_both = phase == 0 && (refresh_reach || policy_changed);
        auto& plan = replays.plans[mode][regrets.slot][strategy_sums.slot][p][refresh_both];
        if (!plan.commands) plan = record(g, p, refresh_both, mode);
        auto enc = m.encoder(cmd);
        id<MTLResource> read[] = {nodes, node_ids, params[p], cards, boards[p], indices[p], heroes[p], rounds,
                                  bet_check_nodes, two_bet_nodes, lead_check_nodes, allin_batches, equity_tables, equity[p], iso, perms[p],
                                  regrets.input, strategy_sums.input};
        id<MTLResource> write[] = {r[0], r[1], v, regrets.output, strategy_sums.output};
        [enc useResources:read count:sizeof(read) / sizeof(read[0]) usage:MTLResourceUsageRead];
        [enc useResources:write
                    count:sizeof(write) / sizeof(write[0])
                    usage:MTLResourceUsageRead | MTLResourceUsageWrite];
        [enc executeCommandsInBuffer:plan.commands withRange:NSMakeRange(0, plan.count)];
        [enc endEncoding];
        if (phase == 0 && symmetrize) {
          m.finish(cmd);
          symmetrize_output(s, p);
          cmd = m.command();
        }
      }
      [cmd commit];
      return cmd;
    }
  }

  void symmetrize_output(Solver& solver, unsigned p) {
    solver.symmetrize_root_street(p, static_cast<float*>(regrets.output.contents),
                                  static_cast<float*>(strategy_sums.output.contents));
  }

  void finish_iteration(id<MTLCommandBuffer> cmd, GameTree& g, Solver& s, int t) {
    m.wait(cmd);
    if (t % 4 == 0 && g.perm_all.size() > 1) symmetrize_output(s, 1 - s.whole_tree_first_player());
  }

  RootStats evaluate_and_prepare(GameTree& g, Solver& s, int next) override {
    @autoreleasepool {
      const uint16_t* committed = nullptr;
      const uint16_t* committed_sums = nullptr;
      if (compact_regrets == s.uses_bfloat_regrets(next) && compact_sums == s.uses_fp16_sums(next)) {
        if (compact_regrets) committed = static_cast<const uint16_t*>(regrets.input.contents);
        if (compact_sums) committed_sums = static_cast<const uint16_t*>(strategy_sums.input.contents);
      } else {
        publish_for_cpu(g);
      }
      auto cmd = submit(g, s, next, false);
      RootStats stats;
      try {
        stats = committed || committed_sums ? s.root_stats_from_packed(committed, committed_sums) : s.root_stats();
      } catch (...) {
        [cmd waitUntilCompleted];
        throw;
      }
      finish_iteration(cmd, g, s, next);
      prepared_iteration = next;
      return stats;
    }
  }

  void iterate(GameTree& g, Solver& s, int t, bool refresh_reach) override {
    if (refresh_reach || prepared_iteration != t) {
      @autoreleasepool {
        auto cmd = submit(g, s, t, refresh_reach);
        finish_iteration(cmd, g, s, t);
      }
    }
    prepared_iteration = 0;
    if (compact_regrets) {
      std::swap(regrets.input, regrets.output);
      regrets.slot ^= 1;
      regrets_current_on_cpu = false;
    } else {
      regrets.publish(g.regrets);
    }
    if (compact_sums) {
      if (write_sums) {
        std::swap(strategy_sums.input, strategy_sums.output);
        strategy_sums.slot ^= 1;
      }
      sums_current_on_cpu = false;
    } else if (write_sums) {
      strategy_sums.publish(g.strat);
    }
  }
};


// A hybrid solve's device (#589, #582's SubtreeDevice on Apple Silicon): the turn subtrees of a share of the turn
// cards, solved on the GPU in every pass while the CPU walks the others. Memory is shared, so its state is the
// tree's own float32 rows, bound without a copy and updated in place: each subtree's rows are its own, and the
// CPU's walks never touch them. Nothing is uploaded or published, and nothing beyond the GPU's workspace is
// allocated. (Arrays too small to be page-aligned cannot be bound: a small tree's rows are copied to the GPU,
// at the same offsets, and its subtrees' ranges copied back.) Reaches and values are the packed plan's root rows,
// read and written where the GPU has them.
class MetalSubtrees final : public SubtreeDevice {
 public:
  MetalSubtrees(GameTree& tree, uint64_t available, double share) : g(tree) {
    if (g.compressed) throw std::runtime_error("16-bit storage: the GPU's turn subtrees need float32 rows");
    for (uint32_t i = 0; i < g.nodes.size(); ++i) {
      const auto& n = g.nodes[i];
      if (n.kind != ANodeKind::Chance || n.board_len != 3 || g.chances[n.aux].flop_deal) continue;
      const auto& ci = g.chances[n.aux];
      for (int k = 0; k < n.num_children; ++k) {
        // an even spread of the cards: k is the device's when floor((k + 1) * share) passes an integer
        if (std::floor((k + 1) * share) == std::floor(k * share)) continue;
        const uint32_t child = n.first_child + uint32_t(k);
        if (g.nodes[child].kind != ANodeKind::Decision) continue;
        index[child] = int(roots.size());
        roots.push_back({child, ci.cards[k]});
      }
    }
    if (roots.empty()) throw std::runtime_error("no turn subtrees for the GPU");
    // Each subtree's rows are one range of storage (the tree is filled depth first): what a copy brings back.
    for (const auto& [node, card] : roots) {
      size_t lo = SIZE_MAX, hi = 0;
      vector<uint32_t> stack{node};
      while (!stack.empty()) {
        const auto& n = g.nodes[stack.back()];
        stack.pop_back();
        if (n.kind == ANodeKind::Decision && !GameTree::shares_row(n)) {
          lo = std::min<size_t>(lo, n.storage);
          hi = std::max<size_t>(hi, n.storage + size_t(n.num_children) * g.num_hands(n.player));
        }
        if (n.kind == ANodeKind::Decision || n.kind == ANodeKind::Chance)
          for (uint32_t c = 0; c < n.num_children; ++c) stack.push_back(n.first_child + c);
      }
      if (lo < hi) ranges.push_back({lo, hi - lo});
    }
    @autoreleasepool {
      s = std::make_unique<PostflopSolve>(g, available, &roots);
      // The whole of the tree's rows is bound (each share's subtrees are spread through it), so a smaller share
      // would not fit where this does not: the CPU then solves alone.
      const uint64_t rows = (uint64_t(g.regrets.size()) + g.strat.size()) * sizeof(float);
      if (m.dev.currentAllocatedSize + rows > m.dev.recommendedMaxWorkingSetSize)
        throw std::runtime_error("the tree is larger than the GPU's working set");
      bind();
      if (std::getenv("WSOLVER_HYBRID_DEBUG"))
        std::fprintf(stderr, "GPU profile: hybrid, %zu turn subtrees, the tree's rows %s\n", roots.size(),
                     copied ? "copied" : "bound in place");
      check = m.buffer(4 * roots.size() * s->stride * sizeof(float));
    }
  }
  ~MetalSubtrees() override { finish(); }

  int root(uint32_t node) const override {
    auto it = index.find(node);
    return it == index.end() ? -1 : it->second;
  }
  // the plan's root rows (PostflopSolve::pack_nodes): written by the host between passes only
  float* reach(int player, int j) override {
    return static_cast<float*>(s->r[player].contents) + size_t(j) * ((g.num_hands(player) + 31) & ~31u);
  }

  void launch(Solver& solver, int t, int phase, int p) override {
    @autoreleasepool {
      const bool early = solver.sqrt_regret_weights();
      if (early != s->early_replay) {
        s->early_replay = early;
        s->replays = {};
        refresh_reach = true;
      }
      const auto average = solver.strategy_average_step(t);
      const AverageMode mode = average.weight == 1.f ? DenseAverage : average.weight == 0.f ? SkipAverage : WeightedAverage;
      auto disc = [](double z) { return float(z / (1 + z)); };
      IterationParams args = {unsigned(g.num_hands(0)),
                              unsigned(g.num_hands(1)),
                              unsigned(s->stride),
                              unsigned(p),
                              disc(std::pow(t, solver.params.alpha)),
                              disc(std::pow(t, solver.params.beta_at(t))),
                              average.reset ? 0.f : average.discount,
                              unsigned(solver.avg_reset || t > solver.avg_delay),
                              unsigned(phase),
                              average.weight};
      memcpy(s->params[p].contents, &args, sizeof(args));
      // p's own reach below the roots is the opponent's last pass's, when that came last: p's strategy has not
      // changed since (#599, as the whole-tree solve). Both after setup, an upload, a report or a new policy.
      const bool both = refresh_reach || p == last_player;
      refresh_reach = false;
      last_player = p;
      auto& plan = s->replays.plans[mode][0][0][p][both];
      if (!plan.commands) plan = s->record(g, unsigned(p), both, mode);
      auto cmd = m.command();
      encode(cmd, unsigned(p), plan);
      [cmd commit];
      std::lock_guard<std::mutex> lock(wait_mu);
      pending = cmd;
      pending_pass = true;
      device_newer = true;
    }
  }
  const float* values(int j) override {
    finish();
    return static_cast<const float*>(s->v.contents) + size_t(j) * s->stride;
  }
  void launch_check() override {
    @autoreleasepool {
      const bool early = s->early_replay;
      s->early_replay = false;  // the average strategy is the sums' plain proportions
      auto cmd = m.command();
      const size_t rows = roots.size() * s->stride * sizeof(float);
      for (unsigned p = 0; p < 2; ++p) {
        IterationParams q{};
        q.hands0 = unsigned(g.num_hands(0));
        q.hands1 = unsigned(g.num_hands(1));
        q.stride = unsigned(s->stride);
        q.p = p;
        q.positive_discount = q.negative_discount = q.average_discount = q.sample_weight = 1.f;
        memcpy(s->params[p].contents, &q, sizeof(q));
        for (unsigned mode = 1; mode <= 2; ++mode) {
          auto& plan = checks[p][mode - 1];
          if (!plan.commands) plan = s->record(g, p, true, SkipAverage, mode);
          encode(cmd, p, plan);
          auto blit = [cmd blitCommandEncoder];
          [blit copyFromBuffer:s->v sourceOffset:0 toBuffer:check destinationOffset:(2 * p + mode - 1) * rows size:rows];
          [blit endEncoding];
        }
      }
      s->early_replay = early;
      refresh_reach = true;  // the reaches below the roots are the average strategy's now
      [cmd commit];
      std::lock_guard<std::mutex> lock(wait_mu);
      pending = cmd;
      pending_pass = false;
    }
  }
  const float* check_values(int p, bool best_response, int j) override {
    finish();
    return static_cast<const float*>(check.contents) + (size_t(2 * p + best_response) * roots.size() + j) * s->stride;
  }
  void finish() override {
    std::lock_guard<std::mutex> lock(wait_mu);
    if (!pending) return;
    id<MTLCommandBuffer> cmd = pending;
    pending = nil;
    m.wait(cmd);
    const double seconds = cmd.GPUEndTime - cmd.GPUStartTime;  // the first command buffer can report 0 as its start
    if (pending_pass && cmd.GPUStartTime > 0 && seconds > 0 && seconds < 60) passes += seconds;
  }
  bool times_passes() const override { return true; }
  double pass_seconds() override {
    finish();
    std::lock_guard<std::mutex> lock(wait_mu);
    const double seconds = passes;
    passes = 0;
    return seconds;
  }
  // Bound rows are the tree's: nothing to copy either way, unless a command between iterations gave the tree
  // new arrays, which are bound again. Copied rows go back as the GPU's subtrees' ranges, and up whole.
  void publish(GameTree&) override {
    finish();
    if (!copied || !device_newer) return;
    for (auto [start, count] : ranges) {
      memcpy(g.regrets.data() + start, static_cast<const float*>(s->regrets.input.contents) + start, count * sizeof(float));
      memcpy(g.strat.data() + start, static_cast<const float*>(s->strategy_sums.input.contents) + start, count * sizeof(float));
    }
    device_newer = false;
  }
  void upload(const GameTree&) override {
    finish();
    refresh_reach = true;
    if (g.regrets.data() != bound[0] || g.strat.data() != bound[1] || g.regrets.size() != bound_floats) {
      if (g.compressed) throw std::runtime_error("16-bit storage: the GPU's turn subtrees need float32 rows");
      @autoreleasepool {
        bind();
      }
    } else if (copied) {
      copy_up();
    }
  }

 private:
  void bind() {
    auto regrets = MetalRows::shared(m, g.regrets), sums = MetalRows::shared(m, g.strat);
    copied = !regrets || !sums;
    if (copied) {
      regrets = m.buffer(g.regrets.size() * sizeof(float));
      sums = m.buffer(g.strat.size() * sizeof(float));
    }
    s->regrets.input = s->regrets.output = regrets;
    s->strategy_sums.input = s->strategy_sums.output = sums;
    if (copied) copy_up();
    s->replays = {};
    refresh_reach = true;
    for (auto& p : checks)
      for (auto& plan : p) plan = {};
    bound[0] = g.regrets.data();
    bound[1] = g.strat.data();
    bound_floats = g.regrets.size();
  }
  void copy_up() {
    if (!g.regrets.empty()) memcpy(s->regrets.input.contents, g.regrets.data(), g.regrets.size() * sizeof(float));
    if (!g.strat.empty()) memcpy(s->strategy_sums.input.contents, g.strat.data(), g.strat.size() * sizeof(float));
    device_newer = false;
  }
  void encode(id<MTLCommandBuffer> cmd, unsigned p, const PostflopSolve::Replay& plan) {
    auto enc = m.encoder(cmd);
    id<MTLResource> read[] = {s->nodes, s->node_ids, s->params[p], s->cards, s->boards[p], s->indices[p], s->heroes[p],
                              s->rounds, s->bet_check_nodes, s->two_bet_nodes, s->lead_check_nodes, s->allin_batches,
                              s->equity_tables, s->equity[p], s->iso, s->perms[p]};
    id<MTLResource> write[] = {s->r[0], s->r[1], s->v, s->regrets.input, s->strategy_sums.input};
    [enc useResources:read count:sizeof(read) / sizeof(read[0]) usage:MTLResourceUsageRead];
    [enc useResources:write count:sizeof(write) / sizeof(write[0]) usage:MTLResourceUsageRead | MTLResourceUsageWrite];
    [enc executeCommandsInBuffer:plan.commands withRange:NSMakeRange(0, plan.count)];
    [enc endEncoding];
  }

  Metal& m = metal();
  GameTree& g;
  std::unique_ptr<PostflopSolve> s;
  vector<std::pair<unsigned, unsigned>> roots;  // node, the turn card dealt to it
  std::unordered_map<uint32_t, int> index;
  PostflopSolve::Replay checks[2][2];  // [player][EV, best response]
  id<MTLBuffer> check;                 // their root rows: [player][EV, best response][root]
  const float* bound[2] = {nullptr, nullptr};
  size_t bound_floats = 0;
  vector<std::pair<size_t, size_t>> ranges;  // the GPU's subtrees' rows in storage: start, floats
  bool copied = false;                       // the rows are a copy (the tree's arrays could not be bound)
  bool device_newer = false;                 // a copy has iterations the tree lacks
  bool refresh_reach = true;                 // the next pass propagates both players' reaches
  int last_player = -1;                      // the player of the last solving pass
  std::mutex wait_mu;
  id<MTLCommandBuffer> pending = nil;
  bool pending_pass = false;  // pending is a solving pass (not a report)
  double passes = 0;          // the GPU's time for the solving passes since pass_seconds
};
}  // namespace

bool metal_available() {
  @autoreleasepool {
    return [MTLCreateSystemDefaultDevice() supportsFamily:MTLGPUFamilyApple7];
  }
}

std::unique_ptr<MetalSolve> make_metal_solve(GameTree& tree, uint64_t available) {
  @autoreleasepool {
    return std::make_unique<PostflopSolve>(tree, available);
  }
}
std::unique_ptr<SubtreeDevice> make_gpu_subtrees(GameTree& tree, uint64_t available, double share) {
  return std::make_unique<MetalSubtrees>(tree, available, share);
}
void metal_flop_table(const GameTree& g, AllinRunouts& ar, const std::vector<std::array<uint32_t, 4>>& runout_boards) {
  @autoreleasepool {
    auto& m = metal();
    static auto rank = m.pipeline(@"rankhands"), count = m.pipeline(@"countpairs");
    const bool compact = g.compact_tables();
    const unsigned H = compact ? ar.live.size() : g.num_hands(0);
    const unsigned O = compact ? H : g.num_hands(1);
    const unsigned B = runout_boards.size(), Bp = (B + 31) & ~31u;
    const unsigned ip_offset = compact ? 0 : (H + 3) & ~3u;
    if (!H || !O || (compact && H < 2)) return;
    const size_t entries = compact ? size_t(H) * (H - 1) / 2 : size_t(H) * O;
    const unsigned hand_rows = ip_offset + O;
    auto hands = m.buffer(hand_rows * sizeof(array<uint8_t, 2>));
    auto* hand_cards = static_cast<array<uint8_t, 2>*>(hands.contents);
    for (unsigned i = 0; i < H; ++i) {
      unsigned h = compact ? ar.live[i] : i;
      hand_cards[i] = {g.hcard[0][0][h], g.hcard[0][1][h]};
    }
    if (!compact) {
      for (unsigned i = H; i < ip_offset; ++i) hand_cards[i] = hand_cards[0];
      for (unsigned o = 0; o < O; ++o) hand_cards[ip_offset + o] = {g.hcard[1][0][o], g.hcard[1][1][o]};
    }
    auto boards = m.buffer(runout_boards.size() * sizeof(runout_boards.front()));
    memcpy(boards.contents, runout_boards.data(), boards.length);
    const unsigned padded_hands = (hand_rows + 3) & ~3u;
    auto ranks = m.buffer(size_t(padded_hands) * Bp * sizeof(uint32_t)), result = m.buffer(entries * sizeof(uint16_t));
    unsigned dims[] = {hand_rows, B, Bp, 0};
    auto cmd = m.command();
    auto enc = m.encoder(cmd);
    [enc setComputePipelineState:rank];
    [enc setBuffer:boards offset:0 atIndex:0];
    [enc setBuffer:hands offset:0 atIndex:1];
    [enc setBuffer:ranks offset:0 atIndex:2];
    [enc setBytes:dims length:sizeof(dims) atIndex:3];
    [enc dispatchThreads:MTLSizeMake(Bp, padded_hands, 1) threadsPerThreadgroup:MTLSizeMake(32, 4, 1)];
    [enc memoryBarrierWithScope:MTLBarrierScopeBuffers];
    [enc setComputePipelineState:count];
    [enc setBuffer:ranks offset:0 atIndex:0];
    [enc setBuffer:result offset:0 atIndex:2];
    unsigned pair_dims[] = {H, O, Bp, ip_offset};
    int center = compact ? ar.valid : 0;
    [enc setBytes:pair_dims length:sizeof(pair_dims) atIndex:3];
    [enc setBytes:&center length:sizeof(center) atIndex:4];
    [enc dispatchThreadgroups:MTLSizeMake((O + 15) / 16, (H + 3) / 4, 1) threadsPerThreadgroup:MTLSizeMake(128, 1, 1)];
    [enc endEncoding];
    m.finish(cmd);
    if (compact) {
      ar.tri16.resize(entries);
      memcpy(ar.tri16.data(), result.contents, result.length);
    } else {
      ar.e2.resize(entries);
      memcpy(ar.e2.data(), result.contents, result.length);
    }
  }
}
}  // namespace pio
