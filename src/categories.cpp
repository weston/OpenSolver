// PioSOLVER 3.10-compatible hand classification for the Range Explorer.
//
// Reverse-engineered rules (verified byte-for-byte against tests/data/pio_categories):
//
// Made-hand categories (line 1 of show_categories):
//   If the best 5-card hand uses no hole card (only possible when the board plays
//   as a straight/flush/straight flush), the combo is "nothing" (0). Otherwise:
//   - straight -> 13, flush -> 14, straight flush -> 18 (a hole card must be part
//     of the actual 5 cards; for a straight, a hole rank in the window suffices).
//   - quads: hole pair matching the quad rank -> 17 (quads); else kicker class.
//   - full house: any hole rank among the eval trips/pair ranks -> 15 (fullhouse);
//     16 (top_fullhouse) is never emitted in this data set... see below.
//   - trips: pocket pair (2 board + pair) -> 12 (set); single hole card matching
//     the trips rank -> 11 (trips); else kicker class.
//   - two pair: without a paired board, both hole cards paired -> 10. On a paired
//     board, a hole card pairing a board rank is classified by the board position
//     of the highest such eval pair rank (7/5/4/3 like one-pair rules). A pocket
//     pair equal to an eval pair rank uses overcard counting, else kicker class.
//   - one pair: pocket pair -> overcard count vs board ranks (0->overpair 9,
//     1->underpair 6, 2->3rd-pair 4, >=3->low_pair 3); pair with a board card ->
//     board position (top->top_pair 7, second->2nd-pair 5, third->3rd-pair 4,
//     lower->low_pair 3); no pair -> kicker class (ace in hand -> 2 ace_high,
//     king -> 1 king_high, else 0 nothing). top_pair_tp (8) is never emitted.
//   Kicker class: A in hole -> 2, else K in hole -> 1, else 0.
//
// Draw categories (line 2 of show_categories; 0=none on rivers):
//   flush draw = 5 (4 of a suit incl. >=1 hole card that suit); combo_draw=6 when
//   a flush draw AND >=4 straight outs; 8out_straight_draw=4 (>=8 straight outs),
//   4out_straight_draw=3 (>=4). Flops: bdfd_1card=1 (3 of a suit, exactly 1 hole
//   card that suit), bdfd_2card=2 (3 of a suit, both hole cards that suit).
//   Straight outs: for each rank r with <4 seen cards, let the drawn card have
//   rank r; r counts (4 - seen[r] times) iff the BEST straight then available
//   (highest 5-window, wheel = 5-high) contains some rank that is neither on the
//   board nor r itself (i.e. the straight necessarily uses a hole card). If the
//   hand already is a straight or better (eval category >= 4), straight outs = 0.
//
// show_cats lines: eval = (evaluate()>>20)<<24 | (evaluate()&0xFFFFF), 0 if blocked;
//   line2 = made*100 (flop/turn) or made (river), -1 if blocked (flop/turn), 0 (river);
//   outs lines (flop/turn only): partition of the unseen cards by the new hand
//   category after adding the card: straight (cat 4, old < 4), flush/SF (5/8),
//   fullhouse/quads (6/7); a card counts only if the new category beats the old.
//
// show_cats_pp: "B1 B2 B3 " header (trailing space), then 1326 lines
//   "<combo> - <made name ljust 9> - <tot ljust 2> <str ljust 2> <fl ljust 2> <fh ljust 2>".

#include "categories.hpp"

#include <algorithm>
#include <cstring>

#include "cards.hpp"
#include "eval.hpp"

namespace pio {

namespace {

const char* kMadeNames[] = {
    "nothing",    "king_high",  "ace_high",   "low_pair", "3rd-pair",
    "2nd-pair",   "underpair",  "top_pair",   "top_pair_tp", "overpair",
    "two_pair",   "trips",      "set",        "straight", "flush",
    "fullhouse",  "top_fullhouse", "quads",   "straight_flush"};
const char* kDrawNames[] = {"no_draw",          "bdfd_1card", "bdfd_2card",
                            "4out_straight_draw", "8out_straight_draw",
                            "flush_draw",       "combo_draw"};

// Highest rank (0..12) of a straight inside a 13-bit rank mask, or -1 (wheel -> 3).
int straight_top(uint32_t m) {
  uint32_t x = (m << 1) | ((m >> 12) & 1u);
  uint32_t r = x & (x >> 1) & (x >> 2) & (x >> 3) & (x >> 4);
  if (!r) return -1;
  return (31 - __builtin_clz(r)) + 3;
}

// The 5 ranks of the straight with top `st` (st==3 -> wheel).
void straight_ranks(int st, int out[5]) {
  if (st == 3) {
    out[0] = 12; out[1] = 0; out[2] = 1; out[3] = 2; out[4] = 3;
  } else {
    for (int k = 0; k < 5; ++k) out[k] = st - k;
  }
}

struct BoardInfo {
  int n;                    // 3, 4 or 5
  int cards[5];
  uint64_t mask;            // 52-bit
  uint8_t rank_cnt[13];
  uint32_t rank_mask;       // 13-bit
  int suit_cnt[4];
  int drs[13];              // distinct board ranks, descending
  int ndrs;
};

BoardInfo make_board_info(const std::vector<int>& board) {
  BoardInfo b;
  b.n = static_cast<int>(board.size());
  b.mask = 0;
  std::memset(b.rank_cnt, 0, sizeof(b.rank_cnt));
  b.rank_mask = 0;
  std::memset(b.suit_cnt, 0, sizeof(b.suit_cnt));
  for (int i = 0; i < b.n; ++i) {
    int c = board[i];
    b.cards[i] = c;
    b.mask |= card_bit(c);
    ++b.rank_cnt[card_rank(c)];
    b.rank_mask |= 1u << card_rank(c);
    ++b.suit_cnt[card_suit(c)];
  }
  b.ndrs = 0;
  for (int r = 12; r >= 0; --r)
    if (b.rank_cnt[r]) b.drs[b.ndrs++] = r;
  return b;
}

int kicker_class(int r1, int r2) {
  if (r1 == 12 || r2 == 12) return 2;
  if (r1 == 11 || r2 == 11) return 1;
  return 0;
}

// Pocket pair classified by the number of distinct board ranks above it.
int overcard_pocket(const BoardInfo& b, int pr) {
  int oc = 0;
  for (int i = 0; i < b.ndrs; ++i)
    if (b.drs[i] > pr) ++oc;
  if (oc == 0) return 9;
  if (oc == 1) return 6;
  if (oc == 2) return 4;
  return 3;
}

// Pair with a board card classified by the paired rank's board position.
int board_pos_class(const BoardInfo& b, int p) {
  for (int i = 0; i < b.ndrs; ++i)
    if (b.drs[i] == p) {
      if (i == 0) return 7;
      if (i == 1) return 5;
      if (i == 2) return 4;
      return 3;
    }
  return 3;
}

int classify_made(const BoardInfo& b, int c1, int c2, uint32_t u) {
  const int r1 = card_rank(c1), r2 = card_rank(c2);
  const int cat = strength_category(u);
  if (cat == HC_STRAIGHT || cat == HC_FLUSH || cat == HC_STRAIGHT_FLUSH) {
    bool uses = false;
    if (cat == HC_STRAIGHT) {
      int st = straight_top(b.rank_mask | (1u << r1) | (1u << r2));
      int sset[5];
      straight_ranks(st, sset);
      for (int k = 0; k < 5; ++k)
        if (sset[k] == r1 || sset[k] == r2) uses = true;
    } else {
      int cards[7];
      cards[0] = c1;
      cards[1] = c2;
      for (int i = 0; i < b.n; ++i) cards[2 + i] = b.cards[i];
      for (int s = 0; s < 4 && !uses; ++s) {
        int tot = b.suit_cnt[s] + (card_suit(c1) == s) + (card_suit(c2) == s);
        if (tot < 5) continue;
        uint32_t sm = 0;
        for (int i = 0; i < b.n + 2; ++i)
          if (card_suit(cards[i]) == s) sm |= 1u << card_rank(cards[i]);
        int st = straight_top(sm);
        if (cat == HC_STRAIGHT_FLUSH && st < 0) continue;
        int top5[5];
        if (cat == HC_STRAIGHT_FLUSH) {
          straight_ranks(st, top5);
        } else {
          uint32_t m = sm;
          for (int k = 0; k < 5; ++k) {
            int r = 31 - __builtin_clz(m);
            top5[k] = r;
            m &= ~(1u << r);
          }
        }
        for (int cc : {c1, c2}) {
          if (card_suit(cc) != s) continue;
          int rc = card_rank(cc);
          for (int k = 0; k < 5; ++k)
            if (top5[k] == rc) uses = true;
        }
      }
    }
    if (!uses) return 0;
    if (cat == HC_STRAIGHT) return 13;
    if (cat == HC_FLUSH) return 14;
    return 18;
  }
  if (cat == HC_QUADS) {
    int q = (u >> 16) & 0xF;
    if (r1 == q || r2 == q) return 17;
    return kicker_class(r1, r2);
  }
  if (cat == HC_FULL_HOUSE) {
    int t = (u >> 16) & 0xF, p = (u >> 12) & 0xF;
    if (r1 == t || r2 == t || r1 == p || r2 == p) return 15;
    return kicker_class(r1, r2);
  }
  if (cat == HC_TRIPS) {
    int t = (u >> 16) & 0xF;
    if (r1 == r2) return 12;
    if (r1 == t || r2 == t) return 11;
    return kicker_class(r1, r2);
  }
  if (cat == HC_TWO_PAIR) {
    int p1 = (u >> 16) & 0xF, p2 = (u >> 12) & 0xF;
    if (r1 == r2) {
      if (r1 == p1 || r1 == p2) return overcard_pocket(b, r1);
      return kicker_class(r1, r2);
    }
    bool h1 = (r1 == p1 || r1 == p2), h2 = (r2 == p1 || r2 == p2);
    bool board_pair = false;
    for (int r = 0; r < 13; ++r)
      if (b.rank_cnt[r] >= 2) board_pair = true;
    if (!board_pair) return (h1 && h2) ? 10 : kicker_class(r1, r2);
    // Board already paired: board position of the top hole-paired eval pair rank.
    int hp = -1;
    if (h1) hp = r1;
    if (h2 && r2 > hp) hp = r2;
    if (hp >= 0) return board_pos_class(b, hp);
    return kicker_class(r1, r2);
  }
  if (cat == HC_PAIR) {
    int p = (u >> 16) & 0xF;
    if (r1 == r2) return overcard_pocket(b, r1);
    if (r1 == p || r2 == p) return board_pos_class(b, p);
    return kicker_class(r1, r2);
  }
  return kicker_class(r1, r2);
}

// Unseen-card equivalents (as ranks) completing a straight that necessarily
// uses a hole card: the best straight after adding a card of rank r must
// contain a rank that is neither on the board nor r itself.
int straight_raw_outs(const BoardInfo& b, int c1, int c2) {
  const int r1 = card_rank(c1), r2 = card_rank(c2);
  const uint32_t rm = b.rank_mask | (1u << r1) | (1u << r2);
  uint8_t seen[13];
  std::memcpy(seen, b.rank_cnt, 13);
  ++seen[r1];
  ++seen[r2];
  int outs = 0;
  for (int r = 0; r < 13; ++r) {
    if (seen[r] == 4) continue;
    int st = straight_top(rm | (1u << r));
    if (st < 0) continue;
    int sset[5];
    straight_ranks(st, sset);
    bool needs_hole = false;
    for (int k = 0; k < 5; ++k)
      if (sset[k] != r && !(b.rank_mask & (1u << sset[k]))) needs_hole = true;
    if (needs_hole) outs += 4 - seen[r];
  }
  return outs;
}

int classify_draw(const BoardInfo& b, int c1, int c2, uint32_t u) {
  if (b.n == 5) return 0;
  int hs[4] = {0, 0, 0, 0};
  ++hs[card_suit(c1)];
  ++hs[card_suit(c2)];
  bool fd = false;
  for (int s = 0; s < 4; ++s)
    if (b.suit_cnt[s] + hs[s] == 4 && hs[s] >= 1) fd = true;
  int souts = strength_category(u) < HC_STRAIGHT ? straight_raw_outs(b, c1, c2) : 0;
  if (fd && souts >= 4) return 6;
  if (fd) return 5;
  if (souts >= 8) return 4;
  if (souts >= 4) return 3;
  if (b.n == 3) {
    bool b1 = false, b2 = false;
    for (int s = 0; s < 4; ++s) {
      if (b.suit_cnt[s] + hs[s] == 3 && hs[s] == 1) b1 = true;
      if (b.suit_cnt[s] + hs[s] == 3 && hs[s] == 2) b2 = true;
    }
    if (b1) return 1;
    if (b2) return 2;
  }
  return 0;
}

// Partition of unseen cards by improved hand category: [tot, str, fl, fh].
void calc_outs(const BoardInfo& b, int c1, int c2, uint32_t u, int out[4]) {
  out[0] = out[1] = out[2] = out[3] = 0;
  if (b.n == 5) return;
  const int oldcat = strength_category(u);
  const uint64_t seen = b.mask | card_bit(c1) | card_bit(c2);
  int cards[7];
  cards[0] = c1;
  cards[1] = c2;
  for (int i = 0; i < b.n; ++i) cards[2 + i] = b.cards[i];
  for (int c = 0; c < 52; ++c) {
    if (seen & card_bit(c)) continue;
    cards[b.n + 2] = c;
    int nc = strength_category(evaluate(cards, b.n + 3));
    if (nc == HC_STRAIGHT && oldcat < HC_STRAIGHT) {
      ++out[1];
    } else if ((nc == HC_FLUSH || nc == HC_STRAIGHT_FLUSH) && oldcat < nc) {
      ++out[2];
    } else if ((nc == HC_FULL_HOUSE || nc == HC_QUADS) && oldcat < nc) {
      ++out[3];
    }
  }
  out[0] = out[1] + out[2] + out[3];
}

template <typename F>
std::string join_ints(const std::array<ComboCats, 1326>& cats, F field) {
  std::string s;
  s.reserve(1326 * 8);
  for (int ci = 0; ci < 1326; ++ci) {
    s += std::to_string(field(cats[ci]));
    s += ' ';
  }
  return s;
}

std::string ljust(const std::string& s, size_t w) {
  return s.size() >= w ? s : s + std::string(w - s.size(), ' ');
}

}  // namespace

const std::vector<std::string>& made_category_names() {
  static const std::vector<std::string> v(std::begin(kMadeNames), std::end(kMadeNames));
  return v;
}

const std::vector<std::string>& draw_category_names() {
  static const std::vector<std::string> v(std::begin(kDrawNames), std::end(kDrawNames));
  return v;
}

std::array<ComboCats, 1326> classify_board(const std::vector<int>& board) {
  std::array<ComboCats, 1326> out;
  const BoardInfo b = make_board_info(board);
  for (int ci = 0; ci < 1326; ++ci) {
    ComboCats& cc = out[ci];
    const int hi = COMBOS[ci].hi, lo = COMBOS[ci].lo;
    if (b.mask & combo_mask(ci)) {
      cc.made = 0;
      cc.draw = b.n < 5 ? -1 : 0;
      cc.eval = 0;
      cc.line2 = b.n < 5 ? -1 : 0;
      cc.total_outs = cc.straight_outs = cc.flush_outs = cc.fullhouse_outs = 0;
      continue;
    }
    int cards[7];
    cards[0] = hi;
    cards[1] = lo;
    for (int i = 0; i < b.n; ++i) cards[2 + i] = b.cards[i];
    const uint32_t u = evaluate(cards, b.n + 2);
    cc.made = classify_made(b, hi, lo, u);
    cc.draw = classify_draw(b, hi, lo, u);
    cc.eval = (static_cast<uint32_t>(u >> 20) << 24) | (u & 0xFFFFFu);
    cc.line2 = b.n == 5 ? cc.made : cc.made * 100;
    int o[4];
    calc_outs(b, hi, lo, u, o);
    cc.total_outs = o[0];
    cc.straight_outs = o[1];
    cc.flush_outs = o[2];
    cc.fullhouse_outs = o[3];
  }
  return out;
}

std::string show_category_names_text() {
  std::string s;
  for (const char* n : kMadeNames) {
    s += n;
    s += ' ';
  }
  s += '\n';
  for (const char* n : kDrawNames) {
    s += n;
    s += ' ';
  }
  return s;
}

std::string show_categories_text(const std::vector<int>& board) {
  const auto cats = classify_board(board);
  std::string s = join_ints(cats, [](const ComboCats& c) { return c.made; });
  s += '\n';
  s += join_ints(cats, [](const ComboCats& c) { return c.draw; });
  return s;
}

std::string show_cats_text(const std::vector<int>& board) {
  const auto cats = classify_board(board);
  std::string s = join_ints(cats, [](const ComboCats& c) { return c.eval; });
  s += '\n';
  s += join_ints(cats, [](const ComboCats& c) { return c.line2; });
  if (board.size() < 5) {
    s += '\n';
    s += join_ints(cats, [](const ComboCats& c) { return c.total_outs; });
    s += '\n';
    s += join_ints(cats, [](const ComboCats& c) { return c.straight_outs; });
    s += '\n';
    s += join_ints(cats, [](const ComboCats& c) { return c.flush_outs; });
    s += '\n';
    s += join_ints(cats, [](const ComboCats& c) { return c.fullhouse_outs; });
  }
  return s;
}

std::string show_cats_pp_text(const std::vector<int>& board) {
  const auto cats = classify_board(board);
  std::string s;
  for (int c : board) {
    s += card_str(c);
    s += ' ';
  }
  for (int ci = 0; ci < 1326; ++ci) {
    const ComboCats& cc = cats[ci];
    s += '\n';
    s += combo_str(ci);
    s += " - ";
    s += ljust(kMadeNames[cc.made], 9);
    s += " - ";
    s += ljust(std::to_string(cc.total_outs), 2);
    s += ' ';
    s += ljust(std::to_string(cc.straight_outs), 2);
    s += ' ';
    s += ljust(std::to_string(cc.flush_outs), 2);
    s += ' ';
    s += ljust(std::to_string(cc.fullhouse_outs), 2);
  }
  return s;
}

}  // namespace pio
