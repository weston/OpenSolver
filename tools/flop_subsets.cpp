// Flop subsets for preflop trees (#486): chooses the flops and their weights. See flop_subsets.hpp for
// what is minimised, and flops/README.md for the files made with it.
//
//   build/flop_subsets make N [N ...] [--dir DIR]   choose N flops; print the file, or write DIR/wsolver_N.txt
//   build/flop_subsets eval FILE [FILE ...]         the error of a flop file (Ks5h2s:weight per line)
//   build/flop_subsets equity FILE A B [A B ...]    class A's all-in equity against B (a class, or classes
//                                                   with commas) through the file's flops, as the solver
//                                                   gets it at a preflop exit, and exactly
//   build/flop_subsets random N COUNT [SEED]        the error of COUNT random N-flop subsets
//   build/flop_subsets sample N SEED [solved]       one random N-flop subset as a flop file
//   build/flop_subsets curve MAX                    the greedy error for every size up to MAX (no swaps)
//   build/flop_subsets deck                         what kind of flops the deck deals
//
// All but `equity` need every flop's tables: the first run computes them (about a minute, all cores),
// checks that all flops together reproduce the exact preflop table, and caches the products the
// search needs in ~/.cache/wsolver/flop_gram_v3.bin (74 MB).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <sstream>
#include <thread>

#include "flop_subsets.hpp"
#include "../src/parallel.hpp"

using namespace pio;
using namespace pio::flops;

namespace {

std::string cache_path() {
  std::string dir;
  if (const char* x = std::getenv("XDG_CACHE_HOME")) dir = std::string(x) + "/wsolver";
  else if (const char* h = std::getenv("HOME")) dir = std::string(h) + "/.cache/wsolver";
  if (dir.empty()) return {};
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  return dir + "/flop_gram_v3.bin";
}

// The Gram matrices of the rows per street (flop, turn, river) and their sum, and what the whole deck
// gives, from the cache or computed (and checked) now.
struct Grams {
  std::vector<double> street[STREETS], all;
  Deck deck;
};
Grams gram(const std::vector<FlopClass>& fc) {
  const int n = static_cast<int>(fc.size());
  const size_t nn = static_cast<size_t>(n) * n;
  Grams g;
  g.deck.pairs = class_pairs();
  g.deck.combos = class_combos();
  for (auto& s : g.street) s.resize(nn);
  for (auto& a : g.deck.ahead) a.resize(NFEAT);
  auto total = [&] {
    g.all.resize(nn);
    for (size_t i = 0; i < nn; ++i) g.all[i] = g.street[0][i] + g.street[1][i] + g.street[2][i];
  };
  const std::string path = cache_path();
  if (!path.empty()) {
    std::ifstream in(path, std::ios::binary);
    bool ok = static_cast<bool>(in);
    for (auto& s : g.street)
      ok = ok && in.read(reinterpret_cast<char*>(s.data()), static_cast<std::streamsize>(nn * sizeof(double)));
    for (auto& a : g.deck.ahead)
      ok = ok && in.read(reinterpret_cast<char*>(a.data()), static_cast<std::streamsize>(NFEAT * sizeof(double)));
    if (ok && in.peek() == EOF) {
      total();
      return g;
    }
  }
  std::fprintf(stderr, "flop tables: %d flop classes, every turn and river ...\n", n);
  std::vector<uint32_t> E2(static_cast<size_t>(n) * NFEAT);
  std::vector<float> rows(static_cast<size_t>(n) * NROW);
  for (int street = 0; street < STREETS; ++street) {
    std::function<void(int)> work = [&](int v) {
      std::vector<uint32_t> e;
      flop_table(fc[v].cards, street, e);
      std::copy(e.begin(), e.end(), E2.begin() + static_cast<size_t>(v) * NFEAT);
    };
    pool().parallel_for(n, work);
    std::vector<uint64_t> sum(NFEAT, 0);
    for (int v = 0; v < n; ++v)
      for (int k = 0; k < NFEAT; ++k) sum[k] += static_cast<uint64_t>(E2[static_cast<size_t>(v) * NFEAT + k]) * fc[v].count;
    g.deck.ahead[street] = ahead_from_sum(sum, street);
    if (street == 2) {
      // All flops at their natural weight must give the exact preflop equities (the hand-vs-hand table
      // over the C(48,5) boards of each pair, made independently).
      const std::vector<double> eq = preflop_equity_sums();
      double worst = 0;
      for (int k = 0; k < NFEAT; ++k) worst = std::max(worst, std::abs(g.deck.ahead[2][k] - eq[k]) / std::max(1.0, eq[k]));
      std::fprintf(stderr, "all flops against the exact preflop table: worst relative difference %.2e\n", worst);
      if (worst > 1e-9) {
        std::fprintf(stderr, "flop tables do not add up to the preflop table\n");
        std::exit(1);
      }
    }
    std::function<void(int)> mk = [&](int v) {
      std::vector<uint32_t> e(E2.begin() + static_cast<size_t>(v) * NFEAT, E2.begin() + static_cast<size_t>(v + 1) * NFEAT);
      feature_row(e, street, g.deck, rows.data() + static_cast<size_t>(v) * NROW);
    };
    pool().parallel_for(n, mk);
    std::vector<double>& G = g.street[street];
    std::function<void(int)> dots = [&](int i) {
      const float* a = rows.data() + static_cast<size_t>(i) * NROW;
      for (int j = i; j < n; ++j) {
        const float* b = rows.data() + static_cast<size_t>(j) * NROW;
        double s = 0;
        for (int k = 0; k < NROW; ++k) s += static_cast<double>(a[k]) * b[k];
        G[static_cast<size_t>(i) * n + j] = G[static_cast<size_t>(j) * n + i] = s;
      }
    };
    pool().parallel_for(n, dots);
  }
  if (!path.empty()) {
    std::ofstream out(path + ".tmp", std::ios::binary);
    for (auto& s : g.street) out.write(reinterpret_cast<const char*>(s.data()), static_cast<std::streamsize>(nn * sizeof(double)));
    for (auto& a : g.deck.ahead) out.write(reinterpret_cast<const char*>(a.data()), static_cast<std::streamsize>(NFEAT * sizeof(double)));
    out.close();
    std::error_code ec;
    std::filesystem::rename(path + ".tmp", path, ec);
  }
  total();
  return g;
}

// What kind of flops a subset is made of, against the deck: shares in percent.
struct Texture {
  double rainbow = 0, two_tone = 0, monotone = 0, paired = 0, trips = 0, high = 0;  // high: mean rank of the top card
};
Texture texture(const std::vector<FlopClass>& fc, const std::vector<int>& S, const std::vector<double>& w) {
  Texture t;
  double sum = 0;
  for (size_t i = 0; i < S.size(); ++i) {
    const auto& c = fc[S[i]].cards;
    const int suits = (1 << card_suit(c[0])) | (1 << card_suit(c[1])) | (1 << card_suit(c[2]));
    const int ns = __builtin_popcount(suits);
    const int nr = 1 + (card_rank(c[1]) != card_rank(c[0])) + (card_rank(c[2]) != card_rank(c[1]));
    (ns == 3 ? t.rainbow : ns == 2 ? t.two_tone : t.monotone) += w[i];
    if (nr == 2) t.paired += w[i];
    if (nr == 1) t.trips += w[i];
    t.high += w[i] * (card_rank(c[0]) + 2);
    sum += w[i];
  }
  for (double* x : {&t.rainbow, &t.two_tone, &t.monotone, &t.paired, &t.trips}) *x *= 100.0 / sum;
  t.high /= sum;
  return t;
}
std::string texture_str(const Texture& t) {
  char buf[200];
  std::snprintf(buf, sizeof buf, "rainbow %.1f%% two-tone %.1f%% monotone %.1f%%, paired %.1f%% trips %.2f%%, mean top card %.2f",
                t.rainbow, t.two_tone, t.monotone, t.paired, t.trips, t.high);
  return buf;
}

std::string file_text(const std::vector<FlopClass>& fc, const Subset& s) {
  // Highest flop first; weights as shares of 100.
  std::vector<size_t> order(s.flops.size());
  std::iota(order.begin(), order.end(), size_t{0});
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return s.flops[a] < s.flops[b]; });
  std::ostringstream o;
  char buf[64];
  for (size_t i : order) {
    std::snprintf(buf, sizeof buf, "%s:%.4f\n", flop_str(fc[s.flops[i]].cards).c_str(), 100.0 * s.weight[i]);
    o << buf;
  }
  return o.str();
}


// Reads a flop file; exits on a bad one.
void read_file(const char* path, const std::vector<FlopClass>& fc, std::vector<int>& S, std::vector<double>& w) {
  std::ifstream in(path);
  std::string bad;
  if (!in || !parse_flop_file(in, fc, S, w, bad) || S.empty()) {
    std::fprintf(stderr, "%s: %s\n", path, !in ? "cannot open" : bad.empty() ? "no flops" : ("bad line: " + bad).c_str());
    std::exit(1);
  }
}

}  // namespace

int main(int argc, char** argv) {
  init_cards();
  if (argc < 2 || (argc < 3 && std::strcmp(argv[1], "deck"))) {
    std::fprintf(stderr, "usage: flop_subsets make N [N ...] [--dir DIR] | eval FILE ... | equity FILE A B ... |\n"
                         "       random N COUNT [SEED] | sample N SEED [solved] | curve MAX | deck\n");
    return 2;
  }
  const std::string cmd = argv[1];
  const std::vector<FlopClass> fc = flop_classes();
  const int n = static_cast<int>(fc.size());
  if (cmd == "equity") {
    // No tables of the whole deck needed: the file's flops on the river, and the exact preflop table.
    std::vector<int> S;
    std::vector<double> w;
    read_file(argv[2], fc, S, w);
    const std::vector<double> e = weighted_table(fc, S, w, 2), exact = preflop_equity_sums(), pairs = class_pairs();
    for (int i = 3; i + 1 < argc; i += 2) {
      // B may be a range of classes ("QQ,JTs,72o"): A against all of them, each hand of B weighing the same
      const int a = preflop_class_of_name(argv[i]);
      double won = 0, of = 0, xwon = 0, xof = 0;
      std::istringstream bs(argv[i + 1]);
      std::string name;
      bool ok = a >= 0;
      while (ok && std::getline(bs, name, ',')) {
        const int b = preflop_class_of_name(name);
        if (b < 0) {
          ok = false;
          break;
        }
        const size_t ab = static_cast<size_t>(a) * NCLS + b, ba = static_cast<size_t>(b) * NCLS + a;
        won += e[ab], of += e[ab] + e[ba], xwon += exact[ab], xof += pairs[ab];
      }
      if (!ok || !(of > 0)) {
        std::fprintf(stderr, "bad classes %s %s\n", argv[i], argv[i + 1]);
        return 1;
      }
      std::printf("%s %s %.9f %.9f\n", argv[i], argv[i + 1], won / of, xwon / xof);
    }
    return 0;
  }
  const Grams gr = gram(fc);
  const std::vector<double>& G = gr.all;
  auto streets = [&](const std::vector<int>& S, const std::vector<double>& w) {
    char buf[120];
    std::snprintf(buf, sizeof buf, "flop %.5f turn %.5f river %.5f", std::sqrt(error_of(gr.street[0], n, S, w)),
                  std::sqrt(error_of(gr.street[1], n, S, w)), std::sqrt(error_of(gr.street[2], n, S, w)));
    return std::string(buf);
  };
  // RMS over the 1,326 hands of the error of a hand's share against all hands, per street
  auto hands = [&](const std::vector<int>& S, const std::vector<double>& w) {
    char buf[160];
    double e[STREETS];
    for (int s = 0; s < STREETS; ++s)
      e[s] = rms_hands(against_all(weighted_table(fc, S, w, s)), against_all(gr.deck.ahead[s]), gr.deck.combos);
    std::snprintf(buf, sizeof buf, "against all hands: flop %.5f turn %.5f river %.5f", e[0], e[1], e[2]);
    return std::string(buf);
  };
  if (cmd == "make") {
    std::string dir;
    std::vector<int> sizes;
    for (int i = 2; i < argc; ++i) {
      if (!std::strcmp(argv[i], "--dir") && i + 1 < argc) dir = argv[++i];
      else sizes.push_back(std::atoi(argv[i]));
    }
    for (int size : sizes) {
      if (size < 1 || size > n) {
        std::fprintf(stderr, "size %d: 1..%d\n", size, n);
        return 2;
      }
      int swaps = 0;
      double greedy = 0;
      const Subset s = choose(G, n, size, [&](int k, double e) {
        if (k < 0) ++swaps;
        else greedy = e;
      });
      std::fprintf(stderr, "%d flops: RMS error %s (greedy total %.5f, then %d swaps: %.5f)\n   %s\n   %s\n",
                   static_cast<int>(s.flops.size()), streets(s.flops, s.weight).c_str(), std::sqrt(greedy), swaps,
                   std::sqrt(s.err2), hands(s.flops, s.weight).c_str(), texture_str(texture(fc, s.flops, s.weight)).c_str());
      // Lines of flop:weight and nothing else, the format other tools read too; what the file is, is in
      // flops/README.md.
      const std::string text = file_text(fc, s);
      if (dir.empty()) std::fputs(text.c_str(), stdout);
      else std::ofstream(dir + "/wsolver_" + std::to_string(size) + ".txt") << text;
    }
    return 0;
  }
  if (cmd == "eval") {
    for (int i = 2; i < argc; ++i) {
      std::vector<int> S;
      std::vector<double> w;
      read_file(argv[i], fc, S, w);
      const double own = error_of(G, n, S, w);
      const Subset best = fit(G, n, S);
      std::printf("%s: %zu flops, RMS error %.5f with its weights (%s), %.5f with weights re-solved\n   %s\n   %s\n", argv[i],
                  S.size(), std::sqrt(own), streets(S, w).c_str(), std::sqrt(best.err2), hands(S, w).c_str(),
                  texture_str(texture(fc, S, w)).c_str());
    }
    return 0;
  }
  if (cmd == "random" && argc >= 4) {
    const int size = std::atoi(argv[2]), count = std::atoi(argv[3]);
    std::mt19937_64 rng(argc > 4 ? std::strtoull(argv[4], nullptr, 10) : 1);
    // flops drawn like the deck deals them: a class in proportion to its count
    std::vector<double> cw;
    for (const auto& f : fc) cw.push_back(f.count);
    std::discrete_distribution<int> pick(cw.begin(), cw.end());
    std::vector<double> natural, solved;
    for (int c = 0; c < count; ++c) {
      std::vector<int> S;
      while (static_cast<int>(S.size()) < size) {
        const int f = pick(rng);
        if (std::find(S.begin(), S.end(), f) == S.end()) S.push_back(f);
      }
      std::vector<double> w;
      for (int f : S) w.push_back(fc[f].count);
      natural.push_back(std::sqrt(error_of(G, n, S, w)));
      solved.push_back(std::sqrt(fit(G, n, S).err2));
    }
    std::sort(natural.begin(), natural.end());
    std::sort(solved.begin(), solved.end());
    std::printf("%d random %d-flop subsets, RMS equity error: class-count weights min %.5f median %.5f max %.5f; "
                "weights re-solved min %.5f median %.5f max %.5f\n",
                count, size, natural.front(), natural[count / 2], natural.back(), solved.front(), solved[count / 2],
                solved.back());
    return 0;
  }
  if (cmd == "sample" && argc >= 4) {  // sample N SEED [solved]: one random subset as a flop file
    const int size = std::atoi(argv[2]);
    std::mt19937_64 rng(std::strtoull(argv[3], nullptr, 10));
    std::vector<double> cw;
    for (const auto& f : fc) cw.push_back(f.count);
    std::discrete_distribution<int> pick(cw.begin(), cw.end());
    Subset s;
    while (static_cast<int>(s.flops.size()) < size) {
      const int f = pick(rng);
      if (std::find(s.flops.begin(), s.flops.end(), f) == s.flops.end()) s.flops.push_back(f);
    }
    double sum = 0;
    for (int f : s.flops) sum += fc[f].count;
    for (int f : s.flops) s.weight.push_back(fc[f].count / sum);
    if (argc > 4 && !std::strcmp(argv[4], "solved")) s = fit(G, n, s.flops);
    std::printf("# %d random flops, seed %s, %s weights\n%s", size, argv[3], argc > 4 ? "solved" : "class-count",
                file_text(fc, s).c_str());
    return 0;
  }
  if (cmd == "deck") {  // the whole deck's texture, for comparison
    std::vector<int> S(n);
    std::iota(S.begin(), S.end(), 0);
    std::vector<double> w;
    for (const auto& f : fc) w.push_back(f.count);
    std::printf("all %d classes: %s\n   %s\n", n, streets(S, w).c_str(), texture_str(texture(fc, S, w)).c_str());
    return 0;
  }
  if (cmd == "curve") {
    choose(G, n, std::atoi(argv[2]), [&](int k, double e) {
      if (k > 0) std::printf("%d %.6f\n", k, std::sqrt(e));
    });
    return 0;
  }
  std::fprintf(stderr, "unknown command %s\n", cmd.c_str());
  return 2;
}
