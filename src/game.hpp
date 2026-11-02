// Materialized game tree: betting tree expanded over dealt cards (with suit isomorphism),
// per-player hand lists, showdown orderings and strategy storage.
#pragma once
#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "action_tree.hpp"
#include "cards.hpp"

namespace pio {

using Weights1326 = std::array<float, NUM_COMBOS>;

struct IcmModel {
  bool enabled = false;
  int32_t stacks[2] = {0, 0};  // starting stacks (OOP, IP)
  std::vector<std::pair<double, double>> points[2];  // (final stack, value), sorted by stack
  double value(int p, double stack) const;
};

struct PayoffModel {
  double rake_frac = 0.0;
  double rake_cap = 0.0;
  IcmModel icm;
};

// A postflop tree attached to a preflop exit (add_schematic_tree).
struct Schematic {
  std::vector<int32_t> exit_line;  // preflop line (add_preflop_line numbers) of the exit node
  int32_t pot[3] = {0, 0, 0};      // set_pot at the time of add_schematic_tree
  int32_t eff_stack = 0;
  LineSet lines;
};

struct SubsetFlop {
  float weight;
  int cards[3];  // as given to add_to_subset (also the node-id token order)
};

struct GameConfig {
  bool preflop = false;            // preflop tree (board empty): lines are preflop lines
  std::vector<Schematic> schematics;
  std::vector<SubsetFlop> subset;  // used only when schematics are attached (full preflop tree)
  std::vector<int> board;          // 3, 4 or 5 cards (empty for preflop)
  int32_t pot[3] = {0, 0, 0};      // set_pot: oop, ip, dead
  int32_t eff_stack = 0;
  Weights1326 range[2]{};          // OOP, IP
  bool iso_flop = true, iso_turn = false;
  LineSet lines;
  int32_t start_pot() const { return pot[0] + pot[1] + pot[2]; }
  bool full_preflop() const { return preflop && !schematics.empty(); }
};

enum GFlags : uint8_t { GF_LOCKED = 1, GF_COMBO_LOCKED = 2, GF_INCENTIVES = 4, GF_ELIMINATED = 8 };

struct GNode {
  ANodeKind kind;
  uint8_t player;      // decision: acting player; fold: folder
  uint8_t board_len;   // cards on board at this node
  uint8_t flags;
  uint32_t anode;      // index into GameTree::anodes
  uint32_t first_child;
  uint16_t num_children;
  uint16_t pad;
  uint32_t aux;        // chance: ChanceInfo index; showdown: board id; decision: decision index
  uint64_t storage;    // decision: float offset into regrets/strat arrays (size A * H[player])
};

struct ChanceInfo {
  // Flop deal at a preflop exit: one child per subset flop (cards/iso unused).
  bool flop_deal = false;
  std::vector<std::array<uint8_t, 3>> flops;
  std::vector<float> fweight;  // weight / (total weight * C(48,3)/C(52,3)): per-pair deal probability
  int8_t slot[NUM_CARDS];    // child slot for each card (-1 = card unavailable)
  uint8_t perm[NUM_CARDS];   // suit permutation mapping the slot's canonical card to this card
  std::vector<uint8_t> cards;           // canonical card for each child slot
  std::vector<std::vector<std::pair<uint8_t, uint8_t>>> iso;  // per slot: (card, perm) duplicates
  uint8_t num_possible;      // cards that can be dealt from the hands' point of view (52 - board - 4)
  uint64_t board_mask;       // board before the deal
};

struct SdEntry {
  uint16_t idx;   // local hand index
  uint8_t c1, c2; // hole cards
  int32_t same;   // local index of the identical combo in the opponent's list, or -1
};

// All-in before the river: the showdown boards of every runout and 1 / (runouts per hand pair).
struct AllinRunouts {
  std::vector<uint32_t> boards;
  float scale;
};

struct ShowdownBoard {
  uint64_t mask;
  // Per player: hands sorted by strength ascending (board-blocked hands excluded). For position i of
  // player p's list, lt_end/le_end give how many opponent entries are strictly weaker / not stronger;
  // these depend only on the board, so the showdown sweep needs no strength comparisons.
  std::vector<SdEntry> ent[2];
  std::vector<uint16_t> lt_end[2], le_end[2];
};

class GameTree {
 public:
  GameConfig cfg;
  PayoffModel payoff;
  std::unique_ptr<ActionNode> aroot;
  std::vector<ActionNode*> anodes;
  std::vector<GNode> nodes;  // nodes[0] = root decision node (r:0)
  std::vector<ChanceInfo> chances;
  std::vector<ShowdownBoard> showdowns;
  std::vector<AllinRunouts> allins;  // GNode::aux of Showdown nodes with board_len < 5

  // Hands
  std::vector<uint16_t> hands[2];      // combo indices
  std::vector<float> init_reach[2];    // range weights per local hand
  std::vector<uint8_t> hcard[2][2];    // [player][0|1][hand] cards
  std::vector<int32_t> local[2];       // [player][combo] -> local index or -1
  std::vector<int32_t> same_hand[2];   // [player][hand] -> local index of same combo in opponent list or -1
  std::vector<std::vector<uint16_t>> hands_with_card[2];  // [player][card] -> local hand indices
  // perm_hand[player][perm][hand] -> local hand index of permuted combo (valid if perm_ok[perm])
  std::vector<std::vector<uint16_t>> perm_hand[2];
  bool perm_ok[NUM_SUIT_PERMS]{};

  // Strategy storage. Uncompressed: float32 arrays indexed by GNode::storage. Compressed: int16 regrets and
  // uint16 strategy sums with one float scale per decision node (indexed by GNode::aux).
  bool compressed = false;
  std::vector<float> regrets;
  std::vector<float> strat;      // cumulative strategy (or locked strategy for locked nodes)
  std::vector<int16_t> regrets_q;
  std::vector<uint16_t> strat_q;
  std::vector<float> rscale, sscale;
  uint64_t storage_floats = 0;
  uint64_t num_decisions = 0;

  size_t node_size(const GNode& n) const { return static_cast<size_t>(n.num_children) * hands[n.player].size(); }
  // Read access: returns the node's A*H values (direct pointer, or decoded into scratch).
  const float* regrets_ro(const GNode& n, float* scratch) const;
  const float* strat_ro(const GNode& n, float* scratch) const;
  // Read-write access: returns a buffer to modify; call the matching commit afterwards.
  float* regrets_rw(const GNode& n, float* scratch);
  float* strat_rw(const GNode& n, float* scratch);
  void regrets_commit(const GNode& n, const float* data, float maxabs = -1.0f);  // maxabs < 0: compute
  void strat_commit(const GNode& n, const float* data, float maxval = -1.0f);
  void clear_storage();  // all regrets / strategy sums to 0

  // Per-node extras (keyed by node index)
  std::unordered_map<uint32_t, std::vector<uint8_t>> combo_lock;  // per local hand: 1 = locked
  std::unordered_map<uint32_t, std::vector<float>> incentives;    // per action
  std::unordered_map<uint32_t, uint32_t> eliminated;              // bitmask of removed actions

  long long iterations = 0;  // CFR iterations run on this tree
  bool incomplete = false;  // a truncated save was loaded (later streets unsolved)

  int num_hands(int p) const { return static_cast<int>(hands[p].size()); }
  int root_board_len() const { return static_cast<int>(cfg.board.size()); }

  // Build from config. Returns false + err.
  bool build(const GameConfig& c, std::string& err, bool allocate = true);
  // Memory needed (bytes) for the current node set; valid after build(..., false).
  uint64_t memory_bytes() const { return memory_bytes_for(compressed); }
  uint64_t memory_bytes_for(bool compress) const;
  void allocate_storage();  // uses `compressed`

  // Payoff for player p at a terminal given outcome (1 win, 0 tie, -1 lose). With subtree=true the
  // preflop offset is left out (schematic-relative values, as show_win_evs prints them).
  double terminal_payoff(const ActionNode& an, int p, int outcome, bool subtree = false) const;
  std::vector<std::unique_ptr<ActionNode>> schematic_roots;  // owned postflop trees (preflop mode)
  std::vector<uint8_t> perm_all;  // suit permutations valid for both ranges (flop-deal symmetrization)

  const ActionNode& an(const GNode& n) const { return *anodes[n.anode]; }

  // Index of the (5-card) showdown board, computing hand orderings on first use.
  int showdown_board_id(const std::vector<int>& board);
  int allin_runouts_id(const std::vector<int>& board);

 private:
  std::unordered_map<uint64_t, int> showdown_index_;
  std::unordered_map<uint64_t, int> allin_index_;
};

}  // namespace pio
