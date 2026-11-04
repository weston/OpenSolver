// Nested scheduler work must run exactly once, finish before returning, and
#include <cstdint>
// remain correct when a worker helps a job from another branch of the tree.
#include <atomic>
#include <iostream>
#include <vector>

#include "../src/parallel.hpp"

// #77: Arena::alloc promises 64-byte-aligned floats. With 16 MB blocks the allocator
// happens to return page-aligned memory, so a misaligned base would not show up here --
// the check that matters is that the aligned base is derived, not assumed. Exercise small
// allocations, block growth and scope reset, and verify the arithmetic directly.
static int check_arena_alignment() {
  int bad = 0;
  for (int round = 0; round < 8; ++round) {
    pio::ArenaScope sc;
    for (int k = 0; k < 16; ++k) {
      float* p = sc.a.alloc(1 + k * 7);
      if (reinterpret_cast<uintptr_t>(p) % 64) ++bad;
      p[0] = 1.0f;  // the returned range must be writable
    }
    float* big = sc.a.alloc_zero(1 << 23);  // forces a new block (> kBlockFloats)
    if (reinterpret_cast<uintptr_t>(big) % 64) ++bad;
    if (big[0] != 0.0f || big[(1 << 23) - 1] != 0.0f) ++bad;  // zeroing covers the whole range
  }
  std::cout << (bad ? "FAIL" : "ok") << ": arena 64-byte alignment, growth, reset, zeroing ("
            << bad << " violations)\n";
  return bad;
}

int main() {
  if (check_arena_alignment()) return 1;
  pio::ThreadPool pool(1);
  for (int threads : {1, 2, 4, 8}) {
    pool.set_threads(threads);
    for (int outer : {1, 3, 37}) {
      for (int repeat = 0; repeat < 20; ++repeat) {
        std::vector<std::atomic<int>> visits(outer * 7 * 11);
        for (auto& v : visits) v.store(0);
        pool.parallel_for(outer, [&](int i) {
          pool.parallel_for(7, [&](int j) {
            pool.parallel_for(11, [&](int k) {
              visits[(i * 7 + j) * 11 + k].fetch_add(1);
            });
          });
        });
        for (const auto& v : visits) {
          if (v.load() != 1) {
            std::cerr << "Nested task missing or repeated at " << threads << " threads\n";
            return 1;
          }
        }
      }
    }
    int calls = 0;
    pool.parallel_for(0, [&](int) { ++calls; });
    pool.parallel_for(-1, [&](int) { ++calls; });
    if (calls != 0) return 1;
  }
  pool.set_threads(1);
  std::cout << "ALL PASSED\n";
}
