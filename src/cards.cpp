#include "cards.hpp"

#include <algorithm>
#include <cctype>

namespace pio {

const char RANK_CHARS[14] = "23456789TJQKA";
const char SUIT_CHARS[5] = "cdhs";

std::array<Combo, NUM_COMBOS> COMBOS;
int16_t COMBO_INDEX[NUM_CARDS][NUM_CARDS];
std::array<std::array<uint8_t, 4>, NUM_SUIT_PERMS> SUIT_PERMS;
uint8_t PERM_CARD[NUM_SUIT_PERMS][NUM_CARDS];
int16_t PERM_COMBO[NUM_SUIT_PERMS][NUM_COMBOS];
uint8_t PERM_INV[NUM_SUIT_PERMS];
uint8_t PERM_COMPOSE[NUM_SUIT_PERMS][NUM_SUIT_PERMS];

int parse_rank(char ch) {
  switch (std::toupper(static_cast<unsigned char>(ch))) {
    case '2': return 0;
    case '3': return 1;
    case '4': return 2;
    case '5': return 3;
    case '6': return 4;
    case '7': return 5;
    case '8': return 6;
    case '9': return 7;
    case 'T': return 8;
    case 'J': return 9;
    case 'Q': return 10;
    case 'K': return 11;
    case 'A': return 12;
    default: return -1;
  }
}

int parse_suit(char ch) {
  switch (std::tolower(static_cast<unsigned char>(ch))) {
    case 'c': return 0;
    case 'd': return 1;
    case 'h': return 2;
    case 's': return 3;
    default: return -1;
  }
}

int parse_card(std::string_view s) {
  if (s.size() != 2) return -1;
  int r = parse_rank(s[0]), su = parse_suit(s[1]);
  if (r < 0 || su < 0) return -1;
  return make_card(r, su);
}

std::string card_str(int c) {
  std::string s;
  s += RANK_CHARS[card_rank(c)];
  s += SUIT_CHARS[card_suit(c)];
  return s;
}

bool parse_cards(std::string_view s, std::vector<int>& out) {
  out.clear();
  std::string compact;
  for (char ch : s)
    if (!std::isspace(static_cast<unsigned char>(ch)) && ch != ',') compact += ch;
  if (compact.size() % 2 != 0) return false;
  uint64_t seen = 0;
  for (size_t i = 0; i < compact.size(); i += 2) {
    int c = parse_card(std::string_view(compact).substr(i, 2));
    if (c < 0 || (seen & card_bit(c))) return false;
    seen |= card_bit(c);
    out.push_back(c);
  }
  return true;
}

std::string cards_str(const std::vector<int>& cards, const char* sep) {
  std::string s;
  for (size_t i = 0; i < cards.size(); ++i) {
    if (i) s += sep;
    s += card_str(cards[i]);
  }
  return s;
}

std::string combo_str(int ci) { return card_str(COMBOS[ci].hi) + card_str(COMBOS[ci].lo); }

int parse_combo(std::string_view s) {
  if (s.size() != 4) return -1;
  int a = parse_card(s.substr(0, 2)), b = parse_card(s.substr(2, 2));
  if (a < 0 || b < 0 || a == b) return -1;
  return combo_index(a, b);
}

int find_perm(const std::array<uint8_t, 4>& images) {
  for (int p = 0; p < NUM_SUIT_PERMS; ++p)
    if (SUIT_PERMS[p] == images) return p;
  return -1;
}

uint64_t board_mask(const std::vector<int>& board) {
  uint64_t m = 0;
  for (int c : board) m |= card_bit(c);
  return m;
}

void init_cards() {
  static bool done = false;
  if (done) return;
  done = true;
  for (int a = 0; a < NUM_CARDS; ++a)
    for (int b = 0; b < NUM_CARDS; ++b) COMBO_INDEX[a][b] = -1;
  int idx = 0;
  for (int hi = 1; hi < NUM_CARDS; ++hi)
    for (int lo = 0; lo < hi; ++lo) {
      COMBOS[idx] = {static_cast<uint8_t>(hi), static_cast<uint8_t>(lo)};
      COMBO_INDEX[hi][lo] = COMBO_INDEX[lo][hi] = static_cast<int16_t>(idx);
      ++idx;
    }
  // Suit permutations, identity first.
  std::array<uint8_t, 4> p = {0, 1, 2, 3};
  int n = 0;
  do {
    SUIT_PERMS[n++] = p;
  } while (std::next_permutation(p.begin(), p.end()));
  for (int k = 0; k < NUM_SUIT_PERMS; ++k) {
    for (int c = 0; c < NUM_CARDS; ++c)
      PERM_CARD[k][c] = static_cast<uint8_t>(make_card(card_rank(c), SUIT_PERMS[k][card_suit(c)]));
    for (int ci = 0; ci < NUM_COMBOS; ++ci)
      PERM_COMBO[k][ci] = static_cast<int16_t>(
          combo_index(PERM_CARD[k][COMBOS[ci].hi], PERM_CARD[k][COMBOS[ci].lo]));
  }
  for (int a = 0; a < NUM_SUIT_PERMS; ++a) {
    std::array<uint8_t, 4> inv{};
    for (int s = 0; s < 4; ++s) inv[SUIT_PERMS[a][s]] = static_cast<uint8_t>(s);
    PERM_INV[a] = static_cast<uint8_t>(find_perm(inv));
    for (int b = 0; b < NUM_SUIT_PERMS; ++b) {
      std::array<uint8_t, 4> comp{};
      for (int s = 0; s < 4; ++s) comp[s] = SUIT_PERMS[a][SUIT_PERMS[b][s]];
      PERM_COMPOSE[a][b] = static_cast<uint8_t>(find_perm(comp));
    }
  }
}

}  // namespace pio
