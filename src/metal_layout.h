enum NodeKind : unsigned { Decision, Deal, RiverShowdown, Fold, AllIn };
struct TraversalNode {
  unsigned child, count, kind, actor, storage, aux, card, deals, deal_count, row[2], value_row, child_value,
      public_cards;
  float chance_weight, win[2], loss[2], tie[2];
};
struct HandPrefixes {
  unsigned short lo[3], hi[3], end[3];
  short same;
};
struct BoardPrefixes {
  unsigned index_offset, hand_offset, start[54];
};
struct IterationParams {
  unsigned hands0, hands1, stride, p;
  float positive_discount, negative_discount, average_discount;
  unsigned accumulate, phase;
};

struct RiverRound {
  unsigned root, board, storage[4], final_raise;
  float win[3][2], loss[3][2], tie[3][2], fold[2][2];
};
