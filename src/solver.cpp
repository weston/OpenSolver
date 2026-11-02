#include "solver.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>

#include "parallel.hpp"
#include "pfh2h.hpp"

namespace pio {

namespace {

inline bool all_zero(const float* v, int n) {
  for (int i = 0; i < n; ++i)
    if (v[i] != 0.0f) return false;
  return true;
}

}  // namespace

void Solver::compat_mass(int p, const float* ro, float* out) const {
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  float cs[NUM_CARDS] = {0};
  float total = 0;
  const uint8_t* oc1 = g_.hcard[o][0].data();
  const uint8_t* oc2 = g_.hcard[o][1].data();
  for (int k = 0; k < Ho; ++k) {
    float r = ro[k];
    total += r;
    cs[oc1[k]] += r;
    cs[oc2[k]] += r;
  }
  const uint8_t* pc1 = g_.hcard[p][0].data();
  const uint8_t* pc2 = g_.hcard[p][1].data();
  const int32_t* same = g_.same_hand[p].data();
  for (int h = 0; h < Hp; ++h) {
    float v = total - cs[pc1[h]] - cs[pc2[h]];
    if (same[h] >= 0) v += ro[same[h]];
    out[h] = v;
  }
}

double Solver::root_matchups() const {
  ArenaScope sc;
  int Hp = g_.num_hands(0);
  float* m = sc.a.alloc(Hp);
  compat_mass(0, g_.init_reach[1].data(), m);
  double s = 0;
  for (int h = 0; h < Hp; ++h) s += static_cast<double>(g_.init_reach[0][h]) * m[h];
  return s;
}

void Solver::fold_value(const GNode& n, int p, const float* ro, float* out) const {
  const ActionNode& an = g_.an(n);
  float pay = static_cast<float>(g_.terminal_payoff(an, p, p == an.player ? -1 : 1));
  compat_mass(p, ro, out);
  const int Hp = g_.num_hands(p);
  for (int h = 0; h < Hp; ++h) out[h] *= pay;
}

// One 8-byte word per 6-byte SdEntry: with the entry fields at fixed little-endian offsets this
// replaces the ldrh + ldrb + ldrb field loads with a single load in the hot loops below (-1.2%
// wall on flop_srp_2, -2.7% flop_3bet, -3.5% deep, bit-identical results; #57). Callers must pass
// only entries before the array's last one, so the 2-byte overread stays inside the vector.
static_assert(std::endian::native == std::endian::little, "SdEntry word loading assumes little-endian");
static inline uint64_t sd_word(const SdEntry* e) {
  uint64_t w;
  std::memcpy(&w, e, 8);
  return w;
}
static inline int sd_w_idx(uint64_t w) { return static_cast<int>(w & 0xffff); }
static inline int sd_w_c1(uint64_t w) { return static_cast<int>((w >> 16) & 0xff); }
static inline int sd_w_c2(uint64_t w) { return static_cast<int>((w >> 24) & 0xff); }
static inline int sd_w_same(uint64_t w) { return static_cast<int16_t>((w >> 32) & 0xffff); }

void Solver::showdown_board(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out) const {
  const int o = 1 - p;
  const SdEntry* pe = sb.ent[p].data();
  const SdEntry* oe = sb.ent[o].data();
  const int np = static_cast<int>(sb.ent[p].size());
  const auto* gr = sb.groups[p].data();
  const int ng = static_cast<int>(sb.groups[p].size());

  // One pass in strength order (see ShowdownBoard::groups): cumulative opponent mass and per-card mass
  // give, for each of p's hands, the mass of strictly weaker (lt) and weaker-or-equal (le) opponents.
  // The word-load loops stop one entry short and do a scalar tail so the 8-byte loads never overrun.
  ArenaScope sc;
  float* ltv = sc.a.alloc(static_cast<size_t>(np) * 2);
  float* lev = ltv + np;
  float cs[NUM_CARDS] = {0};
  float cum = 0;
  int i0 = 0, j0 = 0;
  for (int g = 0; g < ng; ++g) {
    const int i1 = gr[g].first, j1 = gr[g].second;
    for (int i = i0; i + 1 < i1; ++i) {
      const uint64_t w = sd_word(pe + i);
      ltv[i] = cum - cs[sd_w_c1(w)] - cs[sd_w_c2(w)];
    }
    if (i0 < i1) {
      const SdEntry q = pe[i1 - 1];
      ltv[i1 - 1] = cum - cs[q.c1] - cs[q.c2];
    }
    for (int j = j0; j + 1 < j1; ++j) {
      const uint64_t w = sd_word(oe + j);
      const float r = ro[sd_w_idx(w)];
      cum += r;
      cs[sd_w_c1(w)] += r;
      cs[sd_w_c2(w)] += r;
    }
    if (j0 < j1) {
      const SdEntry q = oe[j1 - 1];
      const float r = ro[q.idx];
      cum += r;
      cs[q.c1] += r;
      cs[q.c2] += r;
    }
    for (int i = i0; i + 1 < i1; ++i) {
      const uint64_t w = sd_word(pe + i);
      lev[i] = cum - cs[sd_w_c1(w)] - cs[sd_w_c2(w)];
    }
    if (i0 < i1) {
      const SdEntry q = pe[i1 - 1];
      lev[i1 - 1] = cum - cs[q.c1] - cs[q.c2];
    }
    i0 = i1;
    j0 = j1;
  }
  if (cum == 0.0f) return;
  // value = W*lt + L*(compat - le) + T*(le - lt) = (W - T)*lt + (T - L)*le + L*compat; the identical combo
  // (equal strength, inside "le") is subtracted twice by inclusion-exclusion and added back once.
  const float a = W - T, b = T - L;
  for (int i = 0; i + 1 < np; ++i) {
    const uint64_t w = sd_word(pe + i);
    const float sr = sd_w_same(w) >= 0 ? ro[sd_w_same(w)] : 0.0f;
    const float le = lev[i] + sr;
    const float compat = cum - cs[sd_w_c1(w)] - cs[sd_w_c2(w)] + sr;
    out[sd_w_idx(w)] += a * ltv[i] + b * le + L * compat;
  }
  if (np > 0) {
    const SdEntry q = pe[np - 1];
    const float sr = q.same >= 0 ? ro[q.same] : 0.0f;
    const float le = lev[np - 1] + sr;
    const float compat = cum - cs[q.c1] - cs[q.c2] + sr;
    out[q.idx] += a * ltv[np - 1] + b * le + L * compat;
  }
}

void Solver::showdown_value(const GNode& n, int p, const float* ro, float* out) const {
  const ActionNode& an = g_.an(n);
  const float W = static_cast<float>(g_.terminal_payoff(an, p, 1));
  const float L = static_cast<float>(g_.terminal_payoff(an, p, -1));
  const float T = static_cast<float>(g_.terminal_payoff(an, p, 0));
  const int Hp = g_.num_hands(p);
  std::memset(out, 0, sizeof(float) * Hp);
  if (n.board_len == 5) {
    showdown_board(g_.showdowns[n.aux], W, L, T, p, ro, out);
    return;
  }
  // All-in before the river: average the showdown over every runout.
  const AllinRunouts& ar = g_.allins[n.aux];
  if (ar.exact) {
    // Preflop all-in / pure preflop exit: exact equity via the hand-vs-hand table. For hand h,
    // value(h) = sum_o ro[o] * (W*win(h,o) + T*tie(h,o) + L*lose(h,o)) / C(48,5) over disjoint o;
    // with unit payoff u = L + (W - L)*win/N + (T - L)*tie/N this is a matrix-vector product.
    const PfTable& t = pf_table();
    const int o = 1 - p;
    const int Hp = g_.num_hands(p);
    const int Ho = g_.num_hands(o);
    const uint16_t* phands = g_.hands[p].data();
    const uint16_t* ohands = g_.hands[o].data();
    const double a = static_cast<double>(W - L) / PF_BOARDS_PER_PAIR;
    const double b = static_cast<double>(T - L) / PF_BOARDS_PER_PAIR;
    auto body = [&](int h) {
      uint64_t m = combo_mask(phands[h]);
      const uint32_t* wrow = t.win.data() + static_cast<size_t>(phands[h]) * NUM_COMBOS;
      const uint32_t* trow = t.tie.data() + static_cast<size_t>(phands[h]) * NUM_COMBOS;
      double acc = 0.0, compat = 0.0;
      for (int j = 0; j < Ho; ++j) {
        float r = ro[j];
        if (r == 0.0f) continue;
        uint32_t oc = ohands[j];
        if (combo_mask(oc) & m) continue;  // card removal
        compat += r;
        acc += r * (a * wrow[oc] + b * trow[oc]);
      }
      out[h] = static_cast<float>(acc + compat * L);
    };
    if (parallel && Hp >= 32) {
      std::function<void(int)> fn = body;
      pool().parallel_for(Hp, fn);
    } else {
      for (int h = 0; h < Hp; ++h) body(h);
    }
    return;
  }
  const int nb = static_cast<int>(ar.boards.size());
  const int chunks = parallel ? std::min(nb, 4 * pool().threads()) : 1;
  ArenaScope sc;
  float* acc = sc.a.alloc_zero(static_cast<size_t>(chunks) * Hp);
  auto task = [&](int k) {
    float* dst = acc + static_cast<size_t>(k) * Hp;
    for (int b = k; b < nb; b += chunks) showdown_board(g_.showdowns[ar.boards[b]], W, L, T, p, ro, dst);
  };
  if (chunks > 1) {
    std::function<void(int)> fn = task;
    pool().parallel_for(chunks, fn);
  } else {
    task(0);
  }
  for (int k = 0; k < chunks; ++k) {
    const float* src = acc + static_cast<size_t>(k) * Hp;
    for (int h = 0; h < Hp; ++h) out[h] += src[h];
  }
  for (int h = 0; h < Hp; ++h) out[h] *= ar.scale;
}

void Solver::current_strategy(uint32_t ni, float* out) const {
  const GNode& n = g_.nodes[ni];
  const int A = n.num_children;
  const int H = g_.num_hands(n.player);
  ArenaScope sc;
  const bool need_s = (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) != 0;
  const float* S = need_s ? g_.strat_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr) : nullptr;
  if (n.flags & GF_LOCKED) {
    std::memcpy(out, S, sizeof(float) * A * H);
    if (n.flags & GF_ELIMINATED) apply_elimination(ni, out);
    return;
  }
  const float* R = g_.regrets_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
  // Predictive (PCFR+-style, #71): match on R + the last instantaneous regret as an
  // optimistic guess of the next one. Preds are transient solver state (zero until the
  // node's first update), so this reduces to plain regret matching at t=1.
  const float* P = (predictive && !g_.preds.empty()) ? g_.preds.data() + n.storage : nullptr;
  float* sum = sc.a.alloc_zero(H);
  for (int a = 0; a < A; ++a) {
    const float* r = R + static_cast<size_t>(a) * H;
    const float* pr = P ? P + static_cast<size_t>(a) * H : nullptr;
    float* o = out + static_cast<size_t>(a) * H;
    for (int h = 0; h < H; ++h) {
      float v = r[h] + (pr ? pr[h] : 0.0f);
      v = v > 0.0f ? v : 0.0f;
      o[h] = v;
      sum[h] += v;
    }
  }
  const float uni = 1.0f / static_cast<float>(A);
  // inv = 1/sum, or 0 when all regrets are non-positive (then the strategy is uniform: add `uni`).
  float* add = sc.a.alloc(H);
  for (int h = 0; h < H; ++h) {
    const bool pos = sum[h] > 0.0f;
    add[h] = pos ? 0.0f : uni;
    sum[h] = pos ? 1.0f / sum[h] : 0.0f;
  }
  for (int a = 0; a < A; ++a) {
    float* o = out + static_cast<size_t>(a) * H;
    for (int h = 0; h < H; ++h) o[h] = o[h] * sum[h] + add[h];
  }
  if (n.flags & GF_COMBO_LOCKED) {
    auto it = g_.combo_lock.find(ni);
    if (it != g_.combo_lock.end()) {
      const auto& mask = it->second;
      for (int h = 0; h < H; ++h)
        if (mask[h])
          for (int a = 0; a < A; ++a) out[static_cast<size_t>(a) * H + h] = S[static_cast<size_t>(a) * H + h];
    }
  }
  if (n.flags & GF_ELIMINATED) apply_elimination(ni, out);
}

void Solver::apply_elimination(uint32_t ni, float* out) const {
  auto it = g_.eliminated.find(ni);
  if (it == g_.eliminated.end()) return;
  const GNode& n = g_.nodes[ni];
  const int A = n.num_children;
  const int H = g_.num_hands(n.player);
  const uint32_t mask = it->second;
  int live = 0;
  for (int a = 0; a < A; ++a) live += !((mask >> a) & 1u);
  if (live == 0) return;
  for (int h = 0; h < H; ++h) {
    float sum = 0;
    for (int a = 0; a < A; ++a) {
      float& v = out[static_cast<size_t>(a) * H + h];
      if ((mask >> a) & 1u) v = 0.0f;
      sum += v;
    }
    for (int a = 0; a < A; ++a) {
      float& v = out[static_cast<size_t>(a) * H + h];
      if ((mask >> a) & 1u) continue;
      v = sum > 0.0f ? v / sum : 1.0f / static_cast<float>(live);
    }
  }
}

void Solver::average_strategy(uint32_t ni, float* out) const {
  const GNode& n = g_.nodes[ni];
  const int A = n.num_children;
  const int H = g_.num_hands(n.player);
  ArenaScope sc;
  const float* S = g_.strat_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
  if (n.flags & GF_LOCKED) {
    std::memcpy(out, S, sizeof(float) * A * H);
    return;
  }
  float* sum = sc.a.alloc_zero(H);
  for (int a = 0; a < A; ++a) {
    const float* s = S + static_cast<size_t>(a) * H;
    for (int h = 0; h < H; ++h) sum[h] += s[h];
  }
  float maxsum = 0;
  for (int h = 0; h < H; ++h) maxsum = std::max(maxsum, sum[h]);
  // Hands that (almost) never reach this node have no meaningful average; like PioSOLVER's
  // regret-derived display, use their current regret-matching strategy instead.
  const float thresh = maxsum * 1e-4f;
  bool need_current = false;
  for (int h = 0; h < H; ++h) need_current |= !(sum[h] > thresh);
  float* cur = nullptr;
  if (need_current) {
    cur = sc.a.alloc(static_cast<size_t>(A) * H);
    current_strategy(ni, cur);
  }
  for (int a = 0; a < A; ++a) {
    const float* s = S + static_cast<size_t>(a) * H;
    float* o = out + static_cast<size_t>(a) * H;
    const float* c = cur ? cur + static_cast<size_t>(a) * H : nullptr;
    for (int h = 0; h < H; ++h) o[h] = sum[h] > thresh ? s[h] / sum[h] : c[h];
  }
  if (n.flags & GF_ELIMINATED) apply_elimination(ni, out);
}

void Solver::walk(Walk mode, uint32_t ni, int p, const float* rp, const float* ro, float* out) {
  switch (mode) {
    case Walk::CFR: walk_t<Walk::CFR>(ni, p, rp, ro, out); break;
    case Walk::BestResponse: walk_t<Walk::BestResponse>(ni, p, rp, ro, out); break;
    case Walk::Evaluate: walk_t<Walk::Evaluate>(ni, p, rp, ro, out); break;
  }
}

template <Walk M>
void Solver::walk_t(uint32_t ni, int p, const float* rp, const float* ro, float* out) {
  const GNode& n = g_.nodes[ni];
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  switch (n.kind) {
    case ANodeKind::Fold:
      fold_value(n, p, ro, out);
      return;
    case ANodeKind::Showdown:
      showdown_value(n, p, ro, out);
      return;
    case ANodeKind::Chance: {
      const ChanceInfo& ci = g_.chances[n.aux];
      if (ci.flop_deal) {
        flop_deal(n, ci, M, p, rp, ro, out);
        return;
      }
      const int K = n.num_children;
      ArenaScope sc;
      float* res = sc.a.alloc(static_cast<size_t>(K) * Hp);
      auto task = [&](int k) {
        ArenaScope tsc;
        int card = ci.cards[k];
        float* rk = res + static_cast<size_t>(k) * Hp;
        float* ro2 = tsc.a.alloc(Ho);
        std::memcpy(ro2, ro, sizeof(float) * Ho);
        for (uint16_t h : g_.hands_with_card[o][card]) ro2[h] = 0.0f;
        float* rp2 = tsc.a.alloc(Hp);
        std::memcpy(rp2, rp, sizeof(float) * Hp);
        for (uint16_t h : g_.hands_with_card[p][card]) rp2[h] = 0.0f;
        if (all_zero(ro2, Ho) && !(M == Walk::BestResponse && write_best_response)) {
          std::memset(rk, 0, sizeof(float) * Hp);
          if (M == Walk::CFR) strategy_only(n.first_child + static_cast<uint32_t>(k), p, rp2);
          return;
        }
        walk_t<M>(n.first_child + static_cast<uint32_t>(k), p, rp2, ro2, rk);
        for (uint16_t h : g_.hands_with_card[p][card]) rk[h] = 0.0f;
      };
      if (parallel && K > 1) {
        std::function<void(int)> fn = task;
        pool().parallel_for(K, fn);
      } else {
        for (int k = 0; k < K; ++k) task(k);
      }
      std::memset(out, 0, sizeof(float) * Hp);
      for (int k = 0; k < K; ++k) {
        const float* rk = res + static_cast<size_t>(k) * Hp;
        for (int h = 0; h < Hp; ++h) out[h] += rk[h];
        for (auto& cp : ci.iso[k]) {
          const uint16_t* ph = g_.perm_hand[p][PERM_INV[cp.second]].data();
          for (int h = 0; h < Hp; ++h) out[h] += rk[ph[h]];
        }
      }
      const float inv = 1.0f / static_cast<float>(ci.num_possible);
      for (int h = 0; h < Hp; ++h) out[h] *= inv;
      return;
    }
    case ANodeKind::Decision: break;
  }

  const int A = n.num_children;
  const int actor = n.player;
  const int Ha = g_.num_hands(actor);
  ArenaScope sc;
  float* sigma = sc.a.alloc(static_cast<size_t>(A) * Ha);
  if (M == Walk::CFR) current_strategy(ni, sigma);
  else if (actor == o || M == Walk::Evaluate || (n.flags & (GF_LOCKED | GF_COMBO_LOCKED))) average_strategy(ni, sigma);

  if (actor == p) {
    // Regret-based pruning (#51): in the CFR walk, an action whose current strategy is zero for
    // every one of p's hands contributes exactly nothing to this iteration's strategy, cfv
    // aggregate or strategy-sum rows; the only work lost by skipping its subtree is re-measuring
    // its instantaneous regret, which is Brown & Sandholm's bounded approximation (solver.hpp).
    // Never prune nodes whose update semantics must not change: locked, combo-locked, incentivised
    // (rake/ICM bonuses are added to the regret update) or eliminated.
    uint8_t* pruned = nullptr;
    if (M == Walk::CFR && prune_this_iter_ &&
        !(n.flags & (GF_LOCKED | GF_COMBO_LOCKED | GF_INCENTIVES | GF_ELIMINATED))) {
      pruned = reinterpret_cast<uint8_t*>(sc.a.alloc(static_cast<size_t>((A + 3) / 4)));
      for (int a = 0; a < A; ++a) pruned[a] = all_zero(sigma + static_cast<size_t>(a) * Hp, Hp) ? 1 : 0;
    }
    float* cfv = sc.a.alloc(static_cast<size_t>(A) * Hp);
    float* rp2 = sc.a.alloc(Hp);
    for (int a = 0; a < A; ++a) {
      if (pruned && pruned[a]) continue;  // cfv[a] intentionally left undefined
      const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
      float* ca = cfv + static_cast<size_t>(a) * Hp;
      const GNode& child = g_.nodes[ci];
      if (child.kind == ANodeKind::Fold) {
        // Our own fold: opponent reach is unchanged, so this is just payoff * compatible mass.
        fold_value(child, p, ro, ca);
        continue;
      }
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      if (M == Walk::BestResponse) std::memcpy(rp2, rp, sizeof(float) * Hp);
      else
        for (int h = 0; h < Hp; ++h) rp2[h] = rp[h] * sa[h];
      walk_t<M>(ci, p, rp2, ro, ca);
    }
    if (M == Walk::BestResponse) {
      std::memcpy(out, cfv, sizeof(float) * Hp);
      for (int a = 1; a < A; ++a) {
        const float* c = cfv + static_cast<size_t>(a) * Hp;
        for (int h = 0; h < Hp; ++h) out[h] = std::max(out[h], c[h]);
      }
      // A best response cannot deviate where the strategy is locked (whole node or locked combos);
      // this is what makes MES / exploitability meaningful in node-locking workflows (Pio does the same).
      if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) {
        const uint8_t* lockmask = nullptr;
        if (!(n.flags & GF_LOCKED)) {
          auto it = g_.combo_lock.find(ni);
          if (it != g_.combo_lock.end()) lockmask = it->second.data();
        }
        for (int h = 0; h < Hp; ++h) {
          if (lockmask && !lockmask[h]) continue;
          float v = 0.0f;
          for (int a = 0; a < A; ++a) v += sigma[static_cast<size_t>(a) * Hp + h] * cfv[static_cast<size_t>(a) * Hp + h];
          out[h] = v;
        }
        return;
      }
      if (write_best_response && !(n.flags & GF_LOCKED)) {
        // set_mes: store the pure best response as both strategy and regrets.
        float* R = g_.regrets_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
        float* S = g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
        for (int h = 0; h < Hp; ++h) {
          int best = 0;
          for (int a = 1; a < A; ++a)
            if (cfv[static_cast<size_t>(a) * Hp + h] >= cfv[static_cast<size_t>(best) * Hp + h]) best = a;  // ties: last action
          for (int a = 0; a < A; ++a) {
            float v = a == best ? 1.0f : 0.0f;
            R[static_cast<size_t>(a) * Hp + h] = v;
            S[static_cast<size_t>(a) * Hp + h] = v;
          }
        }
        g_.regrets_commit(n, R);
        g_.strat_commit(n, S);
      }
      return;
    }
    std::memset(out, 0, sizeof(float) * Hp);
    for (int a = 0; a < A; ++a) {
      if (pruned && pruned[a]) continue;  // its sigma row is zero: identical to the full sum
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      const float* c = cfv + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) out[h] += sa[h] * c[h];
    }
    if (M != Walk::CFR) return;
    if (n.flags & GF_LOCKED) return;
    float* R = g_.regrets_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
    // Average accumulation is skipped until t > avg_delay (#71): S stays untouched (zero),
    // so the average starts from the first post-delay iteration like a warm-started run.
    float* S = g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
    // Predictive CFR stores each round's instantaneous regret for the next round's
    // matching input; keep the array fresh whenever it is allocated.
    float* P = !g_.preds.empty() ? g_.preds.data() + n.storage : nullptr;
    const float pm = pos_mult_, nm = neg_mult_, sm = strat_mult_;
    const float* inc = nullptr;
    float* mass = nullptr;
    if (n.flags & GF_INCENTIVES) {
      auto it = g_.incentives.find(ni);
      if (it != g_.incentives.end()) {
        inc = it->second.data();
        mass = sc.a.alloc(Hp);
        compat_mass(p, ro, mass);
      }
    }
    const uint8_t* lockmask = nullptr;
    if (n.flags & GF_COMBO_LOCKED) {
      auto it = g_.combo_lock.find(ni);
      if (it != g_.combo_lock.end()) lockmask = it->second.data();
    }
    float mr = 0.0f, ms = 0.0f;
    const bool fast = !lockmask && !inc;
    const bool accum = accumulate_;
    for (int a = 0; a < A; ++a) {
      float* r = R + static_cast<size_t>(a) * Hp;
      float* s = S + static_cast<size_t>(a) * Hp;
      float* pr = P ? P + static_cast<size_t>(a) * Hp : nullptr;
      const float* c = cfv + static_cast<size_t>(a) * Hp;
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      const float ia = inc ? inc[a] : 0.0f;
      if (pruned && pruned[a]) {
        // RBP pruned: the regret row is NOT touched (not even discounted). Discounting a frozen
        // row makes it forget its magnitude exponentially (at beta=0 it halves every iteration)
        // while the true update would keep it near its (negative) fixed point, so discounted
        // frozen rows churn back into play at every revisiting iteration. A completely frozen row
        // instead stays below the unpruned value until genuinely re-measured, so pruning only ever
        // DELAYS a re-entry, never invents one. The strategy sum gets its usual discount: with
        // sigma == 0 on the whole row that is exactly the unpruned update. (Skip it during the
        // avg_delay phase, exactly like the unpruned rows do.)
        for (int h = 0; h < Hp; ++h) {
          if (accum) {
            float y = s[h] * sm;
            s[h] = y;
            ms = y > ms ? y : ms;
          }
          float ax = std::fabs(r[h]);
          mr = ax > mr ? ax : mr;
        }
        continue;
      }
      if (fast && !g_.compressed) {
        // float32 storage: plain update (vectorizes; no max tracking needed)
        for (int h = 0; h < Hp; ++h) {
          float x = r[h];
          x *= x > 0.0f ? pm : nm;
          const float inst = c[h] - out[h];
          r[h] = x + inst;
          if (pr) pr[h] = inst;
          if (accum) s[h] = s[h] * sm + rp[h] * sa[h];
        }
      } else if (fast) {
        for (int h = 0; h < Hp; ++h) {
          float x = r[h];
          x *= x > 0.0f ? pm : nm;
          const float inst = c[h] - out[h];
          x = x + inst;
          r[h] = x;
          if (pr) pr[h] = inst;
          float ax = std::fabs(x);
          mr = ax > mr ? ax : mr;
          if (accum) {
            float y = s[h] * sm + rp[h] * sa[h];
            s[h] = y;
            ms = y > ms ? y : ms;
          }
        }
      } else {
        for (int h = 0; h < Hp; ++h) {
          if (lockmask && lockmask[h]) continue;
          float x = r[h];
          x *= x > 0.0f ? pm : nm;
          float bonus = inc ? ia * mass[h] : 0.0f;
          const float inst = c[h] + bonus - out[h];
          r[h] = x + inst;
          if (pr) pr[h] = inst;
          if (accum) s[h] = s[h] * sm + rp[h] * sa[h];
        }
      }
    }
    g_.regrets_commit(n, R, fast ? mr : -1.0f);
    if (accum) g_.strat_commit(n, S, fast ? ms : -1.0f);
    return;
  }

  // Opponent node.
  float* ro2 = sc.a.alloc(Ho);
  float* tmp = sc.a.alloc(Hp);
  std::memset(out, 0, sizeof(float) * Hp);
  for (int a = 0; a < A; ++a) {
    const float* sa = sigma + static_cast<size_t>(a) * Ho;
    const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
    if (g_.nodes[ci].kind == ANodeKind::Fold) {
      // Opponent folds: value = payoff * compatible mass of (ro * sigma_fold), fused into one pass
      // over the opponent's hands (no reach vector, no temporary, no extra add pass).
      const float pay = static_cast<float>(g_.terminal_payoff(g_.an(g_.nodes[ci]), p, 1));
      const uint8_t* oc1 = g_.hcard[o][0].data();
      const uint8_t* oc2 = g_.hcard[o][1].data();
      float cs[NUM_CARDS] = {0};
      float total = 0;
      for (int k = 0; k < Ho; ++k) {
        const float v = ro[k] * sa[k];
        total += v;
        cs[oc1[k]] += v;
        cs[oc2[k]] += v;
      }
      if (total == 0.0f) {
        if (M == Walk::CFR) strategy_only(ci, p, rp);
        continue;
      }
      const uint8_t* pc1 = g_.hcard[p][0].data();
      const uint8_t* pc2 = g_.hcard[p][1].data();
      const int32_t* same = g_.same_hand[p].data();
      for (int h = 0; h < Hp; ++h) {
        float m = total - cs[pc1[h]] - cs[pc2[h]];
        if (same[h] >= 0) m += ro[same[h]] * sa[same[h]];
        out[h] += pay * m;
      }
      continue;
    }
    bool any = false;
    for (int k = 0; k < Ho; ++k) {
      float v = ro[k] * sa[k];
      ro2[k] = v;
      any |= v != 0.0f;
    }
    if (!any) {
      if (M == Walk::CFR) strategy_only(n.first_child + static_cast<uint32_t>(a), p, rp);
      // set_mes writes a best response everywhere, also where the opponent never goes (like Pio).
      if (!(M == Walk::BestResponse && write_best_response)) continue;
    }
    walk_t<M>(n.first_child + static_cast<uint32_t>(a), p, rp, ro2, tmp);
    for (int h = 0; h < Hp; ++h) out[h] += tmp[h];
  }
}

// Subtree the opponent never reaches this iteration: counterfactual values (and so instantaneous
// regrets) are exactly zero, but the traverser's regrets must still be discounted and its average
// strategy accumulated, otherwise the average is biased toward iterations where the opponent enters.
void Solver::strategy_only(uint32_t ni, int p, const float* rp) {
  const GNode& n = g_.nodes[ni];
  const int Hp = g_.num_hands(p);
  if (n.kind == ANodeKind::Fold || n.kind == ANodeKind::Showdown) return;
  if (all_zero(rp, Hp)) return;
  if (n.kind == ANodeKind::Chance) {
    const ChanceInfo& ci = g_.chances[n.aux];
    const int K = n.num_children;
    auto task = [&](int k) {
      ArenaScope tsc;
      float* rp2 = tsc.a.alloc(Hp);
      std::memcpy(rp2, rp, sizeof(float) * Hp);
      if (ci.flop_deal) {
        for (uint8_t c : ci.flops[k])
          for (uint16_t h : g_.hands_with_card[p][c]) rp2[h] = 0.0f;
      } else {
        for (uint16_t h : g_.hands_with_card[p][ci.cards[k]]) rp2[h] = 0.0f;
      }
      strategy_only(n.first_child + static_cast<uint32_t>(k), p, rp2);
    };
    if (parallel && K > 1) {
      std::function<void(int)> fn = task;
      pool().parallel_for(K, fn);
    } else {
      for (int k = 0; k < K; ++k) task(k);
    }
    return;
  }
  const int A = n.num_children;
  if (n.player != p) {
    for (int a = 0; a < A; ++a) strategy_only(n.first_child + static_cast<uint32_t>(a), p, rp);
    return;
  }
  ArenaScope sc;
  float* sigma = sc.a.alloc(static_cast<size_t>(A) * Hp);
  current_strategy(ni, sigma);
  if (!(n.flags & GF_LOCKED)) {
    float* R = g_.regrets_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
    float* S = g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
    // An unreached node had zero counterfactual values this round, so its instantaneous
    // regret is zero: the prediction must not reuse a stale one from an earlier round.
    float* P = !g_.preds.empty() ? g_.preds.data() + n.storage : nullptr;
    const uint8_t* lockmask = nullptr;
    if (n.flags & GF_COMBO_LOCKED) {
      auto it = g_.combo_lock.find(ni);
      if (it != g_.combo_lock.end()) lockmask = it->second.data();
    }
    const float pm = pos_mult_, nm = neg_mult_, sm = strat_mult_;
    const bool accum = accumulate_;
    for (int a = 0; a < A; ++a) {
      float* r = R + static_cast<size_t>(a) * Hp;
      float* s = S + static_cast<size_t>(a) * Hp;
      float* pr = P ? P + static_cast<size_t>(a) * Hp : nullptr;
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) {
        if (lockmask && lockmask[h]) continue;
        float x = r[h];
        r[h] = x * (x > 0.0f ? pm : nm);
        if (pr) pr[h] = 0.0f;
        if (accum) s[h] = s[h] * sm + rp[h] * sa[h];
      }
    }
    g_.regrets_commit(n, R);
    if (accum) g_.strat_commit(n, S);
  }
  float* rp2 = sc.a.alloc(Hp);
  for (int a = 0; a < A; ++a) {
    const float* sa = sigma + static_cast<size_t>(a) * Hp;
    for (int h = 0; h < Hp; ++h) rp2[h] = rp[h] * sa[h];
    strategy_only(n.first_child + static_cast<uint32_t>(a), p, rp2);
  }
}

// Preflop exit: deal one of the subset flops. Each subset flop stands for its whole suit-isomorphism
// class, so its values are averaged over every suit permutation valid for the ranges; this keeps the
// preflop game suit-symmetric (and postflop isomorphism valid).
void Solver::flop_deal(const GNode& n, const ChanceInfo& ci, Walk mode, int p, const float* rp, const float* ro,
                       float* out) {
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  const int K = n.num_children;
  ArenaScope sc;
  float* res = sc.a.alloc(static_cast<size_t>(K) * Hp);
  auto zero_flop = [&](int pl, float* v, const std::array<uint8_t, 3>& f) {
    for (uint8_t c : f)
      for (uint16_t h : g_.hands_with_card[pl][c]) v[h] = 0.0f;
  };
  auto task = [&](int k) {
    ArenaScope tsc;
    const auto& f = ci.flops[k];
    float* rk = res + static_cast<size_t>(k) * Hp;
    float* ro2 = tsc.a.alloc(Ho);
    std::memcpy(ro2, ro, sizeof(float) * Ho);
    zero_flop(o, ro2, f);
    float* rp2 = tsc.a.alloc(Hp);
    std::memcpy(rp2, rp, sizeof(float) * Hp);
    zero_flop(p, rp2, f);
    uint32_t child = n.first_child + static_cast<uint32_t>(k);
    if (all_zero(ro2, Ho) && !(mode == Walk::BestResponse && write_best_response)) {
      std::memset(rk, 0, sizeof(float) * Hp);
      if (mode == Walk::CFR) strategy_only(child, p, rp2);
      return;
    }
    walk(mode, child, p, rp2, ro2, rk);
    zero_flop(p, rk, f);
  };
  if (parallel && K > 1) {
    std::function<void(int)> fn = task;
    pool().parallel_for(K, fn);
  } else {
    for (int k = 0; k < K; ++k) task(k);
  }
  std::memset(out, 0, sizeof(float) * Hp);
  const auto& G = g_.perm_all;
  const float gn = 1.0f / static_cast<float>(G.size());
  for (int k = 0; k < K; ++k) {
    const float* rk = res + static_cast<size_t>(k) * Hp;
    const float w = ci.fweight[k] * gn;
    for (uint8_t pm : G) {
      const uint16_t* ph = g_.perm_hand[p][PERM_INV[pm]].data();
      for (int h = 0; h < Hp; ++h) out[h] += w * rk[ph[h]];
    }
  }
  // Normalise: a pair (h, o) is dealt flop variant v with probability w_v / N(h,o). Divide our hand's
  // value by the opponent-reach-weighted mean of N(h,o); this keeps each hand's total deal probability
  // exact (and the value exact whenever N(h,o) does not vary across opponents).
  const float* Wp = g_.flop_compat[p].data();
  const float* nc = g_.flop_card[p].data();
  const float* np2 = g_.flop_pair[p].data();
  const uint8_t* oc1 = g_.hcard[o][0].data();
  const uint8_t* oc2 = g_.hcard[o][1].data();
  const uint16_t* ocombo = g_.hands[o].data();
  const auto& phands = g_.hands[p];
  auto norm = [&](int h0, int h1) {
    for (int h = h0; h < h1; ++h) {
      if (out[h] == 0.0f) continue;
      const uint64_t hm = combo_mask(phands[h]);
      const float* nch = nc + static_cast<size_t>(h) * NUM_CARDS;
      const float* nph = np2 + static_cast<size_t>(h) * NUM_COMBOS;
      double num = 0, den = 0;
      for (int j = 0; j < Ho; ++j) {
        const float r = ro[j];
        if (r == 0.0f || (combo_mask(ocombo[j]) & hm)) continue;
        num += r * (Wp[h] - nch[oc1[j]] - nch[oc2[j]] + nph[ocombo[j]]);
        den += r;
      }
      out[h] = num > 0 ? static_cast<float>(out[h] * den / num) : 0.0f;
    }
  };
  const int chunks = parallel ? std::min(Hp, 4 * pool().threads()) : 1;
  if (chunks > 1) {
    std::function<void(int)> fn = [&](int c) { norm(c * Hp / chunks, (c + 1) * Hp / chunks); };
    pool().parallel_for(chunks, fn);
  } else {
    norm(0, Hp);
  }
}

void Solver::iterate(int t) {
  double td = static_cast<double>(t);
  double pa = std::pow(td, params.alpha);
  double pb = std::pow(td, params.beta);
  pos_mult_ = static_cast<float>(pa / (pa + 1.0));
  neg_mult_ = static_cast<float>(pb / (pb + 1.0));
  strat_mult_ = static_cast<float>(std::pow(td / (td + 1.0), params.gamma));
  accumulate_ = t > avg_delay;  // set_avg_delay: the first t <= avg_delay only move regrets
  // Every prune_period-th iteration is measured in full (the RBP revisiting schedule), and
  // pruning activates only after two full periods (see Solver::pruning): pruning from t=1
  // destroys convergence (flop_3bet to 0.2% pot: 50 -> 230 iterations at period 32).
  prune_this_iter_ = pruning && t > 2 * prune_period && !(prune_period > 1 && t % prune_period == 0);
  for (int k = 0; k < 2; ++k) {
    int p = k == 0 ? first_player : 1 - first_player;
    ArenaScope sc;
    float* out = sc.a.alloc(g_.num_hands(p));
    walk_t<Walk::CFR>(0, p, g_.init_reach[p].data(), g_.init_reach[1 - p].data(), out);
  }
}

void Solver::iterate_subtree(uint32_t ni, int t, const float* r0, const float* r1) {
  double td = static_cast<double>(t);
  double pa = std::pow(td, params.alpha);
  double pb = std::pow(td, params.beta);
  pos_mult_ = static_cast<float>(pa / (pa + 1.0));
  neg_mult_ = static_cast<float>(pb / (pb + 1.0));
  strat_mult_ = static_cast<float>(std::pow(td / (td + 1.0), params.gamma));
  accumulate_ = t > avg_delay;
  prune_this_iter_ = pruning && t > 2 * prune_period && !(prune_period > 1 && t % prune_period == 0);
  const float* r[2] = {r0, r1};
  for (int k = 0; k < 2; ++k) {
    int p = k == 0 ? first_player : 1 - first_player;
    ArenaScope sc;
    float* out = sc.a.alloc(g_.num_hands(p));
    walk_t<Walk::CFR>(ni, p, r[p], r[1 - p], out);
  }
}

RootStats Solver::root_stats() {
  RootStats st;
  double M = root_matchups();
  if (M <= 0) return st;
  for (int p = 0; p < 2; ++p) {
    ArenaScope sc;
    int Hp = g_.num_hands(p);
    float* out = sc.a.alloc(Hp);
    const float* rp = g_.init_reach[p].data();
    const float* ro = g_.init_reach[1 - p].data();
    walk_t<Walk::Evaluate>(0, p, rp, ro, out);
    double s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rp[h]) * out[h];
    st.ev[p] = s / M;
    walk_t<Walk::BestResponse>(0, p, rp, ro, out);
    s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rp[h]) * out[h];
    st.mes[p] = s / M;
  }
  st.exploitability = ((st.mes[0] - st.ev[0]) + (st.mes[1] - st.ev[1])) / 2.0;
  return st;
}

}  // namespace pio
