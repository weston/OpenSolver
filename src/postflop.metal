#include <metal_stdlib>
using namespace metal;
#define TRAVERSAL_BUFFERS                                                                                            \
  device const TraversalNode *nodes [[buffer(0)]], device const uint *ids [[buffer(1)]],                             \
      constant IterationParams &q [[buffer(2)]], device float *oop_reach [[buffer(3)]],                              \
      device float *ip_reach [[buffer(4)]], device float *values [[buffer(5)]], device float *regrets [[buffer(6)]], \
      device float *strategy_sums [[buffer(7)]], device const uchar2 *cards [[buffer(8)]]
#define NODE_THREADS                                                                 \
  uint k [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]], \
      uint width [[threads_per_threadgroup]]
bool dead(uchar2 c, uint card) { return c.x == card || c.y == card; }
bool blocked(uchar2 c, uint board) { return board && (dead(c, (board & 255) - 1) || dead(c, (board >> 8) - 1)); }

kernel void propagate_reach(TRAVERSAL_BUFFERS, device const float* old_regrets [[buffer(12)]], NODE_THREADS) {
  uint task = ids[k], player = task & 1;
  TraversalNode n = nodes[task >> 1];
  uint hands = player ? q.hands1 : q.hands0;
  device float* reach = player ? ip_reach : oop_reach;
  device const float* strategy_regrets = q.phase ? regrets : old_regrets;
  for (uint h = tid; h < hands; h += width) {
    float parent = reach[n.row[player] * q.stride + h];
    float total = 0;
    for (uint a = 0; a < n.count; ++a) total += max(strategy_regrets[n.storage + a * hands + h], 0.f);
    for (uint a = 0; a < n.count; ++a) {
      float prob = total > 0 ? max(strategy_regrets[n.storage + a * hands + h], 0.f) / total : 1.f / n.count;
      reach[nodes[n.child + a].row[player] * q.stride + h] = parent * prob;
    }
  }
}
kernel void update_values(TRAVERSAL_BUFFERS, device const uint2* iso [[buffer(9)]],
                          device const ushort* perms [[buffer(10)]], device const float* old_regrets [[buffer(12)]],
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
        uint perm = m.y & 255;
        uint j = perm ? perms[perm * q.stride + h] : h;
        if (!dead(cards[q.p * q.stride + j], m.y >> 8)) node_value += values[m.x * q.stride + j];
      }
      node_value *= n.chance_weight;
    } else
      for (uint a = 0; a < n.count; ++a) {
        float val = values[(n.child_value + a) * q.stride + h];
        float w = updating ? (sum > 0 ? max(old_regrets[n.storage + a * H + h], 0.f) / sum : 1.f / n.count) : 1.f;
        node_value = fma(w, val, node_value);
      }
    values[n.value_row * q.stride + h] = node_value;
    if (updating)
      for (uint a = 0; a < n.count; ++a) {
        uint j = n.storage + a * H + h;
        float r = old_regrets[j];
        float s = sum > 0 ? max(r, 0.f) / sum : 1.f / n.count;
        regrets[j] = r * (r > 0 ? q.positive_discount : q.negative_discount) +
                     (values[(n.child_value + a) * q.stride + h] - node_value);
        strategy_sums[j] =
            q.accumulate
                ? fma(blocked(cards[q.p * q.stride + h], n.public_cards) ? 0.f : own_reach[n.row[q.p] * q.stride + h],
                      s, old_sums[j] * q.average_discount)
                : old_sums[j];
      }
  }
}
kernel void advance_forced(TRAVERSAL_BUFFERS, device const float* old_regrets [[buffer(12)]],
                           device const float* old_sums [[buffer(13)]], NODE_THREADS) {
  TraversalNode n = nodes[ids[k]];
  uint hands = q.p ? q.hands1 : q.hands0;
  device const float* reach = (q.p ? ip_reach : oop_reach) + n.row[q.p] * q.stride;
  for (uint h = tid; h < hands; h += width) {
    uint j = n.storage + h;
    float r = old_regrets[j];
    regrets[j] = r * (r > 0 ? q.positive_discount : q.negative_discount);
    float weight = reach[h];
    if (blocked(cards[q.p * q.stride + h], n.public_cards)) weight = 0;
    strategy_sums[j] = q.accumulate ? fma(old_sums[j], q.average_discount, weight) : old_sums[j];
  }
}
template <typename Reach>
void river_prefixes(device const BoardPrefixes& b, device const ushort* indices, Reach reach, threadgroup float* prefix,
                    uint tid) {
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
}

float showdown_payoff(HandPrefixes m, threadgroup const float* prefix, float same, float win, float loss, float tie) {
  float compat = prefix[m.end[0]] - prefix[m.end[1]] - prefix[m.end[2]] + same;
  float weaker = prefix[m.lo[0]] - prefix[m.lo[1]] - prefix[m.lo[2]];
  float weaker_or_tied = prefix[m.hi[0]] - prefix[m.hi[1]] - prefix[m.hi[2]] + same;
  return fma(win - tie, weaker, fma(tie - loss, weaker_or_tied, loss * compat));
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
    threadgroup float* live = prefix + 53;
    uint O = q.p ? q.hands0 : q.hands1;
    for (uint j = tid; j < O; j += width)
      live[j] = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : reach[j];
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (tid < 52) {
      float s = 0;
      for (uint j = b.start[tid + 1] + 1; j < b.start[tid + 2]; ++j) s += live[indices[b.index_offset + j]];
      prefix[tid] = s;
    }
    if (tid >= 64 && tid < 96) {
      float s = 0;
      uint lane = tid - 64;
      for (uint j = lane; j < (q.p ? q.hands0 : q.hands1); j += 32) s += live[j];
      s = simd_sum(s);
      if (!lane) prefix[52] = s;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint h = tid; h < H; h += width) {
      uchar2 c = cards[q.p * q.stride + h];
      short same = heroes[b.hand_offset + h].same;
      float compat = prefix[52] - prefix[c.x] - prefix[c.y] + (same >= 0 ? live[same] : 0);
      values[n.value_row * q.stride + h] = n.win[q.p] * compat;
    }
    return;
  }
  river_prefixes(b, indices, reach, prefix, tid);
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    values[n.value_row * q.stride + h] =
        showdown_payoff(m, prefix, m.same >= 0 ? reach[m.same] : 0, n.win[q.p], n.loss[q.p], n.tie[q.p]);
  }
}
kernel void river_call_fold(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                            device const ushort* indices [[buffer(10)]],
                            device const HandPrefixes* heroes [[buffer(11)]],
                            device const float* old_regrets [[buffer(12)]], device const float* old_sums [[buffer(13)]],
                            threadgroup float* prefix [[threadgroup(0)]], NODE_THREADS) {
  TraversalNode n = nodes[ids[k]];
  TraversalNode call = nodes[n.child];
  float fold_pay = nodes[n.child + 1].win[q.p];
  device const BoardPrefixes& b = boards[call.aux];
  uint H = q.p ? q.hands1 : q.hands0, O = q.p ? q.hands0 : q.hands1;
  device const float* opp = (q.p ? oop_reach : ip_reach) + n.row[1 - q.p] * q.stride;
  bool updating = q.p == n.actor;
  threadgroup float* call_reach = prefix + b.start[53];
  threadgroup float* fold_reach = call_reach + O;
  threadgroup float* fold_mass = fold_reach + O;
  if (updating) {
    river_prefixes(b, indices, opp, prefix, tid);
  } else {
    device const float* r = q.phase ? regrets : old_regrets;
    for (uint j = tid; j < O; j += width) {
      float c = max(r[n.storage + j], 0.f), f = max(r[n.storage + O + j], 0.f);
      float total = c + f;
      float mass = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : opp[j];
      call_reach[j] = mass * (total > 0 ? c / total : .5f);
      fold_reach[j] = mass * (total > 0 ? f / total : .5f);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (tid < 52) {
      float sum = 0;
      for (uint j = b.start[tid + 1] + 1; j < b.start[tid + 2]; ++j) sum += fold_reach[indices[b.index_offset + j]];
      fold_mass[tid] = sum;
    }
    if (tid >= 64 && tid < 96) {
      float sum = 0;
      for (uint j = tid - 64; j < O; j += 32) sum += fold_reach[j];
      sum = simd_sum(sum);
      if (tid == 64) fold_mass[52] = sum;
    }
    river_prefixes(b, indices, call_reach, prefix, tid);
  }
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    uchar2 c = cards[q.p * q.stride + h];
    float same = m.same < 0 ? 0 : updating ? opp[m.same] : call_reach[m.same];
    float cv = showdown_payoff(m, prefix, same, call.win[q.p], call.loss[q.p], call.tie[q.p]);
    float v;
    if (updating) {
      float compat = prefix[b.start[1] - 1] - prefix[b.start[c.x + 2] - 1] - prefix[b.start[c.y + 2] - 1] + same;
      float fv = fold_pay * compat;
      uint j = n.storage + h;
      float rc = old_regrets[j], rf = old_regrets[j + H];
      float total = max(rc, 0.f) + max(rf, 0.f);
      float sc = total > 0 ? max(rc, 0.f) / total : .5f, sf = total > 0 ? max(rf, 0.f) / total : .5f;
      v = fma(sf, fv, sc * cv);
      regrets[j] = rc * (rc > 0 ? q.positive_discount : q.negative_discount) + (cv - v);
      regrets[j + H] = rf * (rf > 0 ? q.positive_discount : q.negative_discount) + (fv - v);
      float reach = blocked(c, n.public_cards) ? 0.f : (q.p ? ip_reach : oop_reach)[n.row[q.p] * q.stride + h];
      strategy_sums[j] = q.accumulate ? fma(reach, sc, old_sums[j] * q.average_discount) : old_sums[j];
      strategy_sums[j + H] = q.accumulate ? fma(reach, sf, old_sums[j + H] * q.average_discount) : old_sums[j + H];
    } else {
      float fold_same = m.same < 0 ? 0 : fold_reach[m.same];
      float fv = fold_pay * (fold_mass[52] - fold_mass[c.x] - fold_mass[c.y] + fold_same);
      v = cv + fv;
    }
    values[n.value_row * q.stride + h] = v;
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
  if (!lane) values[n.value_row * q.stride + h] = s;
}

float2 binary_strategy(device const float* r, uint j, uint H) {
  float2 pos = max(float2(r[j], r[j + H]), 0.f);
  float total = pos.x + pos.y;
  return total > 0 ? pos / total : float2(.5f);
}

float3 raise_strategy(device const float* r, uint j, uint H) {
  float3 pos = max(float3(r[j], r[j + H], r[j + 2 * H]), 0.f);
  float sum = pos.x + pos.y + pos.z;
  return sum > 0 ? pos / sum : float3(1.f / 3.f);
}

float update_binary(device float* regrets, device float* sums, device const float* old_r, device const float* old_s,
                    constant IterationParams& q, uint j, uint H, float reach, float2 policy, float2 cfv) {
  float value = fma(policy.y, cfv.y, policy.x * cfv.x);
  for (uint a = 0; a < 2; ++a) {
    uint k = j + a * H;
    float r = old_r[k];
    regrets[k] = r * (r > 0 ? q.positive_discount : q.negative_discount) + (cfv[a] - value);
    sums[k] = q.accumulate ? fma(reach, policy[a], old_s[k] * q.average_discount) : old_s[k];
  }
  return value;
}

kernel void river_betting_round(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                                device const ushort* indices [[buffer(10)]],
                                device const HandPrefixes* heroes [[buffer(11)]],
                                device const float* old_r [[buffer(12)]], device const float* old_s [[buffer(13)]],
                                device const RiverRound* rounds [[buffer(14)]],
                                threadgroup packed_float3* prefix [[threadgroup(0)]], NODE_THREADS) {
  device const RiverRound& r = rounds[k];
  TraversalNode n = nodes[r.root];
  device const BoardPrefixes& b = boards[r.board];
  uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1;
  device const float* opp = (p ? oop_reach : ip_reach) + n.row[1 - p] * q.stride;
  device const float* own = (p ? ip_reach : oop_reach) + n.row[p] * q.stride;
  device const float* opp_r = q.phase ? regrets : old_r;
  threadgroup packed_float3* reaches = prefix + b.start[53];
  bool leading = n.actor == p;
  for (uint j = tid; j < O; j += width) {
    if (r.final_raise) {
      if (leading) {
        float2 response = binary_strategy(opp_r, r.storage[1] + j, O);
        reaches[j] = opp[j] * float3(response.x, 1.f, response.y);
      } else
        reaches[j] = opp[j] * raise_strategy(opp_r, r.storage[0] + j, O);
    } else if (leading) {
      float2 response = binary_strategy(opp_r, r.storage[1] + j, O);
      float2 probe = binary_strategy(opp_r, r.storage[2] + j, O);
      reaches[j] = opp[j] * float3(response.x, probe.x, probe.y);
    } else {
      float2 first = binary_strategy(opp_r, r.storage[0] + j, O);
      float2 response = binary_strategy(opp_r, r.storage[3] + j, O);
      float check = opp[j] * first.y;
      reaches[j] = float3(opp[j] * first.x, check * response.x, check);
    }
  }
  threadgroup_barrier(mem_flags::mem_threadgroup);
  if (tid < 52) {
    uint start = b.start[tid + 1], end = b.start[tid + 2];
    float3 sum = 0;
    prefix[start] = sum;
    for (uint j = start + 1; j < end; ++j) {
      sum += float3(reaches[indices[b.index_offset + j]]);
      prefix[j] = sum;
    }
  }
  if (tid >= 64 && tid < 96) {
    uint lane = tid - 64;
    float3 carry = 0;
    if (!lane) prefix[0] = carry;
    for (uint base = 1; base < b.start[1]; base += 32) {
      uint j = base + lane;
      float3 w = j < b.start[1] ? float3(reaches[indices[b.index_offset + j]]) : 0.f;
      float3 sum = simd_prefix_inclusive_sum(w) + carry;
      carry = simd_broadcast(sum, 31);
      if (j < b.start[1]) prefix[j] = sum;
    }
  }
  threadgroup_barrier(mem_flags::mem_threadgroup);
  float3 win = {r.win[0][p], r.win[1][p], r.win[2][p]};
  float3 loss = {r.loss[0][p], r.loss[1][p], r.loss[2][p]};
  float3 tie = {r.tie[0][p], r.tie[1][p], r.tie[2][p]};
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    float3 same = m.same >= 0 ? float3(reaches[m.same]) : 0.f;
    uchar2 c = cards[p * q.stride + h];
    float3 mass = float3(prefix[b.start[1] - 1]) - float3(prefix[b.start[c.x + 2] - 1]) -
                  float3(prefix[b.start[c.y + 2] - 1]) + same;
    float3 live = float3(prefix[m.end[0]]) - float3(prefix[m.end[1]]) - float3(prefix[m.end[2]]) + same;
    float3 lo = float3(prefix[m.lo[0]]) - float3(prefix[m.lo[1]]) - float3(prefix[m.lo[2]]);
    float3 hi = float3(prefix[m.hi[0]]) - float3(prefix[m.hi[1]]) - float3(prefix[m.hi[2]]) + same;
    float3 sd = fma(win - tie, lo, fma(tie - loss, hi, loss * live));
    float reach = blocked(c, n.public_cards) ? 0.f : own[h], value;
    if (r.final_raise) {
      if (leading) {
        uint j = r.storage[0] + h;
        float3 policy = raise_strategy(old_r, j, H);
        float3 cfv = {sd.x + mass.z * r.fold[0][p], sd.y, mass.y * r.fold[1][p]};
        value = fma(policy.z, cfv.z, fma(policy.y, cfv.y, policy.x * cfv.x));
        for (uint a = 0; a < 3; ++a) {
          uint k = j + a * H;
          float v = old_r[k];
          regrets[k] = v * (v > 0 ? q.positive_discount : q.negative_discount) + (cfv[a] - value);
          strategy_sums[k] = q.accumulate ? fma(reach, policy[a], old_s[k] * q.average_discount) : old_s[k];
        }
      } else {
        float2 response = binary_strategy(old_r, r.storage[1] + h, H);
        float raised = update_binary(regrets, strategy_sums, old_r, old_s, q, r.storage[1] + h, H, reach, response,
                                     float2(sd.x, r.fold[0][p] * mass.x));
        value = (raised + sd.y) + r.fold[1][p] * mass.z;
      }
    } else if (leading) {
      float lead = sd.x + r.fold[0][p] * (mass.y + mass.z - mass.x);
      float2 first = binary_strategy(old_r, r.storage[0] + h, H);
      float2 response = binary_strategy(old_r, r.storage[3] + h, H);
      float check_bet = update_binary(regrets, strategy_sums, old_r, old_s, q, r.storage[3] + h, H, reach * first.y,
                                      response, float2(sd.y, r.fold[1][p] * mass.y));
      value = update_binary(regrets, strategy_sums, old_r, old_s, q, r.storage[0] + h, H, reach, first,
                            float2(lead, check_bet + sd.z));
    } else {
      float2 call = binary_strategy(old_r, r.storage[1] + h, H);
      float2 probe = binary_strategy(old_r, r.storage[2] + h, H);
      float vs_lead = update_binary(regrets, strategy_sums, old_r, old_s, q, r.storage[1] + h, H, reach, call,
                                    float2(sd.x, r.fold[0][p] * mass.x));
      float bet = sd.y + r.fold[1][p] * (mass.z - mass.y);
      value = vs_lead + update_binary(regrets, strategy_sums, old_r, old_s, q, r.storage[2] + h, H, reach, probe,
                                      float2(bet, sd.z));
    }
    values[n.value_row * q.stride + h] = value;
  }
}
