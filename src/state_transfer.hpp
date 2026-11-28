#pragma once
#include <algorithm>
#include <atomic>
#include "parallel.hpp"

namespace pio {
template <class F>
void parallel_state_blocks(size_t count, F&& work, size_t min_block_size = 0) {
  const size_t block_size = std::max<size_t>(1u << 18, min_block_size);
  if (count <= block_size || pool().threads() == 1) {
    work(0, count);
    return;
  }
  pool().parallel_for(static_cast<int>((count + block_size - 1) / block_size), [&](int block) {
    const size_t begin = block * block_size;
    work(begin, std::min(count, begin + block_size));
  });
}

template <class Src, class Dst, class Convert>
void transfer_state(const Src* src, Dst* dst, size_t count, Convert convert) {
  parallel_state_blocks(count, [&](size_t begin, size_t end) {
    for (size_t i = begin; i < end; ++i) dst[i] = convert(src[i]);
  });
}

inline bool fp16_sums_fit(const float* sums, size_t count) {
  std::atomic<bool> fits{true};
  parallel_state_blocks(count, [&](size_t begin, size_t end) {
    if (!fits.load(std::memory_order_relaxed)) return;
    bool valid = true;
    for (size_t i = begin; i < end; ++i) valid &= sums[i] >= 0.f && sums[i] <= 1024.f;
    if (!valid) fits.store(false, std::memory_order_relaxed);
  }, count / (4 * pool().threads()));
  return fits.load(std::memory_order_relaxed);
}
}  // namespace pio
