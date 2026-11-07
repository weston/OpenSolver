#include "action_tree.hpp"

#include <algorithm>
#include <map>

namespace pio {

namespace {

struct Builder {
  ActionTreeParams p;
  std::set<std::vector<int32_t>> removed;
  std::vector<std::vector<int32_t>> forced;
  std::string err;
  bool past_end = false;  // the last add_line ran past the end of the hand and was cut there

  bool is_removed(const std::vector<int32_t>& key) const {
    // A node is removed if its line, or any prefix of it, was removed.
    std::vector<int32_t> pre;
    pre.reserve(key.size());
    for (int32_t x : key) {
      pre.push_back(x);
      if (removed.count(pre)) return true;
    }
    return false;
  }

  // Forced lines (force_line): Pio keeps only the lines every forced line is a prefix of, so a node
  // whose key is a proper prefix of a forced line may only take that line's next action - for each
  // forced line, so a longer force still applies below a shorter one (force_line 0, then 0 50 50).
  // force_line only accepts lines of the tree as it stands, so the forced lines lie on one path.
  // Returns true if `next` is allowed after `key`.
  bool allowed_by_force(const std::vector<int32_t>& key, int32_t next) const {
    for (auto& f : forced)
      if (key.size() < f.size() && std::equal(key.begin(), key.end(), f.begin()) && f[key.size()] != next)
        return false;
    return true;
  }

  // Classify number x at decision node n. Returns false on illegal.
  bool classify(const ActionNode& n, int32_t x, Action& out) {
    int me = n.player, op = 1 - me;
    int32_t mine = n.inv[me], theirs = n.inv[op];
    if (x > p.eff_stack) {
      err = "bet of " + std::to_string(x) + " exceeds effective stack " + std::to_string(p.eff_stack);
      return false;
    }
    if (theirs > mine) {  // facing a bet
      if (x == mine) out = {ActKind::Fold, x};
      else if (x == theirs) out = {ActKind::Call, x};
      else if (x > theirs) out = {ActKind::Bet, x};
      else {
        err = "amount " + std::to_string(x) + " is between current investment and the bet faced";
        return false;
      }
    } else {
      if (x == mine) out = {ActKind::Check, x};
      else if (x > mine) out = {ActKind::Bet, x};
      else {
        err = "amount " + std::to_string(x) + " is below current investment " + std::to_string(mine);
        return false;
      }
    }
    return true;
  }

  std::unique_ptr<ActionNode> make_street_start(const ActionNode& from, int board_len, bool allin,
                                                const std::vector<int32_t>& key) {
    auto n = std::make_unique<ActionNode>();
    n->dead = from.dead;
    n->board_len = static_cast<uint8_t>(board_len);
    n->inv[0] = from.inv[0];
    n->inv[1] = from.inv[1];
    n->street_start[0] = from.inv[0];
    n->street_start[1] = from.inv[1];
    n->line = key;
    n->allin = allin;
    if (allin) {
      n->kind = ANodeKind::Showdown;
    } else {
      n->kind = ANodeKind::Decision;
      n->player = 0;
    }
    return n;
  }

  // Child node reached by taking action a at decision node n.
  std::unique_ptr<ActionNode> make_child(const ActionNode& n, const Action& a) {
    auto key = n.line;
    key.push_back(a.total);
    int me = n.player, op = 1 - me;
    auto c = std::make_unique<ActionNode>();
    c->dead = n.dead;
    c->board_len = n.board_len;
    c->inv[0] = n.inv[0];
    c->inv[1] = n.inv[1];
    c->street_start[0] = n.street_start[0];
    c->street_start[1] = n.street_start[1];
    c->line = key;
    switch (a.kind) {
      case ActKind::Fold:
        c->kind = ANodeKind::Fold;
        c->player = static_cast<uint8_t>(me);
        return c;
      case ActKind::Bet:
        c->inv[me] = a.total;
        c->kind = ANodeKind::Decision;
        c->player = static_cast<uint8_t>(op);
        return c;
      case ActKind::Check:
        // The street's first actor (IP preflop, OOP postflop) checks to the other; the second closes.
        if (me == (n.board_len == 0 ? 1 : 0)) {
          c->kind = ANodeKind::Decision;
          c->player = static_cast<uint8_t>(op);
          return c;
        }
        break;
      case ActKind::Call:
        c->inv[me] = a.total;
        if (n.blind_option) {  // SB limps: the big blind still has the option
          c->kind = ANodeKind::Decision;
          c->player = static_cast<uint8_t>(op);
          return c;
        }
        break;
    }
    // Street closes.
    bool allin = c->inv[0] >= p.eff_stack || c->inv[1] >= p.eff_stack;
    if (n.board_len == 0 && !allin) {
      // Preflop exit: a flop deal whose child is the schematic tree attached by add_schematic_tree.
      c->kind = ANodeKind::Chance;
      c->preflop_exit = true;
      c->board_len = 3;
      return c;
    }
    if (n.board_len == 5 || allin) {
      // Like PioSOLVER, an all-in before the river is a terminal (END_NODE) whose value averages over
      // every runout; no chance nodes are created for it.
      c->kind = ANodeKind::Showdown;
      c->allin = allin;
      return c;
    }
    c->kind = ANodeKind::Chance;
    c->allin = allin;
    c->board_len = static_cast<uint8_t>(n.board_len + 1);
    return c;
  }

  // Ensure the chance node has its single child.
  void ensure_chance_child(ActionNode& n) {
    if (!n.children.empty() || n.preflop_exit) return;
    n.children.push_back(make_street_start(n, n.board_len, n.allin, n.line));
  }

  ActionNode* get_or_add(ActionNode& n, const Action& a) {
    for (size_t i = 0; i < n.actions.size(); ++i)
      if (n.actions[i].kind == a.kind && n.actions[i].total == a.total) return n.children[i].get();
    n.actions.push_back(a);
    n.children.push_back(make_child(n, a));
    return n.children.back().get();
  }

  bool add_line(ActionNode* root, const std::vector<int32_t>& line) {
    ActionNode* n = root;
    for (size_t i = 0; i < line.size(); ++i) {
      while (n->kind == ANodeKind::Chance) {
        ensure_chance_child(*n);
        if (n->children.empty()) break;  // preflop exit: postflop lines belong to schematic trees
        n = n->children[0].get();
      }
      if (n->kind != ANodeKind::Decision) {
        // Pio 3.10 (probed): a line that runs past a fold, an all-in call or the river keeps its
        // valid prefix; add_line reports "Specified line is too long" and the tree still builds. So
        // does a preflop line past an exit with no schematic (add_preflop_line "10 10 25", 2026-11-07).
        past_end = true;
        return true;
      }
      Action a;
      if (!classify(*n, line[i], a)) return false;
      auto key = n->line;
      key.push_back(a.total);
      if (is_removed(key) || !allowed_by_force(n->line, a.total)) return true;  // silently pruned
      n = get_or_add(*n, a);
    }
    return true;
  }

  void complete(ActionNode& n) {
    if (n.kind == ANodeKind::Chance) {
      ensure_chance_child(n);
      if (!n.children.empty()) complete(*n.children[0]);
      return;
    }
    if (n.kind != ANodeKind::Decision) return;
    int me = n.player, op = 1 - me;
    std::vector<Action> implicit;
    if (n.inv[op] > n.inv[me]) {
      implicit.push_back({ActKind::Fold, n.inv[me]});
      implicit.push_back({ActKind::Call, n.inv[op]});
    } else {
      implicit.push_back({ActKind::Check, n.inv[me]});
    }
    for (auto& a : implicit) {
      auto key = n.line;
      key.push_back(a.total);
      if (is_removed(key) || !allowed_by_force(n.line, a.total)) continue;
      get_or_add(n, a);
    }
    // PioSOLVER order (verified against 3.10): bets/raises largest first, then check/call, then fold.
    std::vector<size_t> order(n.actions.size());
    for (size_t i = 0; i < order.size(); ++i) order[i] = i;
    auto rank = [&](const Action& a) {
      switch (a.kind) {
        case ActKind::Bet: return 0;
        case ActKind::Check:
        case ActKind::Call: return 1;
        default: return 2;
      }
    };
    std::stable_sort(order.begin(), order.end(), [&](size_t x, size_t y) {
      int rx = rank(n.actions[x]), ry = rank(n.actions[y]);
      if (rx != ry) return rx < ry;
      return n.actions[x].total > n.actions[y].total;
    });
    std::vector<Action> acts;
    std::vector<std::unique_ptr<ActionNode>> kids;
    for (size_t i : order) {
      acts.push_back(n.actions[i]);
      kids.push_back(std::move(n.children[i]));
    }
    n.actions = std::move(acts);
    n.children = std::move(kids);
    for (auto& c : n.children) complete(*c);
  }
};

}  // namespace

bool validate_line(const ActionTreeParams& p, const std::vector<int32_t>& line, std::string& err) {
  Builder b;
  b.p = p;
  ActionNode root;
  root.board_len = static_cast<uint8_t>(p.board_len);
  root.player = p.board_len == 0 ? 1 : 0;
  root.blind_option = p.board_len == 0;
  root.inv[0] = root.street_start[0] = p.start_inv[0];
  root.inv[1] = root.street_start[1] = p.start_inv[1];
  if (p.eff_stack <= std::max(p.start_inv[0], p.start_inv[1])) {
    root.allin = true;
    root.kind = ANodeKind::Showdown;
  }
  if (!b.add_line(&root, line)) {
    err = b.err;
    return false;
  }
  if (b.past_end) {
    err = "Specified line is too long. Some actions were not included in the tree.";
    return false;
  }
  return true;
}

// Out-of-line so the recursive destructor (and the vector<unique_ptr<ActionNode>> destroy it
// pulls in) is instantiated in this TU only. See the comment in action_tree.hpp (#216).
ActionNode::~ActionNode() = default;

std::unique_ptr<ActionNode> build_action_tree(const ActionTreeParams& p, const LineSet& lines, std::string& err) {
  Builder b;
  b.p = p;
  for (auto& r : lines.remove) b.removed.insert(r);
  b.forced = lines.force;
  auto root = std::make_unique<ActionNode>();
  root->kind = ANodeKind::Decision;
  root->player = p.board_len == 0 ? 1 : 0;  // the small blind (IP) acts first preflop
  root->blind_option = p.board_len == 0;
  root->board_len = static_cast<uint8_t>(p.board_len);
  root->inv[0] = root->street_start[0] = p.start_inv[0];
  root->inv[1] = root->street_start[1] = p.start_inv[1];
  root->dead = p.dead;
  if (p.eff_stack <= std::max(p.start_inv[0], p.start_inv[1])) {
    // No chips behind: straight to an all-in showdown over the runouts.
    root->allin = true;
    root->kind = ANodeKind::Showdown;
  }
  for (auto& l : lines.add) {
    if (!b.add_line(root.get(), l)) {
      err = b.err;
      return nullptr;
    }
  }
  for (auto& f : lines.force) {
    if (!b.add_line(root.get(), f)) {
      err = b.err;
      return nullptr;
    }
  }
  b.complete(*root);
  std::vector<ActionNode*> all;
  index_action_tree(root.get(), all);
  // Every decision needs an action. remove_line refuses to remove a node's last child, but lines removed
  // while no tree can be built yet (no board, or a pending line that doesn't fit) skip that check, and
  // a decision with no children was then built and solved: wrong EVs, nan strategies (#371).
  for (const ActionNode* n : all)
    if (n->kind == ANodeKind::Decision && n->children.empty()) {
      err = "the removed lines leave a decision with no actions";
      return nullptr;
    }
  return root;
}

std::string action_label(const ActionNode& /*node*/, const Action& a) {
  switch (a.kind) {
    case ActKind::Fold: return "f";
    case ActKind::Check:
    case ActKind::Call: return "c";
    case ActKind::Bet: return "b" + std::to_string(a.total);  // cumulative chips invested (Pio 3.x)
  }
  return "?";
}

void index_action_tree(ActionNode* root, std::vector<ActionNode*>& out) {
  out.clear();
  std::vector<ActionNode*> stack{root};
  while (!stack.empty()) {
    ActionNode* n = stack.back();
    stack.pop_back();
    n->id = static_cast<int32_t>(out.size());
    out.push_back(n);
    for (auto it = n->children.rbegin(); it != n->children.rend(); ++it) stack.push_back(it->get());
  }
}

}  // namespace pio
