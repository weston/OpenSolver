#include "metal.hpp"
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <malloc/malloc.h>
#include <unistd.h>
#include <algorithm>
#include <array>
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
    if (cmd.status != MTLCommandBufferStatusCompleted) fail(cmd.error);
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

  void bind(Metal& metal, ZVec<float>& current) {
    input = shared(metal, current);
    if (input) {
      staging.resize(current.size());
      output = shared(metal, staging);
      if (output) return;
      ZVec<float>().swap(staging);
    }
    input = output = metal.buffer(current.size() * sizeof(float));
  }

  void prepare(const ZVec<float>& current) {
    if (input == output && !current.empty()) memcpy(input.contents, current.data(), current.size() * sizeof(float));
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

struct BoardTables {
  vector<BoardPrefixes> boards;
  vector<uint16_t> indices;
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
      unsigned hero_start = 0, opp_start = 0;
      for (auto [hero_end, opp_end] : sd.groups[p]) {
        for (unsigned j = hero_start; j < hero_end; ++j) {
          auto h = sd.ent[p][j];
          auto& m = heroes[b.hand_offset + h.idx];
          m.same = h.same;
          unsigned c[3] = {0, 1u + h.c1, 1u + h.c2};
          for (int k = 0; k < 3; ++k) {
            m.lo[k] = position[c[k]];
            m.end[k] = b.start[c[k] + 1] - 1;
          }
        }
        for (unsigned j = opp_start; j < opp_end; ++j) {
          auto o = opponents[j];
          entries[++position[0]] = o.idx;
          entries[++position[1 + o.c1]] = o.idx;
          entries[++position[1 + o.c2]] = o.idx;
        }
        for (unsigned j = hero_start; j < hero_end; ++j) {
          auto h = sd.ent[p][j];
          auto& m = heroes[b.hand_offset + h.idx];
          m.hi[0] = position[0];
          m.hi[1] = position[1 + h.c1];
          m.hi[2] = position[1 + h.c2];
        }
        hero_start = hero_end;
        opp_start = opp_end;
      }
    });
  }
};
class PostflopSolve final : public MetalSolve {
 public:
  Metal& m = metal();
  id<MTLComputePipelineState> propagate_reach, update_values, advance_forced, terminals, river_pairs, river_rounds,
      allins, wide_rounds;
  id<MTLBuffer> nodes, node_ids, r[2], v, cards, iso, perms[2], boards[2], indices[2], heroes[2], equity[2], offsets[2];
  id<MTLBuffer> rounds;
  MetalRows regrets, strategy_sums;
  struct Range {
    size_t start, count;
  };
  vector<Range> levels;
  vector<array<Range, 2>> reach_levels;
  Range terminal_range, pair_range, round_range, allin_range, forced_range[2];
  size_t stride, state_bytes;
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
  void check_workspace(const GameTree& g, uint64_t available, uint64_t value_rows, uint64_t reach_rows,
                       uint64_t mappings, uint64_t rounds) const {
    const size_t N = g.nodes.size();
    const size_t pairs = size_t(g.num_hands(0)) * g.num_hands(1);
    uint64_t hands = g.num_hands(0) + g.num_hands(1);
    uint64_t boardBytes =
        (g.showdowns.size() + 1) *
        (2 * sizeof(BoardPrefixes) + hands * (sizeof(HandPrefixes) + 3 * sizeof(uint16_t)) + 106 * sizeof(uint16_t));
    uint64_t equityBytes = 2 * g.allins.size() * pairs * sizeof(float);
    uint64_t workspace = (value_rows + reach_rows) * stride * sizeof(float) + 2 * state_bytes;
    uint64_t peak = workspace +
                    2 * (boardBytes + equityBytes + N * (sizeof(TraversalNode) + 4 * sizeof(unsigned)) +
                         mappings * 2 * sizeof(unsigned) + rounds * sizeof(RiverRound)) +
                    128 * stride;
    if (mappings > UINT32_MAX || (g.showdowns.size() + 1) * (3 * stride + 53) > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal board index limits");
    if (peak > available || peak > m.dev.recommendedMaxWorkingSetSize)
      throw std::runtime_error("Metal workspace exceeds available memory");
  }
  explicit PostflopSolve(GameTree& g, uint64_t available) {
    propagate_reach = m.pipeline(@"propagate_reach", true);
    update_values = m.pipeline(@"update_values", true);
    advance_forced = m.pipeline(@"advance_forced", true);
    river_rounds = m.pipeline(@"river_betting_round", true);
    river_pairs = m.pipeline(@"river_call_fold", true);
    terminals = m.pipeline(@"terminal", true);
    allins = m.pipeline(@"allin", true);
    stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    state_bytes = g.storage_floats * sizeof(float);
    const uint64_t indices = uint64_t(g.num_hands(0)) * g.num_hands(1) * g.allins.size();
    if (!g.num_hands(0) || !g.num_hands(1) || g.nodes.size() * stride > UINT32_MAX || g.storage_floats > UINT32_MAX ||
        indices > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal index limits");
    if (2 * g.nodes.size() * sizeof(TraversalNode) > available)
      throw std::runtime_error("Metal workspace exceeds available memory");
    pack_nodes(g, available);
    const size_t round_bytes = 3 * round_channel_bytes(std::max(g.num_hands(0), g.num_hands(1)));
    if (round_range.count && round_bytes > m.dev.maxThreadgroupMemoryLength)
      wide_rounds = m.pipeline(@"river_betting_round_wide", true);
    regrets.bind(m, g.regrets);
    strategy_sums.bind(m, g.strat);
    pack_hands(g);
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
      boards[p] = copy_buffer(b.boards);
      indices[p] = copy_buffer(b.indices);
      heroes[p] = copy_buffer(b.heroes);
      const int H = g.num_hands(p), O = g.num_hands(1 - p);
      vector<float> equities(g.allins.size() * H * O);
      vector<unsigned> equity_offsets(g.allins.size());
      pool().parallel_for(int(g.allins.size()), [&](int k) {
        const auto& ar = g.allins[k];
        const size_t offset = size_t(k) * H * O;
        equity_offsets[k] = offset;
        uint64_t board = 0;
        for (auto c : ar.cards) board |= card_bit(c);
        for (int h = 0; h < H; ++h) {
          uint64_t hero = combo_mask(g.hands[p][h]);
          for (int o = 0; o < O; ++o) {
            uint64_t opp = combo_mask(g.hands[1 - p][o]);
            size_t j = p ? size_t(o) * H + h : size_t(h) * O + o;
            float e = (ar.e2.empty() ? ar.e2b[j] : ar.e2[j]) * .5f * ar.scale;
            equities[offset + size_t(h) * O + o] = (hero & opp) || ((hero | opp) & board) ? -1.f : (p ? 1.f - e : e);
          }
        }
      });
      equity[p] = copy_buffer(equities);
      offsets[p] = copy_buffer(equity_offsets);
    }
  }

  static bool pack_river_round(const GameTree& g, unsigned i, vector<RiverRound>& rounds) {
    const auto& n = g.nodes[i];
    if (n.kind != ANodeKind::Decision || n.board_len != 5) return false;
    if (n.num_children == 3) {
      unsigned raised = n.first_child, called = raised + 1, folded = raised + 2;
      const auto& response = g.nodes[raised];
      if (response.kind != ANodeKind::Decision || response.num_children != 2 || response.player == n.player ||
          g.nodes[called].kind != ANodeKind::Showdown || g.nodes[folded].kind != ANodeKind::Fold ||
          g.nodes[response.first_child].kind != ANodeKind::Showdown ||
          g.nodes[response.first_child + 1].kind != ANodeKind::Fold)
        return false;
      RiverRound r{};
      r.root = i;
      r.final_raise = 1;
      r.board = g.nodes[called].aux;
      r.storage[0] = n.storage;
      r.storage[1] = response.storage;
      for (unsigned j = 0; j < 2; ++j) {
        unsigned call = j ? called : response.first_child;
        const auto& pay = g.term_pay(g.nodes[call]);
        std::copy_n(pay.w, 2, r.win[j]);
        std::copy_n(pay.l, 2, r.loss[j]);
        std::copy_n(pay.t, 2, r.tie[j]);
        std::copy_n(g.term_pay(g.nodes[call + 1]).w, 2, r.fold[j]);
      }
      rounds.push_back(r);
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
    RiverRound r{};
    r.root = i;
    r.board = g.nodes[last].aux;
    unsigned decisions[] = {i, lead, check, bet}, leaves[] = {call0, call1, last};
    for (int j = 0; j < 4; ++j) r.storage[j] = g.nodes[decisions[j]].storage;
    for (int j = 0; j < 3; ++j) {
      const auto& pay = g.term_pay(g.nodes[leaves[j]]);
      std::copy_n(pay.w, 2, r.win[j]);
      std::copy_n(pay.l, 2, r.loss[j]);
      std::copy_n(pay.t, 2, r.tie[j]);
      if (j < 2) std::copy_n(g.term_pay(g.nodes[leaves[j] + 1]).w, 2, r.fold[j]);
    }
    rounds.push_back(r);
    return true;
  }

  static size_t round_channel_bytes(unsigned hands) { return (4 * hands + 53) * sizeof(float); }

  void pack_nodes(const GameTree& g, uint64_t available) {
    const size_t N = g.nodes.size();
    vector<TraversalNode> packed_nodes(N);
    vector<uint8_t> covered(N);
    vector<RiverRound> betting_rounds;
    const bool rounds_fit =
        round_channel_bytes(std::max(g.num_hands(0), g.num_hands(1))) <= m.dev.maxThreadgroupMemoryLength;
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
    unsigned next_row[2] = {1, 1}, value_rows = 1;
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
        if (rounds_fit && pack_river_round(g, i, betting_rounds)) continue;
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
            std::copy_n(a.row, 2, child.row);
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
          a.child_value = single ? a.value_row : value_rows;
          if (!single) value_rows += a.count;
          for (unsigned k = 0; k < a.count; ++k) {
            covered[a.child + k] = skipped;
            packed_nodes[a.child + k].public_cards =
                a.kind == Deal ? (a.public_cards << 8) | (packed_nodes[a.child + k].card + 1) : a.public_cards;
            packed_nodes[a.child + k].value_row = a.child_value + k;
            for (int p = 0; p < 2; ++p)
              packed_nodes[a.child + k].row[p] =
                  (a.kind == Decision && a.actor == unsigned(p) && a.count > 1) ? next_row[p]++ : a.row[p];
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
    allin_range = {list.size(), allin_ids.size()};
    list.insert(list.end(), allin_ids.begin(), allin_ids.end());
    check_workspace(g, available, value_rows, size_t(next_row[0]) + next_row[1], deals_by_node.size(),
                    betting_rounds.size());
    for (int p = 0; p < 2; ++p) {
      r[p] = m.buffer(next_row[p] * stride * 4);
      memcpy(r[p].contents, g.init_reach[p].data(), g.num_hands(p) * 4);
    }
    v = m.buffer(size_t(value_rows) * stride * sizeof(float));
    round_range = {0, betting_rounds.size()};
    rounds = copy_buffer(betting_rounds);
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
                                                  maxCommandCount:reach_levels.size() + levels.size() + 5
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
    dispatch(terminals, terminal_range, (3 * O + 53) * sizeof(float));
    dispatch(river_pairs, pair_range, (5 * O + 106) * sizeof(float));
    buffers[14] = rounds;
    const size_t channel_bytes = round_channel_bytes(O);
    if (channel_bytes * 3 <= m.dev.maxThreadgroupMemoryLength)
      dispatch(river_rounds, round_range, channel_bytes * 3);
    else
      dispatch(wide_rounds, round_range, channel_bytes);
    buffers[9] = offsets[p];
    buffers[10] = equity[p];
    dispatch(allins, allin_range, 0, (g.num_hands(p) + 3) / 4);
    buffers[9] = iso;
    buffers[10] = perms[p];
    for (auto it = levels.rbegin(); it != levels.rend(); ++it) dispatch(update_values, *it);
    return plan;
  }

  id<MTLCommandBuffer> submit(GameTree& g, Solver& s, int t, bool refresh_reach) {
    regrets.prepare(g.regrets);
    strategy_sums.prepare(g.strat);
    auto disc = [](double z) { return float(z / (1 + z)); };
    @autoreleasepool {
      auto cmd = m.command();
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
        id<MTLResource> read[] = {nodes,  node_ids,   params[p], cards, boards[p], indices[p],    heroes[p],
                                  rounds, offsets[p], equity[p], iso,   perms[p],  regrets.input, strategy_sums.input};
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
    if (t % 4 == 0 && g.perm_all.size() > 1) symmetrize_output(s, 1 - s.first_player);
  }

  RootStats evaluate_and_prepare(GameTree& g, Solver& s, int next) override {
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
        auto cmd = submit(g, s, t, refresh_reach);
        finish_iteration(cmd, g, s, t);
      }
    }
    prepared_iteration = 0;
    regrets.publish(g.regrets);
    strategy_sums.publish(g.strat);
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
    unsigned H = ar.live.size(), B = runout_boards.size(), Bp = (B + 31) & ~31;
    if (H < 2) return;
    std::vector<std::array<uint8_t, 2>> live_hands;
    live_hands.reserve(H);
    for (uint16_t h : ar.live) live_hands.push_back({g.hcard[0][0][h], g.hcard[0][1][h]});
    ar.tri16.resize(H * (H - 1) / 2);
    auto boards = m.buffer(runout_boards.size() * sizeof(runout_boards.front()));
    auto hands = m.buffer(live_hands.size() * sizeof(live_hands.front()));
    memcpy(boards.contents, runout_boards.data(), boards.length);
    memcpy(hands.contents, live_hands.data(), hands.length);
    auto ranks = m.buffer(((H + 3) & ~3) * Bp * 4), result = m.buffer(ar.tri16.size() * 2);
    unsigned dims[] = {H, B, Bp, unsigned(ar.valid)};
    auto cmd = m.command();
    auto enc = m.encoder(cmd);
    [enc setComputePipelineState:rank];
    [enc setBuffer:boards offset:0 atIndex:0];
    [enc setBuffer:hands offset:0 atIndex:1];
    [enc setBuffer:ranks offset:0 atIndex:2];
    [enc setBytes:dims length:sizeof(dims) atIndex:3];
    [enc dispatchThreads:MTLSizeMake(Bp, (H + 3) & ~3, 1) threadsPerThreadgroup:MTLSizeMake(32, 4, 1)];
    [enc memoryBarrierWithScope:MTLBarrierScopeBuffers];
    [enc setComputePipelineState:count];
    [enc setBuffer:ranks offset:0 atIndex:0];
    [enc setBuffer:result offset:0 atIndex:2];
    [enc dispatchThreadgroups:MTLSizeMake((H + 15) / 16, (H + 3) / 4, 1) threadsPerThreadgroup:MTLSizeMake(128, 1, 1)];
    [enc endEncoding];
    m.finish(cmd);
    memcpy(ar.tri16.data(), result.contents, ar.tri16.size() * 2);
  }
}
}  // namespace pio
