#include <metal_stdlib>
using namespace metal;
#define TRAVERSAL_BUFFERS                                                                \
  device const TraversalNode *nodes [[buffer(0)]], device const uint *ids [[buffer(1)]], \
      constant IterationParams &q [[buffer(2)]], device float *oop_reach [[buffer(3)]],  \
      device float *ip_reach [[buffer(4)]], device float *values [[buffer(5)]],          \
      device float *regrets [[buffer(6)]], device float *strategy_sums [[buffer(7)]],    \
      device const uchar2 *cards [[buffer(8)]]
#define NODE_THREADS                                                                 \
  uint k [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]], \
      uint width [[threads_per_threadgroup]]
bool dead(uchar2 c, uint card) { return c.x == card || c.y == card; }
kernel void propagate_reach(TRAVERSAL_BUFFERS, device const float* old_regrets [[buffer(12)]], NODE_THREADS) {
  uint task = ids[k], player = task & 1;
  TraversalNode n = nodes[task >> 1];
  uint hands = player ? q.hands1 : q.hands0;
  device float* reach = player ? ip_reach : oop_reach;
  device const float* strategy_regrets = q.phase ? regrets : old_regrets;
  for (uint h = tid; h < hands; h += width) {
    float parent = reach[n.row[player] * q.stride + h];
    if (n.kind == Deal) {
      uchar2 hand = cards[player * q.stride + h];
      for (uint a = 0; a < n.count; ++a) {
        device const TraversalNode& child = nodes[n.child + a];
        reach[child.row[player] * q.stride + h] = dead(hand, child.card) ? 0.f : parent;
      }
    } else {
      float total = 0;
      for (uint a = 0; a < n.count; ++a) total += max(strategy_regrets[n.storage + a * hands + h], 0.f);
      for (uint a = 0; a < n.count; ++a) {
        float prob = total > 0 ? max(strategy_regrets[n.storage + a * hands + h], 0.f) / total : 1.f / n.count;
        reach[nodes[n.child + a].row[player] * q.stride + h] = parent * prob;
      }
    }
  }
}
kernel void update_values(TRAVERSAL_BUFFERS, device const uint2* iso [[buffer(9)]],
                          device const ushort* perms [[buffer(10)]],
                          device const float* old_regrets [[buffer(12)]],
                          device const float* old_sums [[buffer(13)]], NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  uint H = q.p ? q.hands1 : q.hands0;
  device const float* own_reach = q.p ? ip_reach : oop_reach;
  for (uint h = tid; h < H; h += width) {
    float sum = 0;
    bool updating = n.kind == Decision && n.actor == q.p;
    if (updating)
      for (uint a = 0; a < n.count; ++a) sum += max(old_regrets[n.storage + a * H + h], 0.f);
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
        float w = updating ? (sum > 0 ? max(old_regrets[n.storage + a * H + h], 0.f) / sum : 1.f / n.count) : 1.f;
        node_value = fma(w, val, node_value);
      }
    values[i * q.stride + h] = node_value;
    if (updating)
      for (uint a = 0; a < n.count; ++a) {
        uint j = n.storage + a * H + h;
        float r = old_regrets[j];
        float s = sum > 0 ? max(r, 0.f) / sum : 1.f / n.count;
        regrets[j] = r * (r > 0 ? q.positive_discount : q.negative_discount) +
                     (values[(n.child + a) * q.stride + h] - node_value);
        strategy_sums[j] = q.accumulate
                               ? fma(own_reach[n.row[q.p] * q.stride + h], s, old_sums[j] * q.average_discount)
                               : old_sums[j];
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
    values[i * q.stride + h] =
        fma(n.win[q.p] - n.tie[q.p], weaker, fma(n.tie[q.p] - n.loss[q.p], weaker_or_tied, n.loss[q.p] * compat));
  }
}
kernel void allin(TRAVERSAL_BUFFERS, device const uint* offsets [[buffer(9)]],
                  device const float* eq [[buffer(10)]], uint2 group [[threadgroup_position_in_grid]],
                  uint tid [[thread_index_in_threadgroup]], ushort lane [[thread_index_in_simdgroup]]) {
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
