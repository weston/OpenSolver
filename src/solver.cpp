#include "solver.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>

#if PIO_SHOWDOWN_AVX512
#include <immintrin.h>
#endif

#include "parallel.hpp"
#include "pfh2h.hpp"

namespace pio {

namespace {

inline bool all_zero(const float* v, int n) {
  for (int i = 0; i < n; ++i)
    if (v[i] != 0.0f) return false;
  return true;
}

// Regret-update rows, one straight loop per (predictive, accumulate) combination. GCC unswitches
// these loop-invariant conditions by itself, but clang (macOS) does not, and its cost model then
// rejects vectorizing the predicated stores: on the Mac the update ran scalar (20% of an iteration
// on threebet_mono). Same expressions as before, so the floats are identical.
template <bool PR, bool ACC>
inline void regret_row(float* __restrict r, float* __restrict s, float* __restrict pr, const float* __restrict c,
                       const float* __restrict out, const float* __restrict rp, const float* __restrict sa, int Hp,
                       float pm, float nm, float sm) {
  for (int h = 0; h < Hp; ++h) {
    float x = r[h];
    x *= x > 0.0f ? pm : nm;
    const float inst = c[h] - out[h];
    r[h] = x + inst;
    if constexpr (PR) pr[h] = inst;
    if constexpr (ACC) s[h] = s[h] * sm + rp[h] * sa[h];
  }
}

// The 16-bit storage form: also the maxima for the quantisation scales (abs_bits / nonneg_bits).
template <bool PR, bool ACC>
inline void regret_row_max(float* __restrict r, float* __restrict s, float* __restrict pr, const float* __restrict c,
                           const float* __restrict out, const float* __restrict rp, const float* __restrict sa, int Hp,
                           float pm, float nm, float sm, uint32_t& mr_out, uint32_t& ms_out) {
  uint32_t mr = mr_out, ms = ms_out;
  for (int h = 0; h < Hp; ++h) {
    float x = r[h];
    x *= x > 0.0f ? pm : nm;
    const float inst = c[h] - out[h];
    x = x + inst;
    r[h] = x;
    if constexpr (PR) pr[h] = inst;
    mr = std::max(mr, abs_bits(x));
    if constexpr (ACC) {
      float y = s[h] * sm + rp[h] * sa[h];
      s[h] = y;
      ms = std::max(ms, nonneg_bits(y));
    }
  }
  mr_out = mr;
  ms_out = ms;
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

#if PIO_SHOWDOWN_AVX512
// AVX-512 form of the showdown kernel (#89): same values as showdown_board_scalar, bit for bit
// (tests/test_solver.cpp compares the two on every board of several trees). It needs AVX-512F, BW
// and VBMI (Ice Lake / Zen 4 and later); without VBMI the entry fields would have to be gathered,
// and gathers make it slower than scalar (measurements in #89).
//
// Why it pays at all when the loops are this short:
//  - p's strength groups average ~6.5 hands, so one masked 16-lane step covers a whole group with no
//    scalar tail, and the group's card indices are reused between the "lt" and "le" queries;
//  - the 6-byte entries are unpacked with two contiguous masked loads and one vpermt2b, not gathers;
//  - cs[] (52 floats) lives in 4 zmm registers: lookups are vpermt2ps, and the opponent pass adds
//    into the registers under a lane mask (one add per touched lane, in the same order as scalar);
//  - the final pass is computed 16 hands at a time; only its `out[idx] +=` stays scalar, because a
//    gather + scatter there costs more than it saves.
// The opponent pass itself stays sequential: cum and cs[] are running sums, and reassociating them
// would change the results.
static inline __mmask16 sd_tail_mask(int n) { return static_cast<__mmask16>(n >= 16 ? 0xffff : (1u << n) - 1); }

// Dword k = 4 bytes of entry k starting at byte `boff` (0: idx | c1 | c2, 4: same | ...), for the
// first popcount(m) entries at e (m is a prefix mask, <= 16 entries = 96 bytes). Bytes past them are
// not read (masked loads); lanes outside m hold garbage and every caller masks them.
static inline __m512i sd_words(const SdEntry* e, __mmask16 m, int boff) {
  struct Tbl {
    alignas(64) uint8_t b[2][64];
  };
  static const Tbl tbl = [] {
    Tbl t{};
    for (int o = 0; o < 2; ++o)
      for (int j = 0; j < 64; ++j) t.b[o][j] = static_cast<uint8_t>(6 * (j / 4) + j % 4 + 4 * o);
    return t;
  }();
  static_assert(sizeof(SdEntry) == 6);
  const int nb = 6 * std::popcount(static_cast<unsigned>(m));
  const __mmask64 m0 = nb >= 64 ? ~0ull : (1ull << nb) - 1;
  const __mmask64 m1 = nb > 64 ? (1ull << (nb - 64)) - 1 : 0;
  const char* p = reinterpret_cast<const char*>(e);
  const __m512i lo = _mm512_maskz_loadu_epi8(m0, p), hi = _mm512_maskz_loadu_epi8(m1, p + 64);
  return _mm512_permutex2var_epi8(lo, _mm512_load_si512(tbl.b[boff ? 1 : 0]), hi);
}

// cs[0..63] in 4 registers (cards 52..63 stay 0).
struct CsRegs {
  __m512 r[4] = {_mm512_setzero_ps(), _mm512_setzero_ps(), _mm512_setzero_ps(), _mm512_setzero_ps()};
  __m512 operator()(__m512i c) const {
    const __m512 lo = _mm512_permutex2var_ps(r[0], c, r[1]);
    const __m512 hi = _mm512_permutex2var_ps(r[2], c, r[3]);
    return _mm512_mask_blend_ps(_mm512_test_epi32_mask(c, _mm512_set1_epi32(32)), lo, hi);
  }
  // cs[c1] += x; cs[c2] += x (c1 != c2).
  void add2(int c1, int c2, float x) {
    const uint64_t m = (1ull << c1) | (1ull << c2);
    const __m512 vx = _mm512_set1_ps(x);
    for (int k = 0; k < 4; ++k) r[k] = _mm512_mask_add_ps(r[k], static_cast<__mmask16>(m >> (16 * k)), r[k], vx);
  }
};

void showdown_board_avx512(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                           float* compat_out, const SdFold* fold) {
  const int o = 1 - p;
  const SdEntry* pe = sb.ent[p].data();
  const SdEntry* oe = sb.ent[o].data();
  const int np = static_cast<int>(sb.ent[p].size());
  const auto* gr = sb.groups[p].data();
  const int ng = static_cast<int>(sb.groups[p].size());

  ArenaScope sc;
  float* ltv = sc.a.alloc(static_cast<size_t>(np) * 2);
  float* lev = ltv + np;
  CsRegs cs, csf;  // csf / cumf: the opponent-fold mass (fold != nullptr)
  float cum = 0, cumf = 0;
  const float* xf = fold ? fold->xf : nullptr;
  const __m512i b8 = _mm512_set1_epi32(0xff);
  int i0 = 0, j0 = 0;
  for (int g = 0; g < ng; ++g) {
    const int i1 = gr[g].first, j1 = gr[g].second;
    __m512i c1 = _mm512_setzero_si512(), c2 = c1;
    if (i0 < i1) {
      const __m512 vc = _mm512_set1_ps(cum);
      for (int i = i0; i < i1; i += 16) {
        const __mmask16 m = sd_tail_mask(i1 - i);
        const __m512i w = sd_words(pe + i, m, 0);
        c1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
        c2 = _mm512_srli_epi32(w, 24);
        _mm512_mask_storeu_ps(ltv + i, m, _mm512_sub_ps(_mm512_sub_ps(vc, cs(c1)), cs(c2)));
      }
    }
    if (fold) {
      for (int j = j0; j < j1; ++j) {
        const SdEntry q = oe[j];
        const float r = ro[q.idx], f = xf[q.idx];
        cum += r;
        cs.add2(q.c1, q.c2, r);
        cumf += f;
        csf.add2(q.c1, q.c2, f);
      }
    } else {
      for (int j = j0; j < j1; ++j) {
        const SdEntry q = oe[j];
        const float r = ro[q.idx];
        cum += r;
        cs.add2(q.c1, q.c2, r);
      }
    }
    if (i0 < i1) {
      const __m512 vc = _mm512_set1_ps(cum);
      if (i1 - i0 <= 16) {  // the common case: c1/c2 still hold this group's cards
        _mm512_mask_storeu_ps(lev + i0, sd_tail_mask(i1 - i0), _mm512_sub_ps(_mm512_sub_ps(vc, cs(c1)), cs(c2)));
      } else {
        for (int i = i0; i < i1; i += 16) {
          const __mmask16 m = sd_tail_mask(i1 - i);
          const __m512i w = sd_words(pe + i, m, 0);
          c1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
          c2 = _mm512_srli_epi32(w, 24);
          _mm512_mask_storeu_ps(lev + i, m, _mm512_sub_ps(_mm512_sub_ps(vc, cs(c1)), cs(c2)));
        }
      }
    }
    i0 = i1;
    j0 = j1;
  }
  if (cum == 0.0f && cumf == 0.0f) return;
  // The scalar line `out += a*ltv + b*le + L*compat` is contracted by the compiler (GCC's default
  // -ffp-contract=fast for C++) to fma(L, compat, fma(a, ltv, b*le)); this is the same sequence.
  const __m512 vc = _mm512_set1_ps(cum), va = _mm512_set1_ps(W - T), vb = _mm512_set1_ps(T - L),
               vl = _mm512_set1_ps(L), vcf = _mm512_set1_ps(cumf);
  for (int i = 0; i < np; i += 16) {
    const __mmask16 m = sd_tail_mask(np - i);
    const __m512i w = sd_words(pe + i, m, 0);
    const __m512i k1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
    const __m512i k2 = _mm512_srli_epi32(w, 24);
    const __m512i same = _mm512_srai_epi32(_mm512_slli_epi32(sd_words(pe + i, m, 4), 16), 16);  // int16
    const __m512 sr =
        _mm512_mask_i32gather_ps(_mm512_setzero_ps(), _mm512_mask_cmpge_epi32_mask(m, same, _mm512_setzero_si512()),
                                 same, ro, 4);
    const __m512 le = _mm512_add_ps(_mm512_maskz_loadu_ps(m, lev + i), sr);
    const __m512 compat = _mm512_add_ps(_mm512_sub_ps(_mm512_sub_ps(vc, cs(k1)), cs(k2)), sr);
    const __m512 v =
        _mm512_fmadd_ps(vl, compat, _mm512_fmadd_ps(va, _mm512_maskz_loadu_ps(m, ltv + i), _mm512_mul_ps(vb, le)));
    _mm512_mask_storeu_ps(ltv + i, m, v);  // ltv[i..i+15] is consumed: reuse it for the values
    if (compat_out) _mm512_mask_storeu_ps(lev + i, m, compat);  // and lev[] for the compatible mass
    if (fold) {  // lev[] holds the fold's compatible mass instead (compat_out is not used with fold)
      const __mmask16 sm = _mm512_mask_cmpge_epi32_mask(m, same, _mm512_setzero_si512());
      const __m512 fs = _mm512_mask_i32gather_ps(_mm512_setzero_ps(), sm, same, xf, 4);
      _mm512_mask_storeu_ps(lev + i, m, _mm512_add_ps(_mm512_sub_ps(_mm512_sub_ps(vcf, csf(k1)), csf(k2)), fs));
    }
  }
  if (fold) {
    const float pay = fold->pay;
    for (int i = 0; i < np; ++i) {
      const int idx = pe[i].idx;
      out[idx] += ltv[i];
      out[idx] = std::fma(pay, lev[i], out[idx]);
    }
    return;
  }
  if (compat_out) {
    for (int i = 0; i < np; ++i) {
      const int idx = pe[i].idx;
      out[idx] += ltv[i];
      compat_out[idx] = lev[i];
    }
  } else {
    for (int i = 0; i < np; ++i) out[pe[i].idx] += ltv[i];
  }
}
#endif

void Solver::showdown_board(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                            float* compat, const SdFold* fold) const {
#if PIO_SHOWDOWN_AVX512
  showdown_board_avx512(sb, W, L, T, p, ro, out, compat, fold);
#else
  showdown_board_scalar(sb, W, L, T, p, ro, out, compat, fold);
#endif
}

void showdown_board_scalar(const ShowdownBoard& sb, float W, float L, float T, int p, const float* ro, float* out,
                           float* compat_out, const SdFold* fold) {
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
  float cs[NUM_CARDS] = {0}, csf[NUM_CARDS] = {0};
  float cum = 0, cumf = 0;
  if (fold) {  // the opponent-fold mass: same entry order as the AVX-512 kernel's opponent pass
    const int no = static_cast<int>(sb.ent[o].size());
    for (int j = 0; j < no; ++j) {
      const SdEntry q = oe[j];
      const float f = fold->xf[q.idx];
      cumf += f;
      csf[q.c1] += f;
      csf[q.c2] += f;
    }
  }
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
      // About 55-60% of opponent reach is exactly zero at showdowns (measured on the bench
      // trees). Adding zero changes nothing, so skipping is exact (bit-identical) and breaks
      // the store-to-load chain through cs[] for those entries.
      if (r == 0.0f) continue;
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
  if (cum == 0.0f && cumf == 0.0f) return;
  // value = W*lt + L*(compat - le) + T*(le - lt) = (W - T)*lt + (T - L)*le + L*compat; the identical combo
  // (equal strength, inside "le") is subtracted twice by inclusion-exclusion and added back once.
  const float a = W - T, b = T - L;
  for (int i = 0; i + 1 < np; ++i) {
    const uint64_t w = sd_word(pe + i);
    const float sr = sd_w_same(w) >= 0 ? ro[sd_w_same(w)] : 0.0f;
    const float le = lev[i] + sr;
    const float compat = cum - cs[sd_w_c1(w)] - cs[sd_w_c2(w)] + sr;
    out[sd_w_idx(w)] += a * ltv[i] + b * le + L * compat;
    if (compat_out) compat_out[sd_w_idx(w)] = compat;
    if (fold) {
      const int s = sd_w_same(w);
      const float fs = s >= 0 ? fold->xf[s] : 0.0f;
      const float cf = cumf - csf[sd_w_c1(w)] - csf[sd_w_c2(w)] + fs;
      out[sd_w_idx(w)] = std::fma(fold->pay, cf, out[sd_w_idx(w)]);
    }
  }
  if (np > 0) {
    const SdEntry q = pe[np - 1];
    const float sr = q.same >= 0 ? ro[q.same] : 0.0f;
    const float le = lev[np - 1] + sr;
    const float compat = cum - cs[q.c1] - cs[q.c2] + sr;
    out[q.idx] += a * ltv[np - 1] + b * le + L * compat;
    if (compat_out) compat_out[q.idx] = compat;
    if (fold) {
      const float fs = q.same >= 0 ? fold->xf[q.same] : 0.0f;
      const float cf = cumf - csf[q.c1] - csf[q.c2] + fs;
      out[q.idx] = std::fma(fold->pay, cf, out[q.idx]);
    }
  }
}

template <class E>
void Solver::allin_table_value(const AllinRunouts& ar, const E* e2, float W, float L, int p, const float* ro,
                               float* out) const {
  const int H0 = g_.num_hands(0), H1 = g_.num_hands(1);
  const int Hp = g_.num_hands(p);
  const float half = 0.5f * (W - L) * ar.scale;
  const float twoV = 2.0f * static_cast<float>(ar.valid);
  ArenaScope sc;
  float* cm = sc.a.alloc(Hp);
  compat_mass(p, ro, cm);
  if (p == 0) {
    for (int h = 0; h < H0; ++h) {
      const E* row = e2 + static_cast<size_t>(h) * H1;
      float part[16] = {};
      int o = 0;
      for (; o + 16 <= H1; o += 16)
        for (int l = 0; l < 16; ++l) part[l] += ro[o + l] * static_cast<float>(row[o + l]);
      float d = 0.0f;
      for (; o < H1; ++o) d += ro[o] * static_cast<float>(row[o]);
      for (int l = 0; l < 16; ++l) d += part[l];
      out[h] = half * d + L * cm[h];
    }
  } else {
    float* acc = sc.a.alloc_zero(H1);
    for (int h = 0; h < H0; ++h) {
      const float r = ro[h];
      if (r == 0.0f) continue;  // most opponent hands have no reach at an all-in
      const E* row = e2 + static_cast<size_t>(h) * H1;
      for (int o = 0; o < H1; ++o) acc[o] += r * static_cast<float>(row[o]);
    }
    for (int j = 0; j < H1; ++j) out[j] = half * (twoV * cm[j] - acc[j]) + L * cm[j];
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
    // Always serial. The work here is Hp * Ho, bounded by NUM_COMBOS^2 (~1.76M FMAs), which
    // is below the crossover where a parallel_for dispatch pays for itself: measured on a
    // 20-core machine, parallelising this cost 42% on a preflop betting tree (2.99 s vs
    // 1.73 s for 2000 iterations) and nothing on a full preflop tree with flop subsets
    // (5.72 s vs 5.67 s). Outer parallelism over chance children is unaffected (#110).
    for (int h = 0; h < Hp; ++h) body(h);
    return;
  }
  if (allin_tables && !(ar.e2.empty() && ar.e2b.empty()) && !g_.payoff.icm.enabled) {
    // value(h) = scale * sum_o ro[o] * (W*wins + T*ties + L*losses) over the runouts. Without ICM a
    // tie pays the midpoint, so with e2 = 2*wins + ties and V = runouts per disjoint pair,
    // value(h) = (W - L) / (2V) * sum_o ro[o] * e2[h][o] + L * compat(h). For IP the table is read
    // transposed: 2*wins_IP + ties = 2V - e2 on compatible pairs.
    if (!ar.e2b.empty()) allin_table_value(ar, ar.e2b.data(), W, L, p, ro, out);
    else allin_table_value(ar, ar.e2.data(), W, L, p, ro, out);
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

bool Solver::call_and_fold(const GNode& sd, const GNode& fold, int p, const float* ro, float* sd_out,
                           float* fold_out) const {
  if (sd.kind != ANodeKind::Showdown || fold.kind != ANodeKind::Fold || sd.board_len != 5) return false;
  const ActionNode& an = g_.an(sd);
  const float W = static_cast<float>(g_.terminal_payoff(an, p, 1));
  const float L = static_cast<float>(g_.terminal_payoff(an, p, -1));
  const float T = static_cast<float>(g_.terminal_payoff(an, p, 0));
  const int Hp = g_.num_hands(p);
  std::memset(sd_out, 0, sizeof(float) * Hp);
  std::memset(fold_out, 0, sizeof(float) * Hp);
  showdown_board(g_.showdowns[sd.aux], W, L, T, p, ro, sd_out, fold_out);
  const ActionNode& fa = g_.an(fold);
  const float pay = static_cast<float>(g_.terminal_payoff(fa, p, p == fa.player ? -1 : 1));
  for (int h = 0; h < Hp; ++h) fold_out[h] *= pay;
  return true;
}

bool Solver::call_with_opp_fold(const GNode& sd, const GNode& fold, int p, const float* xc, const float* ro,
                                const float* sfold, float* xf, float* out) const {
  if (sd.kind != ANodeKind::Showdown || fold.kind != ANodeKind::Fold || sd.board_len != 5) return false;
  const ActionNode& an = g_.an(sd);
  const float W = static_cast<float>(g_.terminal_payoff(an, p, 1));
  const float L = static_cast<float>(g_.terminal_payoff(an, p, -1));
  const float T = static_cast<float>(g_.terminal_payoff(an, p, 0));
  const int Ho = g_.num_hands(1 - p);
  for (int k = 0; k < Ho; ++k) xf[k] = ro[k] * sfold[k];
  const SdFold f{xf, static_cast<float>(g_.terminal_payoff(g_.an(fold), p, 1))};
  std::memset(out, 0, sizeof(float) * g_.num_hands(p));
  showdown_board(g_.showdowns[sd.aux], W, L, T, p, xc, out, nullptr, &f);
  return true;
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
  // Predictive (PCFR+-style, #71): match on R + the last instantaneous regret as an
  // optimistic guess of the next one. Preds are transient solver state (zero until the
  // node's first update), so this reduces to plain regret matching at t=1.
  const float* P = (predictive && !g_.preds.empty()) ? g_.preds.data() + n.storage : nullptr;
#if PIO_SHOWDOWN_AVX512
  if (!P && A <= 16) {
    // One pass per 16 hands with every action's row in registers: positive parts, their sum, then
    // normalise (uniform where all regrets are <= 0). The same operations in the same order as the
    // generic three-pass loops below (sum over actions in order, 1/sum, o * inv + add as an fma), so
    // the floats are identical; no arena sum/add rows, and 16-bit regrets decode in registers.
    g_.ensure_loaded(n);
    const bool q16 = g_.compressed;
    const float* R = q16 ? nullptr : g_.regrets.data() + n.storage;
    const int16_t* Q = q16 ? g_.regrets_q.data() + n.storage : nullptr;
    const __m512 qs = _mm512_set1_ps(q16 ? g_.rscale[n.aux] : 0.0f), z = _mm512_setzero_ps(),
                 one = _mm512_set1_ps(1.0f), uni = _mm512_set1_ps(1.0f / static_cast<float>(A));
    __m512 v[16];
    for (int h = 0; h < H; h += 16) {
      const __mmask16 m = static_cast<__mmask16>(H - h >= 16 ? 0xffff : (1u << (H - h)) - 1);
      __m512 sum = z;
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        // the rows are read interleaved: prefetch each one ahead (one stream per row per thread)
        if (q16) _mm_prefetch(reinterpret_cast<const char*>(Q + off) + 256, _MM_HINT_T0);
        else _mm_prefetch(reinterpret_cast<const char*>(R + off) + 512, _MM_HINT_T0);
        __m512 x = q16 ? _mm512_mul_ps(_mm512_cvtepi32_ps(_mm512_cvtepi16_epi32(_mm256_maskz_loadu_epi16(m, Q + off))), qs)
                       : _mm512_maskz_loadu_ps(m, R + off);
        x = _mm512_max_ps(x, z);  // v > 0 ? v : 0 (x is never NaN here)
        v[a] = x;
        sum = _mm512_add_ps(sum, x);
      }
      const __mmask16 pos = _mm512_cmp_ps_mask(sum, z, _CMP_GT_OQ);
      const __m512 inv = _mm512_maskz_div_ps(pos, one, sum);
      const __m512 add = _mm512_mask_blend_ps(pos, uni, z);
      for (int a = 0; a < A; ++a)
        _mm512_mask_storeu_ps(out + static_cast<size_t>(a) * H + h, m, _mm512_fmadd_ps(v[a], inv, add));
    }
  } else
#endif
  {
  const float* R = g_.regrets_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
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
    float* rpa = sc.a.alloc(static_cast<size_t>(A) * Hp);  // per-action reach: actions may run in parallel
    // River fold + call: both rows from one showdown kernel call (call_and_fold).
    int paired_f = -1, paired_s = -1;
    if (n.board_len == 5 && A >= 2) {
      for (int a = 0; a < A; ++a) {
        const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
        if (k == ANodeKind::Fold) paired_f = a;
        else if (k == ANodeKind::Showdown) paired_s = a;
      }
      if (paired_f >= 0 && paired_s >= 0 && !(pruned && (pruned[paired_f] || pruned[paired_s])) &&
          call_and_fold(g_.nodes[n.first_child + static_cast<uint32_t>(paired_s)],
                        g_.nodes[n.first_child + static_cast<uint32_t>(paired_f)], p, ro,
                        cfv + static_cast<size_t>(paired_s) * Hp, cfv + static_cast<size_t>(paired_f) * Hp)) {
      } else {
        paired_f = paired_s = -1;
      }
    }
    auto act = [&](int a) {
      if (pruned && pruned[a]) return;  // cfv[a] intentionally left undefined
      if (a == paired_f || a == paired_s) return;  // filled by call_and_fold
      const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
      float* ca = cfv + static_cast<size_t>(a) * Hp;
      const GNode& child = g_.nodes[ci];
      if (child.kind == ANodeKind::Fold) {
        // Our own fold: opponent reach is unchanged, so this is just payoff * compatible mass.
        fold_value(child, p, ro, ca);
        return;
      }
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      float* rp2 = rpa + static_cast<size_t>(a) * Hp;
      if (M == Walk::BestResponse) std::memcpy(rp2, rp, sizeof(float) * Hp);
      else
        for (int h = 0; h < Hp; ++h) rp2[h] = rp[h] * sa[h];
      walk_t<M>(ci, p, rp2, ro, ca);
    };
    run_actions(n, A, act);
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
    // Strategy-sum accumulation (accumulate_). With avg_reset (default) it runs from t = 1 and
    // Solver::iterate zeroes the sums at t = avg_delay + 1 (#135); without it, it is skipped until
    // t > avg_delay and S stays untouched (zero) until then.
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
    uint32_t mr = 0, ms = 0;  // max |regret| and max strategy sum as float bits (abs_bits, game.hpp)
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
        // avg_delay phase, exactly like the unpruned rows do.) The maxima only matter for 16-bit storage.
        if (accum)
          for (int h = 0; h < Hp; ++h) s[h] *= sm;
        if (g_.compressed) {
          if (accum)
            for (int h = 0; h < Hp; ++h) ms = std::max(ms, nonneg_bits(s[h]));
          for (int h = 0; h < Hp; ++h) mr = std::max(mr, abs_bits(r[h]));
        }
        continue;
      }
      if (fast && !g_.compressed) {
        // float32 storage: plain update (no max tracking needed)
        if (pr) {
          if (accum) regret_row<true, true>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm);
          else regret_row<true, false>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm);
        } else {
          if (accum) regret_row<false, true>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm);
          else regret_row<false, false>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm);
        }
      } else if (fast) {
        if (pr) {
          if (accum) regret_row_max<true, true>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm, mr, ms);
          else regret_row_max<true, false>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm, mr, ms);
        } else {
          if (accum) regret_row_max<false, true>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm, mr, ms);
          else regret_row_max<false, false>(r, s, pr, c, out, rp, sa, Hp, pm, nm, sm, mr, ms);
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
    g_.regrets_commit(n, R, fast ? std::bit_cast<float>(mr) : -1.0f);
    if (accum) g_.strat_commit(n, S, fast ? std::bit_cast<float>(ms) : -1.0f);
    return;
  }

  // Opponent node. Each action's contribution goes to its own row (actions may run in parallel)
  // and the rows are summed in action order afterwards, so the result does not depend on the
  // thread count.
  float* ro2s = sc.a.alloc(static_cast<size_t>(A) * Ho);
  float* contrib = sc.a.alloc(static_cast<size_t>(A) * Hp);
  uint8_t* used = reinterpret_cast<uint8_t*>(sc.a.alloc(static_cast<size_t>((A + 3) / 4)));
  // River fold + call of the opponent: one kernel call for both (call_with_opp_fold).
  int opp_f = -1, opp_s = -1;
  if (n.board_len == 5 && A >= 2) {
    for (int a = 0; a < A; ++a) {
      const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
      if (k == ANodeKind::Fold) opp_f = a;
      else if (k == ANodeKind::Showdown) opp_s = a;
    }
    if (opp_f < 0 || opp_s < 0) opp_f = opp_s = -1;
  }
  auto act = [&](int a) {
    used[a] = 0;
    if (a == opp_f) return;  // added by the call's kernel below
    const float* sa = sigma + static_cast<size_t>(a) * Ho;
    const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
    float* tmp = contrib + static_cast<size_t>(a) * Hp;
    if (a == opp_s) {
      float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
      for (int k = 0; k < Ho; ++k) ro2[k] = ro[k] * sa[k];
      used[a] = call_with_opp_fold(g_.nodes[ci], g_.nodes[n.first_child + static_cast<uint32_t>(opp_f)], p, ro2, ro,
                                   sigma + static_cast<size_t>(opp_f) * Ho, ro2s + static_cast<size_t>(opp_f) * Ho, tmp);
      return;
    }
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
        return;
      }
      const uint8_t* pc1 = g_.hcard[p][0].data();
      const uint8_t* pc2 = g_.hcard[p][1].data();
      const int32_t* same = g_.same_hand[p].data();
      for (int h = 0; h < Hp; ++h) {
        float m = total - cs[pc1[h]] - cs[pc2[h]];
        if (same[h] >= 0) m += ro[same[h]] * sa[same[h]];
        tmp[h] = pay * m;
      }
      used[a] = 1;
      return;
    }
    float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
    bool any = false;
    for (int k = 0; k < Ho; ++k) {
      float v = ro[k] * sa[k];
      ro2[k] = v;
      any |= v != 0.0f;
    }
    if (!any) {
      if (M == Walk::CFR) strategy_only(n.first_child + static_cast<uint32_t>(a), p, rp);
      // set_mes writes a best response everywhere, also where the opponent never goes (like Pio).
      if (!(M == Walk::BestResponse && write_best_response)) return;
    }
    walk_t<M>(n.first_child + static_cast<uint32_t>(a), p, rp, ro2, tmp);
    used[a] = 1;
  };
  run_actions(n, A, act);
  std::memset(out, 0, sizeof(float) * Hp);
  for (int a = 0; a < A; ++a) {
    if (!used[a]) continue;
    const float* c = contrib + static_cast<size_t>(a) * Hp;
    for (int h = 0; h < Hp; ++h) out[h] += c[h];
  }
}

// Sibling actions above the river walk disjoint subtrees (their own regret/strategy rows and
// chance nodes), so they can run as parallel tasks: this exposes work between the turn/river
// fan-outs, where threads otherwise idle at every chance-node join (#53). River decisions have
// small subtrees, so they stay sequential (dispatch would cost more than it saves).
template <typename F>
void Solver::run_actions(const GNode& n, int A, F& act) {
  if (parallel && A > 1 && n.board_len < 5 && pool().threads() > 1) {
    std::function<void(int)> fn = act;
    pool().parallel_for(A, fn, /*widen=*/false);
  } else {
    for (int a = 0; a < A; ++a) act(a);
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
      if (!lockmask && !pr) {  // the common case as straight loops (clang does not unswitch; see regret_row)
        for (int h = 0; h < Hp; ++h) r[h] *= r[h] > 0.0f ? pm : nm;
        if (accum)
          for (int h = 0; h < Hp; ++h) s[h] = s[h] * sm + rp[h] * sa[h];
        continue;
      }
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
  double pb = std::pow(td, params.beta_at(t));
  pos_mult_ = static_cast<float>(pa / (pa + 1.0));
  neg_mult_ = static_cast<float>(pb / (pb + 1.0));
  strat_mult_ = static_cast<float>(std::pow(td / (td + 1.0), params.gamma));
  if (avg_reset) {
    accumulate_ = true;
    if (avg_delay > 0 && t == avg_delay + 1) reset_strategy_sums();  // drop the early iterates now
  } else {
    accumulate_ = t > avg_delay;  // set_avg_delay: the first t <= avg_delay only move regrets
  }
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

void Solver::reset_strategy_sums() {
  // Locked nodes keep their locked strategy in the strat array; combo-locked nodes keep it for the
  // locked combos. Everything else restarts from zero, exactly as if accumulation had been skipped.
  const int N = static_cast<int>(g_.nodes.size());
  auto task = [&](int chunk) {
    const int nchunks = std::min(N, 4 * pool().threads());
    const int lo = static_cast<int>(static_cast<int64_t>(N) * chunk / nchunks);
    const int hi = static_cast<int>(static_cast<int64_t>(N) * (chunk + 1) / nchunks);
    for (int ni = lo; ni < hi; ++ni) {
      const GNode& n = g_.nodes[ni];
      if (n.kind != ANodeKind::Decision || (n.flags & GF_LOCKED)) continue;
      const int A = n.num_children, H = g_.num_hands(n.player);
      ArenaScope sc;
      float* S = g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
      const uint8_t* lockmask = nullptr;
      if (n.flags & GF_COMBO_LOCKED) {
        auto it = g_.combo_lock.find(static_cast<uint32_t>(ni));
        if (it != g_.combo_lock.end()) lockmask = it->second.data();
      }
      if (!lockmask) {
        std::memset(S, 0, sizeof(float) * static_cast<size_t>(A) * H);
      } else {
        for (int a = 0; a < A; ++a)
          for (int h = 0; h < H; ++h)
            if (!lockmask[h]) S[static_cast<size_t>(a) * H + h] = 0.0f;
      }
      g_.strat_commit(n, S);
    }
  };
  const int nchunks = std::min(N, 4 * pool().threads());
  if (parallel && nchunks > 1) {
    std::function<void(int)> fn = task;
    pool().parallel_for(nchunks, fn);
  } else {
    for (int c = 0; c < nchunks; ++c) task(c);
  }
}

void Solver::iterate_subtree(uint32_t ni, int t, const float* r0, const float* r1) {
  double td = static_cast<double>(t);
  double pa = std::pow(td, params.alpha);
  double pb = std::pow(td, params.beta_at(t));
  pos_mult_ = static_cast<float>(pa / (pa + 1.0));
  neg_mult_ = static_cast<float>(pb / (pb + 1.0));
  strat_mult_ = static_cast<float>(std::pow(td / (td + 1.0), params.gamma));
  // Partial re-solves (solve_partial, the on-demand street re-solves of truncated saves) are short
  // and often stop before the delay, so with avg_reset they accumulate from t = 1 and ignore the
  // delay: a global reset would wipe the rest of the tree, and skipping would leave the subtree's
  // average empty (#135 review). That is exactly their behaviour before the default became 32.
  accumulate_ = avg_reset ? true : t > avg_delay;
  prune_this_iter_ = pruning && t > 2 * prune_period && !(prune_period > 1 && t % prune_period == 0);
  const float* r[2] = {r0, r1};
  for (int k = 0; k < 2; ++k) {
    int p = k == 0 ? first_player : 1 - first_player;
    ArenaScope sc;
    float* out = sc.a.alloc(g_.num_hands(p));
    walk_t<Walk::CFR>(ni, p, r[p], r[1 - p], out);
  }
}

// Evaluate + BestResponse fused (#53 follow-up). Both walks give the opponent the same reach
// everywhere (its average strategy), so every terminal value - the expensive part - is the same in
// both; they only differ at p's own decisions (mix by the average strategy vs maximize). Carrying
// both vectors up one traversal halves the cost of an exploitability check. The arithmetic per
// vector is exactly that of walk_t<Evaluate> / walk_t<BestResponse>, in the same order.
void Solver::walk_evbr(uint32_t ni, int p, const float* rp, const float* ro, float* ev, float* br) {
  const GNode& n = g_.nodes[ni];
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  switch (n.kind) {
    case ANodeKind::Fold:
      fold_value(n, p, ro, ev);
      std::memcpy(br, ev, sizeof(float) * Hp);
      return;
    case ANodeKind::Showdown:
      showdown_value(n, p, ro, ev);
      std::memcpy(br, ev, sizeof(float) * Hp);
      return;
    case ANodeKind::Chance: {
      const ChanceInfo& ci = g_.chances[n.aux];
      if (ci.flop_deal) {  // preflop trees only: rare, use the separate walks
        flop_deal(n, ci, Walk::Evaluate, p, rp, ro, ev);
        flop_deal(n, ci, Walk::BestResponse, p, rp, ro, br);
        return;
      }
      const int K = n.num_children;
      ArenaScope sc;
      float* res = sc.a.alloc(static_cast<size_t>(K) * 2 * Hp);
      auto task = [&](int k) {
        ArenaScope tsc;
        int card = ci.cards[k];
        float* rk = res + static_cast<size_t>(k) * 2 * Hp;
        float* ro2 = tsc.a.alloc(Ho);
        std::memcpy(ro2, ro, sizeof(float) * Ho);
        for (uint16_t h : g_.hands_with_card[o][card]) ro2[h] = 0.0f;
        float* rp2 = tsc.a.alloc(Hp);
        std::memcpy(rp2, rp, sizeof(float) * Hp);
        for (uint16_t h : g_.hands_with_card[p][card]) rp2[h] = 0.0f;
        if (all_zero(ro2, Ho)) {
          std::memset(rk, 0, sizeof(float) * 2 * Hp);
          return;
        }
        walk_evbr(n.first_child + static_cast<uint32_t>(k), p, rp2, ro2, rk, rk + Hp);
        for (uint16_t h : g_.hands_with_card[p][card]) rk[h] = rk[Hp + h] = 0.0f;
      };
      if (parallel && K > 1) {
        std::function<void(int)> fn = task;
        pool().parallel_for(K, fn);
      } else {
        for (int k = 0; k < K; ++k) task(k);
      }
      const float inv = 1.0f / static_cast<float>(ci.num_possible);
      for (int half = 0; half < 2; ++half) {
        float* out = half ? br : ev;
        std::memset(out, 0, sizeof(float) * Hp);
        for (int k = 0; k < K; ++k) {
          const float* rk = res + static_cast<size_t>(k) * 2 * Hp + static_cast<size_t>(half) * Hp;
          for (int h = 0; h < Hp; ++h) out[h] += rk[h];
          for (auto& cp : ci.iso[k]) {
            const uint16_t* ph = g_.perm_hand[p][PERM_INV[cp.second]].data();
            for (int h = 0; h < Hp; ++h) out[h] += rk[ph[h]];
          }
        }
        for (int h = 0; h < Hp; ++h) out[h] *= inv;
      }
      return;
    }
    case ANodeKind::Decision: break;
  }

  const int A = n.num_children;
  const int actor = n.player;
  const int Ha = g_.num_hands(actor);
  ArenaScope sc;
  float* sigma = sc.a.alloc(static_cast<size_t>(A) * Ha);
  average_strategy(ni, sigma);

  if (actor == p) {
    float* cev = sc.a.alloc(static_cast<size_t>(A) * Hp);
    float* cbr = sc.a.alloc(static_cast<size_t>(A) * Hp);
    float* rpa = sc.a.alloc(static_cast<size_t>(A) * Hp);
    int paired_f = -1, paired_s = -1;  // river fold + call from one kernel call, as in walk_t
    if (n.board_len == 5 && A >= 2) {
      for (int a = 0; a < A; ++a) {
        const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
        if (k == ANodeKind::Fold) paired_f = a;
        else if (k == ANodeKind::Showdown) paired_s = a;
      }
      if (paired_f >= 0 && paired_s >= 0 &&
          call_and_fold(g_.nodes[n.first_child + static_cast<uint32_t>(paired_s)],
                        g_.nodes[n.first_child + static_cast<uint32_t>(paired_f)], p, ro,
                        cev + static_cast<size_t>(paired_s) * Hp, cev + static_cast<size_t>(paired_f) * Hp)) {
        std::memcpy(cbr + static_cast<size_t>(paired_s) * Hp, cev + static_cast<size_t>(paired_s) * Hp, sizeof(float) * Hp);
        std::memcpy(cbr + static_cast<size_t>(paired_f) * Hp, cev + static_cast<size_t>(paired_f) * Hp, sizeof(float) * Hp);
      } else {
        paired_f = paired_s = -1;
      }
    }
    auto act = [&](int a) {
      if (a == paired_f || a == paired_s) return;  // filled by call_and_fold
      const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
      float* ea = cev + static_cast<size_t>(a) * Hp;
      float* ba = cbr + static_cast<size_t>(a) * Hp;
      const GNode& child = g_.nodes[ci];
      if (child.kind == ANodeKind::Fold) {
        fold_value(child, p, ro, ea);
        std::memcpy(ba, ea, sizeof(float) * Hp);
        return;
      }
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      float* rp2 = rpa + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) rp2[h] = rp[h] * sa[h];
      walk_evbr(ci, p, rp2, ro, ea, ba);
    };
    run_actions(n, A, act);
    // on-policy value (walk_t<Evaluate>)
    std::memset(ev, 0, sizeof(float) * Hp);
    for (int a = 0; a < A; ++a) {
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      const float* c = cev + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) ev[h] += sa[h] * c[h];
    }
    // best response (walk_t<BestResponse>); no deviation where the strategy is locked
    std::memcpy(br, cbr, sizeof(float) * Hp);
    for (int a = 1; a < A; ++a) {
      const float* c = cbr + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) br[h] = std::max(br[h], c[h]);
    }
    if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) {
      const uint8_t* lockmask = nullptr;
      if (!(n.flags & GF_LOCKED)) {
        auto it = g_.combo_lock.find(ni);
        if (it != g_.combo_lock.end()) lockmask = it->second.data();
      }
      for (int h = 0; h < Hp; ++h) {
        if (lockmask && !lockmask[h]) continue;
        float v = 0.0f;
        for (int a = 0; a < A; ++a) v += sigma[static_cast<size_t>(a) * Hp + h] * cbr[static_cast<size_t>(a) * Hp + h];
        br[h] = v;
      }
    }
    return;
  }

  // Opponent node: identical contributions for ev and br except below p's later decisions.
  float* ro2s = sc.a.alloc(static_cast<size_t>(A) * Ho);
  float* cev = sc.a.alloc(static_cast<size_t>(A) * Hp);
  float* cbr = sc.a.alloc(static_cast<size_t>(A) * Hp);
  uint8_t* used = reinterpret_cast<uint8_t*>(sc.a.alloc(static_cast<size_t>((A + 3) / 4)));
  // River fold + call of the opponent: one kernel call for both (call_with_opp_fold).
  int opp_f = -1, opp_s = -1;
  if (n.board_len == 5 && A >= 2) {
    for (int a = 0; a < A; ++a) {
      const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
      if (k == ANodeKind::Fold) opp_f = a;
      else if (k == ANodeKind::Showdown) opp_s = a;
    }
    if (opp_f < 0 || opp_s < 0) opp_f = opp_s = -1;
  }
  auto act = [&](int a) {
    used[a] = 0;
    if (a == opp_f) return;  // added by the call's kernel below
    const float* sa = sigma + static_cast<size_t>(a) * Ho;
    const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
    float* ea = cev + static_cast<size_t>(a) * Hp;
    float* ba = cbr + static_cast<size_t>(a) * Hp;
    if (a == opp_s) {
      float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
      for (int k = 0; k < Ho; ++k) ro2[k] = ro[k] * sa[k];
      if (call_with_opp_fold(g_.nodes[ci], g_.nodes[n.first_child + static_cast<uint32_t>(opp_f)], p, ro2, ro,
                             sigma + static_cast<size_t>(opp_f) * Ho, ro2s + static_cast<size_t>(opp_f) * Ho, ea)) {
        std::memcpy(ba, ea, sizeof(float) * Hp);
        used[a] = 1;
      }
      return;
    }
    if (g_.nodes[ci].kind == ANodeKind::Fold) {
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
      if (total == 0.0f) return;
      const uint8_t* pc1 = g_.hcard[p][0].data();
      const uint8_t* pc2 = g_.hcard[p][1].data();
      const int32_t* same = g_.same_hand[p].data();
      for (int h = 0; h < Hp; ++h) {
        float m = total - cs[pc1[h]] - cs[pc2[h]];
        if (same[h] >= 0) m += ro[same[h]] * sa[same[h]];
        ea[h] = pay * m;
      }
      std::memcpy(ba, ea, sizeof(float) * Hp);
      used[a] = 1;
      return;
    }
    float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
    bool any = false;
    for (int k = 0; k < Ho; ++k) {
      float v = ro[k] * sa[k];
      ro2[k] = v;
      any |= v != 0.0f;
    }
    if (!any) return;
    walk_evbr(ci, p, rp, ro2, ea, ba);
    used[a] = 1;
  };
  run_actions(n, A, act);
  std::memset(ev, 0, sizeof(float) * Hp);
  std::memset(br, 0, sizeof(float) * Hp);
  for (int a = 0; a < A; ++a) {
    if (!used[a]) continue;
    const float* e = cev + static_cast<size_t>(a) * Hp;
    const float* b = cbr + static_cast<size_t>(a) * Hp;
    for (int h = 0; h < Hp; ++h) ev[h] += e[h];
    for (int h = 0; h < Hp; ++h) br[h] += b[h];
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
    float* br = sc.a.alloc(Hp);
    const float* rp = g_.init_reach[p].data();
    const float* ro = g_.init_reach[1 - p].data();
    if (write_best_response) {  // set_mes: the BR walk must store its strategy; keep it separate
      walk_t<Walk::Evaluate>(0, p, rp, ro, out);
      walk_t<Walk::BestResponse>(0, p, rp, ro, br);
    } else {
      walk_evbr(0, p, rp, ro, out, br);
    }
    double s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rp[h]) * out[h];
    st.ev[p] = s / M;
    s = 0;
    for (int h = 0; h < Hp; ++h) s += static_cast<double>(rp[h]) * br[h];
    st.mes[p] = s / M;
  }
  st.exploitability = ((st.mes[0] - st.ev[0]) + (st.mes[1] - st.ev[1])) / 2.0;
  return st;
}

}  // namespace pio
