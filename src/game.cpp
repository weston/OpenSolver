#include "game.hpp"
#include "parallel.hpp"
#include "pfh2h.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <numeric>

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

double GameTree::terminal_payoff(const ActionNode& an, int p, int outcome, bool subtree) const {
  // Investments are absolute (they include set_pot's per-player chips); `dead` is the (sub)tree's
  // dead money. Payoff = share of the final pot - own investment.
  double D = an.dead;
  double chips;
  if (an.kind == ANodeKind::Fold) {
    int f = an.player;
    double matched = an.inv[f];
    double P = D + 2.0 * matched;
    double R = std::min(payoff.rake_frac * P, payoff.rake_cap);
    chips = (p == f) ? -static_cast<double>(an.inv[f]) : D + an.inv[f] - R;
  } else {
    double P = D + an.inv[0] + an.inv[1];
    double R = std::min(payoff.rake_frac * P, payoff.rake_cap);
    if (outcome > 0) chips = P - R - an.inv[p];
    else if (outcome < 0) chips = -static_cast<double>(an.inv[p]);
    else chips = (P - R) / 2.0 - an.inv[p];
  }
  if (!subtree) chips += an.offset[p];
  if (!payoff.icm.enabled) return chips;
  double s = payoff.icm.stacks[p];
  return payoff.icm.value(p, s + chips) - payoff.icm.value(p, s);
}

int GameTree::showdown_board_id(const std::vector<int>& board) {
  uint64_t mask = board_mask(board);
  auto it = showdown_index_.find(mask);
  if (it != showdown_index_.end()) return it->second;
  ShowdownBoard sb;
  sb.mask = mask;
  int cs[7];
  for (int i = 0; i < 5; ++i) cs[i] = board[i];
  std::vector<uint32_t> str[2];
  for (int p = 0; p < 2; ++p) {
    std::vector<std::pair<uint32_t, uint16_t>> v;
    v.reserve(hands[p].size());
    for (size_t h = 0; h < hands[p].size(); ++h) {
      if (combo_mask(hands[p][h]) & mask) continue;
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
  int id = static_cast<int>(showdowns.size());
  showdowns.push_back(std::move(sb));
  showdown_index_[mask] = id;
  return id;
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
    flop_compat[p].assign(H, 0.0f);
    flop_card[p].assign(H * NUM_CARDS, 0.0f);
    flop_pair[p].assign(H * NUM_COMBOS, 0.0f);
    for (auto& f : cfg.subset) {
      const float w = static_cast<float>(f.weight / W * gn);
      for (uint8_t pm : perm_all) {
        int c[3] = {PERM_CARD[pm][f.cards[0]], PERM_CARD[pm][f.cards[1]], PERM_CARD[pm][f.cards[2]]};
        uint64_t fm = card_bit(c[0]) | card_bit(c[1]) | card_bit(c[2]);
        int pr[3] = {combo_index(c[0], c[1]), combo_index(c[0], c[2]), combo_index(c[1], c[2])};
        for (size_t h = 0; h < H; ++h) {
          if (combo_mask(hands[p][h]) & fm) continue;
          flop_compat[p][h] += w;
          for (int k = 0; k < 3; ++k) {
            flop_card[p][h * NUM_CARDS + c[k]] += w;
            flop_pair[p][h * NUM_COMBOS + pr[k]] += w;
          }
        }
      }
    }
  }
}

int GameTree::allin_runouts_id(const std::vector<int>& board) {
  uint64_t mask = board_mask(board);
  auto it = allin_index_.find(mask);
  if (it != allin_index_.end()) return it->second;
  AllinRunouts ar;
  std::vector<int> avail;
  for (int c = 0; c < NUM_CARDS; ++c)
    if (!(mask & card_bit(c))) avail.push_back(c);
  std::vector<int> b = board;
  if (board.empty()) {
    // Preflop all-in (or a pure preflop tree's exits): exact equity via the hand-vs-hand table
    // (Solver::showdown_value handles `exact` entries; no sampled boards).
    ar.exact = true;
    ar.scale = 1.0f;
    // Load or generate the table now, on the building thread. Generating it lazily from inside a
    // solver worker would deadlock: generation uses the thread pool, whose other workers would be
    // blocked on this static initialiser.
    (void)pf_table();
  } else if (board.size() == 4) {
    for (int c : avail) {
      b.push_back(c);
      ar.boards.push_back(static_cast<uint32_t>(showdown_board_id(b)));
      b.pop_back();
    }
    ar.scale = 1.0f / static_cast<float>(avail.size() - 4);  // rivers compatible with both hands
  } else {
    for (size_t x = 0; x < avail.size(); ++x)
      for (size_t y = x + 1; y < avail.size(); ++y) {
        b.push_back(avail[x]);
        b.push_back(avail[y]);
        ar.boards.push_back(static_cast<uint32_t>(showdown_board_id(b)));
        b.pop_back();
        b.pop_back();
      }
    double k = static_cast<double>(avail.size() - 4);
    ar.scale = static_cast<float>(2.0 / (k * (k - 1.0)));  // 1 / C(k, 2)
  }
  int id = static_cast<int>(allins.size());
  allins.push_back(std::move(ar));
  allin_index_[mask] = id;
  return id;
}

namespace {

struct Filler {
  GameTree& g;
  bool iso;

  void fill(uint32_t idx, const ActionNode* an, std::vector<int>& board) {
    GNode n{};
    n.kind = an->kind;
    n.player = an->player;
    n.board_len = static_cast<uint8_t>(board.size());
    n.anode = static_cast<uint32_t>(an->id);
    switch (an->kind) {
      case ANodeKind::Decision: {
        n.aux = static_cast<uint32_t>(g.num_decisions++);
        n.storage = g.storage_floats;
        g.storage_floats += an->children.size() * g.hands[an->player].size();
        n.num_children = static_cast<uint16_t>(an->children.size());
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
  incentives.clear();
  storage_floats = 0;
  num_decisions = 0;
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
      if (!sch) {
        std::string l;
        for (int32_t v : x->line) l += (l.empty() ? "" : " ") + std::to_string(v);
        err = "no schematic tree attached to the preflop exit reached by line " + l;
        return false;
      }
      ActionTreeParams sp;
      sp.board_len = 3;
      sp.eff_stack = sch->eff_stack;
      sp.start_inv[0] = sch->pot[0];
      sp.start_inv[1] = sch->pot[1];
      sp.dead = sch->pot[2];
      // The schematic's pot stands for the preflop chips; subtract this player's preflop investment
      // (relative to its schematic share) so preflop decisions see whole-hand payoffs.
      // Like Pio, the schematic's set_pot is taken literally: its per-player chips are investments that
      // payoffs subtract, its dead money is free money. (For whole-hand chips, give each player's
      // preflop investment in set_pot, e.g. `set_pot 100 100 0` after a 3-bet to 100.)
      auto sroot = build_action_tree(sp, sch->lines, err);
      if (!sroot) return false;
      x->children.push_back(std::move(sroot));
    }
  }
  index_action_tree(aroot.get(), anodes);

  bool iso = (bl == 3 && cfg.iso_flop) || (bl == 4 && cfg.iso_turn);
  nodes.reserve(1024);
  nodes.resize(1);
  std::vector<int> board = cfg.board;
  Filler f{*this, iso};
  f.fill(0, aroot.get(), board);
  if (allocate) allocate_storage();
  return true;
}

uint64_t GameTree::memory_bytes_for(bool compress) const {
  uint64_t b = compress ? storage_floats * 4 + num_decisions * 8 : storage_floats * sizeof(float) * 2;
  b += nodes.size() * sizeof(GNode);
  b += chances.size() * (sizeof(ChanceInfo) + 64);
  for (auto& s : showdowns)
    b += (s.ent[0].size() + s.ent[1].size()) * sizeof(SdEntry) + (s.groups[0].size() + s.groups[1].size()) * 4 +
         sizeof(ShowdownBoard);
  return b;
}

void GameTree::allocate_storage() {
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
    regrets_q.resize(storage_floats);
    strat_q.resize(storage_floats);
    rscale.assign(num_decisions, 0.0f);
    sscale.assign(num_decisions, 0.0f);
  } else {
    regrets.resize(storage_floats);
    strat.resize(storage_floats);
  }
}

void GameTree::clear_storage() {
  lazy.reset();  // everything is zero now: nothing left to read from a save file
  std::fill(regrets.begin(), regrets.end(), 0.0f);
  std::fill(strat.begin(), strat.end(), 0.0f);
  std::fill(regrets_q.begin(), regrets_q.end(), int16_t(0));
  std::fill(strat_q.begin(), strat_q.end(), uint16_t(0));
  std::fill(rscale.begin(), rscale.end(), 0.0f);
  std::fill(sscale.begin(), sscale.end(), 0.0f);
}

const float* GameTree::regrets_ro(const GNode& n, float* scratch) const {
  ensure_loaded(n);
  if (!compressed) return regrets.data() + n.storage;
  const size_t len = node_size(n);
  const int16_t* q = regrets_q.data() + n.storage;
  const float s = rscale[n.aux];
  for (size_t i = 0; i < len; ++i) scratch[i] = static_cast<float>(q[i]) * s;
  return scratch;
}

const float* GameTree::strat_ro(const GNode& n, float* scratch) const {
  ensure_loaded(n);
  if (!compressed) return strat.data() + n.storage;
  const size_t len = node_size(n);
  const uint16_t* q = strat_q.data() + n.storage;
  const float s = sscale[n.aux];
  for (size_t i = 0; i < len; ++i) scratch[i] = static_cast<float>(q[i]) * s;
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
    m = 0;
    for (size_t i = 0; i < len; ++i) {
      float x = std::fabs(data[i]);
      m = x > m ? x : m;
    }
  }
  int16_t* q = regrets_q.data() + n.storage;
  if (!(m > 0) || !std::isfinite(m)) {
    rscale[n.aux] = 0;
    std::fill(q, q + len, int16_t(0));
    return;
  }
  rscale[n.aux] = m / 32767.0f;
  const float inv = 32767.0f / m;
  for (size_t i = 0; i < len; ++i) {
    float x = data[i] * inv;
    q[i] = static_cast<int16_t>(x + (x >= 0.0f ? 0.5f : -0.5f));
  }
}

void GameTree::strat_commit(const GNode& n, const float* data, float maxval) {
  if (!compressed) return;
  ensure_loaded(n);
  strat_store(n, data, maxval);
}

void GameTree::strat_store(const GNode& n, const float* data, float maxval) {
  const size_t len = node_size(n);
  float m = maxval;
  if (m < 0) {
    m = 0;
    for (size_t i = 0; i < len; ++i) m = data[i] > m ? data[i] : m;
  }
  uint16_t* q = strat_q.data() + n.storage;
  if (!(m > 0) || !std::isfinite(m)) {
    sscale[n.aux] = 0;
    std::fill(q, q + len, uint16_t(0));
    return;
  }
  sscale[n.aux] = m / 65535.0f;
  const float inv = 65535.0f / m;
  for (size_t i = 0; i < len; ++i) q[i] = static_cast<uint16_t>(std::max(0.0f, data[i]) * inv + 0.5f);
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
