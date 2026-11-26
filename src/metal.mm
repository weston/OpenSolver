#include "metal.hpp"
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <vector>
#include "solver.hpp"
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
    else options.fastMathEnabled = NO;
#endif
#else
    options.fastMathEnabled = NO;
#endif
    library = [dev newLibraryWithSource:[NSString stringWithUTF8String:kernel_source]
                                options:options
                                  error:&error];
    if (!library) fail(error);
  }
  [[noreturn]] static void fail(NSError* error) {
    throw std::runtime_error(error ? error.localizedDescription.UTF8String : "Metal command failed");
  }
  id<MTLComputePipelineState> pipeline(NSString* name) {
    NSError* error = nil;
    auto state = [dev newComputePipelineStateWithFunction:[library newFunctionWithName:name] error:&error];
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
  void finish(id<MTLCommandBuffer> cmd) {
    [cmd commit];
    [cmd waitUntilCompleted];
    if (cmd.status != MTLCommandBufferStatusCompleted) fail(cmd.error);
  }
};
Metal& metal() {
  static Metal result;
  return result;
}
struct BoardTables {
  vector<BoardPrefixes> boards;
  vector<uint16_t> indices;
  vector<HandPrefixes> heroes;
  void add(const ShowdownBoard& sd, int p, int H) {
    BoardPrefixes b{};
    b.index_offset = indices.size();
    b.hand_offset = heroes.size();
    HandPrefixes blocked{};
    blocked.same = -1;
    heroes.resize(b.hand_offset + H, blocked);
    array<vector<int>, 53> lists;
    const auto& opponents = sd.ent[1 - p];
    for (int j = 0; j < int(opponents.size()); ++j) {
      auto o = opponents[j];
      lists[0].push_back(j);
      lists[1 + o.c1].push_back(j);
      lists[1 + o.c2].push_back(j);
    }
    for (int c = 0; c < 53; ++c) {
      b.start[c] = indices.size() - b.index_offset;
      indices.push_back(0);
      for (int pos : lists[c]) indices.push_back(opponents[pos].idx);
    }
    b.start[53] = indices.size() - b.index_offset;
    unsigned i0 = 0, j0 = 0;
    for (auto [i1, j1] : sd.groups[p]) {
      for (unsigned i = i0; i < i1; ++i) {
        auto h = sd.ent[p][i];
        HandPrefixes& m = heroes[b.hand_offset + h.idx];
        m.same = h.same;
        int l = 0;
        for (int c : {0, 1 + h.c1, 1 + h.c2}) {
          const auto& list = lists[c];
          m.lo[l] = b.start[c] + (std::lower_bound(list.begin(), list.end(), j0) - list.begin());
          m.hi[l] = b.start[c] + (std::lower_bound(list.begin(), list.end(), j1) - list.begin());
          m.end[l] = b.start[c] + list.size();
          ++l;
        }
      }
      i0 = i1;
      j0 = j1;
    }
    boards.push_back(b);
  }
};
class PostflopSolve final : public MetalSolve {
 public:
  Metal& m = metal();
  id<MTLComputePipelineState> propagate_reach, update_values, terminals, allins;
  id<MTLBuffer> nodes, node_ids, r[2], v, regrets, strategy_sums, cards, iso, perms[2], boards[2], indices[2],
      heroes[2], equity[2], offsets[2];
  struct Range {
    size_t start, count;
  };
  vector<Range> levels;
  Range terminal_range, allin_range;
  size_t stride, state_bytes;
  template <class V>
  id<MTLBuffer> copy_buffer(const V& x) {
    auto n = x.size() * sizeof(x[0]);
    auto b = m.buffer(std::max(size_t(16), n));
    if (n) memcpy(b.contents, x.data(), n);
    return b;
  }
  void check_workspace(const GameTree& g, uint64_t available) const {
    const size_t N = g.nodes.size();
    const size_t pairs = size_t(g.num_hands(0)) * g.num_hands(1);
    if (!g.num_hands(0) || !g.num_hands(1) || N * stride > UINT32_MAX || g.storage_floats > UINT32_MAX ||
        g.allins.size() * pairs > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal index limits");
    uint64_t rows = 2, mappings = 0;
    for (const auto& node : g.nodes) {
      if (node.kind == ANodeKind::Chance) {
        rows += 2 * node.num_children;
        const auto& chance = g.chances[node.aux];
        mappings += chance.cards.size();
        for (const auto& copies : chance.iso) mappings += copies.size();
      } else if (node.kind == ANodeKind::Decision && node.num_children > 1) {
        rows += node.num_children;
      }
    }
    uint64_t hands = g.num_hands(0) + g.num_hands(1);
    uint64_t boardBytes = (g.showdowns.size() + 1) *
                          (2 * sizeof(BoardPrefixes) + hands * (sizeof(HandPrefixes) + 3 * sizeof(uint16_t)) +
                           106 * sizeof(uint16_t));
    uint64_t equityBytes = 2 * g.allins.size() * pairs * sizeof(float);
    uint64_t workspace = (N + rows) * stride * sizeof(float) + 2 * state_bytes;
    uint64_t peak = workspace +
                    2 * (boardBytes + equityBytes + N * (sizeof(TraversalNode) + 4 * sizeof(unsigned)) +
                         mappings * 2 * sizeof(unsigned)) +
                    128 * stride;
    if (mappings > UINT32_MAX || (g.showdowns.size() + 1) * (3 * stride + 53) > UINT32_MAX)
      throw std::runtime_error("tree exceeds Metal board index limits");
    if (peak > available || peak > m.dev.recommendedMaxWorkingSetSize)
      throw std::runtime_error("Metal workspace exceeds available memory");
  }
  explicit PostflopSolve(GameTree& g, uint64_t available) {
    propagate_reach = m.pipeline(@"propagate_reach");
    update_values = m.pipeline(@"update_values");
    terminals = m.pipeline(@"terminal");
    allins = m.pipeline(@"allin");
    stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    state_bytes = g.storage_floats * sizeof(float);
    check_workspace(g, available);
    regrets = m.buffer(state_bytes);
    strategy_sums = m.buffer(state_bytes);
    pack_hands(g);
    pack_nodes(g);
  }

  void pack_hands(const GameTree& g) {
    vector<array<uint8_t, 2>> hand_cards(2 * stride, {255, 255});
    ShowdownBoard fold;
    for (int p = 0; p < 2; ++p) {
      for (int h = 0; h < g.num_hands(p); ++h) {
        hand_cards[p * stride + h] = {g.hcard[p][0][h], g.hcard[p][1][h]};
        fold.ent[p].push_back({uint16_t(h), hand_cards[p * stride + h][0], hand_cards[p * stride + h][1],
                               int16_t(g.same_hand[p][h])});
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
      BoardTables b;
      for (const auto& board : g.showdowns) b.add(board, p, g.num_hands(p));
      b.add(fold, p, g.num_hands(p));
      boards[p] = copy_buffer(b.boards);
      indices[p] = copy_buffer(b.indices);
      heroes[p] = copy_buffer(b.heroes);
      vector<float> equities;
      vector<unsigned> equity_offsets;
      for (const auto& ar : g.allins) {
        equity_offsets.push_back(equities.size());
        int H1 = g.num_hands(1);
        for (int h = 0; h < g.num_hands(p); ++h)
          for (int o = 0; o < g.num_hands(1 - p); ++o) {
            size_t j = p ? o * H1 + h : h * H1 + o;
            float e = (ar.e2.empty() ? ar.e2b[j] : ar.e2[j]) * .5f * ar.scale;
            bool disjoint = !(combo_mask(g.hands[p][h]) & combo_mask(g.hands[1 - p][o]));
            equities.push_back(disjoint ? (p ? 1.f - e : e) : -1.f);
          }
      }
      equity[p] = copy_buffer(equities);
      offsets[p] = copy_buffer(equity_offsets);
    }
  }

  void pack_nodes(const GameTree& g) {
    const size_t N = g.nodes.size();
    v = m.buffer(N * stride * sizeof(float));
    vector<TraversalNode> packed_nodes(N);
    vector<unsigned> list, frontier{0}, terminal_ids, allin_ids;
    vector<array<unsigned, 2>> deals_by_node;
    unsigned next_row[2] = {1, 1};
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
        if (n.kind == ANodeKind::Decision || n.kind == ANodeKind::Chance) {
          a.kind = n.kind == ANodeKind::Chance ? Deal : Decision;
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
          for (unsigned k = 0; k < a.count; ++k) {
            for (int p = 0; p < 2; ++p)
              packed_nodes[a.child + k].row[p] =
                  (a.kind == Deal || ((a.actor == unsigned(p)) && a.count > 1)) ? next_row[p]++ : a.row[p];
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
    terminal_range = {list.size(), terminal_ids.size()};
    list.insert(list.end(), terminal_ids.begin(), terminal_ids.end());
    allin_range = {list.size(), allin_ids.size()};
    list.insert(list.end(), allin_ids.begin(), allin_ids.end());
    for (int p = 0; p < 2; ++p) {
      r[p] = m.buffer(next_row[p] * stride * 4);
      memcpy(r[p].contents, g.init_reach[p].data(), g.num_hands(p) * 4);
    }
    nodes = copy_buffer(packed_nodes);
    node_ids = copy_buffer(list);
    iso = copy_buffer(deals_by_node);
  }

  void iterate(GameTree& g, Solver& s, int t) override {
    if (state_bytes) {
      memcpy(regrets.contents, g.regrets.data(), state_bytes);
      memcpy(strategy_sums.contents, g.strat.data(), state_bytes);
    }
    auto disc = [](double z) { return float(z / (1 + z)); };
    for (unsigned phase = 0; phase < 2; ++phase) @autoreleasepool {
        unsigned p = phase ^ s.first_player;
        IterationParams args = {unsigned(g.num_hands(0)),
                                unsigned(g.num_hands(1)),
                                unsigned(stride),
                                p,
                                disc(pow(t, s.params.alpha)),
                                disc(pow(t, s.params.beta_at(t))),
                                float(pow(t / (t + 1.), s.params.gamma)),
                                unsigned(s.avg_reset || t > s.avg_delay)};
        if (s.avg_reset && s.avg_delay > 0 && t == s.avg_delay + 1) args.average_discount = 0.f;
        auto cmd = m.command();
        auto enc = m.encoder(cmd);
        [enc setBuffer:nodes offset:0 atIndex:0];
        [enc setBytes:&args length:sizeof(args) atIndex:2];
        for (int j = 0; j < 2; ++j) [enc setBuffer:r[j] offset:0 atIndex:3 + j];
        [enc setBuffer:v offset:0 atIndex:5];
        [enc setBuffer:regrets offset:0 atIndex:6];
        [enc setBuffer:strategy_sums offset:0 atIndex:7];
        [enc setBuffer:cards offset:0 atIndex:8];
        auto dispatch = [&](id<MTLComputePipelineState> pipe, Range span, unsigned y = 1) {
          if (!span.count) return;
          [enc setComputePipelineState:pipe];
          [enc setBuffer:node_ids offset:span.start * 4 atIndex:1];
          [enc dispatchThreadgroups:MTLSizeMake(span.count, y, 1) threadsPerThreadgroup:MTLSizeMake(128, 1, 1)];
          [enc memoryBarrierWithScope:MTLBarrierScopeBuffers];
        };
        for (auto l : levels) dispatch(propagate_reach, l);
        [enc setBuffer:boards[p] offset:0 atIndex:9];
        [enc setBuffer:indices[p] offset:0 atIndex:10];
        [enc setBuffer:heroes[p] offset:0 atIndex:11];
        [enc setThreadgroupMemoryLength:((3 * g.num_hands(1 - p) + 53) * 4 + 15) & ~15 atIndex:0];
        dispatch(terminals, terminal_range);
        [enc setBuffer:offsets[p] offset:0 atIndex:9];
        [enc setBuffer:equity[p] offset:0 atIndex:10];
        dispatch(allins, allin_range, (g.num_hands(p) + 3) / 4);
        [enc setBuffer:iso offset:0 atIndex:9];
        [enc setBuffer:perms[p] offset:0 atIndex:10];
        for (auto it = levels.rbegin(); it != levels.rend(); ++it) dispatch(update_values, *it);
        [enc endEncoding];
        m.finish(cmd);
        if (t % 4 == 0)
          s.symmetrize_root_street(p, static_cast<float*>(regrets.contents),
                                   static_cast<float*>(strategy_sums.contents));
      }
    if (state_bytes) {
      memcpy(g.regrets.data(), regrets.contents, state_bytes);
      memcpy(g.strat.data(), strategy_sums.contents, state_bytes);
    }
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
void metal_flop_table(const GameTree& g, AllinRunouts& ar,
                      const std::vector<std::array<uint32_t, 4>>& runout_boards) {
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
    [enc dispatchThreadgroups:MTLSizeMake((H + 15) / 16, (H + 3) / 4, 1)
        threadsPerThreadgroup:MTLSizeMake(128, 1, 1)];
    [enc endEncoding];
    m.finish(cmd);
    memcpy(ar.tri16.data(), result.contents, ar.tri16.size() * 2);
  }
}
}  // namespace pio
