#include "game.hpp"
#include "metal.hpp"
#include "parallel.hpp"
#include "pfh2h.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <functional>
#include <cstdlib>
#include <numeric>
#include <type_traits>
#include "compact_rows.hpp"

#include "eval.hpp"

namespace pio {

double IcmModel::value(int p, double stack) const {
  const auto& pts = points[p];
  if (pts.empty()) return stack;
  if (pts.size() == 1) return pts[0].second * (pts[0].first != 0 ? stack / pts[0].first : 1.0);
  size_t i = 1;
  while (i + 1 < pts.size() && pts[i].first < stack) ++i;
  const auto& a = pts[i - 1];
  const auto& b = pts[i];
  double t = (b.first == a.first) ? 0.0 : (stack - a.first) / (b.first - a.first);
  return a.second + t * (b.second - a.second);
}

double GameTree::terminal_payoff(const ActionNode& an, int p, int outcome) const {
  // Investments are absolute (they include set_pot's per-player chips); `dead` is the (sub)tree's
  // dead money. Payoff = share of the final pot - own investment.
  double D = an.dead;
  double chips;
  if (an.kind == ANodeKind::Fold) {
    int f = an.player;
    double matched = an.inv[f];
    double P = D + 2.0 * matched;
    double R = std::min(payoff_.rake_frac * P, payoff_.rake_cap);
    chips = (p == f) ? -static_cast<double>(an.inv[f]) : D + an.inv[f] - R;
  } else {
    double P = D + an.inv[0] + an.inv[1];
    double R = std::min(payoff_.rake_frac * P, payoff_.rake_cap);
    if (outcome > 0) chips = P - R - an.inv[p];
    else if (outcome < 0) chips = -static_cast<double>(an.inv[p]);
    else chips = (P - R) / 2.0 - an.inv[p];
  }
  if (!payoff_.icm.enabled) return chips;
  double s = payoff_.icm.stacks[p];
  return payoff_.icm.value(p, s + chips) - payoff_.icm.value(p, s);
}

void GameTree::fill_term_pay() {
  term_pay_.assign(anodes.size(), TermPay{});
  for (size_t i = 0; i < anodes.size(); ++i) {
    const ActionNode& an = *anodes[i];
    TermPay& tp = term_pay_[i];
    for (int p = 0; p < 2; ++p) {
      if (an.kind == ANodeKind::Fold) {
        tp.w[p] = static_cast<float>(terminal_payoff(an, p, p == an.player ? -1 : 1));
      } else if (an.kind == ANodeKind::Showdown) {
        tp.w[p] = static_cast<float>(terminal_payoff(an, p, 1));
        tp.l[p] = static_cast<float>(terminal_payoff(an, p, -1));
        tp.t[p] = static_cast<float>(terminal_payoff(an, p, 0));
      }
    }
  }
}

double GameTree::river_showdowns_per_root() const {
  if (!aroot) return 0.0;
  long long roots = 0, sds = 0;
  auto count_sd = [&](auto&& self, const ActionNode& a) -> long long {
    if (a.kind == ANodeKind::Showdown) return 1;
    long long c = 0;
    for (const auto& ch : a.children) c += self(self, *ch);
    return c;
  };
  auto walk = [&](auto&& self, const ActionNode& a, bool after_chance) -> void {
    if (a.kind == ANodeKind::Decision && a.board_len == 5 && after_chance) {
      ++roots;
      sds += count_sd(count_sd, a);
      return;
    }
    for (const auto& ch : a.children) self(self, *ch, a.kind == ANodeKind::Chance);
  };
  walk(walk, *aroot, false);
  return roots ? static_cast<double>(sds) / static_cast<double>(roots) : 0.0;
}

int GameTree::showdown_board_id(const std::vector<int>& board) {
  uint64_t mask = board_mask(board);
  auto it = showdown_index_.find(mask);
  if (it != showdown_index_.end()) return it->second;
  int id = static_cast<int>(showdowns.size());
  showdowns.emplace_back().mask = mask;
  showdown_index_[mask] = id;
  return id;
}

void GameTree::rank_showdown_boards(size_t first) {
  pool().parallel_for(static_cast<int>(showdowns.size() - first), [&](int offset) {
    rank_showdown_board(showdowns[first + offset]);
  });
}

void GameTree::rank_showdown_board(ShowdownBoard& sb) const {
  const uint64_t mask = sb.mask;
  uint64_t remaining = mask;
  int cs[7];
  for (int i = 0; i < 5; ++i) {
    cs[i] = std::countr_zero(remaining);
    remaining &= remaining - 1;
  }
  std::vector<uint32_t> str[2];
  for (int p = 0; p < 2; ++p) {
    std::vector<std::pair<uint32_t, uint16_t>> v;
    v.reserve(hands[p].size());
    for (size_t h = 0; h < hands[p].size(); ++h) {
      if (combo_mask(hands[p][h]) & mask) {
        sb.blocked[p].push_back(static_cast<uint16_t>(h));
        continue;
      }
      cs[5] = hcard[p][0][h];
      cs[6] = hcard[p][1][h];
      v.push_back({evaluate(cs, 7), static_cast<uint16_t>(h)});
    }
    std::sort(v.begin(), v.end());
    sb.ent[p].resize(v.size());
    str[p].resize(v.size());
    for (size_t i = 0; i < v.size(); ++i) {
      uint16_t h = v[i].second;
      sb.ent[p][i] = {h, hcard[p][0][h], hcard[p][1][h], static_cast<int16_t>(same_hand[p][h])};
      str[p][i] = v[i].first;
    }
  }
  for (int p = 0; p < 2; ++p) {
    const auto& sp = str[p];
    const auto& so = str[1 - p];
    auto& gr = sb.groups[p];
    gr.clear();
    size_t i = 0, j = 0;
    while (i < sp.size() || j < so.size()) {
      uint32_t s = i < sp.size() ? sp[i] : UINT32_MAX;
      if (j < so.size() && so[j] < s) s = so[j];
      while (i < sp.size() && sp[i] == s) ++i;
      while (j < so.size() && so[j] == s) ++j;
      gr.push_back({static_cast<uint16_t>(i), static_cast<uint16_t>(j)});
    }
  }
}

// Flop-deal normalization tables. Over the suit-symmetrized subset (variant weight w = weight / (W*|G|)),
// for each hand h of player p: total compatible weight W_h, per card c the compatible weight of variants
// containing c, and per opponent combo the compatible weight of variants containing both its cards.
// Then the exact pair weight is N(h,o) = W_h - n(h,c1) - n(h,c2) + n(h,c1&c2) (inclusion-exclusion).
void GameTree::compute_flop_compat() {
  double W = 0;
  for (auto& f : cfg.subset) W += f.weight;
  const double gn = 1.0 / static_cast<double>(perm_all.size());
  for (int p = 0; p < 2; ++p) {
    const size_t H = hands[p].size();
    // Summed in double: 1,755 flops in 24 suit variants are 42,120 terms of about 2e-5 each. Summed as
    // floats a hand's own sum came out 2.8e-4 over and the count for a pair of hands 3.5e-4 over (17,302
    // flops where there are 17,296), which took that share of an ante out of every flop (#505).
    std::vector<double> compat(H, 0.0), card(H * NUM_CARDS, 0.0), pair(H * NUM_COMBOS, 0.0);
    for (auto& f : cfg.subset) {
      const double w = f.weight / W * gn;
      for (uint8_t pm : perm_all) {
        int c[3] = {PERM_CARD[pm][f.cards[0]], PERM_CARD[pm][f.cards[1]], PERM_CARD[pm][f.cards[2]]};
        uint64_t fm = card_bit(c[0]) | card_bit(c[1]) | card_bit(c[2]);
        int pr[3] = {combo_index(c[0], c[1]), combo_index(c[0], c[2]), combo_index(c[1], c[2])};
        for (size_t h = 0; h < H; ++h) {
          if (combo_mask(hands[p][h]) & fm) continue;
          compat[h] += w;
          for (int k = 0; k < 3; ++k) {
            card[h * NUM_CARDS + c[k]] += w;
            pair[h * NUM_COMBOS + pr[k]] += w;
          }
        }
      }
    }
    flop_compat[p].assign(compat.begin(), compat.end());
    flop_card[p].assign(card.begin(), card.end());
    flop_pair[p].assign(pair.begin(), pair.end());
  }
}

namespace {
// The runouts of an all-in before the river, in the order their values are summed: the terminal's
// board, then every river (a turn all-in) or every turn and river (a flop all-in) of the cards left.
template <class F>
void for_each_runout(const AllinRunouts& ar, F&& f) {
  std::vector<int> b(ar.cards.begin(), ar.cards.end());
  const uint64_t mask = board_mask(b);
  std::vector<int> avail;
  for (int c = 0; c < NUM_CARDS; ++c)
    if (!(mask & card_bit(c))) avail.push_back(c);
  if (b.size() == 4) {
    for (int c : avail) {
      b.push_back(c);
      f(b);
      b.pop_back();
    }
    return;
  }
  for (size_t x = 0; x < avail.size(); ++x)
    for (size_t y = x + 1; y < avail.size(); ++y) {
      b.push_back(avail[x]);
      b.push_back(avail[y]);
      f(b);
      b.pop_back();
      b.pop_back();
    }
}
}  // namespace

void GameTree::store_allin_boards(AllinRunouts& ar) {
  if (ar.exact || !ar.boards.empty()) return;
  ar.boards.reserve(ar.runouts);
  for_each_runout(ar, [&](const std::vector<int>& b) { ar.boards.push_back(static_cast<uint32_t>(showdown_board_id(b))); });
}

void GameTree::restore_allin_boards() {
  const size_t first = showdowns.size();
  for (AllinRunouts& ar : allins) store_allin_boards(ar);
  rank_showdown_boards(first);
}

int GameTree::allin_runouts_id(const std::vector<int>& board) {
  uint64_t mask = board_mask(board);
  auto it = allin_index_.find(mask);
  if (it != allin_index_.end()) return it->second;
  AllinRunouts ar;
  if (board.empty()) {
    // Preflop all-in (or a pure preflop tree's exits): exact equity via the hand-vs-hand table
    // (Solver::showdown_value handles `exact` entries; no sampled boards).
    ar.exact = true;
    ar.scale = 1.0f;
    // Load or generate the table now, on the building thread. Generating it lazily from inside a
    // solver worker would deadlock: generation uses the thread pool, whose other workers would be
    // blocked on this static initialiser.
    (void)pf_table();
  } else {
    ar.cards.assign(board.begin(), board.end());
    const size_t left = static_cast<size_t>(NUM_CARDS) - board.size();
    const double k = static_cast<double>(left - 4);  // cards a pair of hands leaves for the runout
    if (board.size() == 4) {
      ar.runouts = static_cast<uint16_t>(left);
      ar.scale = 1.0f / static_cast<float>(left - 4);  // rivers compatible with both hands
    } else {
      ar.runouts = static_cast<uint16_t>(left * (left - 1) / 2);
      ar.scale = static_cast<float>(2.0 / (k * (k - 1.0)));  // 1 / C(k, 2)
    }
    // A postflop tree has its boards and a table for every set, as it always had. A preflop tree has
    // a set per flop of its subset, which is most of its memory: plan_allins decides once all exist.
    if (!cfg.preflop) {
      store_allin_boards(ar);
      ar.table = true;
    }
  }
  int id = static_cast<int>(allins.size());
  allins.push_back(std::move(ar));
  allin_index_[mask] = id;
  return id;
}

// What a stored board takes at least: an entry per hand it does not block (its strength groups, which
// need the evaluator, are left out, so a tie in bytes goes to the boards' side by a little).
uint64_t GameTree::showdown_board_bytes(uint64_t m) const {
  uint64_t live = 0;
  int cs[5], n = 0;
  for (int c = 0; c < NUM_CARDS; ++c)
    if (m & card_bit(c)) cs[n++] = c;
  for (int p = 0; p < 2; ++p) {
    int64_t blocked = 0;
    for (int i = 0; i < n; ++i) {
      blocked += static_cast<int64_t>(hands_with_card[p][cs[i]].size());
      for (int j = i + 1; j < n; ++j) blocked -= local[p][combo_index(cs[i], cs[j])] >= 0;  // counted twice
    }
    live += hands[p].size() - static_cast<uint64_t>(blocked);
  }
  return live * sizeof(SdEntry) + sizeof(ShowdownBoard);
}

int GameTree::allin_live_hands(const AllinRunouts& ar) const {
  uint64_t mask = 0;
  for (uint8_t c : ar.cards) mask |= card_bit(c);
  int n = 0;
  for (uint16_t h : hands[0]) n += (combo_mask(h) & mask) == 0;
  return n;
}

uint64_t GameTree::allin_boards_missing_bytes() const {
  std::vector<uint64_t> need;
  for (const AllinRunouts& ar : allins) {
    if (ar.exact || !ar.boards.empty()) continue;
    for_each_runout(ar, [&](const std::vector<int>& b) {
      const uint64_t m = board_mask(b);
      if (!showdown_index_.count(m)) need.push_back(m);
    });
  }
  std::sort(need.begin(), need.end());
  need.erase(std::unique(need.begin(), need.end()), need.end());
  uint64_t bytes = 0;
  for (uint64_t m : need) bytes += showdown_board_bytes(m);
  return bytes;
}

void GameTree::plan_allins() {
  if (!cfg.preflop) return;
  // Boards that only all-ins read (no river showdown of the tree has them), by the level that reads
  // them: [0] flop sets, [1] turn sets. A flop set's runouts are also those of its flop's turn sets.
  std::vector<uint64_t> need[2];
  uint64_t tab[2] = {0, 0};
  for (const AllinRunouts& ar : allins) {
    if (ar.exact) continue;
    const int lvl = ar.cards.size() == 4;
    tab[lvl] += allin_table_bytes(ar);
    for_each_runout(ar, [&](const std::vector<int>& b) {
      const uint64_t m = board_mask(b);
      if (!showdown_index_.count(m)) need[lvl].push_back(m);
    });
  }
  for (auto& v : need) {
    std::sort(v.begin(), v.end());
    v.erase(std::unique(v.begin(), v.end()), v.end());
  }
  auto board_bytes = [&](uint64_t m) { return showdown_board_bytes(m); };
  uint64_t only[2] = {0, 0}, both = 0;
  for (size_t i = 0, j = 0; i < need[0].size() || j < need[1].size();) {
    if (j == need[1].size() || (i < need[0].size() && need[0][i] < need[1][j])) only[0] += board_bytes(need[0][i++]);
    else if (i == need[0].size() || need[1][j] < need[0][i]) only[1] += board_bytes(need[1][j++]);
    else both += board_bytes(need[0][i]), ++i, ++j;
  }
  // The tree's bytes for each choice of levels with tables (bit 0: flop sets, bit 1: turn sets): the
  // tables, and the boards a level without tables still reads. The smallest wins, tables on a tie
  // (they are also the faster way to value a set). ICM values every runout on its board.
  const uint64_t cost[4] = {only[0] + only[1] + both, tab[0] + only[1] + both, tab[1] + only[0] + both, tab[0] + tab[1]};
  int best = 0;
  if (!payoff_.icm.enabled) {
    for (int k = 1; k < 4; ++k)
      if (cost[k] <= cost[best]) best = k;
    // nothing of the runouts is in the count yet: no set has a table, no board of theirs is built
    if (table_limit && memory_bytes_for(limit_compress, limit_preds) + cost[3] <= table_limit) best = 3;
    if (allin_plan >= 0) best = allin_plan & 3;
  }
  const size_t first = showdowns.size();
  for (AllinRunouts& ar : allins) {
    if (ar.exact) continue;
    ar.table = (best >> (ar.cards.size() == 4)) & 1;
    if (!ar.table) store_allin_boards(ar);
  }
  rank_showdown_boards(first);
}

namespace {

struct Filler {
  GameTree& g;
  bool iso;
  std::unordered_map<uint32_t, uint16_t> keep_index;  // cards dealt after the root board -> GNode::pad

  // #239: the compact 16-bit row set of a decision node on `board` (0: full rows). River nodes of
  // postflop trees only. In a preflop tree the hand lists span every flop. At turn nodes the blocked
  // hands' regrets are not 0 (a flop tree, 60 iterations: up to 25,000 at 245 of 539 turn nodes), and
  // they set the node's 16-bit scale, so dropping them would change results; turn rows are ~1% of storage.
  uint16_t keep_set(const std::vector<int>& board) {
    const size_t root = g.cfg.board.size();
    if (!PIO_COMPACT_ROWS || g.cfg.preflop || board.size() != 5 || board.size() <= root) return 0;
    uint32_t key = 0;
    for (size_t k = root; k < board.size(); ++k) key = key * 53u + static_cast<uint32_t>(board[k] + 1);
    auto it = keep_index.find(key);
    if (it != keep_index.end()) return it->second;
    uint16_t id = 0;
    if (g.keep_sets.size() < 65535) {
      // Only the counts here (select_rows(true) lists the hands): H minus the hands holding either card,
      // plus the one holding both, which was taken off twice.
      GameTree::KeepSet ks;
      for (size_t k = root; k < board.size() && k - root < 2; ++k) ks.card[k - root] = board[k];
      bool any = false;
      for (int p = 0; p < 2; ++p) {
        int64_t kept = static_cast<int64_t>(g.hands[p].size());
        for (int c : ks.card)
          if (c >= 0) kept -= static_cast<int64_t>(g.hands_with_card[p][c].size());
        if (ks.card[1] >= 0)
          for (uint16_t h : g.hands_with_card[p][ks.card[0]])
            kept += g.hcard[p][0][h] == ks.card[1] || g.hcard[p][1][h] == ks.card[1];
        ks.kept[p] = static_cast<uint32_t>(kept);
        any |= ks.kept[p] < g.hands[p].size();
      }
      if (any) {
        g.keep_sets.push_back(std::move(ks));
        id = static_cast<uint16_t>(g.keep_sets.size());
      }
    }
    keep_index.emplace(key, id);
    return id;
  }

  // The showdown that `an` leads to when nobody has a choice left on the way: decisions with one action
  // and the deals between them. nullptr if any decision below has more, or if the path crosses a flop
  // deal (which is over the subset's flops, not every runout).
  std::vector<int8_t> forced_known;        // per action node: 0 not looked at, 1 a forced path, 2 not
  std::vector<const ActionNode*> forced_end;
  const ActionNode* forced_showdown(const ActionNode* an) {
    const size_t id = static_cast<size_t>(an->id);
    if (forced_known.size() <= id) {
      forced_known.resize(g.anodes.size(), 0);
      forced_end.resize(g.anodes.size(), nullptr);
    }
    if (!forced_known[id]) {
      const ActionNode* end = nullptr;
      if (an->kind == ANodeKind::Showdown) end = an;
      else if (an->children.size() == 1 &&
               (an->kind == ANodeKind::Decision || (an->kind == ANodeKind::Chance && !an->preflop_exit)))
        end = forced_showdown(an->children[0].get());
      forced_end[id] = end;
      forced_known[id] = end ? 1 : 2;
    }
    return forced_end[id];
  }

  void fill(uint32_t idx, const ActionNode* an, std::vector<int>& board) {
    GNode n{};
    n.kind = an->kind;
    n.player = an->player;
    n.board_len = static_cast<uint8_t>(board.size());
    n.anode = static_cast<uint32_t>(an->id);
    // A check-down (#484): from here to the showdown nobody decides anything, so the node is that
    // showdown on every runout of this board, which is what an all-in here is. One node instead of a
    // subtree of one-action decisions, each of which stores a regret and a strategy row per hand.
    if (g.cfg.collapse_checkdowns && board.size() >= 3 &&
        (an->kind == ANodeKind::Decision || an->kind == ANodeKind::Chance)) {
      if (const ActionNode* end = forced_showdown(an)) {
        ++g.checkdowns;
        n.kind = ANodeKind::Showdown;
        n.player = end->player;
        n.anode = static_cast<uint32_t>(end->id);
        n.aux = static_cast<uint32_t>(board.size() == 5 ? g.showdown_board_id(board) : g.allin_runouts_id(board));
        g.nodes[idx] = n;
        return;
      }
    }
    switch (an->kind) {
      case ANodeKind::Decision: {
        n.aux = static_cast<uint32_t>(g.num_decisions++);
        g.dec_board.push_back(board_mask(board));
        n.storage = g.storage_floats;
        g.storage_floats += an->children.size() * g.hands[an->player].size();
        n.num_children = static_cast<uint16_t>(an->children.size());
        {
          const uint16_t c = keep_set(board);
          g.keep_cand.push_back(c);
          g.storage_compact += an->children.size() * (c ? g.keep_sets[c - 1].kept[an->player]
                                                         : g.hands[an->player].size());
        }
        n.first_child = static_cast<uint32_t>(g.nodes.size());
        g.nodes.resize(g.nodes.size() + an->children.size());
        g.nodes[idx] = n;
        for (size_t i = 0; i < an->children.size(); ++i) fill(n.first_child + static_cast<uint32_t>(i), an->children[i].get(), board);
        return;
      }
      case ANodeKind::Chance: {
        if (an->preflop_exit) {
          if (an->children.empty()) {  // pure preflop tree: the exit is evaluated by equity
            n.kind = ANodeKind::Showdown;
            n.aux = static_cast<uint32_t>(g.allin_runouts_id(board));
            g.nodes[idx] = n;
            return;
          }
          ChanceInfo ci{};
          ci.flop_deal = true;
          for (int c = 0; c < NUM_CARDS; ++c) ci.slot[c] = -1;
          double W = 0;
          for (auto& f : g.cfg.subset) W += f.weight;
          const double frac = 17296.0 / 22100.0;  // C(48,3) / C(52,3): flops compatible with two hands
          (void)frac;
          for (auto& f : g.cfg.subset) {
            ci.flops.push_back({static_cast<uint8_t>(f.cards[0]), static_cast<uint8_t>(f.cards[1]),
                                static_cast<uint8_t>(f.cards[2])});
            ci.fweight.push_back(static_cast<float>(f.weight / W));
          }
          if (g.flop_compat[0].empty()) g.compute_flop_compat();
          size_t k = ci.flops.size();
          n.aux = static_cast<uint32_t>(g.chances.size());
          n.num_children = static_cast<uint16_t>(k);
          n.first_child = static_cast<uint32_t>(g.nodes.size());
          auto flops = ci.flops;
          g.chances.push_back(std::move(ci));
          g.nodes.resize(g.nodes.size() + k);
          g.nodes[idx] = n;
          const ActionNode* child = an->children[0].get();
          for (size_t i = 0; i < k; ++i) {
            std::vector<int> fb = {flops[i][0], flops[i][1], flops[i][2]};
            fill(n.first_child + static_cast<uint32_t>(i), child, fb);
          }
          return;
        }
        ChanceInfo ci{};
        uint64_t bm = board_mask(board);
        ci.board_mask = bm;
        ci.num_possible = static_cast<uint8_t>(NUM_CARDS - board.size() - 4);
        std::vector<int> group;
        group.push_back(IDENTITY_PERM);
        if (iso || (g.cfg.preflop && g.cfg.iso_flop && board.size() >= 3)) {
          for (int p = 1; p < NUM_SUIT_PERMS; ++p) {
            if (!g.perm_ok[p]) continue;
            uint64_t img = 0;
            for (int c : board) img |= card_bit(PERM_CARD[p][c]);
            if (img == bm) group.push_back(p);
          }
        }
        for (int c = 0; c < NUM_CARDS; ++c) {
          ci.slot[c] = -1;
          ci.perm[c] = IDENTITY_PERM;
        }
        for (int c = 0; c < NUM_CARDS; ++c) {
          if (bm & card_bit(c)) continue;
          // canonical = smallest card in orbit
          int canon = c, via = IDENTITY_PERM;
          for (int p : group) {
            int img = PERM_CARD[p][c];
            if (img < canon) canon = img;
          }
          if (canon == c) {
            ci.slot[c] = static_cast<int8_t>(ci.cards.size());
            ci.cards.push_back(static_cast<uint8_t>(c));
            ci.iso.emplace_back();
          } else {
            for (int p : group)
              if (PERM_CARD[p][canon] == c) {
                via = p;
                break;
              }
            int s = ci.slot[canon];
            ci.slot[c] = static_cast<int8_t>(s);
            ci.perm[c] = static_cast<uint8_t>(via);
            ci.iso[s].push_back({static_cast<uint8_t>(c), static_cast<uint8_t>(via)});
          }
        }
        size_t k = ci.cards.size();
        n.aux = static_cast<uint32_t>(g.chances.size());
        n.num_children = static_cast<uint16_t>(k);
        n.first_child = static_cast<uint32_t>(g.nodes.size());
        std::vector<uint8_t> cards = ci.cards;
        g.chances.push_back(std::move(ci));
        g.nodes.resize(g.nodes.size() + k);
        g.nodes[idx] = n;
        const ActionNode* child = an->children[0].get();
        for (size_t i = 0; i < k; ++i) {
          board.push_back(cards[i]);
          fill(n.first_child + static_cast<uint32_t>(i), child, board);
          board.pop_back();
        }
        return;
      }
      case ANodeKind::Showdown:
        n.aux = static_cast<uint32_t>(board.size() == 5 ? g.showdown_board_id(board) : g.allin_runouts_id(board));
        g.nodes[idx] = n;
        return;
      case ANodeKind::Fold:
        g.nodes[idx] = n;
        return;
    }
  }
};

}  // namespace

bool GameTree::build(const GameConfig& c, std::string& err, bool allocate) {
  cfg = c;
  nodes.clear();
  chances.clear();
  showdowns.clear();
  showdown_index_.clear();
  allins.clear();
  allin_index_.clear();
  for (int p = 0; p < 2; ++p) {
    flop_compat[p].clear();
    flop_card[p].clear();
    flop_pair[p].clear();
  }
  regrets.clear();
  regrets.shrink_to_fit();
  strat.clear();
  strat.shrink_to_fit();
  combo_lock.clear();
  combo_lock_s.clear();
  incentives.clear();
  storage_floats = 0;
  num_decisions = 0;
  checkdowns = 0;
  keep_sets.clear();
  keep_cand.clear();
  qoff.clear();
  storage_q = 0;
  storage_compact = 0;
  compact_rows = false;
  dec_board.clear();
  incomplete = false;

  int bl = static_cast<int>(cfg.board.size());
  if (cfg.preflop) {
    cfg.board.clear();
    bl = 0;
  } else if (bl < 3 || bl > 5) {
    err = "board must have 3, 4 or 5 cards";
    return false;
  }
  uint64_t bm = board_mask(cfg.board);
  for (int p = 0; p < 2; ++p) {
    hands[p].clear();
    init_reach[p].clear();
    local[p].assign(NUM_COMBOS, -1);
    for (int ci = 0; ci < NUM_COMBOS; ++ci) {
      float w = cfg.range[p][ci];
      // With union_hands, a hand only in the opponent's range is kept with reach 0: it
      // contributes nothing to anyone's values but gains a slot, so queries can report a
      // counterfactual value for it instead of a bare inf.
      bool keep = w > 0.0f || (cfg.union_hands && cfg.range[1 - p][ci] > 0.0f);
      if (!keep || (combo_mask(ci) & bm)) continue;
      local[p][ci] = static_cast<int32_t>(hands[p].size());
      hands[p].push_back(static_cast<uint16_t>(ci));
      init_reach[p].push_back(w);
    }
    if (hands[p].empty()) {
      err = std::string("range of ") + (p == 0 ? "OOP" : "IP") + " is empty";
      return false;
    }
    if (hands[p].size() > 65535) {
      err = "too many hands";
      return false;
    }
    for (int k = 0; k < 2; ++k) hcard[p][k].resize(hands[p].size());
    hands_with_card[p].assign(NUM_CARDS, {});
    for (size_t h = 0; h < hands[p].size(); ++h) {
      const Combo& cb = COMBOS[hands[p][h]];
      hcard[p][0][h] = cb.hi;
      hcard[p][1][h] = cb.lo;
      hands_with_card[p][cb.hi].push_back(static_cast<uint16_t>(h));
      hands_with_card[p][cb.lo].push_back(static_cast<uint16_t>(h));
    }
  }
  for (int p = 0; p < 2; ++p) {
    same_hand[p].assign(hands[p].size(), -1);
    for (size_t h = 0; h < hands[p].size(); ++h) same_hand[p][h] = local[1 - p][hands[p][h]];
  }
  same_hands = hands[0] == hands[1];
  // Symmetries that preserve the root board and both ranges.
  for (int pm = 0; pm < NUM_SUIT_PERMS; ++pm) {
    uint64_t img = 0;
    for (int cc : cfg.board) img |= card_bit(PERM_CARD[pm][cc]);
    bool ok = img == bm;
    for (int p = 0; p < 2 && ok; ++p)
      for (int ci = 0; ci < NUM_COMBOS && ok; ++ci)
        if (cfg.range[p][ci] != cfg.range[p][PERM_COMBO[pm][ci]]) ok = false;
    perm_ok[pm] = ok;
  }
  for (int p = 0; p < 2; ++p) {
    perm_hand[p].assign(NUM_SUIT_PERMS, {});
    for (int pm = 0; pm < NUM_SUIT_PERMS; ++pm) {
      if (!perm_ok[pm]) continue;
      auto& v = perm_hand[p][pm];
      v.resize(hands[p].size());
      for (size_t h = 0; h < hands[p].size(); ++h) v[h] = static_cast<uint16_t>(local[p][PERM_COMBO[pm][hands[p][h]]]);
    }
  }

  perm_all.clear();
  for (int pm = 0; pm < NUM_SUIT_PERMS; ++pm)
    if (perm_ok[pm]) perm_all.push_back(static_cast<uint8_t>(pm));

  ActionTreeParams ap;
  ap.board_len = bl;
  ap.eff_stack = cfg.eff_stack;
  ap.start_inv[0] = cfg.pot[0];
  ap.start_inv[1] = cfg.pot[1];
  ap.dead = cfg.pot[2];
  aroot = build_action_tree(ap, cfg.lines, err);
  if (!aroot) return false;
  cfg.smart_rivers = cfg.smart_rivers && bl == 3 && simplify_rivers(*aroot, cfg.eff_stack);
  schematic_roots.clear();
  if (cfg.full_preflop()) {
    if (cfg.subset.empty()) {
      err = "no flop subset available";
      return false;
    }
    // Attach a schematic postflop tree to every preflop exit.
    std::vector<ActionNode*> all;
    index_action_tree(aroot.get(), all);
    for (ActionNode* x : all) {
      if (!x->preflop_exit) continue;
      const Schematic* sch = nullptr;
      for (auto& s : cfg.schematics)
        if (s.exit_line == x->line) sch = &s;
      // An exit without a schematic stays a pure preflop exit, evaluated by exact equity (the Filler's
      // childless case), as Pio 3.10.1 does: its add_all_flops accepts missing schematics and shows
      // those exits as END_NODE (#306).
      if (!sch) continue;
      ActionTreeParams sp;
      sp.board_len = 3;
      sp.eff_stack = sch->eff_stack;
      sp.start_inv[0] = sch->pot[0];
      sp.start_inv[1] = sch->pot[1];
      sp.dead = sch->pot[2];
      // The schematic's pot stands for the preflop chips: its per-player chips are
      // investments that payoffs subtract and its dead money is free, exactly as Pio takes
      // set_pot literally. (For whole-hand chips, give each player's preflop investment in
      // set_pot, e.g. `set_pot 100 100 0` after a 3-bet to 100.)
      // Like Pio, the schematic's set_pot is taken literally: its per-player chips are investments that
      // payoffs subtract, its dead money is free money. (For whole-hand chips, give each player's
      // preflop investment in set_pot, e.g. `set_pot 100 100 0` after a 3-bet to 100.)
      auto sroot = build_action_tree(sp, sch->lines, err);
      if (!sroot) return false;
      x->children.push_back(std::move(sroot));
    }
  }
  index_action_tree(aroot.get(), anodes);
  fill_term_pay();

  bool iso = (bl == 3 && cfg.iso_flop) || (bl == 4 && cfg.iso_turn);
  nodes.reserve(1024);
  nodes.resize(1);
  std::vector<int> board = cfg.board;
  Filler f{*this, iso, {}, {}, {}};
  f.fill(0, aroot.get(), board);
  rank_showdown_boards(0);
  plan_allins();
  select_rows(false);
  if (allocate) allocate_storage();
  return true;
}

void GameTree::select_rows(bool compact) {
  compact_rows = compact && PIO_COMPACT_ROWS && !keep_sets.empty();
  for (KeepSet& ks : keep_sets)
    for (int p = 0; p < 2; ++p) {
      ks.keep[p].clear();
      ks.bits[p].clear();
      if (!compact_rows) continue;
      const size_t H = hands[p].size();
      ks.bits[p].assign((H + 31) / 32, 0u);
      for (size_t h = 0; h < H; ++h) {
        const int c0 = hcard[p][0][h], c1 = hcard[p][1][h];
        if (c0 == ks.card[0] || c1 == ks.card[0] || c0 == ks.card[1] || c1 == ks.card[1]) continue;
        ks.keep[p].push_back(static_cast<uint16_t>(h));
        ks.bits[p][h / 32] |= 1u << (h % 32);
      }
      if (ks.keep[p].size() != ks.kept[p]) {  // the build's count is what the layout uses: never differ
        select_rows(false);
        return;
      }
    }
  qoff.clear();
  if (compact_rows) qoff.resize(num_decisions);
  storage_q = 0;
  for (GNode& n : nodes) {
    if (n.kind != ANodeKind::Decision) continue;
    n.pad = compact_rows ? keep_cand[n.aux] : 0;
    if (!compact_rows) continue;
    qoff[n.aux] = storage_q;
    storage_q += stored_size(n);
  }
  if (!compact_rows) storage_q = storage_floats;
}

namespace {
// Runouts whose ranks are held back before they are added to the table (see the fill without boards).
// Measured on 200 flops with every hand (M4, 10 threads, whole build): 1 runout 9.5 s, 4 7.3 s,
// 8 7.1 s, 16 7.0 s, 32 7.2 s, 64 7.6 s - flat from 4 to 32, so the value is not a tuned one.
constexpr int ALLIN_FILL_BLOCK = 16;
template <class E>
void fill_allin_table(const GameTree& g, const AllinRunouts& ar, const std::vector<uint8_t>& compat, std::vector<E>& e2) {
  const int H0 = g.num_hands(0), H1 = g.num_hands(1);
  e2.assign(static_cast<size_t>(H0) * H1, 0);
  std::vector<int16_t> r0(H0), r1(H1);
  for (uint32_t bid : ar.boards) {
    const ShowdownBoard& sb = g.showdowns[bid];
    std::fill(r0.begin(), r0.end(), int16_t(-1));
    std::fill(r1.begin(), r1.end(), int16_t(-1));
    int i0 = 0, j0 = 0;
    int16_t gi = 0;
    for (const auto& gr : sb.groups[0]) {  // strength groups ascending: the group index is the rank
      for (int i = i0; i < gr.first; ++i) r0[sb.ent[0][i].idx] = gi;
      for (int j = j0; j < gr.second; ++j) r1[sb.ent[1][j].idx] = gi;
      i0 = gr.first;
      j0 = gr.second;
      ++gi;
    }
    for (int h = 0; h < H0; ++h) {
      const int16_t rh = r0[h];
      if (rh < 0) continue;
      E* row = e2.data() + static_cast<size_t>(h) * H1;
      const uint8_t* cr = compat.data() + static_cast<size_t>(h) * H1;
      for (int o = 0; o < H1; ++o) {
        const int16_t ro = r1[o];
        const E v = static_cast<E>(ro < 0 ? 0 : rh > ro ? 2 : rh == ro ? 1 : 0);
        row[o] = static_cast<E>(row[o] + (cr[o] ? v : 0));
      }
    }
  }
}

// The same counts for a set whose boards are not stored: strengths straight from the evaluator, so
// nothing but the table is ever allocated for the set. The runouts are taken ALLIN_FILL_BLOCK at a
// time: a row of the table then takes the ranks of all of them while it sits in the first-level cache,
// instead of the whole table (3.5 MB for a flop with every hand) being streamed once per runout.
template <class E>
void fill_allin_table(const GameTree& g, const AllinRunouts& ar, std::vector<E>& e2) {
  const int H0 = g.num_hands(0), H1 = g.num_hands(1);
  const int B = ALLIN_FILL_BLOCK;
  e2.assign(static_cast<size_t>(H0) * H1, 0);
  std::vector<uint32_t> s0(H0), s1(H1), all;  // strength, 0 for a hand the board blocks
  std::vector<int16_t> r0(static_cast<size_t>(B) * H0), r1(static_cast<size_t>(B) * H1);
  all.reserve(static_cast<size_t>(H0) + H1);
  int nb = 0;
  auto flush = [&] {
    for (int h = 0; h < H0; ++h) {
      E* row = e2.data() + static_cast<size_t>(h) * H1;
      for (int b = 0; b < nb; ++b) {
        const int16_t rh = r0[static_cast<size_t>(b) * H0 + h];
        if (rh < 0) continue;
        const int16_t* r = r1.data() + static_cast<size_t>(b) * H1;
        for (int o = 0; o < H1; ++o) row[o] = static_cast<E>(row[o] + (rh > r[o]) + (rh >= r[o]));
      }
    }
    nb = 0;
  };
  for_each_runout(ar, [&](const std::vector<int>& b) {
    const uint64_t mask = board_mask(b);
    int cs[7];
    for (int i = 0; i < 5; ++i) cs[i] = b[i];
    all.clear();
    for (int h = 0; h < H0; ++h) {
      s0[h] = 0;
      if (combo_mask(g.hands[0][h]) & mask) continue;
      cs[5] = g.hcard[0][0][h];
      cs[6] = g.hcard[0][1][h];
      all.push_back(s0[h] = evaluate(cs, 7));
    }
    for (int o = 0; o < H1; ++o) {
      s1[o] = 0;
      if (combo_mask(g.hands[1][o]) & mask) continue;
      const int32_t same = g.same_hand[1][o];
      if (same >= 0) {
        s1[o] = s0[same];
        continue;
      }
      cs[5] = g.hcard[1][0][o];
      cs[6] = g.hcard[1][1][o];
      all.push_back(s1[o] = evaluate(cs, 7));
    }
    // Ranks in 16 bits (a board has at most 2 * 1,081 hands), so the pair loop above is narrow.
    std::sort(all.begin(), all.end());
    all.erase(std::unique(all.begin(), all.end()), all.end());
    auto rank = [&](uint32_t v) { return static_cast<int16_t>(std::lower_bound(all.begin(), all.end(), v) - all.begin()); };
    int16_t* q0 = r0.data() + static_cast<size_t>(nb) * H0;
    int16_t* q1 = r1.data() + static_cast<size_t>(nb) * H1;
    for (int h = 0; h < H0; ++h) q0[h] = s0[h] ? rank(s0[h]) : int16_t(-1);
    for (int o = 0; o < H1; ++o) q1[o] = s1[o] ? rank(s1[o]) : INT16_MAX;  // above every rank: counts 0
    if (++nb == B) flush();
  });
  flush();
  // Pairs sharing a card never meet: their counts are 0, so a reach on them contributes nothing.
  for (int h = 0; h < H0; ++h)
    for (int k = 0; k < 2; ++k)
      for (uint16_t o : g.hands_with_card[1][g.hcard[0][k][h]]) e2[static_cast<size_t>(h) * H1 + o] = 0;
}

// A full table as filled above, made compact: AllinRunouts::tri8 / tri16.
template <class E, class T>
void compact_allin_table(const GameTree& g, AllinRunouts& ar, const std::vector<E>& e2, std::vector<T>& tri) {
  const size_t H = static_cast<size_t>(g.num_hands(0));
  const size_t n = ar.live.size();
  tri.assign(n * (n - (n > 0)) / 2, 0);
  T* t = tri.data();
  for (size_t i = 1; i < n; ++i) {
    const E* row = e2.data() + ar.live[i] * H;
    const uint64_t mi = combo_mask(g.hands[0][ar.live[i]]);
    for (size_t j = 0; j < i; ++j, ++t)
      if (!(combo_mask(g.hands[0][ar.live[j]]) & mi)) *t = static_cast<T>(static_cast<int>(row[ar.live[j]]) - ar.valid);
  }
}
}  // namespace

void GameTree::build_allin_tables() {
  const int H0 = num_hands(0), H1 = num_hands(1);
  std::vector<uint32_t> todo;
  bool built = false;
  for (uint32_t i = 0; i < allins.size(); ++i) {
    if (!allins[i].table) continue;
    todo.push_back(i);
    built |= allins[i].built();
  }
  if (todo.empty() || built) return;
  // Pairs sharing a card never meet: their counts stay 0, so a reach on them contributes nothing.
  std::vector<uint8_t> compat;
  for (uint32_t i : todo)
    if (!allins[i].boards.empty() && compat.empty()) {
      compat.resize(static_cast<size_t>(H0) * H1);
      for (int h = 0; h < H0; ++h) {
        const uint64_t mh = combo_mask(hands[0][h]);
        for (int o = 0; o < H1; ++o) compat[static_cast<size_t>(h) * H1 + o] = (combo_mask(hands[1][o]) & mh) == 0;
      }
    }
  const bool use_gpu = gpu_tables && metal_available();
  std::function<void(int)> fn = [&](int k) {
    AllinRunouts& ar = allins[todo[static_cast<size_t>(k)]];
    ar.valid = static_cast<int>(std::lround(1.0 / static_cast<double>(ar.scale)));
    const bool small = ar.runouts <= 127;  // 2 * 127 fits uint8
    std::vector<uint8_t> full8;  // a compact table is filled as a full one first, which then goes
    std::vector<uint16_t> full16;
    const bool tri = compact_tables();
    if (tri) {
      uint64_t mask = 0;
      for (uint8_t card : ar.cards) mask |= card_bit(card);
      ar.live.clear();
      for (int h = 0; h < H0; ++h)
        if (!(combo_mask(hands[0][h]) & mask)) ar.live.push_back(static_cast<uint16_t>(h));
    }
    const bool wide_postflop = !cfg.preflop && H0 * H1 >= 64 * 1024;
    if (use_gpu && ar.cards.size() == 3 && ((tri && ar.boards.empty()) || wide_postflop)) {
      std::vector<std::array<uint32_t, 4>> boards;
      boards.reserve(ar.runouts);
      for_each_runout(ar, [&](const std::vector<int>& cards) {
        std::array<uint32_t, 4> suits{};
        for (int card : cards) suits[card & 3] |= 1u << (card >> 2);
        boards.push_back(suits);
      });
      try {
        metal_flop_table(*this, ar, boards);
        return;
      } catch (const std::exception&) {
        if (tri) throw;
      }
    }
    std::vector<uint8_t>& t8 = tri ? full8 : ar.e2b;
    std::vector<uint16_t>& t16 = tri ? full16 : ar.e2;
    if (ar.boards.empty()) small ? fill_allin_table(*this, ar, t8) : fill_allin_table(*this, ar, t16);
    else small ? fill_allin_table(*this, ar, compat, t8) : fill_allin_table(*this, ar, compat, t16);
    if (tri) small ? compact_allin_table(*this, ar, full8, ar.tri8) : compact_allin_table(*this, ar, full16, ar.tri16);
  };
  pool().parallel_for(static_cast<int>(todo.size()), fn);
}

uint64_t GameTree::memory_bytes_for(bool compress, bool with_preds, bool compact) const {
  const uint64_t q = compact && PIO_COMPACT_ROWS && !keep_sets.empty() ? storage_compact : storage_floats;
  uint64_t b = compress ? q * 4 + num_decisions * (8 + sizeof(uint64_t)) : storage_floats * sizeof(float) * 2;
  // Predictive CFR's `preds` is never compressed and is allocated after the build, so the
  // pre-allocation check has to be told about it (#98).
  if (with_preds || !preds.empty()) b += storage_floats * sizeof(float);
  b += nodes.size() * sizeof(GNode) + dec_board.size() * sizeof(uint64_t);
  b += chances.size() * (sizeof(ChanceInfo) + 64);
  for (auto& s : showdowns)
    b += (s.ent[0].size() + s.ent[1].size()) * sizeof(SdEntry) + (s.groups[0].size() + s.groups[1].size()) * 4 +
         sizeof(ShowdownBoard);
  for (auto& a : allins)
    if (a.table) b += allin_table_bytes(a);  // built by allocate_storage, counted before as after
  return b;
}

void GameTree::allocate_storage() {
  // The all-in outcome tables belong with the storage: built only for a tree that will be solved,
  // not for estimate_tree's throwaway build or a build the memory check rejects (#185 review).
  build_allin_tables();
  allocate_strategy_storage();
}

void GameTree::allocate_strategy_storage() {
  regrets.clear();
  regrets.shrink_to_fit();
  strat.clear();
  strat.shrink_to_fit();
  regrets_q.clear();
  regrets_q.shrink_to_fit();
  strat_q.clear();
  strat_q.shrink_to_fit();
  // resize() on an empty ZVec is a fresh calloc: zero pages, committed only when touched.
  if (compressed) {
    regrets_q.resize(storage_q);
    strat_q.resize(storage_q);
    rscale.assign(num_decisions, 0.0f);
    sscale.assign(num_decisions, 0.0f);
  } else {
    regrets.resize(storage_floats);
    strat.resize(storage_floats);
  }
}

void GameTree::decompress() {
  if (!compressed) return;
  regrets.resize(storage_floats);
  strat.resize(storage_floats);
  for (const GNode& n : nodes) {
    if (n.kind != ANodeKind::Decision) continue;
    decode_rows(n, regrets_q.data() + qo(n), rscale[n.aux], regrets.data() + n.storage);
    decode_rows(n, strat_q.data() + qo(n), sscale[n.aux], strat.data() + n.storage);
  }
  compressed = false;
  regrets_q.clear();
  regrets_q.shrink_to_fit();
  strat_q.clear();
  strat_q.shrink_to_fit();
}

void GameTree::clear_storage() {
  lazy.reset();  // everything is zero now: nothing left to read from a save file
  std::fill(regrets.begin(), regrets.end(), 0.0f);
  std::fill(strat.begin(), strat.end(), 0.0f);
  std::fill(preds.begin(), preds.end(), 0.0f);
  std::fill(regrets_q.begin(), regrets_q.end(), int16_t(0));
  std::fill(strat_q.begin(), strat_q.end(), uint16_t(0));
  std::fill(rscale.begin(), rscale.end(), 0.0f);
  std::fill(sscale.begin(), sscale.end(), 0.0f);
}

// 16-bit rows (compact or full, #239) -> the node's full A x H floats; dropped hands read 0 (+0, like a
// stored 0 times a scale >= 0).
template <class Q>
void GameTree::decode_rows(const GNode& n, const Q* q, float s, float* out) const {
  const uint16_t* k = keep_of(n);
  if (!k) {
    const size_t len = node_size(n);
    for (size_t i = 0; i < len; ++i) out[i] = static_cast<float>(q[i]) * s;
    return;
  }
#if PIO_COMPACT_ROWS
  // Expand-load 32 kept values at a time into their hands' lanes (dropped hands 0), then convert like
  // the loop above: int -> float, times s (the same single multiply, so the same floats).
  const size_t H = hands[n.player].size();
  const uint32_t* bits = keep_bits(n);
  const __m512 sv = _mm512_set1_ps(s);
  const Q* src = q;  // the rows are consecutive, each popcount(bits) = row_len values long
  for (size_t a = 0; a < n.num_children; ++a) {
    float* o = out + a * H;
    for (size_t h = 0; h < H; h += 32) {
      const __mmask32 m = bits[h / 32];
      const __m512i v = expand32(m, src);
      src += __builtin_popcount(m);
      const __m256i v0 = _mm512_castsi512_si256(v), v1 = _mm512_extracti64x4_epi64(v, 1);
      __m512i w0, w1;
      if constexpr (std::is_signed_v<Q>) {
        w0 = _mm512_cvtepi16_epi32(v0);
        w1 = _mm512_cvtepi16_epi32(v1);
      } else {
        w0 = _mm512_cvtepu16_epi32(v0);
        w1 = _mm512_cvtepu16_epi32(v1);
      }
      const size_t rem = H - h;
      const __mmask16 m0 = rem >= 16 ? 0xffff : static_cast<__mmask16>((1u << rem) - 1);
      const __mmask16 m1 = rem >= 32 ? 0xffff : rem > 16 ? static_cast<__mmask16>((1u << (rem - 16)) - 1) : 0;
      _mm512_mask_storeu_ps(o + h, m0, _mm512_mul_ps(_mm512_cvtepi32_ps(w0), sv));
      if (m1) _mm512_mask_storeu_ps(o + h + 16, m1, _mm512_mul_ps(_mm512_cvtepi32_ps(w1), sv));
    }
  }
#else
  const size_t H = hands[n.player].size(), L = row_len(n);
  for (size_t a = 0; a < n.num_children; ++a) {
    float* o = out + a * H;
    const Q* qa = q + a * L;
    std::fill(o, o + H, 0.0f);
    for (size_t j = 0; j < L; ++j) o[k[j]] = static_cast<float>(qa[j]) * s;
  }
#endif
}
template void GameTree::decode_rows<int16_t>(const GNode&, const int16_t*, float, float*) const;
template void GameTree::decode_rows<uint16_t>(const GNode&, const uint16_t*, float, float*) const;

// Compact nodes encode with their usual full-row loops into this per-thread buffer, then compress_rows
// keeps the kept hands' values: the arithmetic is the full-row code's, by construction.
template <class Q>
static Q* full_rows_tmp(size_t len) {
  thread_local std::vector<Q> buf;
  if (buf.size() < len) buf.resize(len);
  return buf.data();
}

// A node's full 16-bit rows -> its compact stored rows (q[a * L + j] = full[a * H + keep[j]]).
template <class Q>
static void compress_rows(const GameTree& g, const GNode& n, const Q* full, Q* q) {
  const size_t H = g.hands[n.player].size();
#if PIO_COMPACT_ROWS
  const uint32_t* bits = g.keep_bits(n);
  for (size_t a = 0; a < n.num_children; ++a) {
    const Q* src = full + a * H;
    for (size_t h = 0; h < H; h += 32) {
      const __mmask32 m = bits[h / 32];
      const size_t rem = H - h;
      q += compress32(m, _mm512_maskz_loadu_epi16(low_bits32(static_cast<int>(rem)), src + h), q);
    }
  }
#else
  const uint16_t* k = g.keep_of(n);
  const size_t L = g.row_len(n);
  for (size_t a = 0; a < n.num_children; ++a)
    for (size_t j = 0; j < L; ++j) q[a * L + j] = full[a * H + k[j]];
#endif
}

const float* GameTree::regrets_ro(const GNode& n, float* scratch) const {
  ensure_loaded(n);
  if (!compressed) return regrets.data() + n.storage;
  decode_rows(n, regrets_q.data() + qo(n), rscale[n.aux], scratch);
  return scratch;
}

const float* GameTree::strat_ro(const GNode& n, float* scratch) const {
  ensure_loaded(n);
  if (!compressed) return strat.data() + n.storage;
  decode_rows(n, strat_q.data() + qo(n), sscale[n.aux], scratch);
  return scratch;
}

float* GameTree::regrets_rw(const GNode& n, float* scratch) {
  ensure_loaded(n);
  if (!compressed) return regrets.data() + n.storage;
  regrets_ro(n, scratch);
  return scratch;
}

float* GameTree::strat_rw(const GNode& n, float* scratch) {
  ensure_loaded(n);
  if (!compressed) return strat.data() + n.storage;
  strat_ro(n, scratch);
  return scratch;
}

void GameTree::regrets_commit(const GNode& n, const float* data, float maxabs) {
  if (!compressed) return;
  ensure_loaded(n);
  regrets_store(n, data, maxabs);
}

void GameTree::regrets_store(const GNode& n, const float* data, float maxabs) {
  const size_t len = node_size(n);
  float m = maxabs;
  if (m < 0) {
    uint32_t mb = 0;
    for (size_t i = 0; i < len; ++i) mb = std::max(mb, abs_bits(data[i]));
    m = std::bit_cast<float>(mb);
  }
  int16_t* q = regrets_q.data() + qo(n);
  if (!(m > 0) || !std::isfinite(m)) {
    rscale[n.aux] = 0;
    std::fill(q, q + stored_size(n), int16_t(0));
    return;
  }
  rscale[n.aux] = m / 32767.0f;
  const float inv = 32767.0f / m;
  const bool compact = keep_of(n) != nullptr;
  int16_t* dst = compact ? full_rows_tmp<int16_t>(len) : q;
  for (size_t i = 0; i < len; ++i) {
    float x = data[i] * inv;
    dst[i] = static_cast<int16_t>(x + (x >= 0.0f ? 0.5f : -0.5f));
  }
  if (compact) compress_rows(*this, n, dst, q);
}

// The kRoundTable points (k + 0.5) / kRoundTable of [0, 1) in a fixed shuffled order, repeated once so a
// block of up to kRoundTable reads never wraps. An exact grid, not random draws: a table of random
// draws has a fixed empirical bias (4096 draws: about 5% in the share above 0.9), which every rounding
// would then inherit; the grid's bias is at most half a grid step.
static const float* rounding_uniforms() {
  static const std::vector<float> t = [] {
    constexpr size_t N = GameTree::kRoundTable;
    std::vector<float> v(2 * N);
    for (size_t k = 0; k < N; ++k) v[k] = (static_cast<float>(k) + 0.5f) / static_cast<float>(N);
    uint64_t s = 0x243F6A8885A308D3ull;
    for (size_t k = N - 1; k > 0; --k) {  // Fisher-Yates with splitmix64
      s += 0x9E3779B97F4A7C15ull;
      uint64_t z = s;
      z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
      z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
      z ^= z >> 31;
      std::swap(v[k], v[z % (k + 1)]);
    }
    for (size_t k = 0; k < N; ++k) v[k + N] = v[k];
    return v;
  }();
  return t.data();
}

void GameTree::ensure_combo_lock_copies() {
  if (!compressed) return;
  std::vector<float> buf;
  for (auto& [ni, mask] : combo_lock) {
    if (combo_lock_s.count(ni) || ni >= nodes.size()) continue;
    const GNode& n = nodes[ni];
    buf.resize(node_size(n));
    const float* s = strat_ro(n, buf.data());
    combo_lock_s[ni].assign(s, s + node_size(n));
  }
}

void GameTree::strat_commit(const GNode& n, const float* data, float maxval) {
  if (!compressed) return;
  ensure_loaded(n);
  const size_t len = node_size(n);
  std::vector<float> fixed;
  if (n.flags & GF_COMBO_LOCKED) {
    // locked entries from their exact copy, not re-rounded from last iteration's quantized value (#380)
    const size_t ni = static_cast<size_t>(&n - nodes.data());
    auto c = ni < nodes.size() ? combo_lock_s.find(static_cast<uint32_t>(ni)) : combo_lock_s.end();
    auto mk = ni < nodes.size() ? combo_lock.find(static_cast<uint32_t>(ni)) : combo_lock.end();
    if (c != combo_lock_s.end() && mk != combo_lock.end() && c->second.size() == len) {
      const size_t H = hands[n.player].size();
      fixed.assign(data, data + len);
      for (size_t h = 0; h < H && h < mk->second.size(); ++h)
        if (mk->second[h])
          for (size_t a = 0; a < static_cast<size_t>(n.num_children); ++a) fixed[a * H + h] = c->second[a * H + h];
      data = fixed.data();
      maxval = -1.0f;
    }
  }
  float m = maxval;
  if (m < 0) {
    uint32_t mb = 0;
    for (size_t i = 0; i < len; ++i) mb = std::max(mb, nonneg_bits(data[i]));
    m = std::bit_cast<float>(mb);
  }
  uint16_t* q = strat_q.data() + qo(n);
  if (!(m > 0) || !std::isfinite(m)) {
    sscale[n.aux] = 0;
    std::fill(q, q + stored_size(n), uint16_t(0));
    return;
  }
  sscale[n.aux] = m / 65535.0f;
  const float inv = 65535.0f / m;
  // Stochastic rounding (see round_seed): floor(x + u), u uniform in [0, 1) read from a fixed table of
  // uniforms at an offset that a hash of the node's slot and the iteration's seed picks.
  const float* U = rounding_uniforms();
  uint32_t h = static_cast<uint32_t>(n.storage) * 0x9E3779B1u ^ round_seed;
  h ^= h >> 16;
  h *= 0x85EBCA6Bu;
  h ^= h >> 13;
  h *= 0xC2B2AE35u;
  h ^= h >> 16;
  // Compact rows (#239) round in full-row order too, so every value draws the uniform it always drew.
  const bool compact = keep_of(n) != nullptr;
  uint16_t* dst = compact ? full_rows_tmp<uint16_t>(len) : q;
  size_t i = 0;
  while (i < len) {
    const size_t start = (h + i) & (kRoundTable - 1), blk = std::min(len - i, kRoundTable);
    const float* u = U + start;
    for (size_t k = 0; k < blk; ++k)
      dst[i + k] = static_cast<uint16_t>(std::min(65535.0f, std::max(0.0f, data[i + k]) * inv + u[k]));
    i += blk;
  }
  if (compact) compress_rows(*this, n, dst, q);
}

void GameTree::strat_store(const GNode& n, const float* data, float maxval) {
  const size_t len = node_size(n);
  float m = maxval;
  if (m < 0) {
    uint32_t mb = 0;
    for (size_t i = 0; i < len; ++i) mb = std::max(mb, nonneg_bits(data[i]));
    m = std::bit_cast<float>(mb);
  }
  uint16_t* q = strat_q.data() + qo(n);
  if (!(m > 0) || !std::isfinite(m)) {
    sscale[n.aux] = 0;
    std::fill(q, q + stored_size(n), uint16_t(0));
    return;
  }
  sscale[n.aux] = m / 65535.0f;
  const float inv = 65535.0f / m;
  const bool compact = keep_of(n) != nullptr;
  uint16_t* dst = compact ? full_rows_tmp<uint16_t>(len) : q;
  for (size_t i = 0; i < len; ++i) dst[i] = static_cast<uint16_t>(std::max(0.0f, data[i]) * inv + 0.5f);
  if (compact) compress_rows(*this, n, dst, q);
}

StreetSegments GameTree::street_segments() const {
  StreetSegments r;
  r.node_seg.assign(nodes.size(), UINT32_MAX);
  if (nodes.empty()) return r;
  // Depth-first from the root; a decision node reached from a chance node starts a new segment.
  std::vector<std::pair<uint32_t, uint32_t>> st{{0u, UINT32_MAX}};  // (node, segment of its parent decision)
  while (!st.empty()) {
    auto [ni, ps] = st.back();
    st.pop_back();
    const GNode& n = nodes[ni];
    uint32_t seg = UINT32_MAX;
    if (n.kind == ANodeKind::Decision) {
      seg = ps;
      if (seg == UINT32_MAX) {
        seg = static_cast<uint32_t>(r.segs.size());
        r.segs.emplace_back();
      }
      r.node_seg[ni] = seg;
      r.segs[seg].push_back(ni);
    } else if (n.kind != ANodeKind::Chance) {
      continue;
    }
    for (int k = n.num_children - 1; k >= 0; --k) st.push_back({n.first_child + static_cast<uint32_t>(k), seg});
  }
  return r;
}

void GameTree::lazy_touch(uint32_t node) const {
  LazyStore& L = *lazy;
  uint32_t s = L.seg.node_seg[node];
  if (s == UINT32_MAX || L.loaded[s].load(std::memory_order_acquire)) return;
  std::lock_guard<std::mutex> g(L.mu[s % 64]);
  if (L.loaded[s].load(std::memory_order_relaxed)) return;
  try {
    L.load_fn(*this, L, s);
  } catch (const std::exception& e) {
    // Leave the segment unloaded (zeros for now, retried on next access) and report via take_lazy_error.
    std::lock_guard<std::mutex> eg(L.err_mu);
    if (!L.failed.exchange(true)) L.error = e.what();
    return;
  } catch (...) {  // nothing may escape into a pool worker (#360)
    std::lock_guard<std::mutex> eg(L.err_mu);
    if (!L.failed.exchange(true)) L.error = "segment read failed";
    return;
  }
  L.loaded[s].store(1, std::memory_order_release);
  L.remaining.fetch_sub(1);
}

bool GameTree::take_lazy_error(std::string& err) {
  if (!lazy || !lazy->failed.load()) return false;
  std::lock_guard<std::mutex> eg(lazy->err_mu);
  err = lazy->error;
  lazy->failed = false;
  return true;
}

bool GameTree::load_all(std::string& err) {
  if (!lazy) return true;
  LazyStore& L = *lazy;
  const int n = static_cast<int>(L.seg.segs.size());
  pool().parallel_for(n, [&](int s) {
    if (!L.loaded[s].load(std::memory_order_acquire)) lazy_touch(L.seg.segs[s][0]);
  });
  if (take_lazy_error(err)) return false;
  lazy.reset();
  return true;
}

}  // namespace pio
