#include <metal_stdlib>
using namespace metal;
uint high(uint x) { return 31 - clz(x); }
uint top(uint m, int n) {
  uint v = 0;
  for (int k = 0; k < n; ++k) {
    uint r = high(m);
    v |= r << (16 - 4 * k);
    m ^= 1u << r;
  }
  return v;
}
uint straight(uint m) {
  m = m << 1 | m >> 12;
  uint run = m & (m >> 1) & (m >> 2) & (m >> 3) & (m >> 4);
  return run ? high(run) + 4 : 0;
}
uint score(uint4 s) {
  uint ranks = s.x | s.y | s.z | s.w;
  uint2 pair = uint2(s.x & s.y, s.z & s.w);
  uint2 suits = uint2(s.x | s.y, s.z | s.w);
  uint four = pair.x & pair.y, three = (pair.x & suits.y) | (pair.y & suits.x);
  uint two = pair.x | pair.y | (suits.x & suits.y);
  for (int i = 0; i < 4; ++i)
    if (popcount(s[i]) >= 5) {
      uint st = straight(s[i]);
      return st ? (8u << 20) | (st << 16) : (5u << 20) | top(s[i], 5);
    }
  if (four) return (7u << 20) | top(four, 1) | (top(ranks ^ four, 1) >> 4);
  uint tr = three ? 1u << high(three) : 0;
  if (tr && (two ^ tr)) return (6u << 20) | top(tr, 1) | (top(two ^ tr, 1) >> 4);
  uint st = straight(ranks);
  if (st) return (4u << 20) | (st << 16);
  if (tr) return (3u << 20) | top(tr, 1) | (top(ranks ^ tr, 2) >> 4);
  if (two) {
    uint p = 1u << high(two);
    if (two ^ p) {
      uint q = 1u << high(two ^ p);
      return (2u << 20) | top(p | q, 2) | (top(ranks ^ p ^ q, 1) >> 8);
    }
    return (1u << 20) | top(p, 1) | (top(ranks ^ p, 3) >> 4);
  }
  return top(ranks, 5);
}
kernel void rankhands(device const uint4* boards, device const uchar2* hands, device uint* ranks,
                      constant uint4& d, uint2 pos [[thread_position_in_grid]]) {
  uint b = pos.x, h = pos.y;
  if (h >= d.x || b >= d.y) {
    ranks[h * d.z + b] = 0;
    return;
  }
  uint4 s = boards[b];
  uchar2 c = hands[h];
  uint m = 1u << (c.x >> 2), n = 1u << (c.y >> 2);
  bool dead = (s[c.x & 3] & m) || (s[c.y & 3] & n);
  s[c.x & 3] |= m;
  s[c.y & 3] |= n;
  ranks[h * d.z + b] = dead ? 0 : score(s);
}
kernel void countpairs(device const uint* ranks, device const uchar2* hands, device short* table, constant uint4& d,
                       constant int& center,
                       uint2 group [[threadgroup_position_in_grid]], uint t [[thread_index_in_threadgroup]]) {
  uint h0 = group.y * 4, o0 = group.x * 16 + (t / 32) * 4, lane = t % 32;
  bool compact = d.w == 0;
  if (o0 >= d.y || (compact && o0 >= h0 + 4)) return;
  ushort val[4][4] = {};
  for (uint b = lane; b < d.z; b += 32) {
    uint a[4], o[4];
    for (uint l = 0; l < 4; ++l) {
      a[l] = ranks[(h0 + l) * d.z + b];
      uint x = ranks[(d.w + o0 + l) * d.z + b];
      o[l] = x ? x : 0xffffffffu;
    }
    for (uint h = 0; h < 4; ++h)
      for (uint j = 0; j < 4; ++j) val[h][j] += ushort(a[h] > o[j]) + ushort(a[h] >= o[j]);
  }
  for (uint i = 0; i < 4; ++i)
    for (uint j = 0; j < 4; ++j) {
      int e = int(simd_sum(val[i][j])) - center;
      uint h = h0 + i, o = o0 + j;
      if (!lane && h < d.x && o < d.y && (!compact || o < h)) {
        uchar2 a = hands[h], b = hands[d.w + o];
        bool share = a.x == b.x || a.x == b.y || a.y == b.x || a.y == b.y;
        uint row = compact ? h * (h - 1) / 2 : h * d.y;
        table[row + o] = share ? 0 : short(e);
      }
    }
}
