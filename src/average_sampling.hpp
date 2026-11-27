#pragma once
#include <algorithm>
#include <cmath>

namespace pio {
struct AverageStep {
  float discount, weight;
  bool reset;
};

inline AverageStep average_step(int t, double gamma, int delay, bool reset_average, bool sampled, bool packed_sums) {
  const bool reset = reset_average && delay > 0 && t - 1 == delay;
  const auto discount = [&](int previous) { return std::pow((previous + 1.) / (t + 1.), gamma); };
  if (sampled && reset_average) {
    const bool before_reset = delay >= 16 && t <= delay;
    const long long anchor = before_reset ? 8 : std::max(16LL, 1LL + delay);
    const long long elapsed = t - anchor;
    const int stride = before_reset || elapsed <= 64 ? 4 : packed_sums && elapsed <= 64 + 96 ? 2 : 1;
    if (elapsed > 0 && stride > 1) {
      if (elapsed % stride != 0) return {1.f, 0.f, false};
      double weight = 0;
      for (int i = stride - 1; i >= 0; --i) weight += discount(t - i);
      return {float(discount(t - stride)), float(weight), false};
    }
  }
  return {float(discount(t - 1)), 1.f, reset};
}
}  // namespace pio
