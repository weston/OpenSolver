// Port of flop_table.metal for OpenCL and CUDA (the gpu_opencl.h dialect): the all-in outcome table of
// a flop, 2 * wins + ties for each hand pair over every turn and river.
FN uint high(uint x) { return 31 - CLZ(x); }
FN uint top(uint m, int n) {
  uint v = 0;
  for (int k = 0; k < n; ++k) {
    const uint r = high(m);
    v |= r << (16 - 4 * k);
    m ^= 1u << r;
  }
  return v;
}
FN uint straight(uint m) {
  m = m << 1 | m >> 12;
  const uint run = m & (m >> 1) & (m >> 2) & (m >> 3) & (m >> 4);
  return run ? high(run) + 4 : 0;
}
FN uint score(const uint* s) {
  const uint ranks = s[0] | s[1] | s[2] | s[3];
  const uint pair_x = s[0] & s[1], pair_y = s[2] & s[3], suits_x = s[0] | s[1], suits_y = s[2] | s[3];
  const uint four = pair_x & pair_y, three = (pair_x & suits_y) | (pair_y & suits_x);
  const uint two = pair_x | pair_y | (suits_x & suits_y);
  for (int i = 0; i < 4; ++i)
    if (POPCOUNT(s[i]) >= 5) {
      const uint st = straight(s[i]);
      return st ? (8u << 20) | (st << 16) : (5u << 20) | top(s[i], 5);
    }
  if (four) return (7u << 20) | top(four, 1) | (top(ranks ^ four, 1) >> 4);
  const uint tr = three ? 1u << high(three) : 0;
  if (tr && (two ^ tr)) return (6u << 20) | top(tr, 1) | (top(two ^ tr, 1) >> 4);
  const uint st = straight(ranks);
  if (st) return (4u << 20) | (st << 16);
  if (tr) return (3u << 20) | top(tr, 1) | (top(ranks ^ tr, 2) >> 4);
  if (two) {
    const uint p = 1u << high(two);
    if (two ^ p) {
      const uint q = 1u << high(two ^ p);
      return (2u << 20) | top(p | q, 2) | (top(ranks ^ p ^ q, 1) >> 8);
    }
    return (1u << 20) | top(p, 1) | (top(ranks ^ p, 3) >> 4);
  }
  return top(ranks, 5);
}
// d: hand rows, boards, padded boards. Grid: (padded boards, hand rows rounded up to 4).
KERNEL void rankhands(G const uint* boards, G const uchar2* hands, G uint* ranks, const uint4 d) {
  const uint b = GLOBAL_X, h = GLOBAL_Y;
  if (h >= d.x || b >= d.y) {
    ranks[h * d.z + b] = 0;
    return;
  }
  uint s[4] = {boards[4 * b], boards[4 * b + 1], boards[4 * b + 2], boards[4 * b + 3]};
  const uchar2 c = hands[h];
  const uint m = 1u << (c.x >> 2), n = 1u << (c.y >> 2);
  const bool dead = (s[c.x & 3] & m) || (s[c.y & 3] & n);
  s[c.x & 3] |= m;
  s[c.y & 3] |= n;
  ranks[h * d.z + b] = dead ? 0 : score(s);
}
// d: hero hands, opponent hands, padded boards, the opponents' first row (0: one range, compact
// triangle below the diagonal; else a full hero x opponent table). One work-group per 4 x 4 block.
KERNEL void countpairs(G const uint* ranks, G const uchar2* hands, G short* table, const uint4 d, const int center) {
  SHARED float tmp[MAX_WIDTH];
  const uint h0 = GROUP_Y * 4, o0 = GROUP_X * 4, lane = TID, width = WIDTH;
  const bool compact = d.w == 0;
  if (o0 >= d.y || (compact && o0 >= h0 + 4)) return;  // the whole work-group
  uint val[4][4] = {{0}};
  for (uint b = lane; b < d.z; b += width) {
    uint a[4], o[4];
    for (uint l = 0; l < 4; ++l) {
      a[l] = ranks[(h0 + l) * d.z + b];
      const uint x = ranks[(d.w + o0 + l) * d.z + b];
      o[l] = x ? x : 0xffffffffu;
    }
    for (uint h = 0; h < 4; ++h)
      for (uint j = 0; j < 4; ++j) val[h][j] += (uint)(a[h] > o[j]) + (uint)(a[h] >= o[j]);
  }
  for (uint i = 0; i < 4; ++i)
    for (uint j = 0; j < 4; ++j) {
      // counts of at most 2 * 1980 per lane, so the float sum is exact
      const int e = (int)group_sum((float)val[i][j], tmp) - center;
      const uint h = h0 + i, o = o0 + j;
      if (!lane && h < d.x && o < d.y && (!compact || o < h)) {
        const uchar2 a = hands[h], b = hands[d.w + o];
        const bool share = a.x == b.x || a.x == b.y || a.y == b.x || a.y == b.y;
        const uint row = compact ? h * (h - 1) / 2 : h * d.y;
        table[row + o] = share ? 0 : (short)e;
      }
    }
}
