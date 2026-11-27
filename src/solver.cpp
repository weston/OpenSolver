#include "solver.hpp"
#include "bfloat_regrets.hpp"
#include "fp16_sums.hpp"
#include "compact_rows.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <type_traits>

#if PIO_SHOWDOWN_AVX512 || PIO_SHOWDOWN_AVX2
#include <immintrin.h>
#endif

#include "parallel.hpp"
#if PIO_SHOWDOWN_NEON
#include <arm_neon.h>
#endif
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

// Regret-based pruning skips an action that no hand which can occur at the node plays. A hand sharing a
// card with the node's board (a dealt turn or river card) never occurs there: its regrets stay 0, so it
// plays every action uniformly, and counting it blocked nearly every prune below the flop (threebet_mono,
// 150 iterations: 7,454 of 48.7M river action rows pruned; ignoring these hands, 757,403).
bool Solver::prunable_row(uint32_t ni, int p, const float* row) const {
  const GNode& n = g_.nodes[ni];
  const uint64_t bm = g_.dec_board[n.aux];
  const uint16_t* hs = g_.hands[p].data();
  const int Hp = g_.num_hands(p);
  for (int h = 0; h < Hp; ++h)
    if (row[h] != 0.0f && !(combo_mask(hs[h]) & bm)) return false;
  return true;
}

// calc_results, calc_ev and the best responses count an incentive as payoff, as Pio's do (#439): action a of
// an incentivised node is worth its incentive times the hand's compatible opponent mass on top (the bonus the
// regret update adds). The solve itself is unchanged.
void Solver::add_incentives(uint32_t ni, int p, const float* ro, float* rows) const {
  auto it = g_.incentives.find(ni);
  if (it == g_.incentives.end()) return;
  const int Hp = g_.num_hands(p);
  ArenaScope sc;
  float* mass = sc.a.alloc(Hp);
  compat_mass(p, ro, mass);
  const int A = g_.nodes[ni].num_children;
  for (int a = 0; a < A && a < static_cast<int>(it->second.size()); ++a) {
    const float ia = it->second[a];
    if (ia == 0.0f) continue;
    float* r = rows + static_cast<size_t>(a) * Hp;
    for (int h = 0; h < Hp; ++h) r[h] += ia * mass[h];
  }
}

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
  const float pay = g_.term_pay(n).w[p];
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
// #186: up to NT showdown terminals of one board in one sweep; job fi (if >= 0) carries the opponent's
// fused fold. Each job runs exactly the operations showdown_board_avx512 runs for it, in the same
// order (bit-identical outputs); shared are the decode of p's entries, each opponent entry's load and
// card masks, and the final pass's decode. Microbenchmark on srp_big boards, 5 mixed jobs: 0.75x.
template <int NT>
void showdown_batch_avx512(const ShowdownBoard& sb, int p, const SdJob* job, int fi) {
  const int o = 1 - p;
  const SdEntry* pe = sb.ent[p].data();
  const SdEntry* oe = sb.ent[o].data();
  const int np = static_cast<int>(sb.ent[p].size());
  const auto* gr = sb.groups[p].data();
  const int ng = static_cast<int>(sb.groups[p].size());
  ArenaScope sc;
  float* buf = sc.a.alloc(static_cast<size_t>(np) * 2 * NT);
  float* ltv[NT];
  float* lev[NT];
  for (int t = 0; t < NT; ++t) {
    ltv[t] = buf + static_cast<size_t>(2 * t) * np;
    lev[t] = ltv[t] + np;
  }
  CsRegs cs[NT], csf;
  float cum[NT] = {}, cumf = 0;
  const float* xf = fi >= 0 ? job[fi].xf : nullptr;
  const __m512i b8 = _mm512_set1_epi32(0xff);
  int i0 = 0, j0 = 0;
  for (int g = 0; g < ng; ++g) {
    const int i1 = gr[g].first, j1 = gr[g].second;
    __m512i c1 = _mm512_setzero_si512(), c2 = c1;
    if (i0 < i1) {
      for (int i = i0; i < i1; i += 16) {
        const __mmask16 m = sd_tail_mask(i1 - i);
        const __m512i w = sd_words(pe + i, m, 0);
        c1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
        c2 = _mm512_srli_epi32(w, 24);
        for (int t = 0; t < NT; ++t)
          _mm512_mask_storeu_ps(ltv[t] + i, m, _mm512_sub_ps(_mm512_sub_ps(_mm512_set1_ps(cum[t]), cs[t](c1)), cs[t](c2)));
      }
    }
    for (int j = j0; j < j1; ++j) {
      const SdEntry q = oe[j];
      const uint64_t mm = (1ull << q.c1) | (1ull << q.c2);
      const __mmask16 k0 = static_cast<__mmask16>(mm), k1 = static_cast<__mmask16>(mm >> 16),
                      k2 = static_cast<__mmask16>(mm >> 32), k3 = static_cast<__mmask16>(mm >> 48);
      for (int t = 0; t < NT; ++t) {
        const float r = job[t].ro[q.idx];
        cum[t] += r;
        const __m512 vx = _mm512_set1_ps(r);
        cs[t].r[0] = _mm512_mask_add_ps(cs[t].r[0], k0, cs[t].r[0], vx);
        cs[t].r[1] = _mm512_mask_add_ps(cs[t].r[1], k1, cs[t].r[1], vx);
        cs[t].r[2] = _mm512_mask_add_ps(cs[t].r[2], k2, cs[t].r[2], vx);
        cs[t].r[3] = _mm512_mask_add_ps(cs[t].r[3], k3, cs[t].r[3], vx);
      }
      if (xf) {
        const float f = xf[q.idx];
        cumf += f;
        const __m512 vf = _mm512_set1_ps(f);
        csf.r[0] = _mm512_mask_add_ps(csf.r[0], k0, csf.r[0], vf);
        csf.r[1] = _mm512_mask_add_ps(csf.r[1], k1, csf.r[1], vf);
        csf.r[2] = _mm512_mask_add_ps(csf.r[2], k2, csf.r[2], vf);
        csf.r[3] = _mm512_mask_add_ps(csf.r[3], k3, csf.r[3], vf);
      }
    }
    if (i0 < i1) {
      if (i1 - i0 <= 16) {  // c1/c2 still hold this group's cards
        for (int t = 0; t < NT; ++t)
          _mm512_mask_storeu_ps(lev[t] + i0, sd_tail_mask(i1 - i0),
                                _mm512_sub_ps(_mm512_sub_ps(_mm512_set1_ps(cum[t]), cs[t](c1)), cs[t](c2)));
      } else {
        for (int i = i0; i < i1; i += 16) {
          const __mmask16 m = sd_tail_mask(i1 - i);
          const __m512i w = sd_words(pe + i, m, 0);
          c1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
          c2 = _mm512_srli_epi32(w, 24);
          for (int t = 0; t < NT; ++t)
            _mm512_mask_storeu_ps(lev[t] + i, m, _mm512_sub_ps(_mm512_sub_ps(_mm512_set1_ps(cum[t]), cs[t](c1)), cs[t](c2)));
        }
      }
    }
    i0 = i1;
    j0 = j1;
  }
  bool live[NT];
  for (int t = 0; t < NT; ++t) live[t] = !(cum[t] == 0.0f && (t != fi || cumf == 0.0f));
  for (int i = 0; i < np; i += 16) {
    const __mmask16 m = sd_tail_mask(np - i);
    const __m512i w = sd_words(pe + i, m, 0);
    const __m512i k1 = _mm512_and_si512(_mm512_srli_epi32(w, 16), b8);
    const __m512i k2 = _mm512_srli_epi32(w, 24);
    const __m512i same = _mm512_srai_epi32(_mm512_slli_epi32(sd_words(pe + i, m, 4), 16), 16);  // int16
    const __mmask16 sm = _mm512_mask_cmpge_epi32_mask(m, same, _mm512_setzero_si512());
    for (int t = 0; t < NT; ++t) {
      if (!live[t]) continue;
      const __m512 sr = _mm512_mask_i32gather_ps(_mm512_setzero_ps(), sm, same, job[t].ro, 4);
      const __m512 le = _mm512_add_ps(_mm512_maskz_loadu_ps(m, lev[t] + i), sr);
      const __m512 compat = _mm512_add_ps(_mm512_sub_ps(_mm512_sub_ps(_mm512_set1_ps(cum[t]), cs[t](k1)), cs[t](k2)), sr);
      const __m512 v = _mm512_fmadd_ps(_mm512_set1_ps(job[t].L), compat,
                                       _mm512_fmadd_ps(_mm512_set1_ps(job[t].W - job[t].T), _mm512_maskz_loadu_ps(m, ltv[t] + i),
                                                       _mm512_mul_ps(_mm512_set1_ps(job[t].T - job[t].L), le)));
      _mm512_mask_storeu_ps(ltv[t] + i, m, v);
      if (job[t].compat) _mm512_mask_storeu_ps(lev[t] + i, m, compat);
      if (t == fi) {
        const __m512 fs = _mm512_mask_i32gather_ps(_mm512_setzero_ps(), sm, same, xf, 4);
        _mm512_mask_storeu_ps(lev[t] + i, m, _mm512_add_ps(_mm512_sub_ps(_mm512_sub_ps(_mm512_set1_ps(cumf), csf(k1)), csf(k2)), fs));
      }
    }
  }
  for (int t = 0; t < NT; ++t) {
    if (!live[t]) continue;
    float* out = job[t].out;
    if (t == fi) {
      const float pay = job[t].pay;
      for (int i = 0; i < np; ++i) {
        const int idx = pe[i].idx;
        out[idx] += ltv[t][i];
        out[idx] = std::fma(pay, lev[t][i], out[idx]);
      }
    } else if (float* cmp = job[t].compat) {
      for (int i = 0; i < np; ++i) {
        const int idx = pe[i].idx;
        out[idx] += ltv[t][i];
        cmp[idx] = lev[t][i];
      }
    } else {
      for (int i = 0; i < np; ++i) out[pe[i].idx] += ltv[t][i];
    }
  }
}

#endif  // PIO_SHOWDOWN_AVX512

#if PIO_SHOWDOWN_AVX512 || PIO_SHOWDOWN_AVX2
// Showdown jobs as SIMD lanes (#209): one 256-bit lane per job, plus one per fused fold, for batches
// of 3-8 lanes. showdown_batch_avx512 keeps a CsRegs set (4 zmm) per job and adds every opponent
// entry into each with four masked adds; here cs[] is 52 x 8 floats in memory, card-major, so one
// opponent entry is one row load and three adds for all jobs, and p's queries are plain row loads
// instead of vpermt2ps. Every lane runs exactly the scalar kernel's operations in the same order
// (cum and cs[] accumulate the same entries in the same order; lt / le / compat / value use the
// same expressions as showdown_board_avx512), so the outputs are bit-identical; test_solver's
// [river batch] check covers it. The reach rows are transposed hand-major first (8x8 register
// transposes), which replaces gathers: on Zen 5 a 256-bit gather per entry cost more than all of
// the adds it fed. Microbenchmark on srp_big boards (483 / 315 entries): 0.43-0.68x the time of
// showdown_batch_avx512 for 4-14 lanes; 1-3 lanes stay on it (0.86-1.45x there).
// It is plain AVX2 + FMA code (256-bit), so AVX2-only builds use it as well (#225).
static void showdown_lanes_256(const ShowdownBoard& sb, int p, int n, const SdJob* job, int ho) {
  const int o = 1 - p;
  const SdEntry* pe = sb.ent[p].data();
  const SdEntry* oe = sb.ent[o].data();
  const int np = static_cast<int>(sb.ent[p].size());
  const auto* gr = sb.groups[p].data();
  const int ng = static_cast<int>(sb.groups[p].size());
  const float* src[8];
  alignas(32) float va[8] = {}, vb[8] = {}, vl[8] = {}, vpay[8] = {};
  alignas(32) int32_t fsrc[8];
  int lanes = n;
  for (int t = 0; t < 8; ++t) {
    src[t] = job[0].ro;  // unused lanes: any readable row of length ho
    fsrc[t] = t;
  }
  for (int t = 0; t < n; ++t) {
    src[t] = job[t].ro;
    va[t] = job[t].W - job[t].T;
    vb[t] = job[t].T - job[t].L;
    vl[t] = job[t].L;
    if (job[t].xf) {
      src[lanes] = job[t].xf;
      fsrc[t] = lanes;
      vpay[t] = job[t].pay;
      ++lanes;
    }
  }
  ArenaScope sc;
  float* buf = sc.a.alloc(static_cast<size_t>(2 * np + ho + 1) * 8 + 8);
  float* ltT = reinterpret_cast<float*>((reinterpret_cast<uintptr_t>(buf) + 31) & ~uintptr_t(31));
  float* leT = ltT + static_cast<size_t>(np) * 8;
  float* roH = leT + static_cast<size_t>(np) * 8;  // (ho + 1) x 8: each lane's reach by opponent hand; row ho = 0
  int h0 = 0;
  for (; h0 + 8 <= ho; h0 += 8) {
    const __m256 r0 = _mm256_loadu_ps(src[0] + h0), r1 = _mm256_loadu_ps(src[1] + h0);
    const __m256 r2 = _mm256_loadu_ps(src[2] + h0), r3 = _mm256_loadu_ps(src[3] + h0);
    const __m256 r4 = _mm256_loadu_ps(src[4] + h0), r5 = _mm256_loadu_ps(src[5] + h0);
    const __m256 r6 = _mm256_loadu_ps(src[6] + h0), r7 = _mm256_loadu_ps(src[7] + h0);
    const __m256 t0 = _mm256_unpacklo_ps(r0, r1), t1 = _mm256_unpackhi_ps(r0, r1);
    const __m256 t2 = _mm256_unpacklo_ps(r2, r3), t3 = _mm256_unpackhi_ps(r2, r3);
    const __m256 t4 = _mm256_unpacklo_ps(r4, r5), t5 = _mm256_unpackhi_ps(r4, r5);
    const __m256 t6 = _mm256_unpacklo_ps(r6, r7), t7 = _mm256_unpackhi_ps(r6, r7);
    const __m256 u0 = _mm256_shuffle_ps(t0, t2, 0x44), u1 = _mm256_shuffle_ps(t0, t2, 0xEE);
    const __m256 u2 = _mm256_shuffle_ps(t1, t3, 0x44), u3 = _mm256_shuffle_ps(t1, t3, 0xEE);
    const __m256 u4 = _mm256_shuffle_ps(t4, t6, 0x44), u5 = _mm256_shuffle_ps(t4, t6, 0xEE);
    const __m256 u6 = _mm256_shuffle_ps(t5, t7, 0x44), u7 = _mm256_shuffle_ps(t5, t7, 0xEE);
    float* d = roH + static_cast<size_t>(8) * h0;
    _mm256_store_ps(d + 0, _mm256_permute2f128_ps(u0, u4, 0x20));
    _mm256_store_ps(d + 8, _mm256_permute2f128_ps(u1, u5, 0x20));
    _mm256_store_ps(d + 16, _mm256_permute2f128_ps(u2, u6, 0x20));
    _mm256_store_ps(d + 24, _mm256_permute2f128_ps(u3, u7, 0x20));
    _mm256_store_ps(d + 32, _mm256_permute2f128_ps(u0, u4, 0x31));
    _mm256_store_ps(d + 40, _mm256_permute2f128_ps(u1, u5, 0x31));
    _mm256_store_ps(d + 48, _mm256_permute2f128_ps(u2, u6, 0x31));
    _mm256_store_ps(d + 56, _mm256_permute2f128_ps(u3, u7, 0x31));
  }
  for (; h0 < ho; ++h0)
    for (int t = 0; t < 8; ++t) roH[8 * h0 + t] = src[t][h0];
  _mm256_store_ps(roH + static_cast<size_t>(8) * ho, _mm256_setzero_ps());

  alignas(32) float cs[NUM_CARDS][8];
  for (int c = 0; c < NUM_CARDS; ++c) _mm256_store_ps(cs[c], _mm256_setzero_ps());
  __m256 cum = _mm256_setzero_ps();
  auto query = [&](const SdEntry& q) {
    return _mm256_sub_ps(_mm256_sub_ps(cum, _mm256_load_ps(cs[q.c1])), _mm256_load_ps(cs[q.c2]));
  };
  int i0 = 0, j0 = 0;
  for (int g = 0; g < ng; ++g) {
    const int i1 = gr[g].first, j1 = gr[g].second;
    for (int i = i0; i < i1; ++i) _mm256_store_ps(ltT + 8 * i, query(pe[i]));
    for (int j = j0; j < j1; ++j) {
      const SdEntry q = oe[j];
      const __m256 v = _mm256_load_ps(roH + 8 * q.idx);
      cum = _mm256_add_ps(cum, v);
      _mm256_store_ps(cs[q.c1], _mm256_add_ps(_mm256_load_ps(cs[q.c1]), v));
      _mm256_store_ps(cs[q.c2], _mm256_add_ps(_mm256_load_ps(cs[q.c2]), v));
    }
    for (int i = i0; i < i1; ++i) _mm256_store_ps(leT + 8 * i, query(pe[i]));
    i0 = i1;
    j0 = j1;
  }
  // Same sequence as showdown_board_avx512: v = fma(L, compat, fma(a, lt, b*le)); out = 0 + v (the jobs'
  // out rows start zeroed and the kernels add into them); a fused fold then does out = fma(pay, cf, out)
  // with cf the fold lane's compat. Lanes without a fold get pay 0, and fma(0, x, out) == out exactly
  // (out is never -0 after the + 0, and compat is finite).
  const __m256 A = _mm256_load_ps(va), B = _mm256_load_ps(vb), Lv = _mm256_load_ps(vl), PAY = _mm256_load_ps(vpay);
  const __m256i FS = _mm256_load_si256(reinterpret_cast<const __m256i*>(fsrc));
  for (int i = 0; i < np; ++i) {
    const SdEntry q = pe[i];
    const __m256 sr = _mm256_load_ps(roH + 8 * (q.same >= 0 ? q.same : ho));
    const __m256 le = _mm256_add_ps(_mm256_load_ps(leT + 8 * i), sr);
    const __m256 compat = _mm256_add_ps(query(q), sr);
    __m256 v = _mm256_fmadd_ps(Lv, compat, _mm256_fmadd_ps(A, _mm256_load_ps(ltT + 8 * i), _mm256_mul_ps(B, le)));
    v = _mm256_add_ps(_mm256_setzero_ps(), v);
    v = _mm256_fmadd_ps(PAY, _mm256_permutevar8x32_ps(compat, FS), v);
    _mm256_store_ps(ltT + 8 * i, v);
    _mm256_store_ps(leT + 8 * i, compat);
  }
  for (int t = 0; t < n; ++t) {
    float* out = job[t].out;
    for (int i = 0; i < np; ++i) out[pe[i].idx] = ltT[8 * i + t];
    if (float* cmp = job[t].compat)
      for (int i = 0; i < np; ++i) cmp[pe[i].idx] = leT[8 * i + t];
  }
}

#endif

#if PIO_SHOWDOWN_NEON
// The x86 lanes kernel (showdown_lanes_256) for AArch64, one lane per job plus one per fused fold.
// W = 8: two 128-bit halves; W = 4: one, for packs of up to 4 lanes (most river batches: 3-4 lanes
// is 50-65% of the kernel's work on srp_big and threebet_mono), which halves their vector work.
// Same operations per lane as showdown_board_scalar, in the same order (clang and GCC contract its
// value line to fma(L, compat, fma(a, lt, b*le)); the fold is its explicit fma), so the outputs are
// bit-identical whatever the width; test_solver's [river batch] check covers both.
template <int W>
static void showdown_lanes_neon(const ShowdownBoard& sb, int p, int n, const SdJob* job, int ho) {
  static_assert(W == 4 || W == 8);
  constexpr int NH = W / 4;  // 128-bit halves
  const int o = 1 - p;
  const SdEntry* pe = sb.ent[p].data();
  const SdEntry* oe = sb.ent[o].data();
  const int np = static_cast<int>(sb.ent[p].size());
  const auto* gr = sb.groups[p].data();
  const int ng = static_cast<int>(sb.groups[p].size());
  const float* src[W];
  alignas(16) float va[W] = {}, vb[W] = {}, vl[W] = {};
  int fl[W];  // fold lane of job t, or -1
  int lanes = n;
  for (int t = 0; t < W; ++t) src[t] = job[0].ro;  // unused lanes: any readable row of length ho
  for (int t = 0; t < n; ++t) {
    src[t] = job[t].ro;
    va[t] = job[t].W - job[t].T;
    vb[t] = job[t].T - job[t].L;
    vl[t] = job[t].L;
    fl[t] = -1;
    if (job[t].xf) {
      src[lanes] = job[t].xf;
      fl[t] = lanes++;
    }
  }
  ArenaScope sc;
  float* buf = sc.a.alloc(static_cast<size_t>(2 * np + ho + 1) * W + 4);
  float* ltT = reinterpret_cast<float*>((reinterpret_cast<uintptr_t>(buf) + 15) & ~uintptr_t(15));
  float* leT = ltT + static_cast<size_t>(np) * W;
  float* roH = leT + static_cast<size_t>(np) * W;  // (ho + 1) x W: each lane's reach by opponent hand; row ho = 0
  int h0 = 0;
  for (; h0 + 4 <= ho; h0 += 4) {
    for (int half = 0; half < NH; ++half) {
      const float* const* s4 = src + 4 * half;
      const float32x4_t r0 = vld1q_f32(s4[0] + h0), r1 = vld1q_f32(s4[1] + h0);
      const float32x4_t r2 = vld1q_f32(s4[2] + h0), r3 = vld1q_f32(s4[3] + h0);
      const float64x2_t t0 = vreinterpretq_f64_f32(vtrn1q_f32(r0, r1)), t1 = vreinterpretq_f64_f32(vtrn2q_f32(r0, r1));
      const float64x2_t t2 = vreinterpretq_f64_f32(vtrn1q_f32(r2, r3)), t3 = vreinterpretq_f64_f32(vtrn2q_f32(r2, r3));
      float* d = roH + static_cast<size_t>(W) * h0 + 4 * half;
      vst1q_f32(d, vreinterpretq_f32_f64(vtrn1q_f64(t0, t2)));
      vst1q_f32(d + W, vreinterpretq_f32_f64(vtrn1q_f64(t1, t3)));
      vst1q_f32(d + 2 * W, vreinterpretq_f32_f64(vtrn2q_f64(t0, t2)));
      vst1q_f32(d + 3 * W, vreinterpretq_f32_f64(vtrn2q_f64(t1, t3)));
    }
  }
  for (; h0 < ho; ++h0)
    for (int t = 0; t < W; ++t) roH[W * h0 + t] = src[t][h0];
  for (int t = 0; t < W; ++t) roH[W * ho + t] = 0.0f;

  alignas(16) float cs[NUM_CARDS][W];
  std::memset(cs, 0, sizeof(cs));
  float32x4_t cum[NH];
  for (int k = 0; k < NH; ++k) cum[k] = vdupq_n_f32(0.0f);
  auto qh = [&](const SdEntry& q, int k) {
    return vsubq_f32(vsubq_f32(cum[k], vld1q_f32(cs[q.c1] + 4 * k)), vld1q_f32(cs[q.c2] + 4 * k));
  };
  int i0 = 0, j0 = 0;
  for (int g = 0; g < ng; ++g) {
    const int i1 = gr[g].first, j1 = gr[g].second;
    for (int i = i0; i < i1; ++i)
      for (int k = 0; k < NH; ++k) vst1q_f32(ltT + W * i + 4 * k, qh(pe[i], k));
    for (int j = j0; j < j1; ++j) {
      const SdEntry q = oe[j];
      for (int k = 0; k < NH; ++k) {
        const float32x4_t v = vld1q_f32(roH + W * q.idx + 4 * k);
        cum[k] = vaddq_f32(cum[k], v);
        vst1q_f32(cs[q.c1] + 4 * k, vaddq_f32(vld1q_f32(cs[q.c1] + 4 * k), v));
        vst1q_f32(cs[q.c2] + 4 * k, vaddq_f32(vld1q_f32(cs[q.c2] + 4 * k), v));
      }
    }
    for (int i = i0; i < i1; ++i)
      for (int k = 0; k < NH; ++k) vst1q_f32(leT + W * i + 4 * k, qh(pe[i], k));
    i0 = i1;
    j0 = j1;
  }
  float32x4_t Av[NH], Bv[NH], Lv[NH];
  for (int k = 0; k < NH; ++k) {
    Av[k] = vld1q_f32(va + 4 * k);
    Bv[k] = vld1q_f32(vb + 4 * k);
    Lv[k] = vld1q_f32(vl + 4 * k);
  }
  const float32x4_t Z = vdupq_n_f32(0.0f);
  for (int i = 0; i < np; ++i) {
    const SdEntry q = pe[i];
    const float* sr = roH + W * (q.same >= 0 ? q.same : ho);
    for (int k = 0; k < NH; ++k) {
      const float32x4_t srk = vld1q_f32(sr + 4 * k);
      const float32x4_t le = vaddq_f32(vld1q_f32(leT + W * i + 4 * k), srk);
      const float32x4_t c = vaddq_f32(qh(q, k), srk);
      // fma(L, compat, fma(a, lt, b*le)), then 0 + v (the scalar kernel adds into a zeroed row)
      const float32x4_t v = vfmaq_f32(vfmaq_f32(vmulq_f32(Bv[k], le), Av[k], vld1q_f32(ltT + W * i + 4 * k)), Lv[k], c);
      vst1q_f32(ltT + W * i + 4 * k, vaddq_f32(Z, v));
      vst1q_f32(leT + W * i + 4 * k, c);
    }
  }
  for (int t = 0; t < n; ++t) {
    float* out = job[t].out;
    if (fl[t] >= 0) {
      const float pay = job[t].pay;
      const int f = fl[t];
      for (int i = 0; i < np; ++i) out[pe[i].idx] = std::fma(pay, leT[W * i + f], ltT[W * i + t]);
    } else {
      for (int i = 0; i < np; ++i) out[pe[i].idx] = ltT[W * i + t];
    }
    if (float* cmp = job[t].compat)
      for (int i = 0; i < np; ++i) cmp[pe[i].idx] = leT[W * i + t];
  }
}
#endif

// showdown_batch's callers hand it unzeroed rows. The lanes kernels store `out` and `compat` at every
// unblocked hand, with no early exit, so before them only the board-blocked hands need a zero. The other
// kernels add into `out` and skip a job whose opponent mass is zero without writing anything, so their
// rows are zeroed whole, as the walk used to. (M4, 25bb fixture: zeroing whole rows in the walk was 2.7%
// of the samples.)
static void zero_rows(const ShowdownBoard& sb, int p, const SdJob* job, int n, bool store) {
  const auto& bl = sb.blocked[p];
  const size_t hp = sb.ent[p].size() + bl.size();
  for (int t = 0; t < n; ++t) {
    float* c = job[t].compat;
    if (store) {
      for (uint16_t h : bl) job[t].out[h] = 0.0f;
      if (c)
        for (uint16_t h : bl) c[h] = 0.0f;
    } else {
      std::memset(job[t].out, 0, sizeof(float) * hp);
      if (c) std::memset(c, 0, sizeof(float) * hp);
    }
  }
}

void showdown_batch(const ShowdownBoard& sb, int p, int n, const SdJob* job, int ho) {
#if PIO_SHOWDOWN_AVX512
  // Packs of up to 8 lanes (a job, plus one more for a fused fold) go to showdown_lanes_256. Packs of
  // 1-3 lanes (at most one fused fold), where its fixed cost does not pay, use showdown_batch_avx512 as
  // before; the small bench trees have nothing else (flop_3bet: 1 job per board; deep, flop_srp_2: 1-2).
  int k = 0;
  while (k < n) {
    int lanes = 0, m = 0, fi = -1;
    while (k + m < n && lanes + 1 + (job[k + m].xf ? 1 : 0) <= 8) {
      if (job[k + m].xf) fi = m;
      lanes += 1 + (job[k + m].xf ? 1 : 0);
      ++m;
    }
    zero_rows(sb, p, job + k, m, lanes >= 4);
    switch (lanes >= 4 ? 0 : m) {
      case 1: showdown_batch_avx512<1>(sb, p, job + k, fi); break;
      case 2: showdown_batch_avx512<2>(sb, p, job + k, fi); break;
      case 3: showdown_batch_avx512<3>(sb, p, job + k, fi); break;
      default: showdown_lanes_256(sb, p, m, job + k, ho); break;
    }
    k += m;
  }
#elif PIO_SHOWDOWN_NEON || PIO_SHOWDOWN_AVX2
  // Packs of up to 8 lanes; a lone job without a fold stays on the scalar kernel (the lanes kernel's
  // fixed cost is larger than one job's work).
  int k = 0;
  while (k < n) {
    int lanes = 0, m = 0;
    while (k + m < n && lanes + 1 + (job[k + m].xf ? 1 : 0) <= 8) {
      lanes += 1 + (job[k + m].xf ? 1 : 0);
      ++m;
    }
    zero_rows(sb, p, job + k, m, lanes >= 2);
    if (lanes >= 2) {
#if PIO_SHOWDOWN_NEON
      // Half width for up to 4 lanes. On Zen 5 the same split (a 128-bit showdown_lanes_256) measured
      // no faster, so the x86 builds keep one width.
      if (lanes <= 4) showdown_lanes_neon<4>(sb, p, m, job + k, ho);
      else showdown_lanes_neon<8>(sb, p, m, job + k, ho);
#else
      showdown_lanes_256(sb, p, m, job + k, ho);
#endif
    } else {
      for (int i = k; i < k + m; ++i) {
        const SdFold f{job[i].xf, job[i].pay};
        showdown_board_scalar(sb, job[i].W, job[i].L, job[i].T, p, job[i].ro, job[i].out, job[i].compat,
                              job[i].xf ? &f : nullptr);
      }
    }
    k += m;
  }
#else
  (void)ho;
  zero_rows(sb, p, job, n, false);
  for (int i = 0; i < n; ++i) {
    const SdFold f{job[i].xf, job[i].pay};
    showdown_board_scalar(sb, job[i].W, job[i].L, job[i].T, p, job[i].ro, job[i].out, job[i].compat,
                          job[i].xf ? &f : nullptr);
  }
#endif
}

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

// The same value from a compact table (AllinRunouts::tri8): with c = e2 - V antisymmetric,
// sum_o ro[o] * e2[h][o] = S[h] + V * compat(h), S[h] = sum_o ro[o] * c[h][o], and the IP player's count
// against OOP's hand h, 2V - e2[h][x] = V + c[x][h], gives the same expression: one function for both.
// A row of the triangle serves twice, as the part of S[i] under the diagonal and, negated, as column i
// of the rows above it.
namespace {
// In functions of their own: inside the loop over the rows the compiler left the sixteen sums scalar.
template <class E>
[[gnu::noinline]] float tri_dot(const float* r, const E* row, int n) {
  float part[16] = {};
  int j = 0;
  for (; j + 16 <= n; j += 16)
    for (int l = 0; l < 16; ++l) part[l] += r[j + l] * static_cast<float>(row[j + l]);
  float d = 0.0f;
  for (; j < n; ++j) d += r[j] * static_cast<float>(row[j]);
  for (int l = 0; l < 16; ++l) d += part[l];
  return d;
}
template <class E>
[[gnu::noinline]] void tri_axpy(float* s, float a, const E* row, int n) {
  for (int j = 0; j < n; ++j) s[j] += a * static_cast<float>(row[j]);
}
}  // namespace

template <class E>
void Solver::allin_tri_value(const AllinRunouts& ar, const E* tri, float W, float L, int p, const float* ro,
                             float* out) const {
  const int n = static_cast<int>(ar.live.size());
  const uint16_t* live = ar.live.data();
  const float half = 0.5f * (W - L) * ar.scale;
  const float V = static_cast<float>(ar.valid);
  ArenaScope sc;
  float* cm = sc.a.alloc(g_.num_hands(p));
  compat_mass(p, ro, cm);
  float* r = sc.a.alloc(n);
  float* s = sc.a.alloc_zero(n);
  for (int i = 0; i < n; ++i) r[i] = ro[live[i]];
  const E* row = tri;
  bool whole = false;
  if constexpr (sizeof(E) == 1) {
    // A byte table: the sums under the diagonal on whole numbers (reach_digits.hpp; 9 KB of stack).
    if (ReachDigits q; allin_digits && q.set(r, n)) {
      for (int i = 1; i < n; row += i, ++i) {
        s[i] += static_cast<float>(static_cast<double>(tri_dot_digits(q, row, i)) * q.unit);
        if (r[i] != 0.0f) tri_axpy(s, -r[i], row, i);
      }
      whole = true;
    }
  }
  for (int i = 1; !whole && i < n; row += i, ++i) {
    s[i] += tri_dot(r, row, i);
    if (r[i] != 0.0f) tri_axpy(s, -r[i], row, i);  // most opponent hands have no reach at an all-in
  }
  for (int i = 0; i < n; ++i) {
    const int h = live[i];
    out[h] = half * (s[i] + V * cm[h]) + L * cm[h];
  }
}

void Solver::showdown_value(const GNode& n, int p, const float* ro, float* out) const {
  const GameTree::TermPay& tp = g_.term_pay(n);
  const float W = tp.w[p], L = tp.l[p], T = tp.t[p];
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
  // (`allin_tables` off is for tests, which compare the two ways; a set without boards has only its table.)
  if ((allin_tables || ar.boards.empty()) && ar.built() && !g_.payoff().icm.enabled) {
    // value(h) = scale * sum_o ro[o] * (W*wins + T*ties + L*losses) over the runouts. Without ICM a
    // tie pays the midpoint, so with e2 = 2*wins + ties and V = runouts per disjoint pair,
    // value(h) = (W - L) / (2V) * sum_o ro[o] * e2[h][o] + L * compat(h). For IP the table is read
    // transposed: 2*wins_IP + ties = 2V - e2 on compatible pairs.
    if (!ar.tri8.empty()) allin_tri_value(ar, ar.tri8.data(), W, L, p, ro, out);
    else if (!ar.tri16.empty()) allin_tri_value(ar, ar.tri16.data(), W, L, p, ro, out);
    else if (!ar.e2b.empty()) allin_table_value(ar, ar.e2b.data(), W, L, p, ro, out);
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
  const GameTree::TermPay& tp = g_.term_pay(sd);
  const float W = tp.w[p], L = tp.l[p], T = tp.t[p];
  const int Hp = g_.num_hands(p);
  std::memset(sd_out, 0, sizeof(float) * Hp);
  std::memset(fold_out, 0, sizeof(float) * Hp);
  showdown_board(g_.showdowns[sd.aux], W, L, T, p, ro, sd_out, fold_out);
  const float pay = g_.term_pay(fold).w[p];
  for (int h = 0; h < Hp; ++h) fold_out[h] *= pay;
  return true;
}

bool Solver::call_with_opp_fold(const GNode& sd, const GNode& fold, int p, const float* xc, const float* ro,
                                const float* sfold, float* xf, float* out) const {
  if (sd.kind != ANodeKind::Showdown || fold.kind != ANodeKind::Fold || sd.board_len != 5) return false;
  const GameTree::TermPay& tp = g_.term_pay(sd);
  const float W = tp.w[p], L = tp.l[p], T = tp.t[p];
  const int Ho = g_.num_hands(1 - p);
  for (int k = 0; k < Ho; ++k) xf[k] = ro[k] * sfold[k];
  const SdFold f{xf, g_.term_pay(fold).w[p]};
  std::memset(out, 0, sizeof(float) * g_.num_hands(p));
  showdown_board(g_.showdowns[sd.aux], W, L, T, p, xc, out, nullptr, &f);
  return true;
}

static float expand_regret(float value) { return value; }
static float expand_regret(uint16_t value) { return std::bit_cast<float>(bfloat_regret_bits(value)); }

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
  auto float_regrets = [&]() -> const float* {
    if (!report_bfloat16_regrets_) return g_.regrets.data() + n.storage;
    const auto* source = report_bfloat16_regrets_ + n.storage;
    const size_t size = static_cast<size_t>(A) * H;
    auto* decoded = sc.a.alloc(size);
    for (size_t i = 0; i < size; ++i) decoded[i] = expand_regret(source[i]);
    return decoded;
  };
  const float* P = (predictive && !g_.preds.empty()) ? g_.preds.data() + n.storage : nullptr;
#if PIO_SHOWDOWN_AVX512
  if (!P && !sqrt_weights_ && A <= 16) {
    // One pass per 16 hands with every action's row in registers: positive parts, their sum, then
    // normalise (uniform where all regrets are <= 0). The same operations in the same order as the
    // generic three-pass loops below (sum over actions in order, 1/sum, o * inv + add as an fma), so
    // the floats are identical; no arena sum/add rows, and 16-bit regrets decode in registers.
    g_.ensure_loaded(n);
    const bool q16 = g_.compressed;
    const float* R = q16 ? nullptr : float_regrets();
    const int16_t* Q = q16 ? g_.regrets_q.data() + g_.qo(n) : nullptr;
#if PIO_COMPACT_ROWS
    // Compact rows (#239): row a starts at a * L, and each 16 hands take popcount of their kept bits. An
    // expand-load puts the kept values in their hands' lanes and 0 elsewhere, which is what the full row
    // held there, so the floats are the full-row ones.
    const uint32_t* kb = q16 ? g_.keep_bits(n) : nullptr;
    const size_t L = q16 ? g_.row_len(n) : static_cast<size_t>(H);
    size_t used = 0;  // compact values before hand h in every row
#endif
    const __m512 qs = _mm512_set1_ps(q16 ? g_.rscale[n.aux] : 0.0f), z = _mm512_setzero_ps(),
                 one = _mm512_set1_ps(1.0f), uni = _mm512_set1_ps(1.0f / static_cast<float>(A));
    __m512 v[16];
    for (int h = 0; h < H; h += 16) {
      const __mmask16 m = static_cast<__mmask16>(H - h >= 16 ? 0xffff : (1u << (H - h)) - 1);
#if PIO_COMPACT_ROWS
      const __mmask16 km = kb ? static_cast<__mmask16>(kb[h / 32] >> (h % 32)) : 0;
#endif
      __m512 sum = z;
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        __m256i q = _mm256_setzero_si256();
#if PIO_COMPACT_ROWS
        if (kb) {
          const int16_t* qa = Q + static_cast<size_t>(a) * L + used;
          _mm_prefetch(reinterpret_cast<const char*>(qa) + 256, _MM_HINT_T0);
          q = expand16(km, qa);
        } else
#endif
        if (q16) {
          // the rows are read interleaved: prefetch each one ahead (one stream per row per thread)
          _mm_prefetch(reinterpret_cast<const char*>(Q + off) + 256, _MM_HINT_T0);
          q = _mm256_maskz_loadu_epi16(m, Q + off);
        } else {
          _mm_prefetch(reinterpret_cast<const char*>(R + off) + 512, _MM_HINT_T0);
        }
        __m512 x = q16 ? _mm512_mul_ps(_mm512_cvtepi32_ps(_mm512_cvtepi16_epi32(q)), qs)
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
#if PIO_COMPACT_ROWS
      used += static_cast<size_t>(__builtin_popcount(km));
#endif
    }
  } else
#endif
#if PIO_SHOWDOWN_AVX2
  if (!P && !sqrt_weights_ && A <= 16) {
    // AVX2 form of the single pass (#175 / NEON #222) for x86 builds without AVX-512: 8 hands at a
    // time with every action's row in registers. The generic loops' operations in the same order.
    g_.ensure_loaded(n);
    // 16-bit full rows decode in registers; compact ones (#239) are expanded into scratch by regrets_ro
    // first (dropped hands read 0, as stored zeros would), then matched like float32 rows.
    const bool q16 = g_.compressed && !n.pad;
    const float* R = q16 ? nullptr
                         : g_.compressed ? g_.regrets_ro(n, sc.a.alloc(static_cast<size_t>(A) * H))
                                         : float_regrets();
    const int16_t* Q = q16 ? g_.regrets_q.data() + g_.qo(n) : nullptr;
    const __m256 qs = _mm256_set1_ps(q16 ? g_.rscale[n.aux] : 0.0f), z = _mm256_setzero_ps(),
                 one = _mm256_set1_ps(1.0f), uni = _mm256_set1_ps(1.0f / static_cast<float>(A));
    __m256 v[16];
    int h = 0;
    for (; h + 8 <= H; h += 8) {
      __m256 sum = z;
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        __m256 x = q16 ? _mm256_mul_ps(_mm256_cvtepi32_ps(_mm256_cvtepi16_epi32(
                                           _mm_loadu_si128(reinterpret_cast<const __m128i*>(Q + off)))),
                                       qs)
                       : _mm256_loadu_ps(R + off);
        x = _mm256_add_ps(x, z);  // the generic loop's r + 0 (turns -0 into +0)
        x = _mm256_max_ps(x, z);  // v > 0 ? v : 0 (maxps returns its second operand, +0, for -0/+0 and 0)
        v[a] = x;
        sum = _mm256_add_ps(sum, x);
      }
      const __m256 pos = _mm256_cmp_ps(sum, z, _CMP_GT_OQ);
      const __m256 inv = _mm256_and_ps(pos, _mm256_div_ps(one, sum));
      const __m256 add = _mm256_andnot_ps(pos, uni);
      for (int a = 0; a < A; ++a) _mm256_storeu_ps(out + static_cast<size_t>(a) * H + h, _mm256_fmadd_ps(v[a], inv, add));
    }
    for (; h < H; ++h) {  // the generic loops' operations, one hand
      float sum = 0.0f, vv[16];
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        float x = (q16 ? static_cast<float>(Q[off]) * g_.rscale[n.aux] : R[off]) + 0.0f;
        x = x > 0.0f ? x : 0.0f;
        vv[a] = x;
        sum += x;
      }
      const bool pos = sum > 0.0f;
      const float inv = pos ? 1.0f / sum : 0.0f, add = pos ? 0.0f : 1.0f / static_cast<float>(A);
      for (int a = 0; a < A; ++a) out[static_cast<size_t>(a) * H + h] = std::fma(vv[a], inv, add);
    }
  } else
#endif
#if PIO_SHOWDOWN_NEON
  if (!P && !sqrt_weights_ && A <= 16) {
    // NEON form of the AVX-512 single pass above (#175): 4 hands at a time with every action's row in
    // registers, instead of three passes through arena rows. The generic loops' operations in the same
    // order (r + 0, max with 0, sum over actions in order, 1/sum, fma(v, inv, add)): identical floats.
    // M4, 10 threads: -2 to -5% on every bench tree (threebet_mono 6.12 -> 5.95 s).
    g_.ensure_loaded(n);
    // 16-bit full rows decode in registers; compact ones (#239) are expanded into scratch by regrets_ro
    // first (dropped hands read 0, as stored zeros would), then matched like float32 rows.
    const bool q16 = g_.compressed && !n.pad;
    const float* R = q16 ? nullptr
                         : g_.compressed ? g_.regrets_ro(n, sc.a.alloc(static_cast<size_t>(A) * H))
                                         : float_regrets();
    const int16_t* Q = q16 ? g_.regrets_q.data() + g_.qo(n) : nullptr;
    const float qs = q16 ? g_.rscale[n.aux] : 0.0f;
    const float32x4_t z = vdupq_n_f32(0.0f), one = vdupq_n_f32(1.0f), uni = vdupq_n_f32(1.0f / static_cast<float>(A));
    float32x4_t v[16];
    int h = 0;
    for (; h + 4 <= H; h += 4) {
      float32x4_t sum = z;
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        float32x4_t x = q16 ? vmulq_n_f32(vcvtq_f32_s32(vmovl_s16(vld1_s16(Q + off))), qs) : vld1q_f32(R + off);
        x = vaddq_f32(x, z);   // the generic loop's r + 0 (turns -0 into +0)
        x = vmaxq_f32(x, z);   // v > 0 ? v : 0
        v[a] = x;
        sum = vaddq_f32(sum, x);
      }
      const uint32x4_t pos = vcgtq_f32(sum, z);
      const float32x4_t inv = vbslq_f32(pos, vdivq_f32(one, sum), z);
      const float32x4_t add = vbslq_f32(pos, z, uni);
      for (int a = 0; a < A; ++a) vst1q_f32(out + static_cast<size_t>(a) * H + h, vfmaq_f32(add, v[a], inv));
    }
    for (; h < H; ++h) {  // the generic loops' operations, one hand
      float sum = 0.0f, vv[16];
      for (int a = 0; a < A; ++a) {
        const size_t off = static_cast<size_t>(a) * H + h;
        float x = (q16 ? static_cast<float>(Q[off]) * qs : R[off]) + 0.0f;
        x = x > 0.0f ? x : 0.0f;
        vv[a] = x;
        sum += x;
      }
      const bool pos = sum > 0.0f;
      const float inv = pos ? 1.0f / sum : 0.0f, add = pos ? 0.0f : 1.0f / static_cast<float>(A);
      for (int a = 0; a < A; ++a) out[static_cast<size_t>(a) * H + h] = std::fma(vv[a], inv, add);
    }
  } else
#endif
  {
  const float* R = report_bfloat16_regrets_ ? float_regrets()
      : g_.regrets_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
  float* sum = sc.a.alloc_zero(H);
  for (int a = 0; a < A; ++a) {
    const float* r = R + static_cast<size_t>(a) * H;
    const float* pr = P ? P + static_cast<size_t>(a) * H : nullptr;
    float* o = out + static_cast<size_t>(a) * H;
    for (int h = 0; h < H; ++h) {
      float v = r[h] + (pr ? pr[h] : 0.0f);
      v = v > 0.0f ? v : 0.0f;
      if (sqrt_weights_) v = std::sqrt(v);
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

static float normalization_weight(float x) { return x; }
static float normalization_weight(uint16_t x) { return scaled_fp16_sum_value(x); }

template <int actions, class Sum, class Regret>
static void average_float_strategy(const Sum* sums, const Regret* regrets, float* out, int hands) {
  uint32_t max_sum_bits = 0;
  for (int h = 0; h < hands; ++h) {
    float sum = 0;
    for (int a = 0; a < actions; ++a) sum += normalization_weight(sums[a * hands + h]);
    if constexpr (std::is_same_v<Sum, uint16_t>) out[h] = sum;
    max_sum_bits = std::max(max_sum_bits, std::bit_cast<uint32_t>(std::max(0.f, sum)));
  }
  const float threshold = std::bit_cast<float>(max_sum_bits) * 1e-4f;
  for (int h = 0; h < hands; ++h) {
    float total = 0;
    if constexpr (std::is_same_v<Sum, uint16_t>) {
      total = out[h];
    } else {
      for (int a = 0; a < actions; ++a) total += normalization_weight(sums[a * hands + h]);
    }
    if (total > threshold) {
      for (int a = 0; a < actions; ++a) out[a * hands + h] = normalization_weight(sums[a * hands + h]) / total;
    } else {
      float pos[actions], sum = 0;
      for (int a = 0; a < actions; ++a) {
        pos[a] = std::max(0.f, expand_regret(regrets[a * hands + h]));
        sum += pos[a];
      }
      const float inv = sum > 0 ? 1.f / sum : 0.f;
      const float uniform = sum > 0 ? 0.f : 1.f / actions;
      for (int a = 0; a < actions; ++a) out[a * hands + h] = std::fma(pos[a], inv, uniform);
    }
  }
}

void Solver::average_strategy(uint32_t ni, float* out) const {
  const GNode& n = g_.nodes[ni];
  const int A = n.num_children;
  const int H = g_.num_hands(n.player);
  ArenaScope sc;
  const float* S = g_.strat_ro(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
  const uint16_t* half = report_fp16_sums_ ? report_fp16_sums_ + n.storage : nullptr;
  if (!n.flags && !g_.compressed && !predictive && !sqrt_weights_ && A <= 4) {
    const auto read_average = [&](const auto* sums, const auto* regrets) {
      switch (A) {
        case 1: return average_float_strategy<1>(sums, regrets, out, H);
        case 2: return average_float_strategy<2>(sums, regrets, out, H);
        case 3: return average_float_strategy<3>(sums, regrets, out, H);
        case 4: return average_float_strategy<4>(sums, regrets, out, H);
      }
    };
    if (half) return read_average(half, report_bfloat16_regrets_ + n.storage);
    if (report_bfloat16_regrets_) return read_average(S, report_bfloat16_regrets_ + n.storage);
    return read_average(S, g_.regrets.data() + n.storage);
  }
  if (half) {
    const size_t count = static_cast<size_t>(A) * H;
    auto* decoded = sc.a.alloc(count);
    for (size_t i = 0; i < count; ++i) decoded[i] = fp16_sum_value(half[i]);
    S = decoded;
  }
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

// The traverser's regret and strategy-sum update at a decision node (extracted from walk_t so the
// two-phase river walk, #186, runs exactly the same code). out is the node's value under sigma.
void Solver::traverser_update(const GNode& n, uint32_t ni, int p, const float* rp, const float* ro, const float* sigma,
                              const float* cfv, const uint8_t* pruned, const float* out) {
  const int A = n.num_children;
  const int Hp = g_.num_hands(p);
  ArenaScope sc;
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
}


// ---- #186 two-phase river walk ---------------------------------------------------------------
namespace {
struct RNode {
  uint32_t ni;
  bool own;              // actor is the traverser
  const float* rp;       // traverser reach at the node
  const float* ro;       // opponent reach at the node
  float* sigma;          // current strategy, A x H[actor]
  float* rows;           // own: cfv per action; opponent: contribution per action (A x Hp)
  uint8_t* flag;         // own: pruned (or null); opponent: used
  float* out;            // the node's value row (in the parent's rows, or the caller's)
};
struct RiverWork {
  std::vector<RNode> nodes;
  std::vector<SdJob> jobs;
  std::vector<std::pair<float*, float>> scale;  // call_and_fold's fold rows: compat * pay
  const ShowdownBoard* sb = nullptr;
  bool busy = false;
};
thread_local RiverWork tl_rw;
}  // namespace

void Solver::river_down(uint32_t ni, int p, const float* rp, const float* ro, float* out) {
  RiverWork& w = tl_rw;
  Arena& ar = arena();
  const GNode& n = g_.nodes[ni];
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  const int A = n.num_children;
  const int actor = n.player;
  const int Ha = g_.num_hands(actor);
  float* sigma = ar.alloc(static_cast<size_t>(A) * Ha);
  current_strategy(ni, sigma);
  // dst and compat go to showdown_batch unzeroed: it zeroes what its kernel needs (zero_rows).
  auto showdown_job = [&](const GNode& sd, const float* reach, float* dst, float* compat, const float* xf, float pay) {
    const GameTree::TermPay& tp = g_.term_pay(sd);
    w.sb = &g_.showdowns[sd.aux];
    w.jobs.push_back({reach, tp.w[p], tp.l[p], tp.t[p], dst, compat, xf, pay});
  };
  if (actor == p) {
    // Start fetching this node's strategy-sum rows now: traverser_update reads and writes them on the
    // way back up, after the whole subtree and its showdown batch, and by then they have arrived
    // instead of stalling the update (threebet_mono, M4: -6% at 1 and 10 threads; Zen 5: -9% at 1
    // and 8 threads, -7% at 32). The regrets need no hint: current_strategy has just read them.
    if (accumulate_) {
      if (g_.compressed) {
        const size_t len = g_.stored_size(n);
        const char* b = reinterpret_cast<const char*>(g_.strat_q.data() + g_.qo(n));
        for (size_t off = 0; off < len * 2; off += 64) __builtin_prefetch(b + off, 1, 3);
      } else {
        const size_t len = static_cast<size_t>(A) * Hp;
        const char* b = reinterpret_cast<const char*>(g_.strat.data() + n.storage);
        for (size_t off = 0; off < len * 4; off += 64) __builtin_prefetch(b + off, 1, 3);
      }
    }
    uint8_t* pruned = nullptr;
    if (prune_this_iter_ && !(n.flags & (GF_LOCKED | GF_COMBO_LOCKED | GF_INCENTIVES | GF_ELIMINATED))) {
      pruned = reinterpret_cast<uint8_t*>(ar.alloc(static_cast<size_t>((A + 3) / 4)));
      for (int a = 0; a < A; ++a) pruned[a] = prunable_row(ni, p, sigma + static_cast<size_t>(a) * Hp) ? 1 : 0;
    }
    float* cfv = ar.alloc(static_cast<size_t>(A) * Hp);
    float* rpa = ar.alloc(static_cast<size_t>(A) * Hp);
    w.nodes.push_back({ni, true, rp, ro, sigma, cfv, pruned, out});
    int paired_f = -1, paired_s = -1;
    if (A >= 2) {
      for (int a = 0; a < A; ++a) {
        const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
        if (k == ANodeKind::Fold) paired_f = a;
        else if (k == ANodeKind::Showdown) paired_s = a;
      }
      if (paired_f >= 0 && paired_s >= 0 && !(pruned && (pruned[paired_f] || pruned[paired_s]))) {
        float* cs = cfv + static_cast<size_t>(paired_s) * Hp;
        float* cf = cfv + static_cast<size_t>(paired_f) * Hp;
        showdown_job(g_.nodes[n.first_child + static_cast<uint32_t>(paired_s)], ro, cs, cf, nullptr, 0.0f);
        w.scale.push_back({cf, g_.term_pay(g_.nodes[n.first_child + static_cast<uint32_t>(paired_f)]).w[p]});
      } else {
        paired_f = paired_s = -1;
      }
    }
    for (int a = 0; a < A; ++a) {
      if (pruned && pruned[a]) continue;
      if (a == paired_f || a == paired_s) continue;
      const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
      float* ca = cfv + static_cast<size_t>(a) * Hp;
      const GNode& child = g_.nodes[ci];
      if (child.kind == ANodeKind::Fold) {
        fold_value(child, p, ro, ca);
      } else if (child.kind == ANodeKind::Showdown) {
        showdown_job(child, ro, ca, nullptr, nullptr, 0.0f);
      } else {
        const float* sa = sigma + static_cast<size_t>(a) * Hp;
        float* rp2 = rpa + static_cast<size_t>(a) * Hp;
        for (int h = 0; h < Hp; ++h) rp2[h] = rp[h] * sa[h];
        river_down(ci, p, rp2, ro, ca);
      }
    }
    return;
  }
  float* ro2s = ar.alloc(static_cast<size_t>(A) * Ho);
  float* contrib = ar.alloc(static_cast<size_t>(A) * Hp);
  uint8_t* used = reinterpret_cast<uint8_t*>(ar.alloc(static_cast<size_t>((A + 3) / 4)));
  w.nodes.push_back({ni, false, rp, ro, sigma, contrib, used, out});
  int opp_f = -1, opp_s = -1;
  if (A >= 2) {
    for (int a = 0; a < A; ++a) {
      const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
      if (k == ANodeKind::Fold) opp_f = a;
      else if (k == ANodeKind::Showdown) opp_s = a;
    }
    if (opp_f < 0 || opp_s < 0) opp_f = opp_s = -1;
  }
  for (int a = 0; a < A; ++a) {
    used[a] = 0;
    if (a == opp_f) continue;
    const float* sa = sigma + static_cast<size_t>(a) * Ho;
    const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
    float* tmp = contrib + static_cast<size_t>(a) * Hp;
    const GNode& child = g_.nodes[ci];
    if (a == opp_s) {
      float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
      for (int k = 0; k < Ho; ++k) ro2[k] = ro[k] * sa[k];
      float* xf = ro2s + static_cast<size_t>(opp_f) * Ho;
      const float* sf = sigma + static_cast<size_t>(opp_f) * Ho;
      for (int k = 0; k < Ho; ++k) xf[k] = ro[k] * sf[k];
      showdown_job(child, ro2, tmp, nullptr, xf,
                   g_.term_pay(g_.nodes[n.first_child + static_cast<uint32_t>(opp_f)]).w[p]);
      used[a] = 1;
      continue;
    }
    if (child.kind == ANodeKind::Fold) {
      const float pay = g_.term_pay(child).w[p];
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
        strategy_only(ci, p, rp);
        continue;
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
      continue;
    }
    float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
    bool any = false;
    for (int k = 0; k < Ho; ++k) {
      float v = ro[k] * sa[k];
      ro2[k] = v;
      any |= v != 0.0f;
    }
    if (!any) {
      strategy_only(ci, p, rp);
      continue;
    }
    if (child.kind == ANodeKind::Showdown) {
      showdown_job(child, ro2, tmp, nullptr, nullptr, 0.0f);
    } else {
      river_down(ci, p, rp, ro2, tmp);
    }
    used[a] = 1;
  }
}

void Solver::river_cfr(uint32_t root, int p, const float* rp, const float* ro, float* out) {
  RiverWork& w = tl_rw;
  w.busy = true;
  w.nodes.clear();
  w.jobs.clear();
  w.scale.clear();
  w.sb = nullptr;
  river_down(root, p, rp, ro, out);
  if (!w.jobs.empty()) showdown_batch(*w.sb, p, static_cast<int>(w.jobs.size()), w.jobs.data(), g_.num_hands(1 - p));
  const int Hp = g_.num_hands(p);
  for (auto& [row, pay] : w.scale)
    for (int h = 0; h < Hp; ++h) row[h] *= pay;
  for (size_t i = w.nodes.size(); i-- > 0;) {
    const RNode& r = w.nodes[i];
    const GNode& n = g_.nodes[r.ni];
    const int A = n.num_children;
    // The first summed row is written as 0 + x rather than added to a zeroed row: the same floats
    // (0 + x also turns -0 into +0), one pass fewer.
    bool first = true;
    if (r.own) {
      for (int a = 0; a < A; ++a) {
        if (r.flag && r.flag[a]) continue;
        const float* sa = r.sigma + static_cast<size_t>(a) * Hp;
        const float* c = r.rows + static_cast<size_t>(a) * Hp;
        if (first) {
          for (int h = 0; h < Hp; ++h) r.out[h] = 0.0f + sa[h] * c[h];
          first = false;
        } else {
          for (int h = 0; h < Hp; ++h) r.out[h] += sa[h] * c[h];
        }
      }
      if (first) std::memset(r.out, 0, sizeof(float) * Hp);
      if (!(n.flags & GF_LOCKED)) traverser_update(n, r.ni, p, r.rp, r.ro, r.sigma, r.rows, r.flag, r.out);
    } else {
      for (int a = 0; a < A; ++a) {
        if (!r.flag[a]) continue;
        const float* c = r.rows + static_cast<size_t>(a) * Hp;
        if (first) {
          for (int h = 0; h < Hp; ++h) r.out[h] = 0.0f + c[h];
          first = false;
        } else {
          for (int h = 0; h < Hp; ++h) r.out[h] += c[h];
        }
      }
      if (first) std::memset(r.out, 0, sizeof(float) * Hp);
    }
  }
  w.busy = false;
}

// Note (#186): in the CFR walk, river subtrees are walked by Solver::river_cfr / river_down on builds
// where river_batch is on. Any change to the river semantics below (decision nodes at board_len 5,
// fold pairing, fused opponent folds, pruning, strategy_only) must be mirrored there; test_solver's
// [river batch] check compares the two walks bit for bit.
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
        if (M == Walk::CFR && river_batch && n.board_len == 4 && !tl_rw.busy &&
            g_.nodes[n.first_child + static_cast<uint32_t>(k)].kind == ANodeKind::Decision)
          river_cfr(n.first_child + static_cast<uint32_t>(k), p, rp2, ro2, rk);  // #186
        else
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
    // every one of p's hands that can occur here (prunable_row, #435) contributes exactly nothing
    // to this iteration's strategy, cfv aggregate or strategy-sum rows of those hands; the only
    // work lost by skipping its subtree is re-measuring
    // its instantaneous regret, which is Brown & Sandholm's bounded approximation (solver.hpp).
    // Never prune nodes whose update semantics must not change: locked, combo-locked, incentivised
    // (rake/ICM bonuses are added to the regret update) or eliminated.
    uint8_t* pruned = nullptr;
    if (M == Walk::CFR && prune_this_iter_ &&
        !(n.flags & (GF_LOCKED | GF_COMBO_LOCKED | GF_INCENTIVES | GF_ELIMINATED))) {
      pruned = reinterpret_cast<uint8_t*>(sc.a.alloc(static_cast<size_t>((A + 3) / 4)));
      for (int a = 0; a < A; ++a) pruned[a] = prunable_row(ni, p, sigma + static_cast<size_t>(a) * Hp) ? 1 : 0;
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
    if (M != Walk::CFR && (n.flags & GF_INCENTIVES)) add_incentives(ni, p, ro, cfv);
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
        // set_mes: the unlocked hands of a combo-locked node play their best response too (#380: it
        // returned before writing anything here)
        if (write_best_response && lockmask) {
          float* R = g_.regrets_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
          float* S = g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * Hp) : nullptr);
          for (int h = 0; h < Hp; ++h) {
            if (lockmask[h]) continue;
            int best = 0;
            for (int a = 1; a < A; ++a)
              if (cfv[static_cast<size_t>(a) * Hp + h] >= cfv[static_cast<size_t>(best) * Hp + h]) best = a;
            for (int a = 0; a < A; ++a) {
              float v = a == best ? 1.0f : 0.0f;
              R[static_cast<size_t>(a) * Hp + h] = v * br_regret_scale;
              S[static_cast<size_t>(a) * Hp + h] = v;
            }
          }
          g_.regrets_commit(n, R);
          g_.strat_commit(n, S);
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
            R[static_cast<size_t>(a) * Hp + h] = v * br_regret_scale;
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
      if (pruned && pruned[a]) continue;  // zero for every hand that can occur here: their sums are exact
      const float* sa = sigma + static_cast<size_t>(a) * Hp;
      const float* c = cfv + static_cast<size_t>(a) * Hp;
      for (int h = 0; h < Hp; ++h) out[h] += sa[h] * c[h];
    }
    if (M != Walk::CFR) return;
    if (n.flags & GF_LOCKED) return;
    traverser_update(n, ni, p, rp, ro, sigma, cfv, pruned, out);
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
      const float pay = g_.term_pay(g_.nodes[ci]).w[p];
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
  const auto& G = g_.perm_all;
  const float gn = 1.0f / static_cast<float>(G.size());
  // In double: on all flops this is a sum of 42,120 values, and a float sum of them moved a flop exit's
  // value by 9e-5 of the dead money at the most where it was measured, three exits of one spot (#505).
  std::vector<double> acc(static_cast<size_t>(Hp), 0.0);
  for (int k = 0; k < K; ++k) {
    const float* rk = res + static_cast<size_t>(k) * Hp;
    const double w = static_cast<double>(ci.fweight[k]) * gn;
    for (uint8_t pm : G) {
      const uint16_t* ph = g_.perm_hand[p][PERM_INV[pm]].data();
      for (int h = 0; h < Hp; ++h) acc[h] += w * rk[ph[h]];
    }
  }
  for (int h = 0; h < Hp; ++h) out[h] = static_cast<float>(acc[h]);
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

void Solver::iterate(int t, int first) {
  g_.round_seed = static_cast<uint32_t>(t) * 0x27D4EB2Fu + 0x165667B1u;  // stochastic rounding (game.hpp)
  double td = static_cast<double>(t);
  double pa = std::pow(td, params.alpha);
  double pb = std::pow(td, params.beta_at(t));
  pos_mult_ = static_cast<float>(pa / (pa + 1.0));
  neg_mult_ = static_cast<float>(pb / (pb + 1.0));
  auto round_regrets = [&] {
    if (!uses_bfloat_regrets(t)) return;
    for (float& x : g_.regrets) {
      x = expand_regret(bfloat_regret_word(std::bit_cast<uint32_t>(x)));
    }
  };
  if (t == 9) round_regrets();
  const auto average = strategy_average_step(t);
  strat_mult_ = average.discount;
  accumulate_ = average.weight != 0.f && (avg_reset || t > avg_delay);
  if (average.reset) reset_strategy_sums();
  // Every prune_period-th iteration is measured in full (the RBP revisiting schedule), and
  // pruning activates only after two full periods (see Solver::pruning): pruning from t=1
  // destroys convergence (flop_3bet to 0.2% pot: 50 -> 230 iterations at period 32).
  prune_this_iter_ = pruning && prune_period > 1 && t > 2 * prune_period && t % prune_period != 0;  // #380
  for (int k = 0; k < 2; ++k) {
    int p = k == 0 ? first : 1 - first;
    ArenaScope sc;
    float* out = sc.a.alloc(g_.num_hands(p));
    const float* own = g_.init_reach[p].data();
    if (average.weight != 0.f && average.weight != 1.f) {
      float* scaled = sc.a.alloc(g_.num_hands(p));
      for (int h = 0; h < g_.num_hands(p); ++h) scaled[h] = own[h] * average.weight;
      own = scaled;
    }
    walk_t<Walk::CFR>(0, p, own, g_.init_reach[1 - p].data(), out);
    round_regrets();
    // every 4th iteration (before the other player's walk reads p's strategies): rounding asymmetry grows
    // about 3x per iteration where hands are near-indifferent, so it stays near 1e-5 between resets, at a
    // quarter of the cost (3.5% per iteration on threebet_mono's monotone flop when done every iteration)
    if (t % 4 == 0) symmetrize_root_street(p);
  }
  if (uses_fp16_sums(t) && (t == 9 || accumulate_))
    for (float& x : g_.strat) x = fp16_sum_value(fp16_sum_word(x));
}

// #300. On a board with suit symmetries (both ranges symmetric too: GameTree::perm_all), suit-swapped
// hands are the same hand and must keep the same strategy. Float sums run in a hand-dependent order
// (chance children, opponent rows), so they drift apart by rounding; where hands are near-indifferent
// CFR blows that up into different strategies, the opponent's reach stops being symmetric, and the
// isomorphism shortcut at the next deal (which assumes it is) gives wrong values: EV OOP + EV IP then
// drifts off the pot (a flop all-in spot: 60.311 on a 60 pot; set_isomorphism 0 0 gives 60.000).
// Averaging each orbit's rows at the root street keeps it exact; those are the few nodes above the
// first deal, so it costs next to nothing.
void Solver::symmetrize_root_street(int p, float* regrets, float* strategy) {
  const auto& G = g_.perm_all;
  if (G.size() <= 1 || g_.cfg.preflop || g_.nodes.empty()) return;
  if (!sym_ready_) {
    sym_ready_ = true;
    const int bl = g_.nodes[0].board_len;
    for (int q = 0; q < 2; ++q) {
      for (uint32_t ni = 0; ni < g_.nodes.size(); ++ni) {
        const GNode& n = g_.nodes[ni];
        if (n.kind == ANodeKind::Decision && n.player == q && n.board_len == bl) sym_nodes_[q].push_back(ni);
      }
      const int H = g_.num_hands(q);
      std::vector<char> seen(H, 0);
      for (int h = 0; h < H; ++h) {
        if (seen[h]) continue;
        const size_t start = sym_orbits_[q].size();
        for (uint8_t pm : G) {
          const int x = g_.perm_hand[q][pm][h];
          if (!seen[x]) { seen[x] = 1; sym_orbits_[q].push_back(static_cast<uint16_t>(x)); }
        }
        if (sym_orbits_[q].size() - start < 2) sym_orbits_[q].resize(start);  // a hand that is its own image
        else sym_orbit_end_[q].push_back(static_cast<uint32_t>(sym_orbits_[q].size()));
      }
    }
  }
  if (sym_orbit_end_[p].empty() || sym_nodes_[p].empty()) return;
  const int H = g_.num_hands(p);
  const uint16_t* orb = sym_orbits_[p].data();
  const auto& ends = sym_orbit_end_[p];
  auto task = [&](int k) {
    const GNode& n = g_.nodes[sym_nodes_[p][static_cast<size_t>(k)]];
    if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) return;
    const int A = n.num_children;
    ArenaScope sc;
    float* R = regrets ? regrets + n.storage : g_.regrets_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
    float* S = strategy ? strategy + n.storage : g_.strat_rw(n, g_.compressed ? sc.a.alloc(static_cast<size_t>(A) * H) : nullptr);
    for (int a = 0; a < A; ++a) {
      float* r = R + static_cast<size_t>(a) * H;
      float* s = S + static_cast<size_t>(a) * H;
      uint32_t b = 0;
      for (uint32_t e : ends) {
        double sr = 0, ss = 0;
        for (uint32_t i = b; i < e; ++i) { sr += r[orb[i]]; ss += s[orb[i]]; }
        const double inv = 1.0 / static_cast<double>(e - b);
        const float mr = static_cast<float>(sr * inv), ms = static_cast<float>(ss * inv);
        for (uint32_t i = b; i < e; ++i) { r[orb[i]] = mr; s[orb[i]] = ms; }
        b = e;
      }
    }
    if (g_.compressed) {
      g_.regrets_commit(n, R);
      g_.strat_commit(n, S);
    }
  };
  const int K = static_cast<int>(sym_nodes_[p].size());
  if (parallel && K > 1) {
    std::function<void(int)> fn = task;
    pool().parallel_for(K, fn);
  } else {
    for (int k = 0; k < K; ++k) task(k);
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
  cancel_flop_opening();
  g_.round_seed = (static_cast<uint32_t>(t) * 0x27D4EB2Fu + 0x165667B1u) ^ 0x5BD1E995u ^ ni;
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
  prune_this_iter_ = pruning && prune_period > 1 && t > 2 * prune_period && t % prune_period != 0;  // #380
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
// ---- two-phase river walk for the exploitability check ----------------------------------------
// walk_evbr's river subtrees the way river_cfr does CFR's: average strategies and opponent reaches
// top-down, every showdown of the subtree in one batched kernel call (a terminal's value is the same
// for the on-policy and the best-response halves), then on-policy values and best responses bottom-up.
// Same operations in the same order as walk_evbr, so bit-identical; test_solver checks it.
namespace {
struct ENode {
  uint32_t ni;
  bool own;
  float* sigma;   // average strategy, A x H[actor]
  float* cev;     // per action: on-policy value of the child (A x Hp)
  float* cbr;     // per action: best-response value of the child (A x Hp)
  uint8_t* kind;  // per action: 0 unused (opponent nodes only), 1 terminal (cbr = cev), 2 subtree
  float* ev;
  float* br;
};
struct EvbrWork {
  std::vector<ENode> nodes;
  std::vector<SdJob> jobs;
  std::vector<std::pair<float*, float>> scale;
  const ShowdownBoard* sb = nullptr;
  bool busy = false;
};
thread_local EvbrWork tl_ew;
}  // namespace

void Solver::river_evbr_down(uint32_t ni, int p, const float* ro, float* ev, float* br) {
  EvbrWork& w = tl_ew;
  Arena& ar = arena();
  const GNode& n = g_.nodes[ni];
  const int o = 1 - p;
  const int Hp = g_.num_hands(p), Ho = g_.num_hands(o);
  const int A = n.num_children;
  const int actor = n.player;
  const int Ha = g_.num_hands(actor);
  float* sigma = ar.alloc(static_cast<size_t>(A) * Ha);
  average_strategy(ni, sigma);
  float* cev = ar.alloc(static_cast<size_t>(A) * Hp);
  float* cbr = ar.alloc(static_cast<size_t>(A) * Hp);
  uint8_t* kind = reinterpret_cast<uint8_t*>(ar.alloc(static_cast<size_t>((A + 3) / 4)));
  w.nodes.push_back({ni, actor == p, sigma, cev, cbr, kind, ev, br});
  // dst and compat go to showdown_batch unzeroed: it zeroes what its kernel needs (zero_rows).
  auto showdown_job = [&](const GNode& sd, const float* reach, float* dst, float* compat, const float* xf, float pay) {
    const GameTree::TermPay& tp = g_.term_pay(sd);
    w.sb = &g_.showdowns[sd.aux];
    w.jobs.push_back({reach, tp.w[p], tp.l[p], tp.t[p], dst, compat, xf, pay});
  };
  int pf = -1, ps = -1;
  if (A >= 2) {
    for (int a = 0; a < A; ++a) {
      const ANodeKind k = g_.nodes[n.first_child + static_cast<uint32_t>(a)].kind;
      if (k == ANodeKind::Fold) pf = a;
      else if (k == ANodeKind::Showdown) ps = a;
    }
    if (pf < 0 || ps < 0) pf = ps = -1;
  }
  if (actor == p) {
    if (ps >= 0) {  // call_and_fold
      float* cs = cev + static_cast<size_t>(ps) * Hp;
      float* cf = cev + static_cast<size_t>(pf) * Hp;
      showdown_job(g_.nodes[n.first_child + static_cast<uint32_t>(ps)], ro, cs, cf, nullptr, 0.0f);
      w.scale.push_back({cf, g_.term_pay(g_.nodes[n.first_child + static_cast<uint32_t>(pf)]).w[p]});
      kind[ps] = kind[pf] = 1;
    }
    for (int a = 0; a < A; ++a) {
      if (a == pf || a == ps) continue;
      const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
      float* ea = cev + static_cast<size_t>(a) * Hp;
      const GNode& child = g_.nodes[ci];
      if (child.kind == ANodeKind::Fold) {
        fold_value(child, p, ro, ea);
        kind[a] = 1;
      } else if (child.kind == ANodeKind::Showdown) {
        showdown_job(child, ro, ea, nullptr, nullptr, 0.0f);
        kind[a] = 1;
      } else {
        river_evbr_down(ci, p, ro, ea, cbr + static_cast<size_t>(a) * Hp);
        kind[a] = 2;
      }
    }
    return;
  }
  float* ro2s = ar.alloc(static_cast<size_t>(A) * Ho);
  for (int a = 0; a < A; ++a) {
    kind[a] = 0;
    if (a == pf) continue;  // added by the call's kernel below (call_with_opp_fold)
    const float* sa = sigma + static_cast<size_t>(a) * Ho;
    const uint32_t ci = n.first_child + static_cast<uint32_t>(a);
    float* ea = cev + static_cast<size_t>(a) * Hp;
    const GNode& child = g_.nodes[ci];
    if (a == ps) {
      float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
      for (int k = 0; k < Ho; ++k) ro2[k] = ro[k] * sa[k];
      float* xf = ro2s + static_cast<size_t>(pf) * Ho;
      const float* sf = sigma + static_cast<size_t>(pf) * Ho;
      for (int k = 0; k < Ho; ++k) xf[k] = ro[k] * sf[k];
      showdown_job(child, ro2, ea, nullptr, xf,
                   g_.term_pay(g_.nodes[n.first_child + static_cast<uint32_t>(pf)]).w[p]);
      kind[a] = 1;
      continue;
    }
    if (child.kind == ANodeKind::Fold) {
      const float pay = g_.term_pay(child).w[p];
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
      if (total == 0.0f) continue;
      const uint8_t* pc1 = g_.hcard[p][0].data();
      const uint8_t* pc2 = g_.hcard[p][1].data();
      const int32_t* same = g_.same_hand[p].data();
      for (int h = 0; h < Hp; ++h) {
        float m = total - cs[pc1[h]] - cs[pc2[h]];
        if (same[h] >= 0) m += ro[same[h]] * sa[same[h]];
        ea[h] = pay * m;
      }
      kind[a] = 1;
      continue;
    }
    float* ro2 = ro2s + static_cast<size_t>(a) * Ho;
    bool any = false;
    for (int k = 0; k < Ho; ++k) {
      float v = ro[k] * sa[k];
      ro2[k] = v;
      any |= v != 0.0f;
    }
    if (!any) continue;
    if (child.kind == ANodeKind::Showdown) {
      showdown_job(child, ro2, ea, nullptr, nullptr, 0.0f);
      kind[a] = 1;
    } else {
      river_evbr_down(ci, p, ro2, ea, cbr + static_cast<size_t>(a) * Hp);
      kind[a] = 2;
    }
  }
}

void Solver::river_evbr(uint32_t root, int p, const float* ro, float* ev, float* br) {
  EvbrWork& w = tl_ew;
  w.busy = true;
  w.nodes.clear();
  w.jobs.clear();
  w.scale.clear();
  w.sb = nullptr;
  river_evbr_down(root, p, ro, ev, br);
  if (!w.jobs.empty()) showdown_batch(*w.sb, p, static_cast<int>(w.jobs.size()), w.jobs.data(), g_.num_hands(1 - p));
  const int Hp = g_.num_hands(p);
  for (auto& [row, pay] : w.scale)
    for (int h = 0; h < Hp; ++h) row[h] *= pay;
  for (size_t i = w.nodes.size(); i-- > 0;) {
    const ENode& r = w.nodes[i];
    const GNode& n = g_.nodes[r.ni];
    const int A = n.num_children;
    for (int a = 0; a < A; ++a)
      if (r.kind[a] == 1) std::memcpy(r.cbr + static_cast<size_t>(a) * Hp, r.cev + static_cast<size_t>(a) * Hp, sizeof(float) * Hp);
    if (r.own) {
      // the first row as 0 + x, not added to a zeroed row: the same floats (as in river_cfr)
      for (int a = 0; a < A; ++a) {
        const float* sa = r.sigma + static_cast<size_t>(a) * Hp;
        const float* c = r.cev + static_cast<size_t>(a) * Hp;
        if (a == 0) {
          for (int h = 0; h < Hp; ++h) r.ev[h] = 0.0f + sa[h] * c[h];
        } else {
          for (int h = 0; h < Hp; ++h) r.ev[h] += sa[h] * c[h];
        }
      }
      std::memcpy(r.br, r.cbr, sizeof(float) * Hp);
      for (int a = 1; a < A; ++a) {
        const float* c = r.cbr + static_cast<size_t>(a) * Hp;
        for (int h = 0; h < Hp; ++h) r.br[h] = std::max(r.br[h], c[h]);
      }
      if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) {
        const uint8_t* lockmask = nullptr;
        if (!(n.flags & GF_LOCKED)) {
          auto it = g_.combo_lock.find(r.ni);
          if (it != g_.combo_lock.end()) lockmask = it->second.data();
        }
        for (int h = 0; h < Hp; ++h) {
          if (lockmask && !lockmask[h]) continue;
          float v = 0.0f;
          for (int a = 0; a < A; ++a) v += r.sigma[static_cast<size_t>(a) * Hp + h] * r.cbr[static_cast<size_t>(a) * Hp + h];
          r.br[h] = v;
        }
      }
    } else {
      bool first = true;
      for (int a = 0; a < A; ++a) {
        if (!r.kind[a]) continue;
        const float* e = r.cev + static_cast<size_t>(a) * Hp;
        const float* b = r.cbr + static_cast<size_t>(a) * Hp;
        if (first) {
          for (int h = 0; h < Hp; ++h) r.ev[h] = 0.0f + e[h];
          for (int h = 0; h < Hp; ++h) r.br[h] = 0.0f + b[h];
          first = false;
        } else {
          for (int h = 0; h < Hp; ++h) r.ev[h] += e[h];
          for (int h = 0; h < Hp; ++h) r.br[h] += b[h];
        }
      }
      if (first) {
        std::memset(r.ev, 0, sizeof(float) * Hp);
        std::memset(r.br, 0, sizeof(float) * Hp);
      }
    }
  }
  w.busy = false;
}

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
        if (river_batch && g_.incentives.empty() && n.board_len == 4 && !tl_ew.busy &&
            g_.nodes[n.first_child + static_cast<uint32_t>(k)].kind == ANodeKind::Decision)
          river_evbr(n.first_child + static_cast<uint32_t>(k), p, ro2, rk, rk + Hp);
        else
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
    if (n.flags & GF_INCENTIVES) {
      add_incentives(ni, p, ro, cev);
      add_incentives(ni, p, ro, cbr);
    }
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
      const float pay = g_.term_pay(g_.nodes[ci]).w[p];
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


RootStats Solver::root_stats_from_bfloat16(const uint16_t* committed_regrets, const uint16_t* committed_sums) const {
  Solver reader = *this;
  reader.report_bfloat16_regrets_ = committed_regrets;
  reader.report_fp16_sums_ = committed_sums;
  return reader.root_stats();
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
