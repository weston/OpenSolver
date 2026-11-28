#pragma once
// The postflop tree compiled for a discrete GPU (OpenCL, CUDA): flat node records, the work lists of
// each pass, the fused river records, the per-board showdown tables and the all-in outcome tables.
// Built once per solve, uploaded, then every iteration replays the same passes over it. This is the
// packing of src/metal.mm without Metal (metal.mm keeps its own copy, which only a Mac can build).
#include <algorithm>
#include <array>
#include <cstdint>
#include <stdexcept>
#include <unordered_map>
#include <vector>

#include "parallel.hpp"
#include "solver.hpp"

namespace pio {
namespace gpu {
#define WSOLVER_GPU_OPTIONS 1
#include "metal_layout.h"
#undef WSOLVER_GPU_OPTIONS

struct Range {
  size_t start = 0, count = 0;
};

// Per board (plus one for folds, which only need card removal): every opponent hand, then per card the
// opponent hands holding it, in strength order, each span after a head entry. The prefix sums a hero
// hand reads are given slots (only those: the work-group memory holds prefix_count of them). The heads
// hold no hand (kNoHand): the kernels scan a whole table at once, and read 0 there.
struct BoardTables {
  static constexpr uint16_t kNoHand = 0xffff;
  std::vector<BoardPrefixes> boards;
  std::vector<PrefixEntry> indices;
  std::vector<HandPrefixes> heroes;
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
      b.start[0] = 0;
      for (int c = 0; c < 53; ++c) {
        position[c] = b.start[c];
        b.start[c + 1] = b.start[c] + 1 + count[c];
      }
      auto* entries = indices.data() + b.index_offset;
      std::vector<uint16_t> slots(b.start[53]);
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
        entries[first] = {kNoHand, 0};
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

// A hybrid solve's device rows (#582): the tree's storage ranges [tree, tree + floats) of its turn
// subtrees, packed one after another from 0; any other offset (the shared row of one-action decisions,
// which nothing reads) goes to `zero`, a row of zeros after them.
struct DeviceLayout {
  struct Range {
    size_t tree, device, floats;
  };
  std::vector<Range> ranges;  // by tree offset
  size_t floats = 0, zero = 0;
  size_t map(size_t x) const {
    auto it = std::upper_bound(ranges.begin(), ranges.end(), x, [](size_t v, const Range& r) { return v < r.tree; });
    if (it == ranges.begin()) return zero;
    --it;
    return x < it->tree + it->floats ? it->device + (x - it->tree) : zero;
  }
};

struct PostflopPlan {
  size_t stride = 0;  // floats per hand row: the larger range, rounded up to 32
  std::vector<TraversalNode> nodes;
  // Work lists, all in one array: each pass reads its span. Reach tasks are 2 * node + player.
  std::vector<unsigned> list;
  std::vector<Range> levels;                       // value pass, by depth (run deepest first)
  std::vector<std::array<Range, 2>> reach_levels;  // reach pass, by depth and player
  Range terminal_range, pair_range;
  std::vector<RiverRound> rounds, bet_checks;  // whole river rounds; final bets and checks
  std::vector<PairedRiverRound> lead_checks;
  std::vector<TwoBetRiver> two_bets;
  std::vector<AllinBatch> allin_batches;
  std::vector<std::array<unsigned, 2>> iso;  // deals: value row, isomorphism | card << 8
  unsigned reach_floats[2] = {0, 0}, value_rows = 1;

  std::vector<std::array<uint8_t, 2>> cards;  // 2 * stride: each player's hole cards (255 = padding)
  std::vector<uint16_t> perms[2];
  std::vector<BoardPrefixes> boards[2];
  std::vector<PrefixEntry> indices[2];
  std::vector<HandPrefixes> heroes[2];
  size_t round_bytes[2] = {0, 0};  // one channel of a fused river pass, by updating player: prefix slots and reaches
  std::vector<AllinTable> tables;
  std::vector<int16_t> counts[2];  // the outcome tables from each player's side, 8- or 16-bit counts

  // The local memory one channel of a fused river pass needs at most (prefix_count is not known yet
  // when the nodes are packed: every opponent hand can need 3 slots of each of its 3 spans).
  static size_t round_channel_bytes(unsigned hands) { return (4 * hands + 53) * sizeof(float); }

  static void check_tree(const GameTree& g) {
    const size_t stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    const size_t pairs = size_t(g.num_hands(0)) * g.num_hands(1);
    if (!g.num_hands(0) || !g.num_hands(1) || g.nodes.size() * stride > UINT32_MAX || g.storage_floats > UINT32_MAX ||
        pairs * g.allins.size() > UINT32_MAX || (g.showdowns.size() + 1) * (3 * stride + 53) > UINT32_MAX)
      throw std::runtime_error("tree exceeds GPU index limits");
  }

  // Every storage offset the kernels read through the device layout (#582).
  void remap_storage(const DeviceLayout& layout) {
    for (auto& n : nodes) n.storage = unsigned(layout.map(n.storage));
    auto rows = [&](unsigned* storage, int count) {
      for (int k = 0; k < count; ++k) storage[k] = unsigned(layout.map(storage[k]));
    };
    for (auto& r : rounds) rows(r.storage, 4);
    for (auto& r : bet_checks) rows(r.storage, 4);
    for (auto& r : two_bets) rows(r.storage, 4);
    for (auto& r : lead_checks)
      for (auto& b : r.branches) rows(b.storage, 4);
  }

  // The device memory beyond the regrets and strategy sums.
  uint64_t workspace_bytes(const GameTree& g) const {
    const uint64_t hands = g.num_hands(0) + g.num_hands(1);
    const uint64_t board_bytes =
        (g.showdowns.size() + 1) *
        (2 * sizeof(BoardPrefixes) + hands * (sizeof(HandPrefixes) + 3 * sizeof(PrefixEntry)) + 106 * sizeof(PrefixEntry));
    uint64_t table_bytes = tables.size() * sizeof(AllinTable);
    for (const auto& c : counts) table_bytes += c.size() * sizeof(int16_t);
    return (uint64_t(value_rows) * stride + reach_floats[0] + reach_floats[1]) * sizeof(float) + board_bytes +
           table_bytes + nodes.size() * sizeof(TraversalNode) + list.size() * sizeof(unsigned) +
           iso.size() * 2 * sizeof(unsigned) + (rounds.size() + bet_checks.size()) * sizeof(RiverRound) +
           lead_checks.size() * sizeof(PairedRiverRound) + two_bets.size() * sizeof(TwoBetRiver) +
           allin_batches.size() * sizeof(AllinBatch) + 2 * 24 * stride * sizeof(uint16_t) + 2 * stride * 2;
  }

  void pack_hands(const GameTree& g) {
    cards.assign(2 * stride, {255, 255});
    ShowdownBoard fold;
    for (int p = 0; p < 2; ++p) {
      for (int h = 0; h < g.num_hands(p); ++h) {
        cards[p * stride + h] = {g.hcard[p][0][h], g.hcard[p][1][h]};
        fold.ent[p].push_back({uint16_t(h), cards[p * stride + h][0], cards[p * stride + h][1], int16_t(g.same_hand[p][h])});
      }
      perms[p].assign(24 * stride, 0);
      for (uint8_t j = 0; j < 24; ++j)
        if (g.perm_hand[p][j].size()) std::copy(g.perm_hand[p][j].begin(), g.perm_hand[p][j].end(), perms[p].begin() + j * stride);
    }
    for (int p = 0; p < 2; ++p) {
      fold.groups[p].push_back({uint16_t(g.num_hands(p)), uint16_t(g.num_hands(1 - p))});
      BoardTables b(g, fold, p);
      size_t prefixes = 0;
      for (size_t i = 0; i < g.showdowns.size(); ++i) prefixes = std::max(prefixes, size_t(b.boards[i].prefix_count));
      round_bytes[p] = (prefixes + g.num_hands(1 - p)) * sizeof(float);
      boards[p] = std::move(b.boards);
      indices[p] = std::move(b.indices);
      heroes[p] = std::move(b.heroes);
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

  void pack_allins(const GameTree& g) {
    const size_t pairs = size_t(g.num_hands(0)) * g.num_hands(1);
    size_t words = 0;
    tables.clear();
    for (const auto& ar : g.allins) {
      const unsigned width = ar.e2b.empty() ? sizeof(int16_t) : sizeof(int8_t);
      tables.push_back({unsigned(words), width, .5f * ar.scale});
      words += (pairs * width + 1) / 2;
    }
    for (auto& c : counts) c.assign(words, 0);
    pool().parallel_for(int(g.allins.size()), [&](int k) {
      const auto& ar = g.allins[k];
      int16_t* oop = counts[0].data() + tables[k].word_offset;
      int16_t* ip = counts[1].data() + tables[k].word_offset;
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
             g.nodes[node.first_child].kind == ANodeKind::Showdown && g.nodes[node.first_child + 1].kind == ANodeKind::Fold;
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
      if (final_raise || response.kind != ANodeKind::Decision || response.player == n.player || response.num_children != 3)
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

  static bool pack_two_bet_river(const GameTree& g, unsigned index, std::vector<TwoBetRiver>& out) {
    const auto& n = g.nodes[index];
    if (n.kind != ANodeKind::Decision || n.board_len != 5 || (n.num_children != 3 && n.num_children != 4)) return false;
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

  // local_limit: the dynamic work-group memory the fused river kernels may take. A fused pass is used
  // where its channels fit (all at once or the narrow variant); anything else takes the generic passes.
  // roots: the whole tree from node 0, or (a hybrid solve, #582) the turn subtrees at these nodes, each
  // dealt the card beside it: root j's reaches are rows j of each player's reach buffer and its values
  // row j of the values, which the host writes and reads every pass.
  void pack_nodes(const GameTree& g, size_t local_limit,
                  const std::vector<std::pair<unsigned, unsigned>>* roots = nullptr) {
    stride = (std::max(g.num_hands(0), g.num_hands(1)) + 31) & ~size_t(31);
    const size_t N = g.nodes.size();
    nodes.assign(N, TraversalNode{});
    const size_t channel_bytes = round_channel_bytes(std::max(g.num_hands(0), g.num_hands(1)));
    const bool rounds_fit = channel_bytes <= local_limit;
    const bool bet_checks_fit = 2 * channel_bytes <= local_limit;
    std::vector<unsigned> frontier{0}, terminal_ids, paired_ids, allin_ids;
    std::vector<std::array<unsigned, 2>> deals_by_node;
    std::unordered_map<uint64_t, unsigned> outcomes;
    for (unsigned j = 0; j < g.allins.size(); ++j) {
      uint64_t mask = 0;
      for (auto c : g.allins[j].cards) mask |= card_bit(c);
      outcomes[mask] = j;
    }
    std::vector<unsigned> showdown(N, UINT32_MAX);
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
    reach_floats[0] = reach_stride[0];
    reach_floats[1] = reach_stride[1];
    value_rows = 1;
    if (roots) {
      frontier.clear();
      for (unsigned j = 0; j < roots->size(); ++j) {
        const auto [root, card] = (*roots)[j];
        auto& r = nodes[root];
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
      const size_t start = list.size();
      std::vector<unsigned> next;
      for (auto i : frontier) {
        const auto& n = g.nodes[i];
        auto& a = nodes[i];
        a.child = n.first_child;
        a.count = n.num_children;
        a.actor = n.player;
        a.storage = n.storage;
        a.aux = n.aux;
        if (bet_checks_fit && pack_two_bet_river(g, i, two_bets)) continue;
        if (rounds_fit && n.kind == ANodeKind::Decision && n.board_len == 5) {
          PairedRiverRound pair{};
          if (read_paired_river_round(g, i, pair)) {
            lead_checks.push_back(pair);
            continue;
          }
          RiverRound round{};
          if (read_river_round(g, i, round) && (round.kind != BetThenCheck || bet_checks_fit)) {
            (round.kind == BetThenCheck ? bet_checks : rounds).push_back(round);
            continue;
          }
        }
        if (n.kind == ANodeKind::Decision && n.board_len == 5 && n.num_children == 2 &&
            g.nodes[n.first_child].kind == ANodeKind::Showdown && g.nodes[n.first_child + 1].kind == ANodeKind::Fold) {
          paired_ids.push_back(i);
          for (unsigned k = 0; k < 2; ++k) {
            auto& child = nodes[a.child + k];
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
              nodes[a.child + k].card = ci.cards[k];
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
            nodes[a.child + k].public_cards =
                a.kind == Deal ? (a.public_cards << 8) | (nodes[a.child + k].card + 1) : a.public_cards;
            nodes[a.child + k].value_row = reuse_first && k == 0 ? a.value_row : a.child_value + k - reuse_first;
            for (int p = 0; p < 2; ++p) {
              auto& offset = nodes[a.child + k].reach_offset[p];
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
      std::array<Range, 2> by_player;
      for (unsigned p = 0; p < 2; ++p) {
        const size_t start = list.size();
        for (size_t j = level.start; j < level.start + level.count; ++j) {
          const unsigned index = list[j];
          const auto& node = nodes[index];
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
    std::stable_sort(allin_ids.begin(), allin_ids.end(), [&](unsigned a, unsigned b) { return nodes[a].aux < nodes[b].aux; });
    for (unsigned i : allin_ids) {
      const auto& n = nodes[i];
      if (allin_batches.empty() || allin_batches.back().table != n.aux || allin_batches.back().count == 4)
        allin_batches.emplace_back().table = n.aux;
      auto& batch = allin_batches.back();
      const unsigned k = batch.count++;
      batch.value_offset[k] = n.value_row * stride;
      for (unsigned p = 0; p < 2; ++p) {
        batch.reach_offset[p][k] = n.reach_offset[1 - p];
        batch.win_minus_loss[p][k] = n.win[p] - n.loss[p];
        batch.loss[p][k] = n.loss[p];
      }
    }
    for (auto& entry : deals_by_node) {
      const auto& child = nodes[entry[0]];
      entry = {child.value_row, entry[1] | (child.card << 8)};
    }
    iso = std::move(deals_by_node);
  }
};

}  // namespace gpu
}  // namespace pio
