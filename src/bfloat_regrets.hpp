#ifndef WSOLVER_BFLOAT_REGRETS_HPP
#define WSOLVER_BFLOAT_REGRETS_HPP

inline unsigned short bfloat_regret_word(unsigned bits) {
  return static_cast<unsigned short>((bits + 0x7fffu + ((bits >> 16) & 1u)) >> 16);
}

inline unsigned bfloat_regret_bits(unsigned short word) {
  return static_cast<unsigned>(word) << 16;
}

#endif
