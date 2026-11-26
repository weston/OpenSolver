#include <metal_stdlib>
using namespace metal;
#define TRAVERSAL_BUFFERS                                                                                                  \
  device const TraversalNode *nodes [[buffer(0)]], device const uint *ids [[buffer(1)]],                         \
      constant IterationParams &q [[buffer(2)]], device float *oop_reach [[buffer(3)]], device float *ip_reach [[buffer(4)]], \
      device float *values [[buffer(5)]], device float *regrets [[buffer(6)]], device float *strategy_sums [[buffer(7)]],           \
      device const uchar2 *cards [[buffer(8)]]
#define NODE_THREADS                                                                      \
  uint k [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]], \
      uint width [[threads_per_threadgroup]]
bool dead(uchar2 c, uint card) { return c.x == card || c.y == card; }
kernel void propagate_reach(TRAVERSAL_BUFFERS, NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  uint H = n.actor ? q.hands1 : q.hands0;
  for (uint h = tid; h < q.stride; h += width) {
    float x = oop_reach[n.row[0] * q.stride + h], y = ip_reach[n.row[1] * q.stride + h];
    float total = 0;
    if (n.kind == Decision && h < H)
      for (uint a = 0; a < n.count; ++a) total += max(regrets[n.storage + a * H + h], 0.f);
    for (uint a = 0; a < n.count; ++a) {
      uint child = n.child + a;
      float u = x, z = y;
      if (n.kind == Decision && h < H) {
        float s = total > 0 ? max(0.f, regrets[n.storage + a * H + h]) / total : 1.f / n.count;
        if (n.actor)
          z *= s;
        else
          u *= s;
      } else if (n.kind == Deal) {
        uint c = nodes[child].card;
        if (dead(cards[h], c)) u = 0;
        if (dead(cards[q.stride + h], c)) z = 0;
      }
      if (nodes[child].row[0] != n.row[0]) oop_reach[nodes[child].row[0] * q.stride + h] = u;
      if (nodes[child].row[1] != n.row[1]) ip_reach[nodes[child].row[1] * q.stride + h] = z;
    }
  }
}
kernel void update_values(TRAVERSAL_BUFFERS, device const uint2* iso [[buffer(9)]],
                          device const ushort* perms [[buffer(10)]], NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  uint H = q.p ? q.hands1 : q.hands0;
  device const float* own_reach = q.p ? ip_reach : oop_reach;
  for (uint h = tid; h < H; h += width) {
    float sum = 0;
    bool updating = n.kind == Decision && n.actor == q.p;
    if (updating)
      for (uint a = 0; a < n.count; ++a) sum += max(regrets[n.storage + a * H + h], 0.f);
    float node_value = 0;
    if (n.kind == Deal) {
      for (uint a = 0; a < n.deal_count; ++a) {
        uint2 m = iso[n.deals + a];
        uint j = perms[m.y * q.stride + h];
        if (!dead(cards[q.p * q.stride + j], nodes[m.x].card)) node_value += values[m.x * q.stride + j];
      }
      node_value *= n.chance_weight;
    } else
      for (uint a = 0; a < n.count; ++a) {
        float val = values[(n.child + a) * q.stride + h];
        float w = updating ? (sum > 0 ? max(regrets[n.storage + a * H + h], 0.f) / sum : 1.f / n.count) : 1.f;
        node_value = fma(w, val, node_value);
      }
    values[i * q.stride + h] = node_value;
    if (updating)
      for (uint a = 0; a < n.count; ++a) {
        uint j = n.storage + a * H + h;
        float r = regrets[j];
        float s = sum > 0 ? max(r, 0.f) / sum : 1.f / n.count;
        regrets[j] = r * (r > 0 ? q.positive_discount : q.negative_discount) + (values[(n.child + a) * q.stride + h] - node_value);
        if (q.accumulate) strategy_sums[j] = fma(own_reach[n.row[q.p] * q.stride + h], s, strategy_sums[j] * q.average_discount);
      }
  }
}
kernel void terminal(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                     device const ushort* indices [[buffer(10)]], device const HandPrefixes* heroes [[buffer(11)]],
                     threadgroup float* prefix [[threadgroup(0)]], NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  device const BoardPrefixes& b = boards[n.aux];
  uint H = q.p ? q.hands1 : q.hands0;
  device const float* reach = (q.p ? oop_reach : ip_reach) + n.row[1 - q.p] * q.stride;
  if (n.kind == Fold) {
    if (tid < 52) {
      float s = 0;
      for (uint j = b.start[tid + 1] + 1; j < b.start[tid + 2]; ++j) s += reach[indices[b.index_offset + j]];
      prefix[tid] = s;
    }
    if (tid >= 64 && tid < 96) {
      float s = 0;
      uint lane = tid - 64;
      for (uint j = lane; j < (q.p ? q.hands0 : q.hands1); j += 32) s += reach[j];
      s = simd_sum(s);
      if (!lane) prefix[52] = s;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint h = tid; h < H; h += width) {
      uchar2 c = cards[q.p * q.stride + h];
      short same = heroes[b.hand_offset + h].same;
      float compat = prefix[52] - prefix[c.x] - prefix[c.y] + (same >= 0 ? reach[same] : 0);
      values[i * q.stride + h] = n.win[q.p] * compat;
    }
    return;
  }
  if (tid < 52) {
    uint start = b.start[tid + 1], end = b.start[tid + 2] - 1;
    float s = 0;
    prefix[start] = 0;
    for (uint j = start + 1; j <= end; ++j) {
      s += reach[indices[b.index_offset + j]];
      prefix[j] = s;
    }
  }
  if (tid >= 64 && tid < 96) {
    uint lane = tid - 64;
    float carry = 0;
    if (!lane) prefix[0] = 0;
    for (uint base = 1; base < b.start[1]; base += 32) {
      uint j = base + lane;
      float w = j < b.start[1] ? reach[indices[b.index_offset + j]] : 0;
      float t = simd_prefix_inclusive_sum(w) + carry;
      carry = simd_broadcast(t, 31);
      if (j < b.start[1]) prefix[j] = t;
    }
  }
  threadgroup_barrier(mem_flags::mem_threadgroup);
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    float same = m.same >= 0 ? reach[m.same] : 0;
    float compat = prefix[m.end[0]] - prefix[m.end[1]] - prefix[m.end[2]] + same;
    float weaker = prefix[m.lo[0]] - prefix[m.lo[1]] - prefix[m.lo[2]];
    float weaker_or_tied = prefix[m.hi[0]] - prefix[m.hi[1]] - prefix[m.hi[2]] + same;
    values[i * q.stride + h] = fma(n.win[q.p] - n.tie[q.p], weaker,
                              fma(n.tie[q.p] - n.loss[q.p], weaker_or_tied, n.loss[q.p] * compat));
  }
}
kernel void allin(TRAVERSAL_BUFFERS, device const uint* offsets [[buffer(9)]], device const float* eq [[buffer(10)]],
                  uint2 group [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]],
                  ushort lane [[thread_index_in_simdgroup]]) {
  uint i = ids[group.x];
  TraversalNode n = nodes[i];
  uint h = group.y * 4 + tid / 32;
  uint H = q.p ? q.hands1 : q.hands0, O = q.p ? q.hands0 : q.hands1;
  if (h >= H) return;
  device const float* ro = (q.p ? oop_reach : ip_reach) + n.row[1 - q.p] * q.stride;
  float s = 0;
  for (uint j = lane; j < O; j += 32) {
    float e = eq[offsets[n.aux] + h * O + j];
    if (e >= 0) s = fma(ro[j], fma(n.win[q.p] - n.loss[q.p], e, n.loss[q.p]), s);
  }
  s = simd_sum(s);
  if (!lane) values[i * q.stride + h] = s;
}
