// See nn_warm.hpp. Feature math is a 1:1 port of ml/features.py (normative);
// keep the two in sync when either changes.
#include "nn_warm.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <stdexcept>

#include "cards.hpp"

namespace pio {
namespace {

// Same enumeration order as itertools.permutations(range(4)): the tie-break of
// canonical_perm depends on it.
const std::array<std::array<uint8_t, 4>, 24>& lex_perms() {
  static const std::array<std::array<uint8_t, 4>, 24> table = [] {
    std::array<std::array<uint8_t, 4>, 24> out;
    std::array<uint8_t, 4> p{{0, 1, 2, 3}};
    int n = 0;
    do {
      out[static_cast<size_t>(n++)] = p;
    } while (std::next_permutation(p.begin(), p.end()));
    return out;
  }();
  return table;
}

inline int perm_card(int c, const std::array<uint8_t, 4>& p) {
  return (c & ~3) | p[c & 3];
}

// ml/features.py canonical_perm: suit permutation minimising the SORTED tuple
// of permuted display board cards; ties keep the first in lex order.
std::array<uint8_t, 4> canonical_perm(const std::vector<int>& board) {
  std::array<int, 5> best_key{{99, 99, 99, 99, 99}};
  size_t best = 0;
  for (size_t i = 0; i < 24; ++i) {
    std::array<int, 5> im{{99, 99, 99, 99, 99}};
    for (size_t k = 0; k < board.size() && k < 5; ++k)
      im[k] = perm_card(board[k], lex_perms()[i]);
    std::sort(im.begin(), im.begin() + static_cast<long>(board.size()));
    if (std::lexicographical_compare(im.begin(), im.end(), best_key.begin(), best_key.end())) {
      best_key = im;
      best = i;
    }
  }
  return lex_perms()[best];
}

struct Reader {
  explicit Reader(const std::string& path) {
    f = std::fopen(path.c_str(), "rb");
    if (!f) throw std::runtime_error("nn warm start: cannot open " + path);
  }
  ~Reader() { if (f) std::fclose(f); }
  Reader(const Reader&) = delete;

  char magic[8];
  void read_magic() {
    if (std::fread(magic, 1, 8, f) != 8) throw std::runtime_error("nn warm start: truncated file");
  }
  uint32_t u32() {
    uint32_t v;
    if (std::fread(&v, 4, 1, f) != 1) throw std::runtime_error("nn warm start: truncated file");
    return v;
  }
  float f32() {
    float v;
    if (std::fread(&v, 4, 1, f) != 1) throw std::runtime_error("nn warm start: truncated file");
    return v;
  }
  void floats(std::vector<float>& v, size_t n) {
    v.resize(n);
    if (n && std::fread(v.data(), 4, n, f) != n)
      throw std::runtime_error("nn warm start: truncated file");
  }
  bool at_eof() {
    int c = std::fgetc(f);
    return c == EOF;
  }
  std::FILE* f = nullptr;
};

// History entry carried along the walk (ml/features.py replay()).
struct HistEntry {
  int player;    // absolute (0 = OOP)
  int kind;      // 0 bet/raise, 1 check/call, 2 fold (one-hot index)
  float chips;   // per-street bet-to, 0 for c/f
};

struct Walker {
  GameTree& t;
  const NnModel& m;
  int frame;
  std::array<uint8_t, 4> cp;  // canonicalisation perm (display -> canonical)
  float start_pot;
  float eff_stack;
  float beta;  // >0: also preload regret rows with beta * predicted policy
  uint64_t written = 0;

  std::vector<float> zg, zh, z1, z2;  // strat-net trunk scratch [hidden]
  std::vector<float> sigma;           // [A*H]: reach-weighted predicted strategy
  std::vector<float> reg;             // [A*H]: beta-weighted predicted policy (beta>0)

  // nn/feature offsets inside strat layer 1: [g=0..92 | h=93..128 | a=129..132]
  static constexpr int H_OFF = 93;
  static constexpr int A_OFF = 93 + 36;

  // Full forward of strat layers 2+3 from a finished z1.
  float tail_score() {
    const NnLayer& L2 = m.strat[1];
    z2.assign(L2.out, 0.0f);
    for (uint32_t o = 0; o < L2.out; ++o) {
      const float* w = L2.W.data() + static_cast<size_t>(o) * L2.in;
      float s = L2.b[o];
      for (uint32_t i = 0; i < L2.in; ++i) s += w[i] * z1[i];
      z2[o] = s < 0.0f ? 0.0f : s;  // ReLU
    }
    const NnLayer& L3 = m.strat[2];
    float s = L3.b[0];
    for (uint32_t i = 0; i < L3.in; ++i) s += L3.W[i] * z2[i];
    return s;  // output layer: no ReLU
  }

  float score(const float* act_f) {
    const NnLayer& L1 = m.strat[0];
    const uint32_t hid = L1.out;
    if (z1.size() != hid) z1.resize(hid);  // zg/zh/z2 are assigned elsewhere; z1 only here
    for (uint32_t o = 0; o < hid; ++o) {
      const float* wa = L1.W.data() + static_cast<size_t>(o) * L1.in + A_OFF;
      float s = zh[o] + L1.b[o];
      for (int k = 0; k < NN_A; ++k) s += wa[k] * act_f[k];
      z1[o] = s < 0.0f ? 0.0f : s;  // ReLU
    }
    return tail_score();
  }

  void visit(uint32_t ni, std::vector<HistEntry>& hist, std::vector<float>& rp,
             std::vector<float>& ro) {
    const GNode& n = t.nodes[ni];
    if (n.kind != ANodeKind::Decision) {
      if (n.kind == ANodeKind::Chance)
        throw std::runtime_error(
            "warm_start: chance nodes below the root are unsupported (river v1)");
      return;  // terminal: nothing to write
    }
    if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED | GF_INCENTIVES | GF_ELIMINATED))
      throw std::runtime_error("warm_start: locked/eliminated trees are unsupported (v1)");

    const int p = n.player;
    const int A = n.num_children;
    const ActionNode& an = t.an(n);
    const int H = t.num_hands(p);
    const int Ho = t.num_hands(1 - p);
    const float pot = start_pot;

    // ---- global features (features.global_f) ----
    float g[NN_G] = {};
    for (size_t k = 0; k < t.cfg.board.size(); ++k)
      g[perm_card(t.cfg.board[k], cp)] = 1.0f;
    g[52] = std::log1p(pot / NN_POT_SCALE);
    g[53] = std::log1p(eff_stack / NN_POT_SCALE);
    g[54] = std::log1p(eff_stack / pot);
    g[55 + 2] = 1.0f;  // street one-hot: river
    // Per-street investments (river-root tree: street 0; general form anyway).
    const float iv0 = static_cast<float>(an.inv[0] - an.street_start[0]);
    const float iv1 = static_cast<float>(an.inv[1] - an.street_start[1]);
    const float ip = p == 0 ? iv0 : iv1, io = p == 0 ? iv1 : iv0;
    g[59] = std::log1p((pot + iv0 + iv1) / NN_POT_SCALE);
    g[60] = ip / eff_stack;
    g[61] = io / eff_stack;
    g[62] = std::max(0.0f, io - ip) / pot;
    for (size_t k = 0; k < hist.size() && k < 6; ++k) {
      const int b = 63 + 5 * static_cast<int>(k);
      g[b + hist[k].kind] = 1.0f;
      g[b + 3] = hist[k].chips / pot;
      g[b + 4] = hist[k].player != p ? 1.0f : 0.0f;
    }

    // ---- compat masses in the display frame (features.compat_masses) ----
    std::array<float, 52> card_sum{};
    for (int h = 0; h < Ho; ++h) {
      card_sum[PERM_CARD[frame][t.hcard[1 - p][0][h]]] += ro[h];
      card_sum[PERM_CARD[frame][t.hcard[1 - p][1][h]]] += ro[h];
    }
    double total_o = 0.0, mean_m = 0.0, mean_r = 0.0;
    for (int h = 0; h < Ho; ++h) total_o += ro[h];
    std::vector<float> mass(static_cast<size_t>(H));
    for (int h = 0; h < H; ++h) {
      const int c1 = PERM_CARD[frame][t.hcard[p][0][h]];
      const int c2 = PERM_CARD[frame][t.hcard[p][1][h]];
      mass[h] = static_cast<float>(total_o) - card_sum[c1] - card_sum[c2];
      mean_m += mass[h];
      mean_r += rp[h];
    }
    mean_m = H > 0 ? mean_m / H : 0.0;
    mean_r = H > 0 ? mean_r / H : 0.0;

    // ---- per-node strat trunk prefix: W1 cols [0, 93) . g ----
    const NnLayer& L1 = m.strat[0];
    const uint32_t hid = L1.out;
    zg.assign(hid, 0.0f);
    for (uint32_t o = 0; o < hid; ++o) {
      const float* w = L1.W.data() + static_cast<size_t>(o) * L1.in;
      float s = 0.0f;
      for (int i = 0; i < NN_G; ++i) s += w[i] * g[i];
      zg[o] = s;
    }

    // ---- per hand: features, per-action logits, softmax ----
    sigma.assign(static_cast<size_t>(A) * H, 0.0f);
    if (beta > 0.0f) reg.assign(static_cast<size_t>(A) * H, 0.0f);
    float hf[NN_H];
    float act_f[NN_A];
    for (int h = 0; h < H; ++h) {
      int c1 = perm_card(PERM_CARD[frame][t.hcard[p][0][h]], cp);
      int c2 = perm_card(PERM_CARD[frame][t.hcard[p][1][h]], cp);
      if (c2 < c1) std::swap(c1, c2);  // sorted AFTER canonicalising (pairs can flip)
      std::memset(hf, 0, sizeof(hf));
      hf[c1 >> 2] = 1.0f;
      hf[13 + (c1 & 3)] = 1.0f;
      hf[17 + (c2 >> 2)] = 1.0f;
      hf[30 + (c2 & 3)] = 1.0f;
      hf[34] = mean_r > 0.0 ? static_cast<float>(rp[h] / mean_r) : 0.0f;
      hf[35] = mean_m > 0.0 ? static_cast<float>(mass[h] / mean_m) : 0.0f;

      zh.assign(hid, 0.0f);
      for (uint32_t o = 0; o < hid; ++o) {
        const float* w = L1.W.data() + static_cast<size_t>(o) * L1.in + H_OFF;
        float s = zg[o];
        for (int i = 0; i < NN_H; ++i) s += w[i] * hf[i];
        zh[o] = s;
      }

      double maxlogit = -1e30;
      std::vector<double> logits(static_cast<size_t>(A));
      for (int a = 0; a < A; ++a) {
        const Action& act = an.actions[static_cast<size_t>(a)];
        std::memset(act_f, 0, sizeof(act_f));
        if (act.kind == ActKind::Bet) {
          act_f[0] = 1.0f;
          act_f[3] = static_cast<float>(act.total - an.street_start[p]) / pot;
        } else if (act.kind == ActKind::Fold) {
          act_f[2] = 1.0f;
        } else {
          act_f[1] = 1.0f;
        }
        logits[a] = score(act_f);
        maxlogit = std::max(maxlogit, logits[a]);
      }
      double den = 0.0;
      for (int a = 0; a < A; ++a) den += (logits[a] = std::exp(logits[a] - maxlogit));
      for (int a = 0; a < A; ++a) {
        const double p = logits[a] / den;
        sigma[static_cast<size_t>(a) * H + h] =
            static_cast<float>(p) * rp[h];  // reach-weighted for the sum rows
        if (beta > 0.0f) reg[static_cast<size_t>(a) * H + h] = beta * static_cast<float>(p);
      }
    }

    // ---- write strategy sums (fresh tree: rows were zero) ----
    {
      std::vector<float> scratch(t.compressed ? sigma.size() : 0);
      float* S = t.strat_rw(n, t.compressed ? scratch.data() : nullptr);
      std::memcpy(S, sigma.data(), sigma.size() * sizeof(float));
      t.strat_commit(n, S);
    }
    if (beta > 0.0f) {
      // Regret init: regret-matching of beta * p reproduces the net policy as
      // the current strategy (all rows positive, so the max(R,0) clipping is
      // inert until real counterfactual updates push some actions negative).
      std::vector<float> rscratch(t.compressed ? reg.size() : 0);
      float* R = t.regrets_rw(n, t.compressed ? rscratch.data() : nullptr);
      std::memcpy(R, reg.data(), reg.size() * sizeof(float));
      t.regrets_commit(n, R);
    }
    ++written;

    // ---- descend; the acting player's reach becomes the sigma row ----
    for (int a = 0; a < A; ++a) {
      const Action& act = an.actions[static_cast<size_t>(a)];
      const float* row = sigma.data() + static_cast<size_t>(a) * H;
      std::vector<float> c_rp(row, row + H);
      hist.push_back({p,
                      act.kind == ActKind::Fold ? 2 : (act.kind == ActKind::Bet ? 0 : 1),
                      act.kind == ActKind::Bet
                          ? static_cast<float>(act.total - an.street_start[p])
                          : 0.0f});
      // The child swaps seats: its acting player is our non-actor, whose reach
      // is unchanged (ro); the opponent's reach is our updated row (c_rp). The
      // two need not even have the same length (hand lists are per player).
      visit(n.first_child + static_cast<uint32_t>(a), hist, ro, c_rp);
      hist.pop_back();
    }
  }
};

NnLayer read_layer(Reader& r) {
  NnLayer L;
  L.out = r.u32();
  L.in = r.u32();
  if (L.in == 0 || L.out == 0 || L.in > 4096 || L.out > 4096)
    throw std::runtime_error("nn warm start: implausible layer shape");
  r.floats(L.W, static_cast<size_t>(L.out) * L.in);
  r.floats(L.b, L.out);
  return L;
}

void check_net(const std::vector<NnLayer>& net, uint32_t fan_in, uint32_t hidden,
               const char* name) {
  if (net.size() != 3 || net[0].in != fan_in || net[0].out != hidden ||
      net[1].in != hidden || net[1].out != hidden || net[2].in != hidden || net[2].out != 1)
    throw std::runtime_error(std::string("nn warm start: unexpected ") + name + " shape");
}

}  // namespace

NnModel NnModel::load(const std::string& path) {
  Reader r(path);
  r.read_magic();
  if (std::memcmp(r.magic, "WSNW0001", 8) != 0)
    throw std::runtime_error("nn warm start: not a WSNW0001 file");
  const uint32_t G = r.u32(), H = r.u32(), A = r.u32();
  if (G != NN_G || H != NN_H || A != NN_A)
    throw std::runtime_error("nn warm start: feature layout mismatch "
                             "(wsnb " + std::to_string(G) + "/" + std::to_string(H) + "/" +
                             std::to_string(A) + ", solver " + std::to_string(NN_G) + "/" +
                             std::to_string(NN_H) + "/" + std::to_string(NN_A) + ")");
  NnModel m;
  m.pot_scale = r.f32();
  if (m.pot_scale != NN_POT_SCALE)
    throw std::runtime_error("nn warm start: unexpected POT_SCALE");
  m.hidden = r.u32();
  m.value = {read_layer(r), read_layer(r), read_layer(r)};
  m.strat = {read_layer(r), read_layer(r), read_layer(r)};
  check_net(m.value, NN_G + NN_H, m.hidden, "value");
  check_net(m.strat, NN_G + NN_H + NN_A, m.hidden, "strat");
  if (!r.at_eof()) throw std::runtime_error("nn warm start: trailing bytes");
  return m;
}

uint64_t nn_warm_start(GameTree& tree, const NnModel& model, int frame,
                       const std::vector<int>& display_board, int pseudo_iters,
                       float regret_beta) {
  if (pseudo_iters <= 0) throw std::runtime_error("warm_start: pseudo iterations must be > 0");
  if (regret_beta < 0.0f || !std::isfinite(regret_beta))
    throw std::runtime_error("warm_start: regret beta must be a finite number >= 0");
  if (tree.iterations != 0)
    throw std::runtime_error("warm_start: tree already has iterations; build a fresh tree");
  if (tree.lazy) throw std::runtime_error("warm_start: lazily loaded trees are unsupported (v1)");
  if (tree.cfg.board.size() != 5 || tree.nodes.empty() ||
      tree.nodes[0].kind != ANodeKind::Decision)
    throw std::runtime_error("warm_start: river roots only (exactly 5 board cards)");
  if (display_board.size() != 5)
    throw std::runtime_error("warm_start: internal error: display board size");
  if (tree.cfg.eff_stack <= 0 || tree.cfg.start_pot() <= 0)
    throw std::runtime_error("warm_start: tree needs a positive pot and effective stack");

  Walker w{tree,
           model,
           frame,
           canonical_perm(display_board),
           static_cast<float>(tree.cfg.start_pot()),
           static_cast<float>(tree.cfg.eff_stack),
           regret_beta,
           0,
           {},
           {},
           {},
           {},
           {},
           {}};
  std::vector<HistEntry> hist;
  std::vector<float> rp = tree.init_reach[0], ro = tree.init_reach[1];
  w.visit(0, hist, rp, ro);
  tree.iterations = pseudo_iters;
  return w.written;
}

}  // namespace pio
