// Nested scheduler work must run exactly once, finish before returning, and
// remain correct when a worker helps a job from another branch of the tree.
#include <atomic>
#include <iostream>
#include <vector>

#include "../src/parallel.hpp"

int main() {
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
