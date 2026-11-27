#include "metal.hpp"
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
  id<MTLLibrary> library;
  Metal() {
    if (!queue) throw std::runtime_error("Metal command queue unavailable");
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
    library = [dev newLibraryWithSource:[NSString stringWithUTF8String:kernel_source] options:options error:&error];
    if (!library) fail(error);
  }
  [[noreturn]] static void fail(NSError* error) {
    throw std::runtime_error(error ? error.localizedDescription.UTF8String : "Metal command failed");
  }
  id<MTLComputePipelineState> pipeline(NSString* name, bool replay = false) {
    NSError* error = nil;
    auto descriptor = [MTLComputePipelineDescriptor new];
    descriptor.computeFunction = [library newFunctionWithName:name];
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
  id<MTLCommandBuffer> command(id<MTLCommandQueue> queue) {
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
    } else if (!current.empty()) {
      memcpy(current.data(), output.contents, current.size() * sizeof(float));
    }
  }
};

struct SumCheckpoint {
  id<MTLComputePipelineState> pipeline;
  id<MTLBuffer> masks, offsets, literals, used;
  uint32_t count = 0, capacity = 0;

  static size_t groups(size_t words) { return (words + 1023) / 1024; }
  static size_t directory_bytes(size_t words) { return (33 * groups(words) + 1) * sizeof(uint32_t); }

  void allocate(Metal& m, size_t words, size_t budget) {
    count = uint32_t(words);
    capacity = uint32_t((budget - directory_bytes(words)) / sizeof(uint32_t));
    pipeline = m.pipeline(@"checkpoint_sums");
    masks = m.buffer(32 * groups(words) * sizeof(uint32_t));
    offsets = m.buffer(groups(words) * sizeof(uint32_t));
    literals = m.buffer(size_t(capacity) * sizeof(uint32_t));
    used = m.buffer(sizeof(uint32_t));
  }

  void capture(Metal& m, id<MTLCommandQueue> queue, id<MTLBuffer> source) {
    *static_cast<uint32_t*>(used.contents) = 0;
    auto cmd = m.command(queue);
    auto enc = m.encoder(cmd);
    [enc setComputePipelineState:pipeline];
    id<MTLBuffer> buffers[] = {source, masks, offsets, literals, used};
    for (unsigned i = 0; i < 5; ++i) [enc setBuffer:buffers[i] offset:0 atIndex:i];
    const uint32_t limits[] = {count, capacity};
    [enc setBytes:limits length:sizeof(limits) atIndex:5];
    [enc dispatchThreadgroups:MTLSizeMake(groups(count), 1, 1) threadsPerThreadgroup:MTLSizeMake(128, 1, 1)];
    [enc endEncoding];
    m.finish(cmd);
    if (*static_cast<uint32_t*>(used.contents) > capacity) throw std::bad_alloc();
  }

  void restore(float* sums) const {
    const auto* bits = static_cast<const uint32_t*>(masks.contents);
    const auto* positions = static_cast<const uint32_t*>(offsets.contents);
    const auto* words = static_cast<const uint32_t*>(literals.contents);
    std::fill_n(sums, count, 0.0f);
    for (size_t g = 0; g < groups(count); ++g) {
      uint32_t pos = positions[g];
      for (unsigned k = 0; k < 32; ++k)
        for (uint32_t mask = bits[g * 32 + k]; mask; mask &= mask - 1)
          sums[g * 1024 + k * 32 + std::countr_zero(mask)] = std::bit_cast<float>(words[pos++]);
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
  id<MTLCommandQueue> queue = [m.dev newCommandQueue];
  id<MTLComputePipelineState> propagate_reach, update_values, advance_forced, terminals, river_pairs, river_rounds,
      allins, wide_rounds, bet_checks, two_bets, wide_two_bets, lead_checks, wide_lead_checks;
  id<MTLBuffer> nodes, node_ids, r[2], v, cards, iso, perms[2], boards[2], indices[2], heroes[2], rounds;
  id<MTLBuffer> equity[2], equity_tables, allin_batches, bet_check_nodes, two_bet_nodes, lead_check_nodes;
  MetalRows regrets, strategy_sums;
  struct Range {
    size_t start, count;
  };
  vector<Range> levels;
  vector<array<Range, 2>> reach_levels;
  Range terminal_range, pair_range, round_range, bet_check_range, two_bet_range, lead_check_range, allin_range, forced_range[2];
  size_t stride, state_bytes, round_bytes[2]{};
  bool stage_regrets = false, stage_sums = false;
  size_t checkpoint_bytes = 0;
  SumCheckpoint checkpoint;
  int prepared_iteration = 0;
  struct Replay {
    id<MTLIndirectCommandBuffer> commands;
    NSUInteger count = 0;
  };
  Replay replays[2][2][2];
  id<MTLBuffer> params[2];
  unsigned state_slot = 0;
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
    if (peak > available || peak > limit) {
      const uint64_t reduced = peak - state_bytes;
      if (g.iterations > 0 && available > reduced && limit > peak)
        checkpoint_bytes = std::min(available - reduced, limit - peak);
      if (checkpoint_bytes <= SumCheckpoint::directory_bytes(g.storage_floats))
        throw std::runtime_error("Metal workspace exceeds available memory");
      stage_regrets = peak + checkpoint_bytes + state_bytes > limit;
      return;
    }
    // Zero-copy rows also bind the published CPU arrays for the duration of each command.
    stage_sums = peak + 2 * state_bytes > limit;
    stage_regrets = peak + state_bytes > limit;
  }
  explicit PostflopSolve(GameTree& g, uint64_t available) {
    if (!queue) throw std::bad_alloc();
    stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    state_bytes = g.storage_floats * sizeof(float);
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
    pack_nodes(g, available, equity_bytes);
    pack_hands(g);
    if (!levels.empty()) {
      propagate_reach = m.pipeline(@"propagate_reach", true);
      update_values = m.pipeline(@"update_values", true);
    }
    if (forced_range[0].count || forced_range[1].count) advance_forced = m.pipeline(@"advance_forced", true);
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
    regrets.bind(m, g.regrets, stage_regrets);
    if (checkpoint_bytes) {
      strategy_sums.input = strategy_sums.output = MetalRows::shared(m, g.strat);
      if (!strategy_sums.input) throw std::runtime_error("Metal cannot share the published strategy storage");
      checkpoint.allocate(m, g.strat.size(), checkpoint_bytes);
    } else {
      strategy_sums.bind(m, g.strat, stage_sums);
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

  void pack_nodes(const GameTree& g, uint64_t available, uint64_t equity_bytes) {
    const size_t N = g.nodes.size();
    vector<TraversalNode> packed_nodes(N);
    vector<uint8_t> covered(N);
    vector<RiverRound> betting_rounds, final_bets;
    vector<PairedRiverRound> lead_check_rounds;
    vector<TwoBetRiver> two_bet_rivers;
    const size_t channel_bytes = round_channel_bytes(std::max(g.num_hands(0), g.num_hands(1)));
    const bool rounds_fit = channel_bytes <= m.dev.maxThreadgroupMemoryLength;
    const bool bet_checks_fit = 2 * channel_bytes <= m.dev.maxThreadgroupMemoryLength;
    vector<unsigned> list, frontier{0}, terminal_ids, paired_ids, allin_ids;
    array<vector<unsigned>, 2> forced;
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
        const bool skipped = covered[i];
        bool table = false;
        if (!skipped && n.kind == ANodeKind::Chance && showdown[i] != UINT32_MAX) {
          auto found = outcomes.find(g.chances[n.aux].board_mask);
          if (found != outcomes.end()) {
            table = true;
            a.kind = AllIn;
            a.aux = found->second;
            auto pay = g.term_pay(g.nodes[showdown[i]]);
            std::copy_n(pay.w, 2, a.win);
            std::copy_n(pay.l, 2, a.loss);
            allin_ids.push_back(i);
          }
        }
        if ((skipped || table) && n.kind == ANodeKind::Chance) {
          for (unsigned k = 0; k < a.count; ++k) {
            auto& child = packed_nodes[a.child + k];
            covered[a.child + k] = true;
            child.public_cards = (a.public_cards << 8) | (g.chances[n.aux].cards[k] + 1);
            std::copy_n(a.reach_offset, 2, child.reach_offset);
            next.push_back(a.child + k);
          }
          continue;
        }
        if (skipped && n.kind == ANodeKind::Showdown) continue;
        if (n.kind == ANodeKind::Decision || n.kind == ANodeKind::Chance) {
          a.kind = n.kind == ANodeKind::Chance ? Deal : Decision;
          const bool single = a.kind == Decision && a.count == 1;
          if (single)
            forced[a.actor].push_back(i);
          else
            list.push_back(i);
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
            covered[a.child + k] = skipped;
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
    for (int p = 0; p < 2; ++p) {
      forced_range[p] = {list.size(), forced[p].size()};
      list.insert(list.end(), forced[p].begin(), forced[p].end());
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

  Replay record(const GameTree& g, unsigned p, bool refresh_reach) {
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
    auto dispatch = [&](id<MTLComputePipelineState> pipe, Range span, size_t scratch = 0, unsigned y = 1) {
      if (!span.count) return;
      auto c = [plan.commands indirectComputeCommandAtIndex:plan.count++];
      [c setComputePipelineState:pipe];
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
    dispatch(advance_forced, forced_range[p]);
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

  id<MTLCommandBuffer> submit(GameTree& g, Solver& s, int t, bool refresh_reach) {
    regrets.prepare(g.regrets, refresh_reach);
    if (!checkpoint_bytes) strategy_sums.prepare(g.strat, refresh_reach);
    auto disc = [](double z) { return float(z / (1 + z)); };
    @autoreleasepool {
      auto cmd = m.command(queue);
      const bool symmetrize = t % 4 == 0 && g.perm_all.size() > 1;
      for (unsigned phase = 0; phase < 2; ++phase) {
        unsigned p = phase ^ s.first_player;
        IterationParams args = {unsigned(g.num_hands(0)),
                                unsigned(g.num_hands(1)),
                                unsigned(stride),
                                p,
                                disc(pow(t, s.params.alpha)),
                                disc(pow(t, s.params.beta_at(t))),
                                float(pow(t / (t + 1.), s.params.gamma)),
                                unsigned(s.avg_reset || t > s.avg_delay),
                                phase};
        if (s.avg_reset && s.avg_delay > 0 && t == s.avg_delay + 1) args.average_discount = 0.f;
        memcpy(params[p].contents, &args, sizeof(args));
        auto& plan = replays[state_slot][p][phase == 0 && refresh_reach];
        if (!plan.commands) plan = record(g, p, phase == 0 && refresh_reach);
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
          cmd = m.command(queue);
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
    if (t % 4 == 0 && g.perm_all.size() > 1) symmetrize_output(s, 1 - s.first_player);
  }

  RootStats evaluate_and_prepare(GameTree& g, Solver& s, int next) override {
    if (checkpoint_bytes) return s.root_stats();
    @autoreleasepool {
      auto cmd = submit(g, s, next, false);
      RootStats stats;
      try {
        stats = s.root_stats();
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
        if (checkpoint_bytes) checkpoint.capture(m, queue, strategy_sums.input);
        try {
          auto cmd = submit(g, s, t, refresh_reach);
          finish_iteration(cmd, g, s, t);
        } catch (...) {
          if (checkpoint_bytes) checkpoint.restore(g.strat.data());
          throw;
        }
      }
    }
    prepared_iteration = 0;
    regrets.publish(g.regrets);
    if (!checkpoint_bytes) strategy_sums.publish(g.strat);
    state_slot ^= 1;
  }
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
    auto cmd = m.command(m.queue);
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
