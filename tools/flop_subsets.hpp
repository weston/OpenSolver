// Flop subsets for preflop trees (#486): which flops, with which weights, stand in for all of them.
//
// At a preflop exit the solver deals the subset's flops, each standing for its suit class, and what a
// hand h is worth against the opponent's reach R is built from
//     sum_o R[o] * sum_v w_v * val_v(h, o)        (and N(h, o) = sum_v w_v [v fits h and o] to normalise).
// All-in, val_v is the equity on flop v, and with every flop class at its natural weight the sum is the
// exact preflop equity. So a subset is judged by tables over ordered pairs of preflop classes (a, b):
//     E_v[a][b] = sum over the combos of a and b that fit v (and each other) of a's equity against b on v.
// sum_v w_v E_v should equal what all flops give. E_v[a][b] + E_v[b][a] counts the fitting combo pairs,
// so matching both orders matches N (card removal) too.
//
// Equity says who wins by the river, not how a hand plays, so the same table is taken on each street:
// who is ahead on the flop itself, on the turn, and at the river (equity).
//
// The error that is minimised is the one a solve sees: the squared error of a hand's value against an
// opponent range, averaged over the streets, the hands and the ranges. A range is a run of hand classes
// in order of strength (their exact preflop equity against all hands), every run equally likely: one
// class, the best x %, the worst x %, a band in the middle, or all hands. With d[a][b] the error of the
// weighted table at (a, b), b in that order, and P_k = d[a][1] + ... + d[a][k] (P_0 = 0), a run from
// i + 1 to j is off by P_j - P_i, and over all runs
//     mean (P_j - P_i)^2  =  2 / 169 * sum_k (P_k - mean P)^2,      k = 0 .. 169
// so a hand's row is its 170 centred partial sums. Errors of one sign add up along a run, which is the
// point: a subset that is a little kind to weak hands in every matchup is far off for "the top 40 %
// against the rest", and real ranges are that (first objective tried: every class in or out of the
// range independently, where such a tilt hides; see bench/where-the-time-goes.md). Nothing in it is a
// chosen weight.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <istream>
#include <numeric>
#include <sstream>
#include <string>
#include <vector>

#include "../src/cards.hpp"
#include "../src/equity.hpp"
#include "../src/eval.hpp"
#include "../src/pfh2h.hpp"

namespace pio::flops {

constexpr int NCLS = 169;           // preflop classes, preflop_class() order
constexpr int NFEAT = NCLS * NCLS;  // ordered class pairs
constexpr int NROW = NCLS * (NCLS + 1);  // a flop's row per street: per class, 170 partial sums over the opponents
constexpr int STREETS = 3;          // 0: the flop, 1: the turn, 2: the river
// Boards a flop has on each street that miss two given hands: itself, 45 turns, C(45, 2) turn-river pairs.
constexpr int BOARDS_PER_PAIR[STREETS] = {1, 45, 990};
constexpr double FLOPS_PER_PAIR = 17296;                // flops that miss two given hands: C(48, 3)
constexpr double FIT_SHARE = FLOPS_PER_PAIR / 22100.0;  // ... as a share of all C(52, 3)
constexpr double OPPONENTS = 1225;                      // hands that miss a given hand: C(50, 2)

struct FlopClass {
  std::array<uint8_t, 3> cards;  // the representative, highest card first
  int count;                     // flops in the class (4, 12 or 24)
};

inline std::string flop_str(const std::array<uint8_t, 3>& f) {
  return card_str(f[0]) + card_str(f[1]) + card_str(f[2]);
}

// The representative of a flop's suit class: the image under the 24 suit permutations whose cards,
// sorted high to low, compare greatest (AsKsQs, AsKsQh, AsKhQd, ...).
inline std::array<uint8_t, 3> canonical(const std::array<uint8_t, 3>& f) {
  std::array<uint8_t, 3> best{};
  for (int p = 0; p < NUM_SUIT_PERMS; ++p) {
    std::array<uint8_t, 3> g{PERM_CARD[p][f[0]], PERM_CARD[p][f[1]], PERM_CARD[p][f[2]]};
    std::sort(g.begin(), g.end(), std::greater<uint8_t>());
    if (p == 0 || g > best) best = g;
  }
  return best;
}

// The 1,755 classes, highest representative first.
inline std::vector<FlopClass> flop_classes() {
  std::vector<int> count(52 * 52 * 52, 0);
  for (int a = 0; a < 52; ++a)
    for (int b = 0; b < a; ++b)
      for (int c = 0; c < b; ++c) {
        auto r = canonical({static_cast<uint8_t>(a), static_cast<uint8_t>(b), static_cast<uint8_t>(c)});
        ++count[(r[0] * 52 + r[1]) * 52 + r[2]];
      }
  std::vector<FlopClass> out;
  for (int k = 52 * 52 * 52 - 1; k >= 0; --k)
    if (count[k])
      out.push_back({{static_cast<uint8_t>(k / (52 * 52)), static_cast<uint8_t>(k / 52 % 52), static_cast<uint8_t>(k % 52)},
                     count[k]});
  return out;
}

// Combos in each preflop class (6, 4 or 12).
inline std::array<int, NCLS> class_combos() {
  std::array<int, NCLS> n{};
  for (int h = 0; h < NUM_COMBOS; ++h) ++n[preflop_class(h)];
  return n;
}
// Disjoint combo pairs of each ordered pair of classes.
inline std::vector<double> class_pairs() {
  std::vector<double> pairs(NFEAT, 0);
  for (int h = 0; h < NUM_COMBOS; ++h)
    for (int o = 0; o < NUM_COMBOS; ++o)
      if (!(combo_mask(h) & combo_mask(o))) pairs[static_cast<size_t>(preflop_class(h)) * NCLS + preflop_class(o)] += 1;
  return pairs;
}

// A flop's table on one street. E2[a * 169 + b] = sum over the combos ha of a and hb of b that fit the
// flop and each other, and over the street's boards that miss both (see BOARDS_PER_PAIR), of 2 if ha is
// ahead on that board, 1 if they are level. On the river that is all-in equity, E2 / 1980.
inline void flop_table(const std::array<uint8_t, 3>& flop, int street, std::vector<uint32_t>& E2) {
  E2.assign(NFEAT, 0);
  const uint64_t fm = card_bit(flop[0]) | card_bit(flop[1]) | card_bit(flop[2]);
  std::array<uint8_t, NUM_COMBOS> cls;
  for (int h = 0; h < NUM_COMBOS; ++h) cls[h] = static_cast<uint8_t>(preflop_class(h));
  // the classes that hold a card of each rank: where a card's own counts can be non-zero
  std::array<std::vector<uint8_t>, 13> with_rank;
  for (int k = 0; k < NCLS; ++k) {
    const int r1 = 12 - k / 13, r2 = 12 - k % 13;
    with_rank[r1].push_back(static_cast<uint8_t>(k));
    if (r2 != r1) with_rank[r2].push_back(static_cast<uint8_t>(k));
  }
  std::vector<int64_t> acc(NFEAT, 0);
  std::vector<std::pair<uint32_t, uint16_t>> hs;
  std::vector<int32_t> cnt(NCLS), gcnt(NCLS), cc(52 * NCLS), gcc(52 * NCLS);
  // Hands in rising strength on one board. cnt / cc: the weaker hands seen, per class, in all and per
  // card held; a hand beats the weaker hands that share no card with it. The same within a group of ties.
  auto board = [&](uint64_t bm) {
    hs.clear();
    for (int h = 0; h < NUM_COMBOS; ++h) {
      const uint64_t hm = combo_mask(h);
      if (hm & bm) continue;
      hs.push_back({evaluate_mask(bm | hm), static_cast<uint16_t>(h)});
    }
    std::sort(hs.begin(), hs.end());
    std::fill(cnt.begin(), cnt.end(), 0);
    std::fill(cc.begin(), cc.end(), 0);
    for (size_t i = 0; i < hs.size();) {
      size_t j = i;
      while (j < hs.size() && hs[j].first == hs[i].first) ++j;
      const bool ties = j - i > 1;
      if (ties) {
        for (size_t k = i; k < j; ++k) {
          const int h = hs[k].second, c1 = COMBOS[h].hi, c2 = COMBOS[h].lo, a = cls[h];
          ++gcnt[a];
          ++gcc[c1 * NCLS + a];
          ++gcc[c2 * NCLS + a];
        }
      }
      for (size_t k = i; k < j; ++k) {
        const int h = hs[k].second, c1 = COMBOS[h].hi, c2 = COMBOS[h].lo, a = cls[h];
        int64_t* row = acc.data() + static_cast<size_t>(a) * NCLS;
        if (ties) {
          for (int b = 0; b < NCLS; ++b) row[b] += 2 * cnt[b] + gcnt[b];
          for (int c : {c1, c2})
            for (uint8_t b : with_rank[card_rank(c)]) row[b] -= 2 * cc[c * NCLS + b] + gcc[c * NCLS + b];
          row[a] += 1;  // the hand itself: counted once in gcnt and once under each of its cards
        } else {
          for (int b = 0; b < NCLS; ++b) row[b] += 2 * cnt[b];
          for (int c : {c1, c2})
            for (uint8_t b : with_rank[card_rank(c)]) row[b] -= 2 * cc[c * NCLS + b];
        }
      }
      for (size_t k = i; k < j; ++k) {
        const int h = hs[k].second, c1 = COMBOS[h].hi, c2 = COMBOS[h].lo, a = cls[h];
        ++cnt[a];
        ++cc[c1 * NCLS + a];
        ++cc[c2 * NCLS + a];
        if (ties) {
          --gcnt[a];
          --gcc[c1 * NCLS + a];
          --gcc[c2 * NCLS + a];
        }
      }
      i = j;
    }
  };
  if (street == 0) board(fm);
  for (int t = 0; t < 52 && street >= 1; ++t) {
    if (fm & card_bit(t)) continue;
    if (street == 1) board(fm | card_bit(t));
    for (int r = 0; r < t && street == 2; ++r)
      if (!(fm & card_bit(r))) board(fm | card_bit(t) | card_bit(r));
  }
  for (int k = 0; k < NFEAT; ++k) E2[k] = static_cast<uint32_t>(acc[k]);
}

// The same table by comparing every pair of hands on every board: slow, for checking flop_table.
inline void flop_table_direct(const std::array<uint8_t, 3>& flop, int street, std::vector<uint32_t>& E2) {
  E2.assign(NFEAT, 0);
  const uint64_t fm = card_bit(flop[0]) | card_bit(flop[1]) | card_bit(flop[2]);
  std::vector<std::pair<uint32_t, uint16_t>> hs;
  for (int t = 0; t < 52; ++t)
    for (int r = 0; r <= t; ++r) {
      // street 0: one board (t = r = 0 stands for it); 1: t alone (r = t); 2: t and r < t
      if (street == 0 ? t + r > 0 : street == 1 ? r != t : r == t) continue;
      const uint64_t bm = street == 0 ? fm : fm | card_bit(t) | card_bit(r);
      if (__builtin_popcountll(bm) != 3 + street) continue;
      hs.clear();
      for (int h = 0; h < NUM_COMBOS; ++h)
        if (!(combo_mask(h) & bm)) hs.push_back({evaluate_mask(bm | combo_mask(h)), static_cast<uint16_t>(h)});
      for (const auto& x : hs)
        for (const auto& y : hs) {
          if (combo_mask(x.second) & combo_mask(y.second)) continue;
          if (x.first >= y.first)
            E2[static_cast<size_t>(preflop_class(x.second)) * NCLS + preflop_class(y.second)] += x.first > y.first ? 2 : 1;
        }
    }
}

// What all flops together give on each street: ahead[s][a*169+b] = the sum over the disjoint combo pairs
// of (a, b) of how often a is ahead of b (ties half) on a board of street s. On the river: preflop equity.
struct Deck {
  std::vector<double> pairs;           // class_pairs()
  std::array<int, NCLS> combos{};      // class_combos()
  std::vector<double> ahead[STREETS];  // NFEAT each
  std::array<int, NCLS> order{};       // strength_order(): the classes, strongest first
};
// The classes by exact preflop equity against all hands, strongest first (ties by class index).
// eq = preflop_equity_sums(), pairs = class_pairs().
inline std::array<int, NCLS> strength_order(const std::vector<double>& eq, const std::vector<double>& pairs) {
  std::array<double, NCLS> share{};
  for (int a = 0; a < NCLS; ++a) {
    double num = 0, den = 0;
    for (int b = 0; b < NCLS; ++b) num += eq[a * NCLS + b], den += pairs[a * NCLS + b];
    share[a] = num / den;
  }
  std::array<int, NCLS> order{};
  std::iota(order.begin(), order.end(), 0);
  std::stable_sort(order.begin(), order.end(), [&](int x, int y) { return share[x] > share[y]; });
  return order;
}
// sum[k] = sum over all flop classes of count * E2[k] on that street.
inline std::vector<double> ahead_from_sum(const std::vector<uint64_t>& sum, int street) {
  std::vector<double> ahead(NFEAT);
  for (int k = 0; k < NFEAT; ++k) ahead[k] = static_cast<double>(sum[k]) / (2.0 * BOARDS_PER_PAIR[street] * FLOPS_PER_PAIR);
  return ahead;
}
// The exact preflop equity sums from the hand-vs-hand table (made independently, over the C(48, 5)
// boards of each pair), to check the river's `ahead` against.
inline std::vector<double> preflop_equity_sums() {
  std::vector<double> eq(NFEAT, 0);
  const PfTable& T = pf_table();
  for (int h = 0; h < NUM_COMBOS; ++h)
    for (int o = 0; o < NUM_COMBOS; ++o) {
      if (combo_mask(h) & combo_mask(o)) continue;
      const size_t i = static_cast<size_t>(h) * NUM_COMBOS + o;
      eq[static_cast<size_t>(preflop_class(h)) * NCLS + preflop_class(o)] += (2.0 * T.win[i] + T.tie[i]) / (2.0 * PF_BOARDS_PER_PAIR);
    }
  return eq;
}

// A flop's row of the least-squares problem on one street (NROW floats). With weights that sum to 1,
// sum_v w_v E_v[a][b] / FIT_SHARE estimates ahead[a][b]; d[a][b] is that estimate's error when the flop
// stands alone. Per hand of class a (there are combos[a], each facing OPPONENTS hands) the row holds the
// centred partial sums of d[a][.] over the opponents in deck.order, in units of equity against all hands
// (a run of opponents counts for its share of them) and scaled so that |sum_v w_v row_v|^2 is the mean
// over the 1,326 hands and over the runs of the squared error.
inline void feature_row(const std::vector<uint32_t>& E2, int street, const Deck& deck, float* row) {
  for (int a = 0; a < NCLS; ++a) {
    // a class's d is shared by its combos[a] hands: per hand d / combos, as equity d / (combos * OPPONENTS),
    // and combos hands of 1,326 carry it: sqrt(combos / 1326) / (combos * OPPONENTS); 2 / 169 as above
    const double scale = std::sqrt(2.0 / NCLS) / (std::sqrt(deck.combos[a] * 1326.0) * OPPONENTS);
    double part[NCLS + 1];
    double sum = 0, mean = 0;
    part[0] = 0;
    for (int k = 0; k < NCLS; ++k) {
      const int i = a * NCLS + deck.order[k];
      sum += E2[i] / (2.0 * BOARDS_PER_PAIR[street]) / FIT_SHARE - deck.ahead[street][i];
      part[k + 1] = sum;
      mean += sum;
    }
    mean /= NCLS + 1;
    for (int k = 0; k <= NCLS; ++k) row[a * (NCLS + 1) + k] = static_cast<float>((part[k] - mean) * scale);
  }
}

// What a subset gives on one street, to compare with the deck (and with the engine): the weighted table
// sum_v w_v E2_v / (2 * boards), "ahead" sums over the fitting pairs, the weights normalised to sum 1.
inline std::vector<double> weighted_table(const std::vector<FlopClass>& fc, const std::vector<int>& S, const std::vector<double>& w,
                                          int street) {
  const double sum = std::accumulate(w.begin(), w.end(), 0.0);
  std::vector<double> out(NFEAT, 0.0);
  std::vector<uint32_t> E2;
  for (size_t i = 0; i < S.size(); ++i) {
    flop_table(fc[S[i]].cards, street, E2);
    for (int k = 0; k < NFEAT; ++k) out[k] += w[i] / sum * E2[k] / (2.0 * BOARDS_PER_PAIR[street]);
  }
  return out;
}
// A class's share of the pot against all hands on a street, the way the solver gets it at a preflop
// exit: its weighted wins over its weighted fitting pairs. With every flop: ahead / pairs.
inline std::array<double, NCLS> against_all(const std::vector<double>& e) {
  std::array<double, NCLS> out{};
  for (int a = 0; a < NCLS; ++a) {
    double num = 0, den = 0;
    for (int b = 0; b < NCLS; ++b) {
      num += e[a * NCLS + b];
      den += e[a * NCLS + b] + e[b * NCLS + a];
    }
    out[a] = den > 0 ? num / den : 0;
  }
  return out;
}
// Root mean square over the 1,326 hands of the difference between two such lists.
inline double rms_hands(const std::array<double, NCLS>& x, const std::array<double, NCLS>& y, const std::array<int, NCLS>& combos) {
  double s = 0;
  for (int a = 0; a < NCLS; ++a) s += combos[a] * (x[a] - y[a]) * (x[a] - y[a]);
  return std::sqrt(s / NUM_COMBOS);
}

// The index of a flop's class in flop_classes(), from text like "Ks5h2s"; -1 if it is not a flop.
inline int class_index(const std::vector<FlopClass>& fc, const std::string& text) {
  std::vector<int> cs;
  if (!parse_cards(text, cs) || cs.size() != 3 || cs[0] == cs[1] || cs[0] == cs[2] || cs[1] == cs[2]) return -1;
  const auto r = canonical({static_cast<uint8_t>(cs[0]), static_cast<uint8_t>(cs[1]), static_cast<uint8_t>(cs[2])});
  for (size_t i = 0; i < fc.size(); ++i)
    if (fc[i].cards == r) return static_cast<int>(i);
  return -1;
}

// A flop file: one "Ks5h2s:15.76" per line (weight 1 without the colon), # starts a comment. Flops with
// weight 0 are left out. False (and `bad` set to the text) on a line that is not a flop.
inline bool parse_flop_file(std::istream& in, const std::vector<FlopClass>& fc, std::vector<int>& S, std::vector<double>& w,
                            std::string& bad) {
  std::string line;
  while (std::getline(in, line)) {
    line = line.substr(0, line.find('#'));
    const size_t colon = line.find(':');
    std::istringstream ls(line.substr(0, colon));
    std::string b;
    if (!(ls >> b)) continue;
    const int k = class_index(fc, b);
    const double weight = colon == std::string::npos ? 1.0 : std::atof(line.c_str() + colon + 1);
    if (k < 0 || !(weight >= 0)) {
      bad = line;
      return false;
    }
    if (weight > 0) S.push_back(k), w.push_back(weight);
  }
  return true;
}

// ---- choosing the flops ----------------------------------------------------------------------------
// G = the Gram matrix of the rows summed over the streets (n x n). The error of weights w (>= 0, sum 1)
// is w' G w.

struct Subset {
  std::vector<int> flops;      // indices into the classes
  std::vector<double> weight;  // same length, > 0, sum 1
  double err2 = 0;             // w' G w
};

// Least squares on the simplex over a fixed set of flops: w >= 0, sum 1, minimising w' G w. Pairwise
// exact line searches (move weight from one flop to another), always along the pair with the largest
// gradient gap, until no pair improves by more than rounding.
inline double solve_weights(const std::vector<double>& G, int n, const std::vector<int>& S, std::vector<double>& w) {
  const int m = static_cast<int>(S.size());
  if (static_cast<int>(w.size()) != m) w.assign(m, 1.0 / m);
  std::vector<double> g(m, 0.0);  // (G w) restricted to S
  auto at = [&](int i, int j) { return G[static_cast<size_t>(S[i]) * n + S[j]]; };
  for (int i = 0; i < m; ++i)
    for (int j = 0; j < m; ++j) g[i] += at(i, j) * w[j];
  auto value = [&] {
    double v = 0;
    for (int i = 0; i < m; ++i) v += w[i] * g[i];
    return v;
  };
  const int max_steps = 1000 * m + 1000;
  for (int step = 0; step < max_steps && m > 1; ++step) {
    // take from the flop with the largest gradient that has weight, give to the one with the smallest
    int lo = 0, hi = -1;
    for (int i = 0; i < m; ++i) {
      if (g[i] < g[lo]) lo = i;
      if (w[i] > 0 && (hi < 0 || g[i] > g[hi])) hi = i;
    }
    if (hi < 0 || hi == lo) break;
    const double gap = g[hi] - g[lo];
    const double curv = at(hi, hi) + at(lo, lo) - 2 * at(hi, lo);
    if (!(gap > 0) || !(curv > 0)) break;
    const double t = std::min(w[hi], gap / curv);
    if (!(t * gap > 1e-15 * std::abs(value()))) break;  // below rounding: converged
    w[hi] -= t;
    w[lo] += t;
    for (int i = 0; i < m; ++i) g[i] += t * (at(i, lo) - at(i, hi));
  }
  return value();
}

// A given set of flops with its weights solved.
inline Subset fit(const std::vector<double>& G, int n, const std::vector<int>& S) {
  Subset s;
  s.flops = S;
  s.err2 = solve_weights(G, n, s.flops, s.weight);
  return s;
}

// w' G w for a subset with the weights it came with (normalised to sum 1).
inline double error_of(const std::vector<double>& G, int n, const std::vector<int>& S, const std::vector<double>& w) {
  const double sum = std::accumulate(w.begin(), w.end(), 0.0);
  double v = 0;
  for (size_t i = 0; i < S.size(); ++i)
    for (size_t j = 0; j < S.size(); ++j) v += w[i] * w[j] * G[static_cast<size_t>(S[i]) * n + S[j]];
  return std::max(0.0, v / (sum * sum));
}

// Choose `size` flops. Greedy: add the flop that lowers the error most when mixed in at its best
// share, re-solve the weights, repeat. Then swaps: replace a flop by a better one while any swap helps.
// `progress(k, err2)` is called after each addition (k = flops so far) and each accepted swap (k < 0).
inline Subset choose(const std::vector<double>& G, int n, int size, const std::function<void(int, double)>& progress = {},
                     bool swaps = true) {
  Subset s;
  std::vector<char> in(n, 0);
  std::vector<double> gw(n, 0.0);  // G w over all flops
  auto products = [&](const std::vector<int>& S, const std::vector<double>& w, std::vector<double>& out) {
    std::fill(out.begin(), out.end(), 0.0);
    for (size_t i = 0; i < S.size(); ++i) {
      const double* row = G.data() + static_cast<size_t>(S[i]) * n;
      for (int f = 0; f < n; ++f) out[f] += w[i] * row[f];
    }
  };
  // The error after mixing flop f into weights with error v and products gwv: the least over t in
  // [0, 1] of (1-t)^2 v + 2 t (1-t) gwv[f] + t^2 G[f][f].
  auto mixed = [&](double v, const double* gwv, int f) {
    const double gff = G[static_cast<size_t>(f) * n + f];
    const double den = v - 2 * gwv[f] + gff;
    double t = den > 0 ? (v - gwv[f]) / den : 1.0;
    t = std::clamp(t, 0.0, 1.0);
    return (1 - t) * (1 - t) * v + 2 * t * (1 - t) * gwv[f] + t * t * gff;
  };
  while (static_cast<int>(s.flops.size()) < size) {
    int best = -1;
    double bv = 0;
    for (int f = 0; f < n; ++f) {
      if (in[f]) continue;
      const double v = s.flops.empty() ? G[static_cast<size_t>(f) * n + f] : mixed(s.err2, gw.data(), f);
      if (best < 0 || v < bv) best = f, bv = v;
    }
    in[best] = 1;
    for (double& x : s.weight) x *= 1.0 - 1.0 / (s.flops.size() + 1);
    s.flops.push_back(best);
    s.weight.push_back(1.0 / s.flops.size());
    s.err2 = solve_weights(G, n, s.flops, s.weight);
    products(s.flops, s.weight, gw);
    if (progress) progress(static_cast<int>(s.flops.size()), s.err2);
  }
  // Swaps. For each flop of the subset: drop it and re-solve the weights of the rest, then score every
  // outside flop mixed in at its best share. Take the best swap overall (weights re-solved) while one
  // lowers the error; each pass lowers it, so this ends.
  std::vector<double> gw2(n);
  for (int pass = 0; swaps && s.flops.size() > 1; ++pass) {
    int bi = -1, bf = -1;
    double bv = s.err2;
    for (size_t i = 0; i < s.flops.size(); ++i) {
      std::vector<int> rest;
      for (size_t j = 0; j < s.flops.size(); ++j)
        if (j != i) rest.push_back(s.flops[j]);
      std::vector<double> rw;
      const double v2 = solve_weights(G, n, rest, rw);
      products(rest, rw, gw2);
      for (int f = 0; f < n; ++f) {
        if (in[f]) continue;
        const double v = mixed(v2, gw2.data(), f);
        if (v < bv) bv = v, bi = static_cast<int>(i), bf = f;
      }
    }
    if (bi < 0) break;
    Subset trial = s;
    trial.flops[bi] = bf;
    trial.weight.clear();
    trial.err2 = solve_weights(G, n, trial.flops, trial.weight);
    if (!(trial.err2 < s.err2)) break;
    in[s.flops[bi]] = 0;
    in[bf] = 1;
    s = trial;
    products(s.flops, s.weight, gw);
    if (progress) progress(-(pass + 1), s.err2);
  }
  // a flop whose weight the solver drove to zero is not part of the subset
  Subset out;
  for (size_t i = 0; i < s.flops.size(); ++i)
    if (s.weight[i] > 0) out.flops.push_back(s.flops[i]), out.weight.push_back(s.weight[i]);
  out.err2 = s.err2;
  return out;
}

}  // namespace pio::flops
