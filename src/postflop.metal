#include <metal_stdlib>
using namespace metal;
#if WSOLVER_BFLOAT
using RegretWord = ushort;
float load_regret(RegretWord x) { return as_type<float>(bfloat_regret_bits(x)); }
RegretWord store_regret(float x) {
  return bfloat_regret_word(as_type<uint>(x));
}
#else
using RegretWord = float;
float load_regret(float x) { return x; }
float store_regret(float x) { return x; }
#endif
#if WSOLVER_FP16_SUMS
using SumWord = half;
constant float sum_scale = 32.f;
#else
using SumWord = float;
constant float sum_scale = 1.f;
#endif
#define TRAVERSAL_BUFFERS                                                                                            \
  device const TraversalNode *nodes [[buffer(0)]], device const uint *ids [[buffer(1)]],                             \
      constant IterationParams &q [[buffer(2)]], device float *oop_reach [[buffer(3)]],                              \
      device float *ip_reach [[buffer(4)]], device float *values [[buffer(5)]], device RegretWord* regrets [[buffer(6)]], \
      device SumWord* strategy_sums [[buffer(7)]], device const uchar2 *cards [[buffer(8)]]
#define NODE_THREADS                                                                 \
  uint k [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]], \
      uint width [[threads_per_threadgroup]]
constant bool sqrt_regret_weights [[function_constant(0)]];
constant uint average_mode [[function_constant(1)]];
float average_reach(float reach, constant IterationParams& q) {
  return average_mode == WeightedAverage ? reach * q.sample_weight : reach;
}
void update_average(device SumWord* sums, device const SumWord* old, uint index, float reach, float probability,
                    constant IterationParams& q) {
  if (average_mode != SkipAverage) {
    sums[index] = q.accumulate ? SumWord(fma(sum_scale * average_reach(reach, q), probability, float(old[index]) * q.average_discount)) : old[index];
  }
}
template <class T> T regret_weight(T value) {
  const T positive = max(value, T(0.f));
  return sqrt_regret_weights ? sqrt(positive) : positive;
}
bool dead(uchar2 c, uint card) { return c.x == card || c.y == card; }
bool blocked(uchar2 c, uint board) { return board && (dead(c, (board & 255) - 1) || dead(c, (board >> 8) - 1)); }

kernel void propagate_reach(TRAVERSAL_BUFFERS, device const RegretWord* old_regrets [[buffer(12)]], NODE_THREADS) {
  uint task = ids[k], player = task & 1;
  TraversalNode n = nodes[task >> 1];
  uint hands = player ? q.hands1 : q.hands0;
  device float* reach = player ? ip_reach : oop_reach;
  device const RegretWord* strategy_regrets = q.phase ? regrets : old_regrets;
  for (uint h = tid; h < hands; h += width) {
    float parent = reach[n.reach_offset[player] + h];
    float total = 0;
    for (uint a = 0; a < n.count; ++a) total += regret_weight(load_regret(strategy_regrets[n.storage + a * hands + h]));
    for (uint a = 0; a < n.count; ++a) {
      float prob = total > 0 ? regret_weight(load_regret(strategy_regrets[n.storage + a * hands + h])) / total : 1.f / n.count;
      reach[nodes[n.child + a].reach_offset[player] + h] = parent * prob;
    }
  }
}
kernel void update_values(TRAVERSAL_BUFFERS, device const uint2* iso [[buffer(9)]],
                          device const ushort* perms [[buffer(10)]], device const RegretWord* old_regrets [[buffer(12)]],
                          device const SumWord* old_sums [[buffer(13)]], NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  uint H = q.p ? q.hands1 : q.hands0;
  device const float* own_reach = q.p ? ip_reach : oop_reach;
  for (uint h = tid; h < H; h += width) {
    float sum = 0;
    bool updating = n.kind == Decision && n.actor == q.p;
    if (updating)
      for (uint a = 0; a < n.count; ++a) sum += regret_weight(load_regret(old_regrets[n.storage + a * H + h]));
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
        float val = values[(a == 0 ? n.value_row : n.child_value + a - 1) * q.stride + h];
        float w = updating ? (sum > 0 ? regret_weight(load_regret(old_regrets[n.storage + a * H + h])) / sum : 1.f / n.count) : 1.f;
        node_value = fma(w, val, node_value);
      }
    if (updating)
      for (uint a = 0; a < n.count; ++a) {
        uint j = n.storage + a * H + h;
        float r = load_regret(old_regrets[j]);
        float s = sum > 0 ? regret_weight(r) / sum : 1.f / n.count;
        regrets[j] = store_regret(r * (r > 0 ? q.positive_discount : q.negative_discount) +
                     (values[(a == 0 ? n.value_row : n.child_value + a - 1) * q.stride + h] - node_value));
        float reach = blocked(cards[q.p * q.stride + h], n.public_cards) ? 0.f : own_reach[n.reach_offset[q.p] + h];
        update_average(strategy_sums, old_sums, j, reach, s, q);
      }
    values[n.value_row * q.stride + h] = node_value;
  }
}
kernel void advance_forced(TRAVERSAL_BUFFERS, device const RegretWord* old_regrets [[buffer(12)]],
                           device const SumWord* old_sums [[buffer(13)]], NODE_THREADS) {
  TraversalNode n = nodes[ids[k]];
  uint hands = q.p ? q.hands1 : q.hands0;
  device const float* reach = (q.p ? ip_reach : oop_reach) + n.reach_offset[q.p];
  for (uint h = tid; h < hands; h += width) {
    uint j = n.storage + h;
    float r = load_regret(old_regrets[j]);
    regrets[j] = store_regret(r * (r > 0 ? q.positive_discount : q.negative_discount));
    float weight = reach[h];
    if (blocked(cards[q.p * q.stride + h], n.public_cards)) weight = 0;
    if (average_mode != SkipAverage)
      strategy_sums[j] = q.accumulate ? SumWord(fma(float(old_sums[j]), q.average_discount, sum_scale * average_reach(weight, q))) : old_sums[j];
  }
}
template <typename Sum = float, typename Reach, typename Entry>
void river_prefixes(device const BoardPrefixes& b, device const PrefixEntry* indices, Reach reach,
                    threadgroup Entry* prefix, uint tid) {
  if (tid < 52) {
    uint start = b.start[tid + 1], end = b.start[tid + 2] - 1;
    Sum s = 0;
    if (start == end) prefix[first_card_prefix + tid] = 0;
    for (uint j = start + 1; j <= end; ++j) {
      PrefixEntry entry = indices[b.index_offset + j];
      s += Sum(reach[entry.hand]);
      if (entry.slot) prefix[entry.slot] = s;
    }
  }
  if (tid >= 64 && tid < 96) {
    uint lane = tid - 64;
    Sum carry = 0;
    if (!lane) {
      prefix[empty_prefix] = 0;
      if (b.start[1] == 1) prefix[total_prefix] = 0;
    }
    for (uint base = 1; base < b.start[1]; base += 32) {
      uint j = base + lane;
      PrefixEntry entry = {};
      if (j < b.start[1]) entry = indices[b.index_offset + j];
      Sum w = j < b.start[1] ? Sum(reach[entry.hand]) : Sum(0);
      Sum t = simd_prefix_inclusive_sum(w) + carry;
      carry = simd_broadcast(t, 31);
      if (entry.slot) prefix[entry.slot] = t;
    }
  }
  threadgroup_barrier(mem_flags::mem_threadgroup);
}

template <typename Entry, typename Sum>
Sum compatible_mass(threadgroup const Entry* prefix, uchar2 cards, Sum same) {
  return Sum(prefix[total_prefix]) - Sum(prefix[first_card_prefix + cards.x]) -
         Sum(prefix[first_card_prefix + cards.y]) + same;
}

template <typename Sum, typename Entry>
Sum showdown_payoff(HandPrefixes m, threadgroup const Entry* prefix, Sum same, Sum mass, Sum win, Sum loss, Sum tie) {
  Sum compat = m.live ? mass : Sum(0);
  Sum weaker = Sum(prefix[m.lo[0]]) - Sum(prefix[m.lo[1]]) - Sum(prefix[m.lo[2]]);
  Sum weaker_or_tied = Sum(prefix[m.hi[0]]) - Sum(prefix[m.hi[1]]) - Sum(prefix[m.hi[2]]) + same;
  return fma(win - tie, weaker, fma(tie - loss, weaker_or_tied, loss * compat));
}

void count_fold_reach(device const BoardPrefixes& board, device const PrefixEntry* indices,
                      threadgroup const float* reach, threadgroup float* totals, uint hands, uint tid) {
  threadgroup_barrier(mem_flags::mem_threadgroup);
  if (tid < 52) {
    float sum = 0;
    for (uint j = board.start[tid + 1] + 1; j < board.start[tid + 2]; ++j)
      sum += reach[indices[board.index_offset + j].hand];
    totals[tid] = sum;
  }
  if (tid >= 64 && tid < 96) {
    float sum = 0;
    for (uint j = tid - 64; j < hands; j += 32) sum += reach[j];
    sum = simd_sum(sum);
    if (tid == 64) totals[52] = sum;
  }
}

float compatible_fold_reach(threadgroup const float* totals, threadgroup const float* reach, uchar2 cards, short same) {
  return totals[52] - totals[cards.x] - totals[cards.y] + (same >= 0 ? reach[same] : 0.f);
}

kernel void terminal(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                     device const PrefixEntry* indices [[buffer(10)]], device const HandPrefixes* heroes [[buffer(11)]],
                     threadgroup float* prefix [[threadgroup(0)]], NODE_THREADS) {
  uint i = ids[k];
  TraversalNode n = nodes[i];
  device const BoardPrefixes& b = boards[n.aux];
  uint H = q.p ? q.hands1 : q.hands0;
  device const float* reach = (q.p ? oop_reach : ip_reach) + n.reach_offset[1 - q.p];
  if (n.kind == Fold) {
    threadgroup float* live = prefix + 53;
    uint O = q.p ? q.hands0 : q.hands1;
    for (uint j = tid; j < O; j += width)
      live[j] = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : reach[j];
    count_fold_reach(b, indices, live, prefix, O, tid);
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint h = tid; h < H; h += width) {
      uchar2 c = cards[q.p * q.stride + h];
      short same = heroes[b.hand_offset + h].same;
      float compat = compatible_fold_reach(prefix, live, c, same);
      values[n.value_row * q.stride + h] = n.win[q.p] * compat;
    }
    return;
  }
  river_prefixes(b, indices, reach, prefix, tid);
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    float same = m.same >= 0 ? reach[m.same] : 0;
    uchar2 c = cards[q.p * q.stride + h];
    float mass = compatible_mass(prefix, c, same);
    values[n.value_row * q.stride + h] = showdown_payoff(m, prefix, same, mass, n.win[q.p], n.loss[q.p], n.tie[q.p]);
  }
}
kernel void river_call_fold(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                            device const PrefixEntry* indices [[buffer(10)]],
                            device const HandPrefixes* heroes [[buffer(11)]],
                            device const RegretWord* old_regrets [[buffer(12)]], device const SumWord* old_sums [[buffer(13)]],
                            threadgroup float* prefix [[threadgroup(0)]], NODE_THREADS) {
  TraversalNode n = nodes[ids[k]];
  TraversalNode call = nodes[n.child];
  float fold_pay = nodes[n.child + 1].win[q.p];
  device const BoardPrefixes& b = boards[call.aux];
  uint H = q.p ? q.hands1 : q.hands0, O = q.p ? q.hands0 : q.hands1;
  device const float* opp = (q.p ? oop_reach : ip_reach) + n.reach_offset[1 - q.p];
  bool updating = q.p == n.actor;
  threadgroup float* call_reach = prefix + b.prefix_count;
  threadgroup float* fold_reach = call_reach + O;
  threadgroup float* fold_mass = fold_reach + O;
  if (updating) {
    river_prefixes(b, indices, opp, prefix, tid);
  } else {
    device const RegretWord* r = q.phase ? regrets : old_regrets;
    for (uint j = tid; j < O; j += width) {
      float c = regret_weight(load_regret(r[n.storage + j])), f = regret_weight(load_regret(r[n.storage + O + j]));
      float total = c + f;
      float mass = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : opp[j];
      call_reach[j] = mass * (total > 0 ? c / total : .5f);
      fold_reach[j] = mass * (total > 0 ? f / total : .5f);
    }
    count_fold_reach(b, indices, fold_reach, fold_mass, O, tid);
    river_prefixes(b, indices, call_reach, prefix, tid);
  }
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    uchar2 c = cards[q.p * q.stride + h];
    float same = m.same < 0 ? 0 : updating ? opp[m.same] : call_reach[m.same];
    float compat = compatible_mass(prefix, c, same);
    float cv = showdown_payoff(m, prefix, same, compat, call.win[q.p], call.loss[q.p], call.tie[q.p]);
    float v;
    if (updating) {
      float fv = fold_pay * compat;
      uint j = n.storage + h;
      float rc = load_regret(old_regrets[j]), rf = load_regret(old_regrets[j + H]);
      float total = regret_weight(rc) + regret_weight(rf);
      float sc = total > 0 ? regret_weight(rc) / total : .5f, sf = total > 0 ? regret_weight(rf) / total : .5f;
      v = fma(sf, fv, sc * cv);
      regrets[j] = store_regret(rc * (rc > 0 ? q.positive_discount : q.negative_discount) + (cv - v));
      regrets[j + H] = store_regret(rf * (rf > 0 ? q.positive_discount : q.negative_discount) + (fv - v));
      float reach = blocked(c, n.public_cards) ? 0.f : (q.p ? ip_reach : oop_reach)[n.reach_offset[q.p] + h];
      update_average(strategy_sums, old_sums, j, reach, sc, q);
      update_average(strategy_sums, old_sums, j + H, reach, sf, q);
    } else {
      float fv = fold_pay * compatible_fold_reach(fold_mass, fold_reach, c, m.same);
      v = cv + fv;
    }
    values[n.value_row * q.stride + h] = v;
  }
}

template <typename Count>
float4 allin_sums(device const Count* row, uint O, uint lane, float scale, uint p, device const float* reach,
                  uint4 reaches, float4 spread, float4 loss) {
  float4 sums = 0;
  for (uint j = lane; j < O; j += 32) {
    int count = row[j];
    if (count < 0) continue;
    float e = float(count) * scale;
    if (p) e = 1.f - e;
    float4 weights(reach[reaches.x + j], reach[reaches.y + j], reach[reaches.z + j], reach[reaches.w + j]);
    sums = fma(weights, fma(spread, float4(e), loss), sums);
  }
  return sums;
}

kernel void allin(TRAVERSAL_BUFFERS, device const AllinTable* tables [[buffer(9)]],
                  device const short* counts [[buffer(10)]], device const AllinBatch* batches [[buffer(14)]],
                  uint2 group [[threadgroup_position_in_grid]], uint tid [[thread_index_in_threadgroup]],
                  ushort lane [[thread_index_in_simdgroup]]) {
  uint h = group.y * 4 + tid / 32;
  uint H = q.p ? q.hands1 : q.hands0, O = q.p ? q.hands0 : q.hands1;
  if (h >= H) return;
  device const AllinBatch& batch = batches[group.x];
  uint4 reaches;
  float4 spread, loss;
  for (uint k = 0; k < 4; ++k) {
    reaches[k] = batch.reach_offset[q.p][k];
    spread[k] = batch.win_minus_loss[q.p][k];
    loss[k] = batch.loss[q.p][k];
  }
  device const float* reach = q.p ? oop_reach : ip_reach;
  AllinTable table = tables[batch.table];
  device const short* start = counts + table.word_offset;
  float4 sums;
  if (table.bytes_per_count == 1)
    sums = allin_sums(reinterpret_cast<device const char*>(start) + h * O, O, lane, table.scale, q.p,
                      reach, reaches, spread, loss);
  else
    sums = allin_sums(start + h * O, O, lane, table.scale, q.p, reach, reaches, spread, loss);
  for (uint k = 0; k < 4; ++k) {
    float s = simd_sum(sums[k]);
    if (!lane && k < batch.count) values[batch.value_offset[k] + h] = s;
  }
}

float2 binary_strategy(device const RegretWord* r, uint j, uint H) {
  float2 pos = regret_weight(float2(load_regret(r[j]), load_regret(r[j + H])));
  float total = pos.x + pos.y;
  return total > 0 ? pos / total : float2(.5f);
}

float3 raise_strategy(device const RegretWord* r, uint j, uint H) {
  float3 pos = regret_weight(float3(load_regret(r[j]), load_regret(r[j + H]), load_regret(r[j + 2 * H])));
  float sum = pos.x + pos.y + pos.z;
  return sum > 0 ? pos / sum : float3(1.f / 3.f);
}

template <uint N, typename Vec>
float update_actions(device RegretWord* regrets, device SumWord* sums, device const RegretWord* old_r, device const SumWord* old_s,
                     constant IterationParams& q, uint j, uint H, float reach, Vec policy, Vec cfv) {
  float value = policy.x * cfv.x;
  for (uint a = 1; a < N; ++a) value = fma(policy[a], cfv[a], value);
  for (uint a = 0; a < N; ++a) {
    uint k = j + a * H;
    float r = load_regret(old_r[k]);
    regrets[k] = store_regret(r * (r > 0 ? q.positive_discount : q.negative_discount) + (cfv[a] - value));
    update_average(sums, old_s, k, reach, policy[a], q);
  }
  return value;
}

float3 round_reach(device const RiverRound& r, device const RegretWord* opp_r, float weight, uint j, uint O, bool leading) {
  if (r.kind == BetWithRaise) {
    if (leading) {
      float3 response = raise_strategy(opp_r, r.storage[1] + j, O);
      return weight * float3(response.xy, 1.f);
    }
    float2 first = binary_strategy(opp_r, r.storage[0] + j, O);
    float2 response = binary_strategy(opp_r, r.storage[2] + j, O);
    float bet = weight * first.x;
    return float3(bet * response.x, bet, weight * first.y);
  }
  if (r.kind == FinalRaise) {
    if (leading) {
      float2 response = binary_strategy(opp_r, r.storage[1] + j, O);
      return weight * float3(response.x, 1.f, response.y);
    }
    return weight * raise_strategy(opp_r, r.storage[0] + j, O);
  }
  if (leading) {
    float2 response = binary_strategy(opp_r, r.storage[1] + j, O);
    float2 probe = binary_strategy(opp_r, r.storage[2] + j, O);
    return weight * float3(response.x, probe.x, probe.y);
  }
  float2 first = binary_strategy(opp_r, r.storage[0] + j, O);
  float2 response = binary_strategy(opp_r, r.storage[3] + j, O);
  float check = weight * first.y;
  return float3(weight * first.x, check * response.x, check);
}

float update_river_round(device const RiverRound& r, constant IterationParams& q, device const RegretWord* old_r,
                         device const SumWord* old_s, device RegretWord* regrets, device SumWord* strategy_sums, uint h, uint H,
                         float reach, bool leading, float3 sd, float3 mass) {
  uint p = q.p;
  float value;
  if (r.kind == BetWithRaise) {
    if (leading) {
      float2 first = binary_strategy(old_r, r.storage[0] + h, H);
      float2 response = binary_strategy(old_r, r.storage[2] + h, H);
      float raised = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[2] + h, H,
                                       reach * first.x, response, float2(sd.x, r.fold[0][p] * mass.x));
      float bet = (raised + sd.y) + r.fold[1][p] * ((mass.z - mass.x) - mass.y);
      return update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[0] + h, H, reach,
                               first, float2(bet, sd.z));
    }
    uint j = r.storage[1] + h;
    float3 policy = raise_strategy(old_r, j, H);
    float3 cfv = {sd.x + r.fold[0][p] * (mass.y - mass.x), sd.y, r.fold[1][p] * mass.y};
    return update_actions<3>(regrets, strategy_sums, old_r, old_s, q, j, H, reach, policy, cfv) + sd.z;
  }
  if (r.kind == FinalRaise) {
    if (leading) {
      uint j = r.storage[0] + h;
      float3 policy = raise_strategy(old_r, j, H);
      float3 cfv = {sd.x + mass.z * r.fold[0][p], sd.y, mass.y * r.fold[1][p]};
      value = update_actions<3>(regrets, strategy_sums, old_r, old_s, q, j, H, reach, policy, cfv);
    } else {
      float2 response = binary_strategy(old_r, r.storage[1] + h, H);
      float raised = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[1] + h, H, reach, response,
                                       float2(sd.x, r.fold[0][p] * mass.x));
      value = (raised + sd.y) + r.fold[1][p] * mass.z;
    }
  } else if (leading) {
    float lead = sd.x + r.fold[0][p] * (mass.y + mass.z - mass.x);
    float2 first = binary_strategy(old_r, r.storage[0] + h, H);
    float2 response = binary_strategy(old_r, r.storage[3] + h, H);
    float check_bet = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[3] + h, H, reach * first.y,
                                        response, float2(sd.y, r.fold[1][p] * mass.y));
    value = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[0] + h, H, reach, first,
                              float2(lead, check_bet + sd.z));
  } else {
    float2 call = binary_strategy(old_r, r.storage[1] + h, H);
    float2 probe = binary_strategy(old_r, r.storage[2] + h, H);
    float vs_lead = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[1] + h, H, reach, call,
                                      float2(sd.x, r.fold[0][p] * mass.x));
    float bet = sd.y + r.fold[1][p] * (mass.z - mass.y);
    value = vs_lead + update_actions<2>(regrets, strategy_sums, old_r, old_s, q, r.storage[2] + h, H, reach, probe,
                                        float2(bet, sd.z));
  }
  return value;
}

kernel void river_bet_check(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]],
                            device const PrefixEntry* indices [[buffer(10)]],
                            device const HandPrefixes* heroes [[buffer(11)]], device const RegretWord* old_r [[buffer(12)]],
                            device const SumWord* old_s [[buffer(13)]], device const RiverRound* rounds [[buffer(14)]],
                            threadgroup float2* prefix [[threadgroup(0)]], NODE_THREADS) {
  device const RiverRound& r = rounds[k];
  TraversalNode n = nodes[r.root];
  device const BoardPrefixes& b = boards[r.board];
  uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1;
  bool bettor = n.actor == p;
  device const float* opp = (p ? oop_reach : ip_reach) + n.reach_offset[1 - p];
  device const float* own = (p ? ip_reach : oop_reach) + n.reach_offset[p];
  device const RegretWord* opp_regrets = q.phase ? regrets : old_r;
  threadgroup float2* reaches = prefix + b.prefix_count;
  for (uint j = tid; j < O; j += width) {
    float2 policy = binary_strategy(opp_regrets, r.storage[bettor ? 1 : 0] + j, O);
    reaches[j] = opp[j] * (bettor ? float2(policy.x, 1.f) : policy);
  }
  threadgroup_barrier(mem_flags::mem_threadgroup);
  river_prefixes<float2>(b, indices, reaches, prefix, tid);
  float2 win = {r.win[0][p], r.win[1][p]}, loss = {r.loss[0][p], r.loss[1][p]}, tie = {r.tie[0][p], r.tie[1][p]};
  for (uint h = tid; h < H; h += width) {
    HandPrefixes m = heroes[b.hand_offset + h];
    float2 same = m.same >= 0 ? reaches[m.same] : float2(0);
    uchar2 c = cards[p * q.stride + h];
    float2 mass = compatible_mass(prefix, c, same);
    float2 sd = showdown_payoff(m, prefix, same, mass, win, loss, tie);
    uint j = r.storage[bettor ? 0 : 1] + h;
    float2 policy = binary_strategy(old_r, j, H);
    float reach = blocked(c, n.public_cards) ? 0.f : own[h];
    float2 cfv = bettor ? float2(sd.x + r.fold[0][p] * (mass.y - mass.x), sd.y) : float2(sd.x, r.fold[0][p] * mass.x);
    float value = update_actions<2>(regrets, strategy_sums, old_r, old_s, q, j, H, reach, policy, cfv);
    values[n.value_row * q.stride + h] = bettor ? value : value + sd.y;
  }
}

float4 two_bet_strategy(device const RegretWord* r, uint j, uint H, bool can_fold, bool has_big_bet) {
  if (!has_big_bet) return float4(0.f, raise_strategy(r, j, H));
  if (!can_fold) return float4(raise_strategy(r, j, H), 0.f);
  float4 positive = regret_weight(float4(load_regret(r[j]), load_regret(r[j + H]), load_regret(r[j + 2 * H]), load_regret(r[j + 3 * H])));
  float sum = positive.x + positive.y + positive.z + positive.w;
  return sum > 0 ? positive / sum : float4(.25f);
}

float4 two_bet_reaches(device const TwoBetRiver& r, device const RegretWord* opp_r, float weight, uint j, uint O,
                       bool bettor, bool can_fold) {
  if (bettor) {
    float2 big = r.has_big_bet ? binary_strategy(opp_r, r.storage[1] + j, O) : float2(0.f);
    float3 small = raise_strategy(opp_r, r.storage[2] + j, O);
    return weight * float4(big.x, small.x, small.y, 1.f);
  }
  float4 root = two_bet_strategy(opp_r, r.storage[0] + j, O, can_fold, r.has_big_bet);
  float2 response = binary_strategy(opp_r, r.storage[3] + j, O);
  float small = weight * root.y;
  return float4(weight * root.x, small * response.x, small, weight * root.z);
}

float update_two_bet_river(device const TwoBetRiver& r, constant IterationParams& q, device const RegretWord* old_r,
                           device const SumWord* old_s, device RegretWord* regrets, device SumWord* sums, uint h, uint H,
                           float reach, bool bettor, bool can_fold, float fold_value, float4 sd, float4 mass) {
  uint p = q.p;
  if (bettor) {
    uint j = r.storage[0] + h, response = r.storage[3] + h;
    float4 root = two_bet_strategy(old_r, j, H, can_fold, r.has_big_bet);
    float2 policy = binary_strategy(old_r, response, H);
    float vs_raise = update_actions<2>(regrets, sums, old_r, old_s, q, response, H, reach * root.y, policy,
                                       float2(sd.y, mass.y * r.fold[1][p]));
    float big = sd.x + (mass.w - mass.x) * r.fold[0][p];
    float small = (vs_raise + sd.z) + ((mass.w - mass.y) - mass.z) * r.fold[2][p];
    if (!r.has_big_bet)
      return update_actions<3>(regrets, sums, old_r, old_s, q, j, H, reach, root.yzw,
                               float3(small, sd.w, mass.w * r.fold[3][p]));
    if (can_fold)
      return update_actions<4>(regrets, sums, old_r, old_s, q, j, H, reach, root,
                               float4(big, small, sd.w, mass.w * r.fold[3][p]));
    return update_actions<3>(regrets, sums, old_r, old_s, q, j, H, reach, root.xyz, float3(big, small, sd.w));
  }
  uint small = r.storage[2] + h;
  float big_value = 0;
  if (r.has_big_bet) {
    uint big = r.storage[1] + h;
    float2 big_policy = binary_strategy(old_r, big, H);
    big_value = update_actions<2>(regrets, sums, old_r, old_s, q, big, H, reach, big_policy,
                                  float2(sd.x, mass.x * r.fold[0][p]));
  }
  float3 small_policy = raise_strategy(old_r, small, H);
  float3 small_cfv = {sd.y + (mass.z - mass.y) * r.fold[1][p], sd.z, mass.z * r.fold[2][p]};
  float small_value = update_actions<3>(regrets, sums, old_r, old_s, q, small, H, reach, small_policy, small_cfv);
  return ((big_value + small_value) + sd.w) + fold_value;
}

#define TWO_BET_BUFFERS TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]], \
    device const PrefixEntry* indices [[buffer(10)]], device const HandPrefixes* heroes [[buffer(11)]], \
    device const RegretWord* old_r [[buffer(12)]], device const SumWord* old_s [[buffer(13)]], \
    device const TwoBetRiver* rounds [[buffer(14)]]

template <typename Vec>
void solve_two_bet_river(device const TwoBetRiver& r, device const TraversalNode& n, device const BoardPrefixes& b,
                         device const PrefixEntry* indices, device const HandPrefixes* heroes, device const uchar2* cards,
                         device const float* opp, device const float* own, device const RegretWord* old_r, device const SumWord* old_s,
                         device RegretWord* regrets, device SumWord* sums, device float* values, constant IterationParams& q,
                         threadgroup Vec* prefix, uint tid, uint width) {
  uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1;
  device const RegretWord* opp_r = q.phase ? regrets : old_r;
  threadgroup Vec* reaches = prefix + b.prefix_count;
  bool bettor = n.actor == p;
  constexpr uint lanes = sizeof(Vec) / sizeof(float);
  constexpr uint max_hands_per_thread = (1326 + 127) / 128;
  float2 first_mass[max_hands_per_thread], first_payoff[max_hands_per_thread];
  bool can_fold = n.count == 4 || !r.has_big_bet;
  float fold_value[max_hands_per_thread];
  if (can_fold && !bettor) {
    threadgroup float* folds = reinterpret_cast<threadgroup float*>(prefix);
    threadgroup float* totals = folds + O;
    for (uint j = tid; j < O; j += width) {
      float weight = blocked(cards[(1 - p) * q.stride + j], n.public_cards) ? 0.f : opp[j];
      folds[j] = weight * two_bet_strategy(opp_r, r.storage[0] + j, O, true, r.has_big_bet).w;
    }
    count_fold_reach(b, indices, folds, totals, O, tid);
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint h = tid; h < H; h += width) {
      uchar2 c = cards[p * q.stride + h];
      short same = heroes[b.hand_offset + h].same;
      fold_value[h / width] = r.fold[3][p] * compatible_fold_reach(totals, folds, c, same);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
  }
  for (uint group = 0; group < 4; group += lanes) {
    for (uint j = tid; j < O; j += width) {
      float4 weights = two_bet_reaches(r, opp_r, opp[j], j, O, bettor, can_fold);
      Vec row;
      for (uint lane = 0; lane < lanes; ++lane) row[lane] = weights[group + lane];
      reaches[j] = row;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    river_prefixes<Vec>(b, indices, reaches, prefix, tid);
    Vec win, loss, tie;
    for (uint lane = 0; lane < lanes; ++lane) {
      win[lane] = r.win[group + lane][p];
      loss[lane] = r.loss[group + lane][p];
      tie[lane] = r.tie[group + lane][p];
    }
    for (uint h = tid; h < H; h += width) {
      HandPrefixes m = heroes[b.hand_offset + h];
      uchar2 c = cards[p * q.stride + h];
      Vec same = m.same >= 0 ? reaches[m.same] : Vec(0.f);
      Vec mass = compatible_mass(prefix, c, same);
      Vec sd = showdown_payoff(m, prefix, same, mass, win, loss, tie);
      float4 all_mass, all_payoff;
      if constexpr (lanes == 2) {
        if (group == 0) {
          first_mass[h / width] = mass;
          first_payoff[h / width] = sd;
          continue;
        }
        all_mass = float4(first_mass[h / width], mass);
        all_payoff = float4(first_payoff[h / width], sd);
      } else {
        all_mass = mass;
        all_payoff = sd;
      }
      float reach = blocked(c, n.public_cards) ? 0.f : own[h];
      float fold = can_fold && !bettor ? fold_value[h / width] : 0.f;
      values[n.value_row * q.stride + h] =
          update_two_bet_river(r, q, old_r, old_s, regrets, sums, h, H, reach, bettor, can_fold, fold, all_payoff, all_mass);
    }
    if (group + lanes < 4) threadgroup_barrier(mem_flags::mem_threadgroup);
  }
}

#define TWO_BET_KERNEL(NAME, VEC) \
kernel void NAME(TWO_BET_BUFFERS, threadgroup VEC* prefix [[threadgroup(0)]], NODE_THREADS) { \
  device const TwoBetRiver& r = rounds[k]; \
  device const TraversalNode& n = nodes[r.root]; \
  solve_two_bet_river(r, n, boards[r.board], indices, heroes, cards, \
                      (q.p ? oop_reach : ip_reach) + n.reach_offset[1 - q.p], \
                      (q.p ? ip_reach : oop_reach) + n.reach_offset[q.p], \
                      old_r, old_s, regrets, strategy_sums, values, q, prefix, tid, width); \
}
TWO_BET_KERNEL(river_two_bets, float4)
TWO_BET_KERNEL(river_two_bets_wide, float2)

template <bool paired, typename Value, typename Entry, uint channels>
void solve_river_rounds(device const RiverRound* branches, device const TraversalNode& root, device const BoardPrefixes& b,
                      device const PrefixEntry* indices, device const HandPrefixes* heroes, device const uchar2* cards,
                      device const float* opp, device const float* own, device const RegretWord* old_r, device const SumWord* old_s,
                      device RegretWord* regrets, device SumWord* sums, device float* values, constant IterationParams& q,
                      threadgroup Entry* prefix, uint tid, uint width) {
  uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1;
  bool leading = paired ? root.actor != p : root.actor == p;
  device const RegretWord* opp_r = q.phase ? regrets : old_r;
  threadgroup Entry* reaches = prefix + b.prefix_count;
  constexpr uint max_rows = (1326 + 127) / 128;
  float lead_values[max_rows];
  constexpr uint branch_count = paired ? 2 : 1;
  for (uint branch = 0; branch < branch_count; ++branch) {
    device const RiverRound& r = branches[branch];
    float3 partial_mass[max_rows], partial_payoff[max_rows];
    for (uint base = 0; base < 3; base += channels) {
      for (uint o = tid; o < O; o += width) {
        float weight = opp[o];
        if (paired && leading) weight *= binary_strategy(opp_r, root.storage + o, O)[branch];
        float3 all = round_reach(r, opp_r, weight, o, O, leading);
        if constexpr (channels == 1) reaches[o] = all[base];
        else reaches[o] = all;
      }
      threadgroup_barrier(mem_flags::mem_threadgroup);
      river_prefixes<Value>(b, indices, reaches, prefix, tid);
      Value win, loss, tie;
      if constexpr (channels == 1) {
        win = r.win[base][p];
        loss = r.loss[base][p];
        tie = r.tie[base][p];
      } else {
        win = {r.win[0][p], r.win[1][p], r.win[2][p]};
        loss = {r.loss[0][p], r.loss[1][p], r.loss[2][p]};
        tie = {r.tie[0][p], r.tie[1][p], r.tie[2][p]};
      }
      for (uint h = tid; h < H; h += width) {
        HandPrefixes hero = heroes[b.hand_offset + h];
        uchar2 c = cards[p * q.stride + h];
        Value same = hero.same < 0 ? Value(0.f) : Value(reaches[hero.same]);
        Value mass = compatible_mass(prefix, c, same);
        Value sd = showdown_payoff(hero, prefix, same, mass, win, loss, tie);
        float3 all_mass, all_payoff;
        if constexpr (channels == 1) {
          partial_mass[h / width][base] = mass;
          partial_payoff[h / width][base] = sd;
          if (base < 2) continue;
          all_mass = partial_mass[h / width];
          all_payoff = partial_payoff[h / width];
        } else {
          all_mass = mass;
          all_payoff = sd;
        }
        float reach = blocked(c, root.public_cards) ? 0.f : own[h];
        float2 policy = paired && !leading ? binary_strategy(old_r, root.storage + h, H) : float2(1.f);
        float value = update_river_round(r, q, old_r, old_s, regrets, sums, h, H, reach * policy[branch],
                                         leading, all_payoff, all_mass);
        if constexpr (!paired) values[root.value_row * q.stride + h] = value;
        else if (!branch) lead_values[h / width] = value;
        else {
          float2 children = {lead_values[h / width], value};
          values[root.value_row * q.stride + h] = !leading
              ? update_actions<2>(regrets, sums, old_r, old_s, q, root.storage + h, H, reach, policy, children)
              : children.x + children.y;
        }
      }
      if (branch + 1 < branch_count || base + channels < 3) threadgroup_barrier(mem_flags::mem_threadgroup);
    }
  }
}

#define RIVER_ROUND_KERNEL(NAME, PAIRED, RECORD, BRANCHES, VALUE, ENTRY, CHANNELS) \
kernel void NAME(TRAVERSAL_BUFFERS, device const BoardPrefixes* boards [[buffer(9)]], \
                 device const PrefixEntry* indices [[buffer(10)]], device const HandPrefixes* heroes [[buffer(11)]], \
                 device const RegretWord* old_r [[buffer(12)]], device const SumWord* old_s [[buffer(13)]], \
                 device const RECORD* rounds [[buffer(14)]], threadgroup ENTRY* prefix [[threadgroup(0)]], NODE_THREADS) { \
  device const TraversalNode& root = nodes[rounds[k].root]; \
  device const RiverRound* branches = BRANCHES; \
  solve_river_rounds<PAIRED, VALUE, ENTRY, CHANNELS>(branches, root, boards[branches[0].board], indices, heroes, cards, \
      (q.p ? oop_reach : ip_reach) + root.reach_offset[1 - q.p], (q.p ? ip_reach : oop_reach) + root.reach_offset[q.p], \
      old_r, old_s, regrets, strategy_sums, values, q, prefix, tid, width); \
}
RIVER_ROUND_KERNEL(river_betting_round, false, RiverRound, rounds + k, float3, packed_float3, 3)
RIVER_ROUND_KERNEL(river_betting_round_wide, false, RiverRound, rounds + k, float, float, 1)
RIVER_ROUND_KERNEL(river_lead_check, true, PairedRiverRound, rounds[k].branches, float3, packed_float3, 3)
RIVER_ROUND_KERNEL(river_lead_check_wide, true, PairedRiverRound, rounds[k].branches, float, float, 1)
