// Cards, combos (PioSOLVER hand order) and suit permutations.
#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace pio {

constexpr int NUM_CARDS = 52;
constexpr int NUM_COMBOS = 1326;
constexpr int NUM_SUIT_PERMS = 24;

// card index = rank * 4 + suit; rank 0 = '2' ... 12 = 'A'; suit 0=c 1=d 2=h 3=s
inline int card_rank(int c) { return c >> 2; }
inline int card_suit(int c) { return c & 3; }
inline int make_card(int rank, int suit) { return rank * 4 + suit; }

extern const char RANK_CHARS[14];
extern const char SUIT_CHARS[5];

int parse_rank(char ch);  // -1 if invalid
int parse_suit(char ch);  // -1 if invalid
int parse_card(std::string_view s);  // "Ah" -> index; -1 if invalid
std::string card_str(int c);
// Accepts "AcKdJc" or "Ac Kd Jc" (or comma separated). Returns false on error or duplicates.
bool parse_cards(std::string_view s, std::vector<int>& out);
std::string cards_str(const std::vector<int>& cards, const char* sep = " ");

// A combo: two distinct cards, hi > lo by card index.
struct Combo {
  uint8_t hi, lo;
};

// PioSOLVER hand order: for hi in 1..51, for lo in 0..hi-1 -> "2d2c 2h2c 2h2d 2s2c ..."
extern std::array<Combo, NUM_COMBOS> COMBOS;
extern int16_t COMBO_INDEX[NUM_CARDS][NUM_CARDS];  // symmetric; -1 on diagonal

inline int combo_index(int a, int b) { return COMBO_INDEX[a][b]; }
inline uint64_t card_bit(int c) { return 1ULL << c; }
inline uint64_t combo_mask(int ci) { return card_bit(COMBOS[ci].hi) | card_bit(COMBOS[ci].lo); }
std::string combo_str(int ci);  // e.g. "AdAc" (higher card index first, Pio style)
int parse_combo(std::string_view s);  // "AhKd" / "KdAh" -> combo index, -1 if invalid

// Suit permutations. SUIT_PERMS[p][s] = image of suit s.
extern std::array<std::array<uint8_t, 4>, NUM_SUIT_PERMS> SUIT_PERMS;
extern uint8_t PERM_CARD[NUM_SUIT_PERMS][NUM_CARDS];
extern int16_t PERM_COMBO[NUM_SUIT_PERMS][NUM_COMBOS];
extern uint8_t PERM_INV[NUM_SUIT_PERMS];
extern uint8_t PERM_COMPOSE[NUM_SUIT_PERMS][NUM_SUIT_PERMS];  // [a][b] = a after b (apply b first)
constexpr int IDENTITY_PERM = 0;
int find_perm(const std::array<uint8_t, 4>& images);  // -1 if not a permutation

uint64_t board_mask(const std::vector<int>& board);

// Must be called once at startup.
void init_cards();

}  // namespace pio
