#include "equity.hpp"

#include <algorithm>
#include <cstring>
#include <random>

#include "eval.hpp"

namespace pio {

namespace {

struct Live {
  std::vector<int> combos;
  std::vector<float> w;
  std::array<float, NUM_COMBOS> by_combo{};
};

Live live_hands(const std::array<float, NUM_COMBOS>& r, uint64_t dead) {
  Live l;
  for (int c = 0; c < NUM_COMBOS; ++c)
    if (r[c] > 0 && !(combo_mask(c) & dead)) {
      l.combos.push_back(c);
      l.w.push_back(r[c]);
      l.by_combo[c] = r[c];
    }
  return l;
}

// For a complete 5-card board, add per hero hand: weighted villain mass it beats (win), ties with
// (tie), and the total compatible villain mass (compat). Card removal is exact: villain hands
// sharing a card with the hero hand or the board are excluded.
void accumulate_board(const int* board5, const Live& hero, const Live& vil, double* win, double* tie, double* compat) {
  uint64_t bm = 0;
  for (int i = 0; i < 5; ++i) bm |= card_bit(board5[i]);
  int cs7[7];
  for (int i = 0; i < 5; ++i) cs7[i] = board5[i];
  auto strengths = [&](const Live& l, std::vector<std::pair<uint32_t, int>>& out) {
    out.clear();
    for (size_t i = 0; i < l.combos.size(); ++i) {
      int c = l.combos[i];
      if (combo_mask(c) & bm) continue;
      cs7[5] = COMBOS[c].hi;
      cs7[6] = COMBOS[c].lo;
      out.push_back({evaluate(cs7, 7), static_cast<int>(i)});
    }
    std::sort(out.begin(), out.end());
  };
  thread_local std::vector<std::pair<uint32_t, int>> hs, vs;
  thread_local std::vector<double> winv;
  strengths(hero, hs);
  strengths(vil, vs);
  double csall[NUM_CARDS] = {0}, total = 0;
  for (auto& v : vs) {
    int c = vil.combos[v.second];
    double w = vil.w[v.second];
    total += w;
    csall[COMBOS[c].hi] += w;
    csall[COMBOS[c].lo] += w;
  }
  double cs[NUM_CARDS];
  std::memset(cs, 0, sizeof(cs));
  double cum = 0;
  size_t j = 0;
  winv.assign(hs.size(), 0.0);
  for (size_t i = 0; i < hs.size(); ++i) {
    while (j < vs.size() && vs[j].first < hs[i].first) {
      int c = vil.combos[vs[j].second];
      double w = vil.w[vs[j].second];
      cum += w;
      cs[COMBOS[c].hi] += w;
      cs[COMBOS[c].lo] += w;
      ++j;
    }
    int hc = hero.combos[hs[i].second];
    winv[i] = cum - cs[COMBOS[hc].hi] - cs[COMBOS[hc].lo];
  }
  std::memset(cs, 0, sizeof(cs));
  cum = 0;
  size_t k = vs.size();
  for (size_t i = hs.size(); i-- > 0;) {
    while (k > 0 && vs[k - 1].first > hs[i].first) {
      int c = vil.combos[vs[k - 1].second];
      double w = vil.w[vs[k - 1].second];
      cum += w;
      cs[COMBOS[c].hi] += w;
      cs[COMBOS[c].lo] += w;
      --k;
    }
    int idx = hs[i].second;
    int hc = hero.combos[idx];
    double lose = cum - cs[COMBOS[hc].hi] - cs[COMBOS[hc].lo];
    // Inclusion-exclusion: the identical villain combo contains both hero cards and was
    // subtracted twice, so add it back once.
    double comp = total - csall[COMBOS[hc].hi] - csall[COMBOS[hc].lo] + vil.by_combo[hc];
    win[idx] += winv[i];
    compat[idx] += comp;
    tie[idx] += comp - winv[i] - lose;
  }
}

}  // namespace

EquityResult range_equity(const std::vector<int>& board, const std::array<float, NUM_COMBOS>& hero,
                          const std::array<float, NUM_COMBOS>& villain) {
  EquityResult res;
  uint64_t bm = board_mask(board);
  Live h = live_hands(hero, bm), v = live_hands(villain, bm);
  std::vector<double> win(h.combos.size(), 0), tie(h.combos.size(), 0), compat(h.combos.size(), 0);
  int b5[5];
  for (size_t i = 0; i < board.size(); ++i) b5[i] = board[i];
  std::vector<int> avail;
  for (int c = 0; c < NUM_CARDS; ++c)
    if (!(bm & card_bit(c))) avail.push_back(c);
  int need = 5 - static_cast<int>(board.size());
  if (need == 0) {
    accumulate_board(b5, h, v, win.data(), tie.data(), compat.data());
  } else if (need == 1) {
    for (int c : avail) {
      b5[4] = c;
      accumulate_board(b5, h, v, win.data(), tie.data(), compat.data());
    }
  } else {
    for (size_t x = 0; x < avail.size(); ++x)
      for (size_t y = x + 1; y < avail.size(); ++y) {
        b5[3] = avail[x];
        b5[4] = avail[y];
        accumulate_board(b5, h, v, win.data(), tie.data(), compat.data());
      }
  }
  double csall[NUM_CARDS] = {0}, total = 0;
  for (size_t i = 0; i < v.combos.size(); ++i) {
    total += v.w[i];
    csall[COMBOS[v.combos[i]].hi] += v.w[i];
    csall[COMBOS[v.combos[i]].lo] += v.w[i];
  }
  double num = 0, den = 0;
  for (size_t i = 0; i < h.combos.size(); ++i) {
    int hc = h.combos[i];
    double m = total - csall[COMBOS[hc].hi] - csall[COMBOS[hc].lo] + v.by_combo[hc];
    res.matchups[hc] = static_cast<float>(m);
    res.eq[hc] = compat[i] > 0 ? static_cast<float>((win[i] + 0.5 * tie[i]) / compat[i]) : 0.0f;
    num += h.w[i] * (win[i] + 0.5 * tie[i]);
    den += h.w[i] * compat[i];
  }
  res.total = den > 0 ? num / den : 0;
  return res;
}

int preflop_class(int combo) {
  int a = COMBOS[combo].hi, b = COMBOS[combo].lo;
  int ra = card_rank(a), rb = card_rank(b);
  int hi = std::max(ra, rb), lo = std::min(ra, rb);
  bool suited = card_suit(a) == card_suit(b);
  // show_preflop_order: rows r1 = A..2, cols r2 = A..2; r1 > r2 -> suited, r1 < r2 -> offsuit.
  int r1, r2;
  if (hi == lo) r1 = r2 = hi;
  else if (suited) {
    r1 = hi;
    r2 = lo;
  } else {
    r1 = lo;
    r2 = hi;
  }
  return (12 - r1) * 13 + (12 - r2);
}

int preflop_class_of_name(const std::string& name) {
  if (name.size() < 2) return -1;
  int a = parse_rank(name[0]), b = parse_rank(name[1]);
  if (a < 0 || b < 0) return -1;
  int hi = std::max(a, b), lo = std::min(a, b);
  int r1, r2;
  if (hi == lo) r1 = r2 = hi;
  else if (name.size() > 2 && name[2] == 's') {
    r1 = hi;
    r2 = lo;
  } else {
    r1 = lo;
    r2 = hi;
  }
  return (12 - r1) * 13 + (12 - r2);
}

PreflopEquity preflop_equity(const std::array<float, NUM_COMBOS>& hero, const std::array<float, NUM_COMBOS>& villain,
                             int boards) {
  PreflopEquity res;
  res.eq.assign(169, 0);
  res.matchups.assign(169, 0);
  Live h = live_hands(hero, 0), v = live_hands(villain, 0);
  std::vector<double> win(h.combos.size(), 0), tie(h.combos.size(), 0), compat(h.combos.size(), 0);
  std::mt19937_64 rng(12345);
  int b5[5];
  for (int it = 0; it < boards; ++it) {
    uint64_t m = 0;
    for (int i = 0; i < 5; ++i) {
      int c;
      do {
        c = static_cast<int>(rng() % NUM_CARDS);
      } while (m & card_bit(c));
      m |= card_bit(c);
      b5[i] = c;
    }
    accumulate_board(b5, h, v, win.data(), tie.data(), compat.data());
  }
  std::vector<double> cw(169, 0), cn(169, 0), cm(169, 0);
  double csall[NUM_CARDS] = {0}, total = 0;
  for (size_t i = 0; i < v.combos.size(); ++i) {
    total += v.w[i];
    csall[COMBOS[v.combos[i]].hi] += v.w[i];
    csall[COMBOS[v.combos[i]].lo] += v.w[i];
  }
  double num = 0, den = 0;
  for (size_t i = 0; i < h.combos.size(); ++i) {
    int hc = h.combos[i];
    int k = preflop_class(hc);
    double mu = total - csall[COMBOS[hc].hi] - csall[COMBOS[hc].lo] + v.by_combo[hc];
    cm[k] += h.w[i] * mu;
    cw[k] += h.w[i] * (win[i] + 0.5 * tie[i]);
    cn[k] += h.w[i] * compat[i];
    num += h.w[i] * (win[i] + 0.5 * tie[i]);
    den += h.w[i] * compat[i];
  }
  for (int k = 0; k < 169; ++k) {
    res.eq[k] = cn[k] > 0 ? static_cast<float>(cw[k] / cn[k]) : 0.0f;
    res.matchups[k] = static_cast<float>(cm[k]);
  }
  res.total = den > 0 ? num / den : 0;
  return res;
}

}  // namespace pio
