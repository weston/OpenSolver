#pragma once

#include <array>
#include <optional>
#include <vector>

namespace pio {
class GameTree;
class Solver;

class FlopStability {
 public:
  bool observe(long long iteration, const GameTree& tree, const Solver& solver);

 private:
  struct Decision {
    std::vector<float> policy, matchups;
    std::vector<double> mix;
    double mass = 0;
  };
  using Snapshot = std::vector<Decision>;
  static Snapshot capture(const GameTree& tree, const Solver& solver);
  static bool within_limits(const Snapshot& before, const Snapshot& now, double root_matchups);

  std::array<long long, 4> next_{40, 50, 60, 75};
  std::array<std::optional<Snapshot>, 4> prior_;
  unsigned slot_ = 0;
  long long first_pass_ = 0, last_iteration_ = 0;
  int average_delay_ = 0;
};
}  // namespace pio
