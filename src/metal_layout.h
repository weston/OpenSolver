enum NodeKind : unsigned { Decision, Deal, RiverShowdown, Fold, AllIn };
struct TraversalNode {
  unsigned child, count, kind, actor, storage, aux, card, deals, deal_count, reach_offset[2], value_row, child_value,
      public_cards;
  float chance_weight, win[2], loss[2], tie[2];
};
struct HandPrefixes {
  unsigned short lo[3], hi[3], live;
  short same;
};
struct PrefixEntry {
  unsigned short hand, slot;
};
enum : unsigned { empty_prefix = 0, total_prefix = 1, first_card_prefix = 2, first_rank_prefix = first_card_prefix + 52 };
struct BoardPrefixes {
  unsigned index_offset, hand_offset, start[54], prefix_count;
};
struct IterationParams {
  unsigned hands0, hands1, stride, p;
  float positive_discount, negative_discount, average_discount;
  unsigned accumulate, phase;
};

enum RiverRoundKind : unsigned { BetBothSides, FinalRaise, BetWithRaise };
struct RiverRound {
  unsigned root, board, storage[4];
  RiverRoundKind kind;
  float win[3][2], loss[3][2], tie[3][2], fold[2][2];
};

struct TwoBetRiver {
  unsigned root, board, storage[4];
  float win[4][2], loss[4][2], tie[4][2], fold[4][2];
};

struct AllinTable {
  unsigned word_offset, bytes_per_count;
  float scale;
};

struct AllinBatch {
  unsigned table, count, value_offset[4], reach_offset[2][4];
  float win_minus_loss[2][4], loss[2][4];
};
