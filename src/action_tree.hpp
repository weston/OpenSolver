// Betting ("schematic") tree built from PioSOLVER-style add_line / remove_line / force_line.
//
// add_line numbers are each acting player's total chips invested in the tree after their action
// (cumulative across streets). Checks, calls and folds are implicit at every decision node unless
// removed; explicit lines add bets/raises. Node IDs (r:0:c:b30:...) use per-street bet-to amounts.
#pragma once
#include <cstdint>
#include <memory>
#include <set>
#include <string>
#include <vector>

namespace pio {

enum class ActKind : uint8_t { Fold, Check, Call, Bet };

struct Action {
  ActKind kind;
  int32_t total;  // acting player's total invested after the action (cumulative, add_line units)
};

enum class ANodeKind : uint8_t { Decision, Chance, Fold, Showdown };

struct ActionNode {
  ANodeKind kind = ANodeKind::Decision;
  bool blind_option = false;  // preflop: SB faces the big blind; a call (limp) does not close the action
  bool preflop_exit = false;  // preflop street closed: flop deal into an attached schematic tree
  uint8_t player = 0;      // Decision: player to act (0 = OOP, 1 = IP). Fold: player who folded.
  uint8_t board_len = 3;   // board cards known at this node (Chance: cards after the deal)
  bool allin = false;      // no further betting possible (all-in runout)
  int32_t inv[2] = {0, 0};          // total invested (absolute: includes set_pot's per-player chips)
  int32_t dead = 0;                  // dead money of the (sub)tree this node belongs to
  int32_t street_start[2] = {0, 0};  // invested at the start of the current street
  std::vector<int32_t> line;         // add_line-style number sequence to reach this node
  std::vector<Action> actions;       // Decision only; same order as children
  std::vector<std::unique_ptr<ActionNode>> children;  // Decision: per action. Chance: exactly 1.
  int32_t id = -1;                   // dense index assigned after building

  // Out-of-line, defined once in action_tree.cpp (#216). The node is recursive through `children`,
  // so an implicit destructor is instantiated in every TU that destroys one, and under PGO those
  // per-TU instantiations can disagree on their control-flow hash -- which discards the profile
  // counts for them and warns. Declaring it here keeps the instantiation to a single TU.
  ActionNode() = default;
  ~ActionNode();
  ActionNode(const ActionNode&) = delete;
  ActionNode& operator=(const ActionNode&) = delete;
};

struct LineSet {
  std::vector<std::vector<int32_t>> add, remove, force;
  void clear() { add.clear(); remove.clear(); force.clear(); }
};

struct ActionTreeParams {
  int board_len = 3;               // 0 = preflop, 3 = flop tree, 4 = turn tree, 5 = river tree
  int32_t eff_stack = 0;           // maximum total investment (absolute, like add_line numbers)
  int32_t start_inv[2] = {0, 0};   // set_pot's per-player chips (preflop: the blinds)
  int32_t dead = 0;                // set_pot's dead money
};

// Builds the tree; returns nullptr and sets err on invalid lines.
std::unique_ptr<ActionNode> build_action_tree(const ActionTreeParams& p, const LineSet& lines, std::string& err);

// Validates a single line against the params without building (used by add_line for early errors).
bool validate_line(const ActionTreeParams& p, const std::vector<int32_t>& line, std::string& err);

// Action label used in node IDs: "f", "c", or "b<per-street amount>".
std::string action_label(const ActionNode& node, const Action& a);

bool simplify_rivers(ActionNode& node, int32_t stack);

// Collect nodes in DFS order (assigns ids).
void index_action_tree(ActionNode* root, std::vector<ActionNode*>& out);

}  // namespace pio
