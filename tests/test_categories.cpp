// Byte-exact tests for the range explorer against captured PioSOLVER 3.10
// outputs in tests/data/pio_categories/. Run from the repository root:
//   clang++ -std=c++20 -O2 -Wall -Wextra -o build/test_categories \
//     tests/test_categories.cpp src/categories.cpp src/eval.cpp src/cards.cpp
//   ./build/test_categories
// Prints ALL PASSED and returns 0 only when every file matches exactly.
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../src/cards.hpp"
#include "../src/categories.hpp"

namespace {

const std::string kDataDir = "tests/data/pio_categories/";
int g_mismatches = 0;

std::string read_file(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    std::cout << "ERROR: cannot open " << path << "\n";
    ++g_mismatches;
    return "";
  }
  std::ostringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

// The reference files end with a single trailing newline; the show_* functions
// must return the same text without it.
std::string strip_trailing_newline(std::string s) {
  if (!s.empty() && s.back() == '\n') s.pop_back();
  return s;
}

std::vector<std::string> split_lines(const std::string& s) {
  std::vector<std::string> lines;
  size_t pos = 0;
  while (true) {
    size_t nl = s.find('\n', pos);
    if (nl == std::string::npos) {
      lines.push_back(s.substr(pos));
      break;
    }
    lines.push_back(s.substr(pos, nl - pos));
    pos = nl + 1;
  }
  return lines;
}

std::vector<std::string> split_tokens(const std::string& line) {
  std::vector<std::string> tok;
  std::istringstream is(line);
  std::string t;
  while (is >> t) tok.push_back(t);
  return tok;
}

// Compare one whitespace-separated column line token by token; the i-th token
// belongs to combo i (Pio hand order). Reports count and up to 5 examples.
void compare_column(const std::string& board, const std::string& label,
                    const std::string& pio_line, const std::string& our_line) {
  const std::vector<std::string> pv = split_tokens(pio_line);
  const std::vector<std::string> ov = split_tokens(our_line);
  if (pv.size() != ov.size()) {
    std::cout << board << " " << label << ": token count pio=" << pv.size()
              << " ours=" << ov.size() << "\n";
    ++g_mismatches;
    return;
  }
  int bad = 0;
  std::string examples;
  for (size_t i = 0; i < pv.size(); ++i) {
    if (pv[i] != ov[i]) {
      if (bad < 5) {
        examples += "\n    " + pio::combo_str(static_cast<int>(i)) + ": pio=" +
                    pv[i] + " ours=" + ov[i];
      }
      ++bad;
    }
  }
  if (bad) {
    std::cout << board << " " << label << ": " << bad << " mismatches" << examples
              << "\n";
    g_mismatches += bad;
  }
}

// Compare a full show_* output against the reference text: every line must be
// identical; column lines additionally get a per-token diagnosis.
void compare_text(const std::string& board, const std::string& kind,
                  const std::vector<std::string>& labels,
                  const std::string& pio_text, const std::string& our_text) {
  if (pio_text == our_text) return;
  const std::vector<std::string> pl = split_lines(pio_text);
  const std::vector<std::string> ol = split_lines(our_text);
  if (pl.size() != ol.size()) {
    std::cout << board << " " << kind << ": line count pio=" << pl.size()
              << " ours=" << ol.size() << "\n";
    ++g_mismatches;
  }
  const int before = g_mismatches;
  const size_t n = std::min(pl.size(), ol.size());
  for (size_t i = 0; i < n; ++i) {
    if (pl[i] == ol[i]) continue;
    const std::string label =
        i < labels.size() ? labels[i] : ("line" + std::to_string(i));
    compare_column(board, kind + "/" + label, pl[i], ol[i]);
  }
  // Whitespace/ordering differences with equal tokens still fail.
  if (g_mismatches == before && pl.size() == ol.size()) {
    ++g_mismatches;
    std::cout << board << " " << kind
              << ": text differs byte-wise (spacing/order only)\n";
  }
}

// Row-wise comparison for cats_pp (whole row strings, keyed by combo order).
void compare_pp(const std::string& board, const std::string& pio_text,
                const std::string& our_text) {
  if (pio_text == our_text) return;
  const std::vector<std::string> pl = split_lines(pio_text);
  const std::vector<std::string> ol = split_lines(our_text);
  if (pl.size() != ol.size()) {
    std::cout << board << " cats_pp: line count pio=" << pl.size()
              << " ours=" << ol.size() << "\n";
    ++g_mismatches;
  }
  int bad = 0;
  std::string examples;
  const size_t n = std::min(pl.size(), ol.size());
  for (size_t i = 0; i < n; ++i) {
    if (pl[i] == ol[i]) continue;
    if (bad < 5) {
      std::string tag =
          i == 0 ? "header" : pio::combo_str(static_cast<int>(i - 1));
      examples += "\n    " + tag + ": pio=[" + pl[i] + "] ours=[" + ol[i] + "]";
    }
    ++bad;
  }
  std::cout << board << " cats_pp: " << bad << " mismatched lines" << examples
            << "\n";
  g_mismatches += bad;
}

bool file_exists(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  return f.good();
}

}  // namespace

int main() {
  pio::init_cards();

  // Category names (board-independent).
  const std::string pio_names =
      strip_trailing_newline(read_file(kDataDir + "category_names.txt"));
  const std::string our_names = pio::show_category_names_text();
  if (pio_names != our_names) {
    std::cout << "category_names: MISMATCH\n  pio =[" << pio_names << "]\n  ours=["
              << our_names << "]\n";
    ++g_mismatches;
  }

  std::ifstream bf(kDataDir + "boards.txt");
  if (!bf) {
    std::cout << "ERROR: cannot open " << kDataDir << "boards.txt\n";
    return 1;
  }
  std::vector<std::string> boards;
  std::string line;
  while (bf >> line) boards.push_back(line);

  const std::vector<std::string> categories_labels = {"made", "draw"};
  const std::vector<std::string> cats_labels = {
      "eval", "made", "total_outs", "straight_outs", "flush_outs",
      "fullhouse_outs"};

  int boards_ok = 0;
  for (const std::string& b : boards) {
    std::vector<int> board;
    if (!pio::parse_cards(b, board)) {
      std::cout << "ERROR: cannot parse board " << b << "\n";
      ++g_mismatches;
      continue;
    }
    const int before = g_mismatches;
    compare_text(b, "categories", categories_labels,
                 strip_trailing_newline(read_file(kDataDir + b + ".categories.txt")),
                 pio::show_categories_text(board));
    compare_text(b, "cats", cats_labels,
                 strip_trailing_newline(read_file(kDataDir + b + ".cats.txt")),
                 pio::show_cats_text(board));
    if (file_exists(kDataDir + b + ".cats_pp.txt")) {
      compare_pp(b, strip_trailing_newline(read_file(kDataDir + b + ".cats_pp.txt")),
                 pio::show_cats_pp_text(board));
    }
    if (g_mismatches == before) ++boards_ok;
  }

  std::cout << "\n=== SUMMARY ===\n";
  std::cout << "boards: " << boards_ok << "/" << boards.size() << " clean\n";
  std::cout << "total mismatches: " << g_mismatches << "\n";
  if (g_mismatches == 0) {
    std::cout << "ALL PASSED\n";
    return 0;
  }
  std::cout << "FAILED\n";
  return 1;
}
