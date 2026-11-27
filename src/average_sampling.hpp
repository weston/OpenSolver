#pragma once
#include <algorithm>
#include <cmath>

namespace pio {
struct AverageStep {
  float discount, weight;
  bool reset;
};

inline AverageStep average_step(int t, double gamma, int delay, bool reset_average, bool sampled) {
  const bool reset = reset_average && delay > 0 && t - 1 == delay;
  const auto discount = [&](int previous) { return std::pow((previous + 1.) / (t + 1.), gamma); };
  if (sampled && reset_average) {
    const bool before_reset = delay >= 16 && t <= delay;
    const long long anchor = before_reset ? 8 : std::max(16LL, 1LL + delay);
    if (t > anchor && (before_reset || t - anchor <= 64)) {
      if ((t - anchor) % 4 != 0) return {1.f, 0.f, false};
      // Sum four discounted contributions using the current reach and policy for the interval.
      double weight = 0;
      for (int i = 3; i >= 0; --i) weight += discount(t - i);
      return {float(discount(t - 4)), float(weight), false};
    }
  }
  return {float(discount(t - 1)), 1.f, reset};
}
}  // namespace pio
