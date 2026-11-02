#include "pfh2h.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <functional>
#include <mutex>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

#include <unistd.h>

#include "eval.hpp"
#include "parallel.hpp"

namespace pio {

namespace {

constexpr uint32_t kMagic = 0x57483231;    // "WH21"
constexpr uint32_t kVersion = 1;
constexpr const char* kFileName = "pf_h2h_v1.bin";

// A 5-card board up to suit permutation (canonical = lexicographically smallest sorted tuple
// among the 24 suit images).
struct CBoard {
  std::array<uint8_t, 5> cards;  // ascending card indices
  uint64_t mask;
  uint32_t stab;  // # suit permutations mapping the 5-card set to itself
};

void sort5(uint8_t* c) {
  for (int i = 1; i < 5; ++i) {
    uint8_t v = c[i];
    int j = i;
    while (j > 0 && c[j - 1] > v) {
      c[j] = c[j - 1];
      --j;
    }
    c[j] = v;
  }
}

int key5_cmp(const uint8_t* a, const uint8_t* b) {
  for (int i = 0; i < 5; ++i)
    if (a[i] != b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
}

std::vector<CBoard> canonical_boards() {
  std::vector<CBoard> out;
  out.reserve(140000);
  for (uint8_t i0 = 0; i0 < NUM_CARDS; ++i0)
    for (uint8_t i1 = i0 + 1; i1 < NUM_CARDS; ++i1)
      for (uint8_t i2 = i1 + 1; i2 < NUM_CARDS; ++i2)
        for (uint8_t i3 = i2 + 1; i3 < NUM_CARDS; ++i3)
          for (uint8_t i4 = i3 + 1; i4 < NUM_CARDS; ++i4) {
            uint8_t c[5] = {i0, i1, i2, i3, i4};
            bool minimal = true;
            uint32_t stab = 0;
            for (int p = 0; p < NUM_SUIT_PERMS; ++p) {
              uint8_t d[5];
              for (int k = 0; k < 5; ++k) d[k] = PERM_CARD[p][c[k]];
              sort5(d);
              int cmp = key5_cmp(d, c);
              if (cmp < 0) {
                minimal = false;
                break;
              }
              stab += cmp == 0;
            }
            if (!minimal) continue;
            CBoard b;
            b.cards = {i0, i1, i2, i3, i4};
            b.mask = card_bit(i0) | card_bit(i1) | card_bit(i2) | card_bit(i3) | card_bit(i4);
            b.stab = stab;
            out.push_back(b);
          }
  return out;
}

uint32_t enc_pair(int h, int o) { return static_cast<uint32_t>(h) * NUM_COMBOS + o; }

// Canonical hand-pair matchups: unordered disjoint pairs of combos modulo the 24 suit
// permutations (which also swap the two hands' roles implicitly through pair-key minimization).
struct CanonPairs {
  std::vector<std::array<uint16_t, NUM_SUIT_PERMS>> ph, po;  // per-pair permuted combos
  std::vector<std::array<uint64_t, NUM_SUIT_PERMS>> pm;      // joint card masks
  std::unordered_map<uint32_t, uint32_t> id;                 // canonical enc -> pair index
};

CanonPairs canonical_pairs() {
  CanonPairs cp;
  cp.id.reserve(65536);
  for (int h = 0; h < NUM_COMBOS; ++h) {
    uint64_t mh = combo_mask(h);
    for (int o = h + 1; o < NUM_COMBOS; ++o) {
      if (combo_mask(o) & mh) continue;
      uint32_t best = UINT32_MAX;
      int bh = 0, bo = 0;
      for (int s = 0; s < NUM_SUIT_PERMS; ++s) {
        int a = PERM_COMBO[s][h], b = PERM_COMBO[s][o];
        uint32_t k1 = enc_pair(a, b), k2 = enc_pair(b, a);
        if (k1 < best) {
          best = k1;
          bh = a;
          bo = b;
        }
        if (k2 < best) {
          best = k2;
          bh = b;
          bo = a;
        }
      }
      auto [it, ins] = cp.id.emplace(best, static_cast<uint32_t>(cp.ph.size()));
      if (!ins) continue;
      std::array<uint16_t, NUM_SUIT_PERMS> ah, ao;
      std::array<uint64_t, NUM_SUIT_PERMS> am;
      for (int s = 0; s < NUM_SUIT_PERMS; ++s) {
        int x = PERM_COMBO[s][bh], y = PERM_COMBO[s][bo];
        ah[s] = static_cast<uint16_t>(x);
        ao[s] = static_cast<uint16_t>(y);
        am[s] = combo_mask(x) | combo_mask(y);
      }
      cp.ph.push_back(ah);
      cp.po.push_back(ao);
      cp.pm.push_back(am);
    }
  }
  return cp;
}

PfTable generate_table() {
  auto t0 = std::chrono::steady_clock::now();
  init_cards();
  std::vector<CBoard> boards = canonical_boards();
  CanonPairs cp = canonical_pairs();
  const int nb = static_cast<int>(boards.size());
  const int np = static_cast<int>(cp.ph.size());
  std::fprintf(stderr,
               "pf_h2h: generating exact preflop equity table (one-time): %d canonical boards x %d "
               "canonical matchups on %d threads\n",
               nb, np, pool().threads());
  std::fflush(stderr);

  // Per-thread accumulators (integer win/tie counts per canonical matchup). Thread-local storage
  // is registered into this list on first touch and merged after the parallel loop.
  std::mutex acc_mu;
  std::vector<std::pair<std::vector<uint32_t>*, std::vector<uint32_t>*>> accs;
  auto task = [&](int i) {
    thread_local std::vector<uint32_t> twin, ttie;
    thread_local bool registered = false;
    if (!registered) {
      twin.assign(np, 0);
      ttie.assign(np, 0);
      std::lock_guard<std::mutex> lk(acc_mu);
      accs.emplace_back(&twin, &ttie);
      registered = true;
    }
    const CBoard& b = boards[i];
    // Strength of every combo compatible with this board (only those are looked up below).
    uint32_t str[NUM_COMBOS];
    int cs7[7];
    for (int k = 0; k < 5; ++k) cs7[k] = b.cards[k];
    for (int c = 0; c < NUM_COMBOS; ++c) {
      if (combo_mask(c) & b.mask) continue;
      cs7[5] = COMBOS[c].hi;
      cs7[6] = COMBOS[c].lo;
      str[c] = evaluate(cs7, 7);
    }
    const uint32_t stab = b.stab;
    // Sum over the 24 suit images of the hands (one image per real board in b's orbit, weighted
    // exactly by the full S4 action: contributions are constant on stabilizer cosets, so dividing
    // by |stab| gives the exact per-board count for every pair in this matchup class).
    for (int p = 0; p < np; ++p) {
      const uint64_t* mm = cp.pm[p].data();
      const uint16_t* xh = cp.ph[p].data();
      const uint16_t* xo = cp.po[p].data();
      uint32_t w = 0, t = 0;
      for (int s = 0; s < NUM_SUIT_PERMS; ++s) {
        if (mm[s] & b.mask) continue;
        uint32_t a = str[xh[s]], v = str[xo[s]];
        w += a > v;
        t += a == v;
      }
      twin[p] += w / stab;
      ttie[p] += t / stab;
    }
  };
  {
    std::function<void(int)> fn = task;
    pool().parallel_for(nb, fn);
  }
  std::vector<uint32_t> win(np, 0), tie(np, 0);
  {
    std::lock_guard<std::mutex> lk(acc_mu);
    for (auto& [w, t] : accs)
      for (int p = 0; p < np; ++p) {
        win[p] += (*w)[p];
        tie[p] += (*t)[p];
      }
  }

  // Expand to the dense 1326x1326 table.
  PfTable t;
  t.win.assign(static_cast<size_t>(NUM_COMBOS) * NUM_COMBOS, 0);
  t.tie.assign(static_cast<size_t>(NUM_COMBOS) * NUM_COMBOS, 0);
  for (int x = 0; x < NUM_COMBOS; ++x) {
    uint64_t mx = combo_mask(x);
    for (int y = 0; y < NUM_COMBOS; ++y) {
      if (y == x || (combo_mask(y) & mx)) continue;
      uint32_t best = UINT32_MAX;
      bool oriented = true;
      for (int s = 0; s < NUM_SUIT_PERMS; ++s) {
        int a = PERM_COMBO[s][x], bb = PERM_COMBO[s][y];
        uint32_t k1 = enc_pair(a, bb), k2 = enc_pair(bb, a);
        if (k1 < best) {
          best = k1;
          oriented = true;
        }
        if (k2 < best) {
          best = k2;
          oriented = false;
        }
      }
      uint32_t id = cp.id.at(best);
      uint32_t w = win[id], ti = tie[id];
      uint32_t l = PF_BOARDS_PER_PAIR - w - ti;
      t.win[static_cast<size_t>(x) * NUM_COMBOS + y] = oriented ? w : l;
      t.tie[static_cast<size_t>(x) * NUM_COMBOS + y] = ti;
    }
  }

  // Verify a sample of pairs by independent direct enumeration before trusting the table.
  for (auto& [hs, os] : std::vector<std::pair<const char*, const char*>>{
           {"AsAh", "KcKd"}, {"AsAh", "KsKh"}, {"AcKd", "QhJs"},
           {"AsKs", "QsJs"}, {"AsKs", "QhJh"}, {"2c3d", "AhKs"}}) {
    int h = parse_combo(hs), o = parse_combo(os);
    auto [dw, dt] = pf_pair_direct(h, o);
    uint32_t tww = t.win[static_cast<size_t>(h) * NUM_COMBOS + o];
    uint32_t ttt = t.tie[static_cast<size_t>(h) * NUM_COMBOS + o];
    if (dw != tww || dt != ttt)
      throw std::runtime_error("pf_h2h: generated table failed direct verification (" + std::string(hs) + " vs " +
                               os + ")");
  }

  auto t1 = std::chrono::steady_clock::now();
  double secs = std::chrono::duration<double>(t1 - t0).count();
  std::fprintf(stderr, "pf_h2h: table generated and verified in %.1fs\n", secs);
  std::fflush(stderr);
  return t;
}

std::string cache_dir() {
  if (const char* d = std::getenv("WSOLVER_CACHE_DIR")) return d;
  if (const char* x = std::getenv("XDG_CACHE_HOME")) return std::string(x) + "/wsolver";
  if (const char* h = std::getenv("HOME")) return std::string(h) + "/.cache/wsolver";
  if (const char* w = std::getenv("LOCALAPPDATA")) return std::string(w) + "/wsolver";  // Windows
  return {};
}

bool load_table_cache(const std::string& path, PfTable& t) {
  FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  uint32_t hdr[2] = {0, 0};
  bool ok = std::fread(hdr, 4, 2, f) == 2 && hdr[0] == kMagic && hdr[1] == kVersion;
  const size_t n = static_cast<size_t>(NUM_COMBOS) * NUM_COMBOS;
  if (ok) {
    t.win.resize(n);
    t.tie.resize(n);
    ok = std::fread(t.win.data(), 4, n, f) == n && std::fread(t.tie.data(), 4, n, f) == n;
  }
  std::fclose(f);
  if (!ok) {
    t.win.clear();
    t.tie.clear();
  }
  return ok;
}

void save_table_cache(const std::string& dir, const PfTable& t) {
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  if (ec) return;
  std::string tmp = dir + "/.pf_h2h.tmp." + std::to_string(static_cast<long long>(::getpid()));
  FILE* f = std::fopen(tmp.c_str(), "wb");
  if (!f) return;
  uint32_t hdr[2] = {kMagic, kVersion};
  const size_t n = static_cast<size_t>(NUM_COMBOS) * NUM_COMBOS;
  bool ok = std::fwrite(hdr, 4, 2, f) == 2 && std::fwrite(t.win.data(), 4, n, f) == n &&
            std::fwrite(t.tie.data(), 4, n, f) == n && std::fflush(f) == 0;
  std::fclose(f);
  if (!ok) {
    std::filesystem::remove(tmp, ec);
    return;
  }
  std::filesystem::rename(tmp, dir + "/" + kFileName, ec);  // same directory: atomic
  if (ec) std::filesystem::remove(tmp, ec);
}

PfTable load_or_generate() {
  init_cards();
  std::string dir = cache_dir();
  PfTable t;
  if (!dir.empty() && load_table_cache(dir + "/" + kFileName, t)) return t;
  t = generate_table();
  if (!dir.empty()) {
    save_table_cache(dir, t);
    std::fprintf(stderr, "pf_h2h: cached %s (%.1f MB); later runs load instantly\n",
                 (dir + "/" + kFileName).c_str(),
                 (2 * sizeof(uint32_t) * NUM_COMBOS * NUM_COMBOS) / 1048576.0);
    std::fflush(stderr);
  }
  return t;
}

}  // namespace

const PfTable& pf_table() {
  static const PfTable t = load_or_generate();
  return t;
}

std::pair<uint32_t, uint32_t> pf_pair_direct(int h, int o) {
  uint64_t m = combo_mask(h) | combo_mask(o);
  int avail[NUM_CARDS];
  int n = 0;
  for (int c = 0; c < NUM_CARDS; ++c)
    if (!(m & card_bit(c))) avail[n++] = c;
  int cs[7] = {COMBOS[h].hi, COMBOS[h].lo, 0, 0, 0, 0, 0};
  int os[7] = {COMBOS[o].hi, COMBOS[o].lo, 0, 0, 0, 0, 0};
  uint32_t win = 0, tie = 0;
  for (int a = 0; a < n - 4; ++a)
    for (int b = a + 1; b < n - 3; ++b)
      for (int c = b + 1; c < n - 2; ++c)
        for (int d = c + 1; d < n - 1; ++d)
          for (int e = d + 1; e < n; ++e) {
            cs[2] = os[2] = avail[a];
            cs[3] = os[3] = avail[b];
            cs[4] = os[4] = avail[c];
            cs[5] = os[5] = avail[d];
            cs[6] = os[6] = avail[e];
            uint32_t sh = evaluate(cs, 7), sv = evaluate(os, 7);
            win += sh > sv;
            tie += sh == sv;
          }
  return {win, tie};
}

}  // namespace pio
