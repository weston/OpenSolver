#include <cstdio>
#include <iostream>

#include "upi.hpp"

int main() {
  std::ios::sync_with_stdio(false);
  std::cin.tie(nullptr);
  pio::Upi upi;
  return upi.run();
}
