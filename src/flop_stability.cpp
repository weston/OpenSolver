#include "flop_stability.hpp"

#include "solver.hpp"
#include <algorithm>
#include <cmath>
#include <utility>

namespace pio {

FlopStability::Snapshot FlopStability::capture(const GameTree& tree, const Solver& solver) {
  Snapshot result;
  auto visit = [&](auto&& self, uint32_t id, std::array<const float*, 2> reach) -> void {
    const auto& node = tree.nodes[id];
    if (node.kind != ANodeKind::Decision) return;
    if (node.num_children == 1) {
      self(self, node.first_child, reach);
      return;
    }

    const int actor = node.player, hands = tree.num_hands(actor);
    Decision row;
    row.policy.resize(tree.node_size(node));
    solver.average_strategy(id, row.policy.data());
    row.matchups.resize(hands);
    solver.compat_mass(actor, reach[1 - actor], row.matchups.data());
    for (int h = 0; h < hands; ++h) {
      row.matchups[h] = std::max(0.0f, row.matchups[h] * reach[actor][h]);
      row.mass += row.matchups[h];
    }
    for (unsigned a = 0; a < node.num_children; ++a) {
      const float* action = row.policy.data() + static_cast<size_t>(a) * hands;
      double mass = 0;
      for (int h = 0; h < hands; ++h) mass += static_cast<double>(row.matchups[h]) * action[h];
      row.mix.push_back(row.mass > 0 ? mass / row.mass : 0);
      const uint32_t child = node.first_child + a;
      if (tree.nodes[child].kind == ANodeKind::Decision) {
        std::vector<float> next(hands);
        for (int h = 0; h < hands; ++h) next[h] = reach[actor][h] * action[h];
        auto child_reach = reach;
        child_reach[actor] = next.data();
        self(self, child, child_reach);
      }
    }
    result.push_back(std::move(row));
  };
  visit(visit, 0, {tree.init_reach[0].data(), tree.init_reach[1].data()});
  return result;
}

bool FlopStability::within_limits(const Snapshot& before, const Snapshot& now, double root_matchups) {
  double total = 0, range_change = 0, hand_change = 0;
  for (size_t i = 0; i < now.size(); ++i) {
    const auto& old = before[i];
    const auto& row = now[i];
    if (row.mass == 0) continue;
    const size_t hands = row.matchups.size();
    double node_change = 0;
    for (size_t a = 0; a < row.mix.size(); ++a) {
      double old_action_mass = 0;
      for (size_t h = 0; h < hands; ++h) {
        const size_t k = a * hands + h;
        const double w = row.matchups[h];
        hand_change += 0.5 * w * std::abs(static_cast<double>(row.policy[k]) - old.policy[k]);
        if (old.mass == 0) old_action_mass += w * old.policy[k];
      }
      const double old_mix = old.mass > 0 ? old.mix[a] : old_action_mass / row.mass;
      node_change += 0.5 * std::abs(row.mix[a] - old_mix);
    }
    if (row.mass >= 0.05 * root_matchups && node_change > 0.02) return false;
    total += row.mass;
    range_change += row.mass * node_change;
  }
  return range_change <= 0.01 * total && hand_change <= 0.03 * total;
}

bool FlopStability::observe(long long iteration, const GameTree& tree, const Solver& solver) {
  if (iteration <= last_iteration_ || average_delay_ != solver.avg_delay) {
    *this = FlopStability{};
    average_delay_ = solver.avg_delay;
  }
  last_iteration_ = iteration;
  if (iteration < next_[slot_]) return false;
  while (next_[slot_] < iteration) {
    next_[slot_] *= 2;
    prior_[slot_].reset();
    slot_ = (slot_ + 1) % next_.size();
  }
  if (iteration != next_[slot_]) return false;
  const unsigned slot = slot_;
  next_[slot] *= 2;
  slot_ = (slot + 1) % next_.size();
  if (iteration <= solver.avg_delay) return false;
  auto current = capture(tree, solver);
  if (prior_[slot]) {
    if (!within_limits(*prior_[slot], current, solver.root_matchups())) first_pass_ = 0;
    else if (first_pass_ == 0) first_pass_ = iteration;
    else if (5 * iteration >= 6 * first_pass_) return true;
  }
  prior_[slot] = std::move(current);
  return false;
}
}  // namespace pio
