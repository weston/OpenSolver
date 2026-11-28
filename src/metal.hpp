#pragma once
#include <array>
#include <cstdint>
#include <vector>
#include <memory>

namespace pio {
class GameTree;
class Solver;
struct AllinRunouts;
struct RootStats;

class MetalSolve {
 public:
  virtual ~MetalSolve() = default;
  virtual void publish_for_cpu(GameTree&) {}
  virtual void finish_for_cpu(GameTree& tree) { publish_for_cpu(tree); }
  virtual RootStats evaluate_and_prepare(GameTree& tree, Solver& solver, int next_iteration) = 0;
  virtual void iterate(GameTree& tree, Solver& solver, int iteration, bool refresh_reach) = 0;
};

#if defined(__APPLE__) && defined(__aarch64__)
bool metal_available();
std::unique_ptr<MetalSolve> make_metal_solve(GameTree& tree, uint64_t available);
void metal_flop_table(const GameTree& tree, AllinRunouts& runouts,
                      const std::vector<std::array<uint32_t, 4>>& boards);
#else
inline bool metal_available() { return false; }
inline std::unique_ptr<MetalSolve> make_metal_solve(GameTree&, uint64_t) { return {}; }
inline void metal_flop_table(const GameTree&, AllinRunouts&, const std::vector<std::array<uint32_t, 4>>&) {}
#endif
}  // namespace pio
