#pragma once
// The GPU postflop solve, whatever the backend: Metal on Apple Silicon (metal.mm); elsewhere CUDA on
// NVIDIA GPUs (cuda.cpp) or OpenCL on AMD and other GPUs (opencl.cpp), through gpu_solve.cpp. All run
// the same compiled traversal.
#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include "metal.hpp"

namespace pio {
// The GPU solve's algorithm: v4_gpu on any platform, or v4_metal, its name from when Metal was the only
// backend.
inline bool gpu_algorithm(const std::string& a) { return a == "v4_gpu" || a == "v4_metal"; }
#if defined(__APPLE__) && defined(__aarch64__)
constexpr const char* kGpuAlgorithm = "v4_metal";  // what the solver starts with where it prefers the GPU
#else
constexpr const char* kGpuAlgorithm = "v4_gpu";
#endif
#if defined(__APPLE__) && defined(__aarch64__)
inline bool gpu_available() { return metal_available(); }
inline std::string gpu_name() { return "Apple GPU (Metal)"; }
inline std::string gpu_unavailable_reason() { return "no Metal GPU"; }
// Solve on the GPU by default: Metal is faster than the CPU on Apple Silicon.
inline bool gpu_preferred() { return metal_available(); }
inline uint64_t gpu_memory_bytes() { return 0; }  // shares the CPU's memory
inline std::unique_ptr<MetalSolve> make_gpu_solve(GameTree& tree, uint64_t available) {
  return make_metal_solve(tree, available);
}
inline void gpu_flop_table(const GameTree& tree, AllinRunouts& runouts,
                           const std::vector<std::array<uint32_t, 4>>& boards) {
  metal_flop_table(tree, runouts, boards);
}
#else
// The first call looks for a GPU and loads its driver; kernels are compiled on first use.
bool gpu_available();
std::string gpu_name();  // "<device> (CUDA)", "<device> (OpenCL)"
std::string gpu_unavailable_reason();
// Solve on the GPU by default (see the definition for when).
bool gpu_preferred();
uint64_t gpu_memory_bytes();  // the GPU's own memory
std::unique_ptr<MetalSolve> make_gpu_solve(GameTree& tree, uint64_t available);
void gpu_flop_table(const GameTree& tree, AllinRunouts& runouts, const std::vector<std::array<uint32_t, 4>>& boards);
#endif
}  // namespace pio
