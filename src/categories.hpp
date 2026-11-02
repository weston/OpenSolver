// Range Explorer hand classification (PioSOLVER UPI show_categories / show_cats).
// Reproduces PioSOLVER 3.10 outputs byte-for-byte (see tests/data/pio_categories).
#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace pio {

const std::vector<std::string>& made_category_names();  // index = category id
const std::vector<std::string>& draw_category_names();

struct ComboCats {
  int made;            // index into made_category_names(); 0 for blocked combos
  int draw;            // index into draw_category_names(); -1 for blocked combos
                       // on flop/turn boards, 0 for blocked combos on rivers
  uint32_t eval;       // Pio eval: (category << 24) | (evaluate() & 0xFFFFF); 0 if blocked
  int line2;           // second line of show_cats: made*100 (flop/turn), made id (river);
                       // -1 for blocked combos on flop/turn, 0 on rivers
  int total_outs;      // straight_outs + flush_outs + fullhouse_outs (0 on rivers)
  int straight_outs;   // unseen cards after which the hand is a straight (wasn't before)
  int flush_outs;      // unseen cards improving to flush / straight flush
  int fullhouse_outs;  // unseen cards improving to full house / quads
};

// board: 3, 4 or 5 cards (card index = rank*4+suit).
std::array<ComboCats, 1326> classify_board(const std::vector<int>& board);

// Output text for the UPI commands (no trailing newline, no END marker):
std::string show_category_names_text();           // 2 lines: made names, then draw names
std::string show_categories_text(const std::vector<int>& board);  // 2 lines: made ids, draw ids
std::string show_cats_text(const std::vector<int>& board);  // 6 lines (flop/turn) or 2 lines (river)
std::string show_cats_pp_text(const std::vector<int>& board);  // header line + 1326 combo lines

}  // namespace pio
