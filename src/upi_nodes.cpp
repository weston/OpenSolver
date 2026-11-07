// UPI node navigation, queries, strategies, locking and line commands.
#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <set>
#include <tuple>
#include <sstream>

#include "categories.hpp"
#include "equity.hpp"
#include "eval.hpp"
#include "parallel.hpp"
#include "range.hpp"
#include "upi.hpp"

namespace pio {

namespace {

std::vector<std::string> split_colon(const std::string& s) {
  std::vector<std::string> out;
  std::string cur;
  for (char ch : s) {
    if (ch == ':') {
      out.push_back(cur);
      cur.clear();
    } else {
      cur += ch;
    }
  }
  out.push_back(cur);
  return out;
}

std::vector<float> parse_floats(const std::vector<std::string>& a, size_t from) {
  std::vector<float> v;
  v.reserve(a.size() - std::min(a.size(), from));
  for (size_t i = from; i < a.size(); ++i) {
    char* end = nullptr;
    float f = std::strtof(a[i].c_str(), &end);
    if (!end || *end != '\0' || !std::isfinite(f)) throw CmdError{"incorrect or missing argument"};
    v.push_back(f);
  }
  return v;
}


int street_arg(const std::string& s) {
  if (s == "flop" || s == "3") return 3;
  if (s == "turn" || s == "4") return 4;
  if (s == "river" || s == "5") return 5;
  throw CmdError{"incorrect or missing argument"};
}

}  // namespace

int Upi::display_card(int c) const { return PERM_CARD[display_perm_][c]; }
int Upi::original_card(int c) const { return PERM_CARD[PERM_INV[display_perm_]][c]; }

NodeRef Upi::resolve(const std::string& id) const {
  require_tree();
  NodeRef r;
  r.frame = display_perm_;
  for (int c : tree_->cfg.board) r.board.push_back(display_card(c));
  auto toks = split_colon(id);
  if (toks.empty() || toks[0] != "r") throw CmdError{"incorrect path"};
  r.id = "r";
  if (toks.size() == 1) {
    r.pseudo_root = true;
    return r;
  }
  if (toks[1] != "0") throw CmdError{"incorrect path"};
  r.node = 0;
  r.id = "r:0";
  for (size_t i = 2; i < toks.size(); ++i) {
    const GNode& n = tree_->nodes[r.node];
    const std::string& t = toks[i];
    if (n.kind == ANodeKind::Decision) {
      const ActionNode& an = tree_->an(n);
      int found = -1;
      for (size_t a = 0; a < an.actions.size(); ++a)
        if (action_label(an, an.actions[a]) == t) {
          found = static_cast<int>(a);
          break;
        }
      if (found < 0) throw CmdError{"incorrect path"};
      r.steps.push_back({r.node, found, IDENTITY_PERM});
      r.node = n.first_child + static_cast<uint32_t>(found);
      r.id += ":" + t;
    } else if (n.kind == ANodeKind::Chance && tree_->chances[n.aux].flop_deal) {
      const ChanceInfo& ci = tree_->chances[n.aux];
      int slot = -1;
      for (size_t k = 0; k < ci.flops.size(); ++k) {
        std::string tok = card_str(ci.flops[k][0]) + card_str(ci.flops[k][1]) + card_str(ci.flops[k][2]);
        if (t == "?" ? k == 0 : t == tok) slot = static_cast<int>(k);
      }
      if (slot < 0) throw CmdError{"incorrect path"};
      r.steps.push_back({r.node, slot, IDENTITY_PERM});
      r.node = n.first_child + static_cast<uint32_t>(slot);
      for (uint8_t c : ci.flops[slot]) r.board.push_back(c);
      r.id += ":" + (t == "?" ? std::string("?")
                              : card_str(ci.flops[slot][0]) + card_str(ci.flops[slot][1]) + card_str(ci.flops[slot][2]));
    } else if (n.kind == ANodeKind::Chance) {
      const ChanceInfo& ci = tree_->chances[n.aux];
      int slot, perm, shown;
      if (t == "?") {
        // first runout in display order: the highest card still available
        uint64_t bm = board_mask(r.board);
        int x = NUM_CARDS - 1;
        while (x >= 0 && (bm & card_bit(x))) --x;
        int y = PERM_CARD[PERM_INV[r.frame]][x];
        slot = ci.slot[y];
        perm = ci.perm[y];
        shown = x;
      } else {
        int x = parse_card(t);
        if (x < 0) throw CmdError{"incorrect path"};
        int y = PERM_CARD[PERM_INV[r.frame]][x];
        if (ci.slot[y] < 0) throw CmdError{"incorrect path"};
        slot = ci.slot[y];
        perm = ci.perm[y];
        shown = x;
      }
      r.steps.push_back({r.node, slot, perm});
      r.frame = PERM_COMPOSE[r.frame][perm];
      r.board.push_back(shown);
      r.node = n.first_child + static_cast<uint32_t>(slot);
      r.id += ":" + (t == "?" ? std::string("?") : card_str(shown));
    } else {
      throw CmdError{"incorrect path"};
    }
  }
  return r;
}

std::string Upi::node_type(const NodeRef& r) const {
  if (r.pseudo_root) return "ROOT";
  const GNode& n = tree_->nodes[r.node];
  switch (n.kind) {
    case ANodeKind::Decision: return n.player == 0 ? "OOP_DEC" : "IP_DEC";
    case ANodeKind::Chance: return "SPLIT_NODE";
    default: return "END_NODE";
  }
}

std::string Upi::alg_flag() const {
  // The algorithm is a property of the tree (fixed at build_tree / rebuild_tree), like in Pio.
  if (tree_alg_ == "v4_large") return "V4_LARGE_ALG";
  if (tree_alg_ == "pio_cfr") return "PIO_CFR";
  if (tree_alg_ == "original_pio") return "PIO_ALG";
  return "V4_SMALL_ALG";
}

// Pio layout: id, type, board, pot, "<n> children", "flags: <f1> <f2> " and a blank line; board, pot and
// flags lines carry a trailing space.
std::string Upi::show_node_text(const NodeRef& r) const {
  std::string s = r.id + "\n" + node_type(r) + "\n";
  if (r.board.empty()) s += "---";
  for (int c : r.board) s += card_str(c) + " ";
  s += "\n";
  const bool pf = tree_->cfg.preflop;
  const auto& c = tree_->cfg;
  int children = 1;
  std::vector<std::string> flags;
  if (r.pseudo_root) {
    s += std::to_string(c.pot[0]) + " " + std::to_string(c.pot[1]) + " " + std::to_string(c.pot[2]) + " \n";
    if (tree_->incomplete) flags.push_back("INCOMPLETE_TREE");
    if ((pf || tree_->root_board_len() == 3) && (tree_->cfg.iso_flop)) flags.push_back("ISOMORPHISM_ON");
    if (pf) {
      flags.push_back("SMALL_STRATS");
    } else if (tree_->fast_loaded || tree_->loaded_compressed) {
      if (tree_->fast_loaded) flags.push_back("FAST_LOAD");
      if (tree_->loaded_compressed) flags.push_back("COMPRESSED");
    } else {
      if (tree_->iterations == 0) flags.push_back("NEW_TREE");
      flags.push_back(alg_flag());
    }
  } else {
    const GNode& n = tree_->nodes[r.node];
    const ActionNode& an = tree_->an(n);
    s += std::to_string(an.inv[0]) + " " + std::to_string(an.inv[1]) + " " + std::to_string(an.dead) + " \n";
    if (n.kind == ANodeKind::Decision) children = n.num_children;
    else if (n.kind == ANodeKind::Chance)
      children = tree_->chances[n.aux].flop_deal ? n.num_children : NUM_CARDS - static_cast<int>(r.board.size());
    else children = 0;
    if (n.kind == ANodeKind::Decision) {
      if (n.flags & GF_LOCKED) flags.push_back("LOCKED");
      if (n.flags & GF_COMBO_LOCKED) flags.push_back("COMBO_LOCKED");
      if (n.flags & GF_INCENTIVES) flags.push_back("INCENTIVES");
      if (tree_->recalced.count(r.node)) {
        flags.push_back("ALREADY_RECALCED");
        flags.push_back(alg_flag());
      } else if (pf) {
        if (n.board_len >= 3) flags.push_back("SMALL_STRATS");
      } else if (tree_->loaded_compressed) {
        flags.push_back("COMPRESSED");
      } else {
        flags.push_back(alg_flag());
      }
    }
  }
  s += std::to_string(children) + " children\nflags: ";
  for (auto& f : flags) s += f + " ";
  s += "\n\n";
  return s;
}

std::vector<std::pair<std::string, NodeRef>> Upi::children_of(const NodeRef& r) const {
  std::vector<std::pair<std::string, NodeRef>> out;
  if (r.pseudo_root) {
    out.push_back({"r:0", resolve("r:0")});
    return out;
  }
  const GNode& n = tree_->nodes[r.node];
  if (n.kind == ANodeKind::Decision) {
    const ActionNode& an = tree_->an(n);
    for (size_t a = 0; a < an.actions.size(); ++a) {
      std::string id = r.id + ":" + action_label(an, an.actions[a]);
      out.push_back({id, resolve(id)});
    }
  } else if (n.kind == ANodeKind::Chance && tree_->chances[n.aux].flop_deal) {
    for (auto& f : tree_->chances[n.aux].flops) {
      std::string id = r.id + ":" + card_str(f[0]) + card_str(f[1]) + card_str(f[2]);
      out.push_back({id, resolve(id)});
    }
  } else if (n.kind == ANodeKind::Chance) {
    uint64_t bm = board_mask(r.board);
    for (int x = NUM_CARDS - 1; x >= 0; --x) {  // Pio lists dealt cards from As down to 2c
      if (bm & card_bit(x)) continue;
      std::string id = r.id + ":" + card_str(x);
      out.push_back({id, resolve(id)});
    }
  }
  return out;
}

void Upi::node_reach(const NodeRef& r, std::vector<float>& r0, std::vector<float>& r1) {
  r0 = tree_->init_reach[0];
  r1 = tree_->init_reach[1];
  std::vector<float>* rr[2] = {&r0, &r1};
  for (auto& st : r.steps) {
    const GNode& n = tree_->nodes[st.node];
    if (n.kind == ANodeKind::Decision) {
      int actor = n.player;
      int H = tree_->num_hands(actor);
      std::vector<float> sig(static_cast<size_t>(n.num_children) * H);
      solver_->average_strategy(st.node, sig.data());
      auto& v = *rr[actor];
      for (int h = 0; h < H; ++h) v[h] *= sig[static_cast<size_t>(st.choice) * H + h];
    } else if (tree_->chances[n.aux].flop_deal) {
      for (int p = 0; p < 2; ++p)
        for (uint8_t c : tree_->chances[n.aux].flops[st.choice])
          for (uint16_t h : tree_->hands_with_card[p][c]) (*rr[p])[h] = 0.0f;
    } else {
      const ChanceInfo& ci = tree_->chances[n.aux];
      int card = ci.cards[st.choice];
      for (int p = 0; p < 2; ++p) {
        auto& v = *rr[p];
        if (st.perm != IDENTITY_PERM) {
          std::vector<float> old = v;
          const auto& ph = tree_->perm_hand[p][st.perm];
          for (size_t h = 0; h < v.size(); ++h) v[h] = old[ph[h]];
        }
        for (uint16_t h : tree_->hands_with_card[p][card]) v[h] = 0.0f;
      }
    }
  }
}

void Upi::to_1326(int p, int frame, const float* local, float* out, float fill) const {
  for (int i = 0; i < NUM_COMBOS; ++i) out[i] = fill;
  const auto& hs = tree_->hands[p];
  for (size_t h = 0; h < hs.size(); ++h) out[PERM_COMBO[frame][hs[h]]] = local[h];
}

void Upi::from_1326(int p, int frame, const float* in, float* local) const {
  const auto& hs = tree_->hands[p];
  for (size_t h = 0; h < hs.size(); ++h) local[h] = in[PERM_COMBO[frame][hs[h]]];
}

// ---------------------------------------------------------------------------------------------

std::string Upi::cmd_show_strategy(const Args& a, bool pp) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  NodeRef r = resolve(a[0]);
  if (r.pseudo_root || tree_->nodes[r.node].kind != ANodeKind::Decision) throw CmdError{"incorrect node"};
  ensure_recalced(r);
  const GNode& n = tree_->nodes[r.node];
  int actor = n.player, A = n.num_children, H = tree_->num_hands(actor);
  std::vector<float> sig(static_cast<size_t>(A) * H);
  solver_->average_strategy(r.node, sig.data());
  // Hands not in the player's range print 0: not in the hand list, or with set_union_hands the
  // opponent-only hands, which Pio also prints as 0 here (probe 12) while its _pp lists show them.
  if (!pp) {
    std::string s;
    std::vector<float> line(NUM_COMBOS);
    // Hands the node's board blocks (a turn or river card) print 0, as in Pio and print_all_strats:
    // their rows hold the never-reached regrets of hands that cannot occur here.
    const uint64_t bm = board_mask(r.board);
    for (int k = 0; k < A; ++k) {
      to_1326(actor, r.frame, sig.data() + static_cast<size_t>(k) * H, line.data(), 0.0f);
      for (int c = 0; c < NUM_COMBOS; ++c)
        if (combo_mask(c) & bm) line[c] = 0.0f;
      if (tree_->cfg.union_hands)
        for (int c = 0; c < NUM_COMBOS; ++c)
          if (tree_->cfg.range[actor][PERM_COMBO[PERM_INV[display_perm_]][c]] <= 0) line[c] = 0.0f;
      if (k) s += "\n";
      s += floats_line(line.data(), NUM_COMBOS);
    }
    return s;
  }
  std::vector<float> r0, r1;
  node_reach(r, r0, r1);
  const auto& reach = actor == 0 ? r0 : r1;
  if (r.board.empty()) {
    // Preflop: one reach-weighted line per hand class, in show_preflop_order ("AA : f f f ").
    std::vector<double> num(169 * static_cast<size_t>(A), 0.0), den(169, 0.0);
    for (int h = 0; h < H; ++h) {
      int k = preflop_class(tree_->hands[actor][h]);
      den[k] += reach[h];
      for (int x = 0; x < A; ++x) num[static_cast<size_t>(k) * A + x] += reach[h] * sig[static_cast<size_t>(x) * H + h];
    }
    std::istringstream names(kPreflopOrder);
    std::string s, name;
    for (int k = 0; k < 169 && (names >> name); ++k) {
      // kPreflopOrder lists classes in Pio order; map each name to our class index.
      int cls = preflop_class_of_name(name);
      std::string pad = name.size() < 3 ? name + " " : name;
      s += pad + ": ";
      for (int x = 0; x < A; ++x)
        s += pfmt("%f", den[cls] > 0 ? num[static_cast<size_t>(cls) * A + x] / den[cls] : 0.0) + " ";
      s += "\n";
    }
    if (!s.empty()) s.pop_back();
    return s;
  }
  // pp: "<hand>:   <f>  <f>  " for hands in the range at this node, sorted by equity vs all hands.
  std::array<float, NUM_COMBOS> hero{}, all{};
  for (int c = 0; c < NUM_COMBOS; ++c) all[c] = 1.0f;
  std::vector<std::pair<float, int>> order;
  // With union_hands the list also holds hands only in the opponent's range, which reach
  // this node with 0: Pio lists them too, showing their regret-derived strategy.
  const bool keep_unreached = tree_->cfg.union_hands;
  for (int h = 0; h < H; ++h)
    if (reach[h] > 0 || keep_unreached) hero[PERM_COMBO[r.frame][tree_->hands[actor][h]]] = 1.0f;
  EquityResult eq = range_equity(r.board, hero, all);
  for (int h = 0; h < H; ++h)
    if (reach[h] > 0 || keep_unreached) order.push_back({eq.eq[PERM_COMBO[r.frame][tree_->hands[actor][h]]], h});
  std::stable_sort(order.begin(), order.end(), [](auto& x, auto& y) { return x.first > y.first; });
  std::string s;
  for (auto& [e, h] : order) {
    (void)e;
    s += combo_str(PERM_COMBO[r.frame][tree_->hands[actor][h]]) + ":   ";
    for (int k = 0; k < A; ++k) s += pfmt("%f", sig[static_cast<size_t>(k) * H + h]) + "  ";
    s += "\n";
  }
  return s + "\n";  // rows end in a newline; one more is the blank line Pio prints before END (#125)
}

std::string Upi::cmd_calc_ev(const Args& a, bool pp) {
  if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  NodeRef r = resolve(a[1]);
  if (r.pseudo_root) r = resolve("r:0");
  ensure_recalced(r);
  std::vector<float> r0, r1;
  node_reach(r, r0, r1);
  const float* rp = p == 0 ? r0.data() : r1.data();
  const float* ro = p == 0 ? r1.data() : r0.data();
  int Hp = tree_->num_hands(p);
  std::vector<float> cfv(Hp), mass(Hp);
  solver_->walk(Walk::Evaluate, r.node, p, rp, ro, cfv.data());
  solver_->compat_mass(p, ro, mass.data());
  // Pio semantics: matchups = own reach * compatible opponent mass; EV = cfv / compatible mass, the
  // hand's counterfactual value, for every hand of the player's own range - also one whose reach
  // here is exactly 0 (Pio prints a finite EV there, #247: its average strategy never quite reaches
  // 0, and its on-the-fly river recalcs give 0-reach hands their EV too). Hands outside the own
  // range print +-inf/nan (cfv / 0), untracked hands nan.
  const float nanv = std::numeric_limits<float>::quiet_NaN(), infv = std::numeric_limits<float>::infinity();
  std::vector<float> ev1326(NUM_COMBOS, nanv), m1326(NUM_COMBOS, 0.0f);
  uint64_t bm = board_mask(r.board);
  // Hands only in the opponent's range: Pio tracks them too (EV = x / 0). With union_hands
  // they have a slot, so the loop below computes a real cfv and gets the sign right; this
  // unsigned fallback is only for the narrow lists, where no cfv exists for them at all.
  if (!tree_->cfg.union_hands)
    for (int c = 0; c < NUM_COMBOS; ++c) {
      int orig = PERM_COMBO[PERM_INV[display_perm_]][c];
      if (!(combo_mask(c) & bm) && tree_->cfg.range[p][orig] <= 0 && tree_->cfg.range[1 - p][orig] > 0)
        ev1326[c] = infv;
    }
  for (int h = 0; h < Hp; ++h) {
    int c = PERM_COMBO[r.frame][tree_->hands[p][h]];
    if (combo_mask(c) & bm) continue;
    m1326[c] = rp[h] * mass[h];
    const bool in_range = tree_->cfg.range[p][PERM_COMBO[PERM_INV[display_perm_]][c]] > 0;
    if ((rp[h] > 0 || in_range) && mass[h] > 0) ev1326[c] = cfv[h] / mass[h];
    else ev1326[c] = cfv[h] > 0 ? infv : cfv[h] < 0 ? -infv : nanv;
  }
  if (!pp) return floats_line(ev1326.data(), NUM_COMBOS) + "\n" + floats_line(m1326.data(), NUM_COMBOS);
  double num = 0, den = 0;
  std::vector<std::pair<float, int>> rows;
  for (int h = 0; h < Hp; ++h) {
    int c = PERM_COMBO[r.frame][tree_->hands[p][h]];
    if (combo_mask(c) & bm || !(mass[h] > 0)) continue;
    num += static_cast<double>(rp[h]) * cfv[h];
    den += static_cast<double>(rp[h]) * mass[h];
    rows.push_back({cfv[h] / mass[h], c});
  }
  std::stable_sort(rows.begin(), rows.end(), [](auto& x, auto& y) { return x.first > y.first; });
  std::string s = "EV: " + pfmt("%f", den > 0 ? num / den : 0.0);
  for (auto& [e, c] : rows) s += "\n" + combo_str(c) + ": " + pfmt("%.2f", e) + "; " + pfmt("%.2f", m1326[c]);
  return s + "\n\n";  // blank line before END, like Pio (#125)
}

std::string Upi::cmd_calc_eq(const Args& a, bool pp, bool node) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);
  std::array<float, NUM_COMBOS> hero{}, vil{};
  std::vector<int> board;
  if (node) {
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[1]);
    if (r.pseudo_root) r = resolve("r:0");
    ensure_recalced(r);
    std::vector<float> r0, r1;
    node_reach(r, r0, r1);
    to_1326(p, r.frame, (p == 0 ? r0 : r1).data(), hero.data());
    to_1326(1 - p, r.frame, (p == 0 ? r1 : r0).data(), vil.data());
    board = r.board;
  } else {
    if (cfg_.board.size() < 3) throw CmdError{"incorrect or missing argument"};
    for (int i = 0; i < NUM_COMBOS; ++i) {
      hero[i] = cfg_.range[p][i];
      vil[i] = cfg_.range[1 - p][i];
    }
    board = cfg_.board;
  }
  // Pio reports matchups as weight * compatible villain mass * number of (ordered) runouts.
  int bl = static_cast<int>(board.size());
  double runouts = bl == 3 ? 45.0 * 44.0 : bl == 4 ? 44.0 : 1.0;
  std::array<float, NUM_COMBOS> all_hero{};
  for (int c = 0; c < NUM_COMBOS; ++c) all_hero[c] = node ? hero[c] : 1.0f;  // global: Pio evaluates every hand
  EquityResult er = range_equity(board, all_hero, vil);
  EquityResult tot = range_equity(board, hero, vil);
  uint64_t bm = board_mask(board);
  const float nanv = std::numeric_limits<float>::quiet_NaN(), infv = std::numeric_limits<float>::infinity();
  std::array<float, NUM_COMBOS> eq{}, mu{};
  for (int c = 0; c < NUM_COMBOS; ++c) {
    mu[c] = static_cast<float>(hero[c] * er.matchups[c] * runouts);
    if (combo_mask(c) & bm) eq[c] = nanv;
    else if (hero[c] > 0) eq[c] = er.matchups[c] > 0 ? er.eq[c] : nanv;
    else if (node) eq[c] = nanv;
    else eq[c] = er.eq[c] > 0 ? infv : nanv;  // weight 0: equity / 0
    if (combo_mask(c) & bm) mu[c] = 0.0f;
  }
  if (pp) {
    std::vector<std::pair<float, int>> rows;
    for (int c = 0; c < NUM_COMBOS; ++c)
      if (hero[c] > 0 && !(combo_mask(c) & bm) && mu[c] > 0) rows.push_back({eq[c], c});
    std::stable_sort(rows.begin(), rows.end(), [](auto& x, auto& y) { return x.first > y.first; });
    std::string s = "EV: " + pfmt("%f", tot.total * 100.0);
    for (auto& [e, c] : rows) s += "\n" + combo_str(c) + ": " + pfmt("%.2f", e * 100.0) + "; " + pfmt("%.2f", mu[c]);
    return s + "\n\n";  // blank line before END, like Pio (#125)
  }
  std::string s = floats_line(eq.data(), NUM_COMBOS) + "\n" + floats_line(mu.data(), NUM_COMBOS);
  if (node) s += "\n" + fmt(tot.total);
  return s;
}

std::string Upi::cmd_calc_eq_preflop(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);
  std::array<float, NUM_COMBOS> hero{}, vil{};
  for (int i = 0; i < NUM_COMBOS; ++i) {
    hero[i] = cfg_.range[p][i];
    vil[i] = cfg_.range[1 - p][i];
  }
  int boards = a.size() > 1 ? std::max(100, std::atoi(a[1].c_str())) : 20000;
  PreflopEquity pe = preflop_equity(hero, vil, boards);
  // Output in Pio's class order (show_preflop_order).
  std::vector<float> eq, mu;
  std::istringstream names(kPreflopOrder);
  std::string name;
  while (names >> name) {
    int k = preflop_class_of_name(name);
    eq.push_back(pe.matchups[k] > 0 ? pe.eq[k] : std::numeric_limits<float>::quiet_NaN());  // Pio: nan, not 0
    mu.push_back(pe.matchups[k]);
  }
  return floats_line(eq.data(), static_cast<int>(eq.size())) + "\n" + floats_line(mu.data(), static_cast<int>(mu.size())) +
         "\n" + fmt(pe.total);
}

std::string Upi::cmd_show_nuts(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);  // Pio checks the player before the board argument
  if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
  std::vector<int> board;
  if (!parse_cards(a[1], board) || board.size() < 3) throw CmdError{"incorrect or missing argument"};
  uint64_t bm = board_mask(board);
  std::vector<std::pair<uint32_t, int>> vil;
  for (int c = 0; c < NUM_COMBOS; ++c) {
    if (!(cfg_.range[1 - p][c] > 0) || (combo_mask(c) & bm)) continue;
    std::vector<int> cs = board;
    cs.push_back(COMBOS[c].hi);
    cs.push_back(COMBOS[c].lo);
    vil.push_back({evaluate(cs.data(), static_cast<int>(cs.size())), c});
  }
  std::vector<std::tuple<uint32_t, int, int>> rows;  // strength, combo, beaten-by count
  for (int c = 0; c < NUM_COMBOS; ++c) {  // Pio lists every hand of either range
    if (!(cfg_.range[p][c] > 0 || cfg_.range[1 - p][c] > 0) || (combo_mask(c) & bm)) continue;
    std::vector<int> cs = board;
    cs.push_back(COMBOS[c].hi);
    cs.push_back(COMBOS[c].lo);
    uint32_t s = evaluate(cs.data(), static_cast<int>(cs.size()));
    int beats = 0;
    for (auto& [vs, vc] : vil)
      if (vs > s && !(combo_mask(vc) & combo_mask(c))) ++beats;
    rows.push_back({s, c, beats});
  }
  std::stable_sort(rows.begin(), rows.end(), [](auto& x, auto& y) { return std::get<0>(x) > std::get<0>(y); });
  std::string out;
  for (auto& [s, c, b] : rows) {
    (void)s;
    if (!out.empty()) out += "\n";
    out += combo_str(c) + " - " + std::to_string(b);
  }
  return out + "\n\n";  // Pio ends the list with an empty line
}

// ---------------------------------------------------------------------------------------------
// Lines over all runouts

void Upi::for_each_runout(const std::string& line_id, const std::function<void(const NodeRef&, double)>& f) const {
  require_tree();
  auto toks = split_colon(line_id);
  if (toks.size() < 2 || toks[0] != "r" || toks[1] != "0") {
    if (toks.size() == 1 && toks[0] == "r") {
      f(resolve("r"), 1.0);
      return;
    }
    throw CmdError{"incorrect path"};
  }
  // Depth-first over runouts; card tokens act as wildcards.
  std::function<void(size_t, std::string, double)> rec = [&](size_t i, std::string prefix, double w) {
    if (i == toks.size()) {
      f(resolve(prefix), w);
      return;
    }
    NodeRef cur = resolve(prefix);
    const GNode& n = tree_->nodes[cur.node];
    if (n.kind == ANodeKind::Chance && tree_->chances[n.aux].flop_deal) {
      const ChanceInfo& ci = tree_->chances[n.aux];
      for (size_t k = 0; k < ci.flops.size(); ++k)
        rec(i + 1, prefix + ":" + card_str(ci.flops[k][0]) + card_str(ci.flops[k][1]) + card_str(ci.flops[k][2]),
            w * ci.fweight[k]);
      return;
    }
    if (n.kind == ANodeKind::Chance) {
      const ChanceInfo& ci = tree_->chances[n.aux];
      uint64_t bm = board_mask(cur.board);
      double cw = w / ci.num_possible;
      for (int x = 0; x < NUM_CARDS; ++x) {
        if (bm & card_bit(x)) continue;
        rec(i + 1, prefix + ":" + card_str(x), cw);
      }
      return;
    }
    rec(i + 1, prefix + ":" + toks[i], w);
  };
  rec(2, "r:0", 1.0);
}

double Upi::line_matchups(const std::string& line_id) {
  double total = 0;
  for_each_runout(line_id, [&](const NodeRef& r, double w) {
    if (r.pseudo_root) {
      total += solver_->root_matchups();
      return;
    }
    std::vector<float> r0, r1;
    node_reach(r, r0, r1);
    std::vector<float> m(tree_->num_hands(0));
    solver_->compat_mass(0, r1.data(), m.data());
    double s = 0;
    for (size_t h = 0; h < m.size(); ++h) s += static_cast<double>(r0[h]) * m[h];
    total += w * s;
  });
  return total;
}

// Pio 3.9+ line listing: depth first, children in reverse display order (fold, check/call, bets
// ascending), "?" for dealt cards, and no separate line for the decision node right after a deal.
std::string Upi::cmd_show_all_lines() {
  require_tree();
  std::string out = "r\nr:0";
  std::function<void(const NodeRef&)> rec = [&](const NodeRef& r) {
    const GNode& n = tree_->nodes[r.node];
    if (n.kind == ANodeKind::Decision) {
      const ActionNode& an = tree_->an(n);
      for (size_t k = an.actions.size(); k-- > 0;) {
        std::string cid = r.id + ":" + action_label(an, an.actions[k]);
        NodeRef c = resolve(cid);
        c.id = cid;  // keep "?" card markers
        out += "\n" + cid;
        rec(c);
      }
    } else if (n.kind == ANodeKind::Chance) {
      NodeRef c = resolve(r.id + ":?");
      c.id = r.id + ":?";
      rec(c);
    }
  };
  rec(resolve("r:0"));
  return out;
}

std::string Upi::cmd_show_all_freqs(const Args& a) {
  if (a.empty() || (a[0] != "global" && a[0] != "local")) throw CmdError{"incorrect or missing argument"};
  bool local = a[0] == "local";
  bool pp = a.size() > 1 && a[1] == "pp";
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  std::string lines = cmd_show_all_lines();
  std::istringstream in(lines);
  std::string line;
  double root = solver_->root_matchups();
  std::ostringstream o;
  bool first = true;
  while (std::getline(in, line)) {
    double v = 1.0;
    if (line != "r" && line != "r:0") {
      std::string parent = line.substr(0, line.rfind(':'));
      while (parent.size() > 2 && parent.back() == '?') parent = parent.substr(0, parent.rfind(':'));
      double m = line_matchups(line);
      if (local) {
        double pm = parent == "r:0" ? root : line_matchups(parent);
        v = pm > 0 ? m / pm : 0.0;
      } else {
        v = root > 0 ? m / root : 0.0;
      }
    }
    if (!first) o << "\n";
    first = false;
    if (pp) o << line << " - " << pfmt("%.2f", v * 100.0) << "%";
    else o << line << " - " << fmt(v);
  }
  return o.str();
}

// ---------------------------------------------------------------------------------------------
// Strategy writing / locking

void Upi::set_node_strategy(uint32_t ni, const float* strat, bool persist) {
  // Pio 3.10 (probe9): under the default v4 algorithms an edited strategy persists through further
  // solving (it is written as a dominant regret); under original_pio / pio_cfr it is only a start.
  // persist = false: only a start under every algorithm (eliminate_path, #421).
  const float regret_scale = (!persist || tree_alg_ == "original_pio" || tree_alg_ == "pio_cfr") ? 1.0f : 1e7f;
  const GNode& n = tree_->nodes[ni];
  int A = n.num_children, H = tree_->num_hands(n.player);
  std::vector<float> rbuf, sbuf;
  if (tree_->compressed) {
    rbuf.resize(static_cast<size_t>(A) * H);
    sbuf.resize(static_cast<size_t>(A) * H);
  }
  float* R = tree_->regrets_rw(n, rbuf.data());
  float* S = tree_->strat_rw(n, sbuf.data());
  for (int h = 0; h < H; ++h) {
    double sum = 0;
    for (int k = 0; k < A; ++k) sum += std::max(0.0f, strat[static_cast<size_t>(k) * H + h]);
    for (int k = 0; k < A; ++k) {
      float v = sum > 0 ? static_cast<float>(std::max(0.0f, strat[static_cast<size_t>(k) * H + h]) / sum)
                        : 1.0f / static_cast<float>(A);
      S[static_cast<size_t>(k) * H + h] = v;
      R[static_cast<size_t>(k) * H + h] = v * regret_scale;
    }
  }
  tree_->regrets_commit(n, R);
  tree_->strat_commit(n, S);
}

void Upi::lock_node_impl(uint32_t ni, bool lock) {
  GNode& n = tree_->nodes[ni];
  if (n.kind != ANodeKind::Decision) throw CmdError{"incorrect node"};
  if (lock) {
    if (n.flags & GF_LOCKED) return;
    int A = n.num_children, H = tree_->num_hands(n.player);
    std::vector<float> sig(static_cast<size_t>(A) * H);
    solver_->average_strategy(ni, sig.data());
    // The flag locks (a locked node plays its strategy sums; its regrets aren't updated), so the strategy is
    // written as a start, not a dominant regret: after unlock_node the node solves on, as in Pio (#429).
    set_node_strategy(ni, sig.data(), false);
    n.flags |= GF_LOCKED;
  } else {
    n.flags &= static_cast<uint8_t>(~GF_LOCKED);
  }
}

std::string Upi::cmd_set_strategy(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  NodeRef r = resolve(a[0]);
  if (r.pseudo_root || tree_->nodes[r.node].kind != ANodeKind::Decision) throw CmdError{"incorrect node"};
  const GNode& n = tree_->nodes[r.node];
  int A = n.num_children, H = tree_->num_hands(n.player);
  auto v = parse_floats(a, 1);
  if (v.size() != static_cast<size_t>(A) * NUM_COMBOS)
    throw CmdError{"incorrect or missing argument"};
  std::vector<float> local(static_cast<size_t>(A) * H);
  for (int k = 0; k < A; ++k) from_1326(n.player, r.frame, v.data() + static_cast<size_t>(k) * NUM_COMBOS, local.data() + static_cast<size_t>(k) * H);
  set_node_strategy(r.node, local.data());
  return "set_strategy ok!";
}

std::string Upi::cmd_set_mes(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);
  require_tree();
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  int Hp = tree_->num_hands(p);
  std::vector<float> out(Hp);
  // Like set_strategy (set_node_strategy): under the v4 algorithms Pio's written best response persists
  // through further solving, under original_pio / pio_cfr it is a start (#423, paired probes).
  solver_->br_regret_scale = (tree_alg_ == "original_pio" || tree_alg_ == "pio_cfr") ? 1.0f : 1e7f;
  solver_->write_best_response = true;
  solver_->walk(Walk::BestResponse, 0, p, tree_->init_reach[p].data(), tree_->init_reach[1 - p].data(), out.data());
  solver_->write_best_response = false;
  solver_->br_regret_scale = 1.0f;
  return "set_mes ok!";
}

std::string Upi::cmd_round_up_to(const Args& a) {
  if (a.size() < 3) throw CmdError{"incorrect or missing argument"};
  int p = player_arg(a[0]);
  int N = std::atoi(a[1].c_str());
  if (N <= 0) throw CmdError{"incorrect or missing argument"};
  int street = street_arg(a[2]);
  require_tree();
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  for (uint32_t ni = 0; ni < tree_->nodes.size(); ++ni) {
    const GNode& n = tree_->nodes[ni];
    if (n.kind != ANodeKind::Decision || n.player != p || n.board_len != street) continue;
    int A = n.num_children, H = tree_->num_hands(p);
    std::vector<float> sig(static_cast<size_t>(A) * H);
    solver_->average_strategy(ni, sig.data());
    for (int h = 0; h < H; ++h) {
      // largest remainder rounding to multiples of 1/N
      std::vector<std::pair<double, int>> rem;
      int used = 0;
      std::vector<int> units(A);
      for (int k = 0; k < A; ++k) {
        double x = sig[static_cast<size_t>(k) * H + h] * N;
        units[k] = static_cast<int>(std::floor(x));
        used += units[k];
        rem.push_back({x - units[k], k});
      }
      std::sort(rem.begin(), rem.end(), [](auto& x, auto& y) { return x.first > y.first; });
      for (int i = 0; used < N && i < A; ++i, ++used) units[rem[i].second]++;
      for (int k = 0; k < A; ++k) sig[static_cast<size_t>(k) * H + h] = static_cast<float>(units[k]) / N;
    }
    set_node_strategy(ni, sig.data());
  }
  return "round_up_to ok!";
}

// ---------------------------------------------------------------------------------------------
// Partial solving

double Upi::explo_at(const NodeRef& r, std::vector<float>& r0, std::vector<float>& r1, RootStats* out_stats) {
  node_reach(r, r0, r1);
  return explo_at_reach(r.node, r0.data(), r1.data(), out_stats);
}

double Upi::explo_at_reach(uint32_t node, const float* r0, const float* r1, RootStats* out_stats) {
  RootStats st;
  const float* rr[2] = {r0, r1};
  int H0 = tree_->num_hands(0);
  std::vector<float> m(H0);
  solver_->compat_mass(0, rr[1], m.data());
  double M = 0;
  for (int h = 0; h < H0; ++h) M += static_cast<double>(rr[0][h]) * m[h];
  if (M <= 0) {
    if (out_stats) *out_stats = st;
    return 0;
  }
  for (int p = 0; p < 2; ++p) {
    int Hp = tree_->num_hands(p);
    std::vector<float> out(Hp);
    solver_->walk(Walk::Evaluate, node, p, rr[p], rr[1 - p], out.data());
    double s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rr[p][h]) * out[h];
    st.ev[p] = s / M;
    solver_->walk(Walk::BestResponse, node, p, rr[p], rr[1 - p], out.data());
    s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rr[p][h]) * out[h];
    st.mes[p] = s / M;
  }
  st.exploitability = ((st.mes[0] - st.ev[0]) + (st.mes[1] - st.ev[1])) / 2.0;
  if (out_stats) *out_stats = st;
  return st.exploitability;
}

void Upi::solve_subtree(const NodeRef& r, double accuracy, long long max_iters, double max_seconds) {
  std::vector<float> r0, r1;
  node_reach(r, r0, r1);
  auto t0 = std::chrono::steady_clock::now();
  for (long long t = 1; t <= max_iters; ++t) {
    solver_->iterate_subtree(r.node, static_cast<int>(t), r0.data(), r1.data());
    if (t % 25 == 0 || t == max_iters) {
      std::vector<float> a0, a1;
      double e = explo_at(r, a0, a1);
      if (accuracy > 0 && e <= accuracy) break;
    }
    if (max_seconds > 0 && std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() > max_seconds) break;
  }
}

void Upi::ensure_recalced(const NodeRef& r) {
  if (!tree_ || r.pseudo_root) return;
  // Which street must be re-solved: the first one missing from a truncated save, or the street chosen
  // with set_always_recalc (3 = flop, 4 = turn, 5 = river).
  int street = 0;
  if (tree_->missing_street) street = tree_->missing_street;
  if (always_recalc_street_ >= 3 && (street == 0 || always_recalc_street_ < street)) street = always_recalc_street_;
  if (street == 0 || tree_->nodes[r.node].board_len < street) return;
  // The street root is the decision node reached right after the deal into `street`.
  NodeRef sub = r;
  size_t cut = 0;  // no deal into `street` on the path: the tree root starts that street
  for (size_t i = 0; i < r.steps.size(); ++i) {
    const GNode& from = tree_->nodes[r.steps[i].node];
    const GNode& to = tree_->nodes[i + 1 < r.steps.size() ? r.steps[i + 1].node : r.node];
    if (from.kind == ANodeKind::Chance && to.board_len >= street) {
      cut = i + 1;
      break;
    }
  }
  sub.steps.assign(r.steps.begin(), r.steps.begin() + static_cast<long>(cut));
  sub.node = cut < r.steps.size() ? r.steps[cut].node : r.node;
  if (tree_->nodes[sub.node].kind != ANodeKind::Decision || tree_->recalced.count(sub.node)) return;
  const ActionNode& an = tree_->an(tree_->nodes[sub.node]);
  double pot = an.dead + an.inv[0] + an.inv[1];
  double acc = recalc_accuracy_[std::clamp(street - 3, 0, 2)] * pot;
  double secs = always_recalc_ms_ > 0 ? always_recalc_ms_ / 1000.0 : 0.0;
  solve_subtree(sub, acc, 5000, secs);
  tree_->recalced.insert(sub.node);
}

std::string Upi::cmd_solve_partial(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  stop_solver();
  NodeRef r = resolve(a[0]);
  if (r.pseudo_root) r = resolve("r:0");
  double acc = accuracy_fraction_ ? accuracy_ * tree_->cfg.start_pot() : accuracy_;
  if (acc <= 0) acc = 0.0002 * tree_->cfg.start_pot();
  solve_subtree(r, acc, 3000);
  tree_->recalced.insert(r.node);
  return "solve_partial ok!";
}

std::string Upi::cmd_explo_partial(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  NodeRef r = resolve(a[0]);
  if (r.pseudo_root) r = resolve("r:0");
  ensure_recalced(r);
  std::vector<float> r0, r1;
  return pfmt("%f", explo_at(r, r0, r1));
}

std::string Upi::cmd_solve_all_splits(const Args& a) {
  if (a.empty() || (a[0] != "turns" && a[0] != "rivers")) throw CmdError{"incorrect or missing argument"};
  require_tree();
  stop_solver();
  if (tree_->fast_loaded) throw CmdError{"Can't do that on partially loaded tree - please call load_all_nodes"};
  int street = a[0] == "turns" ? 4 : 5;
  std::vector<uint32_t> locked;
  for (uint32_t ni = 0; ni < tree_->nodes.size(); ++ni) {
    GNode& n = tree_->nodes[ni];
    if (n.kind == ANodeKind::Decision && n.board_len < street && !(n.flags & GF_LOCKED)) {
      lock_node_impl(ni, true);
      locked.push_back(ni);
    }
  }
  double acc = recalc_accuracy_[street - 3];
  double target = acc > 0 ? acc * tree_->cfg.start_pot() / 100.0 : 0.0;
  double prev = 1e30;
  for (long long t = 1; t <= 2000; ++t) {
    solver_->iterate(static_cast<int>(t));
    if (t % 25 == 0) {
      double e = solver_->root_stats().exploitability;
      if ((target > 0 && e <= target) || std::abs(prev - e) < 1e-4 * std::max(1.0, std::abs(e))) break;
      prev = e;
    }
  }
  for (uint32_t ni : locked) lock_node_impl(ni, false);
  tree_->incomplete = false;
  tree_->missing_street = 0;
  return "solve_all_splits ok!";
}

// ---------------------------------------------------------------------------------------------

std::string Upi::cmd_node_count() {
  require_tree();
  // Pio layout: H_DEC (OOP), V_DEC (IP), SPLIT, END, ROOT, each with per-street counts, then TOTAL.
  const char* names[] = {"H_DEC", "V_DEC", "SPLIT", "END", "ROOT"};
  long long counts[5][3] = {};
  // Preflop nodes use the first column too, just like the loop below. A
  // preflop root has board_len == 0 and must not index the row at -3.
  counts[4][std::clamp(tree_->root_board_len() - 3, 0, 2)] = 1;
  for (auto& n : tree_->nodes) {
    int s = n.board_len - 3;
    // GNode::board_len of a chance node is the board before its deal, so it already names the street.
    s = std::clamp(s, 0, 2);
    int t = n.kind == ANodeKind::Decision ? (n.player == 0 ? 0 : 1) : n.kind == ANodeKind::Chance ? 2 : 3;
    counts[t][s]++;
  }
  std::ostringstream o;
  long long total = 0;
  for (int t = 0; t < 5; ++t) {
    long long sum = counts[t][0] + counts[t][1] + counts[t][2];
    total += sum;
    o << names[t] << ": " << sum << "\n  FLOP  " << counts[t][0] << "\n  TURN  " << counts[t][1] << "\n  RIVER "
      << counts[t][2] << "\n";
  }
  o << "TOTAL: " << total;
  return o.str();
}

std::string Upi::cmd_print_all_strats() {
  require_tree();
  TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  std::string s;
  std::function<void(const NodeRef&)> rec = [&](const NodeRef& r) {
    s += r.id + "\n";
    const GNode& n = tree_->nodes[r.node];
    if (n.kind == ANodeKind::Decision) {
      int A = n.num_children, H = tree_->num_hands(n.player);
      std::vector<float> sig(static_cast<size_t>(A) * H), line(NUM_COMBOS);
      solver_->average_strategy(r.node, sig.data());
      uint64_t bm = board_mask(r.board);
      for (int k = 0; k < A; ++k) {
        to_1326(n.player, r.frame, sig.data() + static_cast<size_t>(k) * H, line.data(), 1.0f / static_cast<float>(A));
        for (int i = 0; i < NUM_COMBOS; ++i)
          if (combo_mask(i) & bm) line[i] = 0.0f;
        s += floats_line(line.data(), NUM_COMBOS) + "\n";
      }
      const ActionNode& an = tree_->an(n);
      for (auto& act : an.actions) rec(resolve(r.id + ":" + action_label(an, act)));
    } else if (n.kind == ANodeKind::Chance) {
      const ChanceInfo& ci = tree_->chances[n.aux];
      for (uint8_t c : ci.cards) rec(resolve(r.id + ":" + card_str(PERM_CARD[r.frame][c])));
    }
  };
  rec(resolve("r:0"));
  if (!s.empty()) s.pop_back();
  return s;
}

// ---------------------------------------------------------------------------------------------

void Upi::register_node_commands() {
  auto& H = handlers_;

  H["show_node"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    return show_node_text(resolve(a[0]));
  };
  H["show_children"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    auto kids = children_of(resolve(a[0]));
    std::string s;
    for (size_t i = 0; i < kids.size(); ++i) s += "child " + std::to_string(i) + ":\n" + show_node_text(kids[i].second);
    return s;
  };
  H["show_effective_stack"] = [this](const Args&) {
    require_tree();
    return std::to_string(tree_->cfg.eff_stack);
  };
  H["show_range"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    int p = player_arg(a[0]);
    std::vector<float> out(NUM_COMBOS, 0.0f);
    if (a.size() == 1) {
      for (int i = 0; i < NUM_COMBOS; ++i) out[i] = cfg_.range[p][i];
      return floats_line(out.data(), NUM_COMBOS);
    }
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[1]);
    std::vector<float> r0, r1;
    if (r.pseudo_root) {
      r0 = tree_->init_reach[0];
      r1 = tree_->init_reach[1];
    } else {
      node_reach(r, r0, r1);
    }
    to_1326(p, r.frame, (p == 0 ? r0 : r1).data(), out.data());
    return floats_line(out.data(), NUM_COMBOS);
  };
  H["show_strategy"] = [this](const Args& a) { return cmd_show_strategy(a, false); };
  H["show_strategy_pp"] = [this](const Args& a) { return cmd_show_strategy(a, true); };
  H["calc_ev"] = [this](const Args& a) { return cmd_calc_ev(a, false); };
  H["calc_ev_pp"] = [this](const Args& a) { return cmd_calc_ev(a, true); };
  H["calc_eq"] = [this](const Args& a) { return cmd_calc_eq(a, false, false); };
  H["calc_eq_pp"] = [this](const Args& a) { return cmd_calc_eq(a, true, false); };
  H["calc_eq_node"] = [this](const Args& a) { return cmd_calc_eq(a, false, true); };
  H["calc_eq_preflop"] = [this](const Args& a) { return cmd_calc_eq_preflop(a); };
  H["calc_results"] = [this](const Args&) {
    require_tree();
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    RootStats st;
    if (tree_->missing_street && tree_->has_saved_stats) {
      // A truncated save whose streets are still missing: those are unsolved until browsed, so report
      // the results of the solve that produced the save, as Pio does (#195).
      const double* v = tree_->saved_stats;
      st.ev[0] = v[0], st.ev[1] = v[1], st.mes[0] = v[2], st.mes[1] = v[3], st.exploitability = v[4];
    } else {
      st = solver_->root_stats();
    }
    std::string s = stats_block(st, solve_seconds_);
    // drop the leading "running time" line to match the 3.x layout (EV OOP first)
    s = s.substr(s.find('\n') + 1);
    if (!s.empty() && s.back() == '\n') s.pop_back();
    return s;
  };
  H["calc_matchups_line"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    return fmt(line_matchups(a[0]));
  };
  H["calc_line_freq"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    double root = solver_->root_matchups();
    return fmt(root > 0 ? line_matchups(a[0]) / root : 0.0);
  };
  H["calc_global_freq"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) return std::string("1");
    std::vector<float> r0, r1;
    node_reach(r, r0, r1);
    int H0 = tree_->num_hands(0);
    std::vector<float> m(H0);
    solver_->compat_mass(0, r1.data(), m.data());
    double num = 0;
    for (int h = 0; h < H0; ++h) num += static_cast<double>(r0[h]) * m[h];
    // Over all the root's matchups, as Pio: the turn and river cards' card removal counts (probe 13:
    // after an As turn that blocks AA/AK/AQ/AJ combos, 0.0692 where only the strategies give 1/12).
    double den = solver_->root_matchups();
    return fmt(den > 0 ? num / den : 0.0);
  };
  H["show_all_lines"] = [this](const Args&) {
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    return cmd_show_all_lines();
  };
  H["show_all_freqs"] = [this](const Args& a) { return cmd_show_all_freqs(a); };
  H["show_win_evs"] = [this](const Args& a) {
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    int p = player_arg(a[0]);
    NodeRef r = resolve(a[1]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    const GNode& n = tree_->nodes[r.node];
    const ActionNode& an = tree_->an(n);
    bool final_node = n.kind == ANodeKind::Fold || n.kind == ANodeKind::Showdown;
    if (!final_node) throw CmdError{"incorrect node"};
    auto v = [&](int outcome) {
      // Schematic-relative, like Pio: the schematic's own set_pot supplies the pot, so no
      // extra adjustment is needed here (the old `subtree` flag only gated a payoff offset
      // that was always zero, so it never did anything - see #107).
      double x = tree_->terminal_payoff(an, p, outcome);
      return fmt(x == 0 ? 0.0 : x);
    };
    if (n.kind == ANodeKind::Fold) return v(0) + " " + v(0) + " " + v(0);
    // terminal_payoff treats every non-fold node as a showdown of the current investments.
    return v(1) + " " + v(-1) + " " + v(0);
  };
  H["print_all_strats"] = [this](const Args&) { return cmd_print_all_strats(); };
  H["node_count"] = [this](const Args&) { return cmd_node_count(); };
  H["show_nuts"] = [this](const Args& a) { return cmd_show_nuts(a); };

  // ---- partial trees ----
  H["rebuild_tree"] = [this](const Args&) {
    require_tree();
    if (tree_->fast_loaded) throw CmdError{"Can't do that on partially loaded tree - please call load_all_nodes"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    tree_alg_ = algorithm_;
    // As Pio (#427, paired probes): the regrets and the iteration count start over, the strategy sums stay.
    // So the displayed strategies are the solved ones until the next solve (probe3's calc_line_freq after it
    // is Pio's solved 0.00319), its first iteration gives the old sums zero weight (DCFR at t = 1), and N
    // iterations later it is a fresh N-iteration solve; locks stay, a locked node's strategy unchanged.
    std::fill(tree_->regrets.begin(), tree_->regrets.end(), 0.0f);
    std::fill(tree_->regrets_q.begin(), tree_->regrets_q.end(), int16_t(0));
    std::fill(tree_->rscale.begin(), tree_->rscale.end(), 0.0f);
    std::fill(tree_->preds.begin(), tree_->preds.end(), 0.0f);
    tree_->iterations = 0;
    iterations_ = 0;
    discount_restart_ = 0;
    return std::string("rebuild_tree ok!");
  };
  H["estimate_rebuild_tree"] = [this](const Args&) {
    require_tree();
    if (tree_->fast_loaded) throw CmdError{"Can't do that on partially loaded tree - please call load_all_nodes"};
    return "Rebuild tree. Memory needed: " + std::to_string(tree_->memory_bytes() >> 20) + " MB; available: " +
           std::to_string(available_memory_bytes() >> 20) + " MB\nestimate_rebuild_tree ok!";
  };
  for (const char* c : {"rebuild_forgotten_streets", "estimate_rebuild_forgotten_streets"}) {
    H[c] = [](const Args&) -> std::string { throw CmdError{"removed in UPI 3.10; use rebuild_tree"}; };
  }
  H["solve_partial"] = [this](const Args& a) { return cmd_solve_partial(a); };
  H["explo_partial"] = [this](const Args& a) { return cmd_explo_partial(a); };
  H["solve_all_splits"] = [this](const Args& a) { return cmd_solve_all_splits(a); };

  // ---- range explorer ----
  H["show_category_names"] = [](const Args&) { return show_category_names_text(); };
  auto board_of = [](const Args& a) {
    std::vector<int> b;
    if (a.size() != 1 || !parse_cards(a[0], b) || b.size() < 3 || b.size() > 5)
      throw CmdError{"incorrect or missing argument"};
    return b;
  };
  H["show_categories"] = [board_of](const Args& a) { return show_categories_text(board_of(a)); };
  H["show_cats"] = [board_of](const Args& a) { return show_cats_text(board_of(a)); };
  H["show_cats_pp"] = [board_of](const Args& a) { return show_cats_pp_text(board_of(a)); };

  // ---- display isomorphism ----
  H["set_display_iso"] = [this](const Args& a) {
    // Pio accepts the swaps sh sd sc hd hc dc, applied in order to the original board.
    int perm = IDENTITY_PERM;
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    for (auto& t : a) {
      static const char* ok[] = {"sh", "sd", "sc", "hd", "hc", "dc"};
      if (std::find_if(std::begin(ok), std::end(ok), [&](const char* x) { return t == x; }) == std::end(ok))
        throw CmdError{"incorrect or missing argument"};
      int s1 = parse_suit(t[0]), s2 = parse_suit(t[1]);
      std::array<uint8_t, 4> sw = {0, 1, 2, 3};
      std::swap(sw[s1], sw[s2]);
      perm = PERM_COMPOSE[find_perm(sw)][perm];
    }
    display_perm_ = perm;
    return std::string("set_display_iso ok!");
  };
  H["reset_display_iso"] = [this](const Args&) {
    display_perm_ = IDENTITY_PERM;
    return std::string("reset_display_iso ok!");
  };
  H["show_board_no_iso"] = [this](const Args&) {
    const auto& b = tree_ ? tree_->cfg.board : cfg_.board;
    std::string s;
    for (int c : b) s += card_str(c) + " ";
    return s;
  };

  // ---- locking / strategies ----
  H["lock_node"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    lock_node_impl(r.node, true);
    return std::string("lock_node ok!");
  };
  H["unlock_node"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    lock_node_impl(r.node, false);
    return std::string("unlock_node ok!");
  };
  // Pio: clears every whole-node lock; combo locks stay (as with unlock_node).
  H["unlock_all"] = [this](const Args&) {
    require_tree();
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    for (auto& n : tree_->nodes) n.flags &= static_cast<uint8_t>(~GF_LOCKED);
    return std::string("unlock_all ok!");
  };
  // Extension (#255): Pio's unlock_all leaves combo locks, and nothing lists them, so a viewer
  // could not clear the ones that came with a loaded save. This clears every combo lock.
  H["combo_unlock_all"] = [this](const Args&) {
    require_tree();
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    for (auto& n : tree_->nodes) n.flags &= static_cast<uint8_t>(~GF_COMBO_LOCKED);
    tree_->combo_lock.clear();
    tree_->combo_lock_s.clear();
    return std::string("combo_unlock_all ok!");
  };
  auto line_nodes = [this](const std::string& id) {
    std::set<uint32_t> nodes;
    for_each_runout(id, [&](const NodeRef& r, double) {
      if (!r.pseudo_root) nodes.insert(r.node);
    });
    return nodes;
  };
  H["lock_line"] = [this, line_nodes](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    for (uint32_t ni : line_nodes(a[0])) lock_node_impl(ni, true);
    return std::string("lock_line ok!");
  };
  H["unlock_line"] = [this, line_nodes](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    for (uint32_t ni : line_nodes(a[0])) lock_node_impl(ni, false);
    return std::string("unlock_line ok!");
  };
  H["combo_lock_node"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root || tree_->nodes[r.node].kind != ANodeKind::Decision) throw CmdError{"incorrect node"};
    auto v = parse_floats(a, 1);
    if (v.size() != NUM_COMBOS) throw CmdError{"incorrect or missing argument"};
    GNode& n = tree_->nodes[r.node];
    int A = n.num_children, Hn = tree_->num_hands(n.player);
    std::vector<float> local(Hn);
    from_1326(n.player, r.frame, v.data(), local.data());
    std::vector<float> sig(static_cast<size_t>(A) * Hn);
    solver_->average_strategy(r.node, sig.data());
    std::vector<float> sbuf(tree_->compressed ? static_cast<size_t>(A) * Hn : 0);
    float* S = tree_->strat_rw(n, sbuf.data());
    std::vector<uint8_t> mask(Hn, 0);
    bool any = false;
    for (int h = 0; h < Hn; ++h) {
      if (local[h] != 0) {
        mask[h] = 1;
        any = true;
        for (int k = 0; k < A; ++k) S[static_cast<size_t>(k) * Hn + h] = sig[static_cast<size_t>(k) * Hn + h];
      }
    }
    if (any && tree_->compressed)  // the exact rows, before they are quantized (#380)
      tree_->combo_lock_s[r.node].assign(S, S + static_cast<size_t>(A) * Hn);
    else
      tree_->combo_lock_s.erase(r.node);
    tree_->strat_commit(n, S);
    if (any) {
      tree_->combo_lock[r.node] = mask;
      n.flags |= GF_COMBO_LOCKED;
    } else {
      tree_->combo_lock.erase(r.node);
      n.flags &= static_cast<uint8_t>(~GF_COMBO_LOCKED);
    }
    return std::string("combo_lock_node ok!");
  };
  H["show_locked_combos"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    const GNode& n = tree_->nodes[r.node];
    int Hn = tree_->num_hands(n.player);
    std::vector<float> local(Hn, 0.0f), out(NUM_COMBOS);
    if (n.flags & GF_LOCKED) std::fill(local.begin(), local.end(), 1.0f);
    else if (n.flags & GF_COMBO_LOCKED) {
      auto& m = tree_->combo_lock[r.node];
      for (int h = 0; h < Hn; ++h) local[h] = m[h];
    }
    to_1326(n.player, r.frame, local.data(), out.data());
    return floats_line(out.data(), NUM_COMBOS) + "\n\n";  // Pio: blank line before END (#125)
  };
  H["set_strategy"] = [this](const Args& a) { return cmd_set_strategy(a); };
  H["set_equal_strats"] = [this](const Args&) {
    require_tree();
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    tree_->clear_storage();
    for (auto& n : tree_->nodes) n.flags &= static_cast<uint8_t>(~(GF_LOCKED | GF_COMBO_LOCKED));
    tree_->combo_lock.clear();
    tree_->combo_lock_s.clear();
    // A fresh start, as in Pio (show_iters 0; 60 iterations later it is exactly a fresh 60-iteration solve,
    // #425): the DCFR discounts and the average's weighting restart with the count.
    tree_->iterations = 0;
    iterations_ = 0;
    discount_restart_ = 0;
    return std::string("set_equal_strats ok!");
  };
  H["set_mes"] = [this](const Args& a) { return cmd_set_mes(a); };
  H["round_up_to"] = [this](const Args& a) { return cmd_round_up_to(a); };
  H["eliminate_path"] = [this](const Args& a) {
    // Pio 3.10.1 semantics, probed (#421): a one-time edit on every runout of the line. Only a bet, raise
    // or check can be eliminated (a call or fold: "incorrect path"). Each hand's frequency of the action
    // goes to its best remaining action by EV (probe3's r:0:b66:b300: 22 of 22 hands, some to call, some
    // to fold), and further solving may bring the action back.
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    std::set<std::pair<uint32_t, int>> done;
    bool passive_path = false;
    for_each_runout(a[0], [&](const NodeRef& r, double) {
      if (r.steps.empty()) return;
      auto& st = r.steps.back();
      const GNode& parent = tree_->nodes[st.node];
      if (parent.kind != ANodeKind::Decision) return;
      const ActionNode& an = tree_->an(parent);
      const ActKind kind = an.actions[st.choice].kind;
      if (kind == ActKind::Call || kind == ActKind::Fold) {
        passive_path = true;
        return;
      }
      if (!done.insert({st.node, st.choice}).second) return;
      ensure_recalced(r);  // as calc_ev: a street a truncated save left out is solved first (covers the siblings)
      const int A = parent.num_children, p = parent.player, H = tree_->num_hands(p);
      std::vector<float> sig(static_cast<size_t>(A) * H);
      solver_->average_strategy(st.node, sig.data());
      // Each remaining action's counterfactual value per hand. EV is that over the hand's compatible
      // opponent mass, the same for every action here (the actor's choice doesn't move the opponent's
      // reach), so the best action by EV is the best by value.
      std::vector<float> r0, r1;
      node_reach(r, r0, r1);  // the opponent's reach at the child is the parent's
      const std::vector<float> own(static_cast<size_t>(H), 1.0f);
      const float* ro = p == 0 ? r1.data() : r0.data();
      std::vector<float> cfv(static_cast<size_t>(A) * H, 0.0f);
      for (int k = 0; k < A; ++k)
        if (k != st.choice)
          solver_->walk(Walk::Evaluate, parent.first_child + static_cast<uint32_t>(k), p, own.data(), ro,
                        cfv.data() + static_cast<size_t>(k) * H);
      for (int h = 0; h < H; ++h) {
        int best = -1;
        for (int k = 0; k < A; ++k)
          if (k != st.choice && (best < 0 || cfv[static_cast<size_t>(k) * H + h] > cfv[static_cast<size_t>(best) * H + h]))
            best = k;
        float& v = sig[static_cast<size_t>(st.choice) * H + h];
        if (best >= 0) sig[static_cast<size_t>(best) * H + h] += v;
        v = 0.0f;
      }
      // only a start, not a dominant regret: Pio's solve brings the action back (probe3: 100 more
      // iterations after eliminating r:0:b66:b300 end at 0.089 exploitability; a persistent edit, 2.36)
      set_node_strategy(st.node, sig.data(), false);
    });
    if (passive_path && done.empty()) throw CmdError{"incorrect path"};  // Pio's answer for a call or fold
    if (done.empty()) throw CmdError{"incorrect node"};
    return std::string("eliminate_path ok!");
  };

  // ---- incentives ----
  auto set_inc = [this](uint32_t ni, const std::vector<float>& v) {
    GNode& n = tree_->nodes[ni];
    if (n.kind != ANodeKind::Decision) throw CmdError{"incorrect node"};
    if (v.size() != n.num_children) throw CmdError{"incorrect or missing argument"};
    tree_->incentives[ni] = v;
    n.flags |= GF_INCENTIVES;
  };
  H["set_incentives_node"] = [this, set_inc](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    set_inc(r.node, parse_floats(a, 1));
    return std::string("set_incentives_node ok!");
  };
  H["set_incentives_line"] = [this, set_inc, line_nodes](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    auto v = parse_floats(a, 1);
    for (uint32_t ni : line_nodes(a[0])) set_inc(ni, v);
    return std::string("set_incentives_line ok!");
  };
  H["show_incentives"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    const GNode& n = tree_->nodes[r.node];
    auto it = tree_->incentives.find(r.node);
    if (it == tree_->incentives.end()) return std::string("Incentives are not used in this node!");
    (void)n;
    std::string s;  // Pio: "%f " per action ("1.000000 2.000000 ")
    for (float x : it->second) s += pfmt("%f", x) + " ";
    return s;
  };
  H["clear_incentives"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    TreePause np(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect node"};
    tree_->incentives.erase(r.node);
    tree_->nodes[r.node].flags &= static_cast<uint8_t>(~GF_INCENTIVES);
    return std::string("clear_incentives ok!");
  };

  register_save_commands();
}

}  // namespace pio
