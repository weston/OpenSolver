// Shared by the host, Metal (C++) and OpenCL C, which has no enum base types or bare struct names.
#ifdef __OPENCL_VERSION__
#define WSOLVER_ENUM(name) enum name
#define WSOLVER_ANON_ENUM enum
typedef struct TraversalNode TraversalNode;
typedef struct HandPrefixes HandPrefixes;
typedef struct PrefixEntry PrefixEntry;
typedef struct BoardPrefixes BoardPrefixes;
typedef struct IterationParams IterationParams;
typedef struct RiverRound RiverRound;
typedef struct PairedRiverRound PairedRiverRound;
typedef struct TwoBetRiver TwoBetRiver;
typedef struct AllinTable AllinTable;
typedef struct AllinBatch AllinBatch;
typedef uint RiverRoundKind;  // the enum below is RiverRoundKinds: a struct member of enum type would be int
#define WSOLVER_ROUND_KIND RiverRoundKinds
#else
#define WSOLVER_ENUM(name) enum name : unsigned
#define WSOLVER_ANON_ENUM enum : unsigned
#define WSOLVER_ROUND_KIND RiverRoundKind
#endif
WSOLVER_ENUM(NodeKind) { Decision, Deal, RiverShowdown, Fold, AllIn };
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
WSOLVER_ANON_ENUM { empty_prefix = 0, total_prefix = 1, first_card_prefix = 2, first_rank_prefix = first_card_prefix + 52 };
struct BoardPrefixes {
  unsigned index_offset, hand_offset, start[54], prefix_count;
};
WSOLVER_ENUM(AverageMode) { DenseAverage, WeightedAverage, SkipAverage };
struct IterationParams {
  unsigned hands0, hands1, stride, p;
  float positive_discount, negative_discount, average_discount;
  unsigned accumulate, phase;
  float sample_weight;
#ifdef WSOLVER_GPU_OPTIONS
  // OpenCL and CUDA (gpu_pack.hpp): what Metal makes function constants
  unsigned sqrt_regret_weights, average_mode;
#endif
};

WSOLVER_ENUM(WSOLVER_ROUND_KIND) { BetBothSides, FinalRaise, BetWithRaise, BetThenCheck };
struct RiverRound {
  unsigned root, board, storage[4];
  RiverRoundKind kind;
  float win[3][2], loss[3][2], tie[3][2], fold[2][2];
};

struct PairedRiverRound {
  unsigned root;
  RiverRound branches[2];
};

struct TwoBetRiver {
  unsigned root, board, storage[4], has_big_bet;
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
