// The postflop traversal for OpenCL and CUDA GPUs: a port of postflop.metal (see there, and metal.mm,
// for what each pass does), in the dialect of gpu_opencl.h / gpu_cuda.h. It differs from Metal in:
// - regrets and strategy sums are updated in place (one device buffer each): every work-item reads the
//   entries it writes before writing them, and a pass never reads entries another work-item of the same
//   phase writes, so this is Metal's arithmetic with its separate input and output buffers;
// - no SIMD-group operations or other OpenCL 2.0 features: scans and sums go through work-group
//   memory, so the code is the same on any wave size, on NVIDIA's OpenCL 1.2, and under CUDA;
// - Metal's function constants (square-root regret weights, the average mode) are IterationParams
//   fields, so one program serves every iteration; bfloat16 regrets and fp16 sums are separate builds;
// - a pass's node or record list offset is the id_offset argument instead of a buffer offset.
// Work-groups are at most 256 wide.

#if WSOLVER_BFLOAT
typedef ushort RegretWord;
FN float load_regret(RegretWord x) { return AS_FLOAT(((uint)x) << 16); }
FN RegretWord store_regret(float x) {
  const uint bits = AS_UINT(x);
  return (ushort)((bits + 0x7fffu + ((bits >> 16) & 1u)) >> 16);
}
#else
typedef float RegretWord;
FN float load_regret(float x) { return x; }
FN float store_regret(float x) { return x; }
#endif

#if WSOLVER_FP16_SUMS
// IEEE half words, converted in software so that every backend rounds alike (to nearest even, as
// fp16_sums.hpp on the host); the sums are scaled by 32 to stay clear of the subnormals.
typedef ushort SumWord;
#define SUM_SCALE 32.f
FN float load_sum(ushort h) {
  const uint sign = ((uint)h & 0x8000u) << 16, e = ((uint)h >> 10) & 0x1fu, m = (uint)h & 0x3ffu;
  if (e == 0) {
    const float v = (float)m * 5.9604644775390625e-8f;
    return sign ? -v : v;
  }
  if (e == 31) return AS_FLOAT(sign | 0x7f800000u | (m << 13));
  return AS_FLOAT(sign | ((e + 112u) << 23) | (m << 13));
}
FN ushort store_sum(float f) {
  uint x = AS_UINT(f);
  const uint sign = (x >> 16) & 0x8000u;
  x &= 0x7fffffffu;
  if (x >= 0x477ff000u) return (ushort)(sign | (x > 0x7f800000u ? 0x7e00u : 0x7c00u));
  if (x < 0x38800000u) return (ushort)(sign | (uint)RINT(AS_FLOAT(x) * 16777216.f));
  return (ushort)(sign | ((x + 0xfffu + ((x >> 13) & 1u) - 0x38000000u) >> 13));
}
#else
typedef float SumWord;
#define SUM_SCALE 1.f
FN float load_sum(float x) { return x; }
FN float store_sum(float x) { return x; }
#endif

#define TRAVERSAL_ARGS                                                                                         \
  G const TraversalNode *nodes, G const uint *ids, const IterationParams q, G float *oop_reach,               \
      G float *ip_reach, G float *values, G RegretWord *regrets, G SumWord *strategy_sums,                     \
      G const uchar2 *cards, const uint id_offset
#define TABLE_ARGS G const BoardPrefixes *boards, G const PrefixEntry *indices, G const HandPrefixes *heroes
#define NO_HAND 0xffffu
#define MAX_WIDTH 256
#define MAX_ROWS ((1326 + 127) / 128)  // hands per work-item at the narrowest width, 128

FN float pos0(float x) { return x > 0.f ? x : 0.f; }
FN float regret_weight(float value, uint sqrt_weights) {
  const float positive = pos0(value);
  return sqrt_weights ? sqrt(positive) : positive;
}
FN float average_reach(float reach, const IterationParams q) {
  return q.average_mode == WeightedAverage ? reach * q.sample_weight : reach;
}
FN void update_average(G SumWord* sums, uint index, float reach, float probability, const IterationParams q) {
  if (q.average_mode != SkipAverage && q.accumulate)
    sums[index] = store_sum(fma(SUM_SCALE * average_reach(reach, q), probability, load_sum(sums[index]) * q.average_discount));
}
FN float discounted(float r, float delta, const IterationParams q) {
  return r * (r > 0 ? q.positive_discount : q.negative_discount) + delta;
}
FN bool dead(uchar2 c, uint card) { return c.x == card || c.y == card; }
FN bool blocked(uchar2 c, uint board) { return board && (dead(c, (board & 255) - 1) || dead(c, (board >> 8) - 1)); }

// ----- work-group scans and sums (tmp: MAX_WIDTH floats; every work-item calls them) ----------------
FN float group_scan_exclusive(float x, L float* tmp) {
  const uint tid = TID, width = WIDTH;
  tmp[tid] = x;
  BARRIER();
  for (uint offset = 1; offset < width; offset <<= 1) {
    const float t = tid >= offset ? tmp[tid - offset] : 0.f;
    BARRIER();
    tmp[tid] += t;
    BARRIER();
  }
  const float result = tid ? tmp[tid - 1] : 0.f;
  BARRIER();
  return result;
}
FN float group_sum(float x, L float* tmp) {
  const uint tid = TID, width = WIDTH;
  tmp[tid] = x;
  BARRIER();
  for (uint half_width = width / 2; half_width; half_width /= 2) {
    if (tid < half_width) tmp[tid] += tmp[tid + half_width];
    BARRIER();
  }
  const float result = tmp[0];
  BARRIER();
  return result;
}

// ----- river prefix sums --------------------------------------------------------------------------
// A board's table (gpu_pack.hpp BoardTables): span 0 is every opponent hand, span 1 + c those holding
// card c, each by strength, each after a head entry that holds no hand. Metal sums each card span in
// one SIMD lane and the first span in one SIMD-group; here the whole work-group scans the whole table
// once: each work-item sums a run of entries, one scan places the runs, each run is summed again from
// its offset, recording the running total at each head it meets, and every recorded slot then drops
// the total at its span's head. Slots hold sums for N channels side by side.
FN uint span_of(L const uint* starts, uint j) {  // the c with starts[c] <= j < starts[c + 1]
  uint lo = 0, hi = 53;
  while (hi - lo > 1) {
    const uint mid = (lo + hi) / 2;
    if (starts[mid] <= j) lo = mid;
    else hi = mid;
  }
  return lo;
}
FN void load_starts(G const BoardPrefixes* b, L uint* starts) {
  if (TID < 54) starts[TID] = b->start[TID];
  BARRIER();
}

#define RIVER_PREFIXES(NAME, AS, N, T, ZERO)                                                                   \
  FN void NAME(G const BoardPrefixes* b, G const PrefixEntry* indices, AS const float* reach, L float* prefix, \
               L const uint* starts, L float* heads, L float* tmp) {                                         \
    const uint tid = TID, width = WIDTH, n = starts[53];                                                     \
    G const PrefixEntry* entries = indices + b->index_offset;                                                \
    const uint per = (n + width - 1) / width, lo = min(n, tid * per), hi = min(n, lo + per);                 \
    T s = ZERO;                                                                                              \
    for (uint j = lo; j < hi; ++j) {                                                                         \
      const uint hand = entries[j].hand;                                                                     \
      if (hand != NO_HAND) s += vload##N(hand, reach);                                                       \
    }                                                                                                        \
    T run;                                                                                                   \
    for (uint c = 0; c < N; ++c) ((float*)&run)[c] = group_scan_exclusive(((float*)&s)[c], tmp);            \
    uint span = lo < hi ? span_of(starts, lo) : 0;                                                           \
    for (uint j = lo; j < hi; ++j) {                                                                         \
      const PrefixEntry e = entries[j];                                                                      \
      if (e.hand == NO_HAND) {                                                                               \
        span = span_of(starts, j);                                                                           \
        vstore##N(run, span, heads);                                                                         \
      } else {                                                                                               \
        run += vload##N(e.hand, reach);                                                                      \
        if (e.slot) vstore##N(run, e.slot, prefix);                                                          \
      }                                                                                                      \
    }                                                                                                        \
    BARRIER();                                                                                               \
    if (lo < hi) {                                                                                           \
      span = span_of(starts, lo);                                                                            \
      for (uint j = lo; j < hi; ++j) {                                                                       \
        while (j >= starts[span + 1]) ++span;                                                                \
        const uint slot = entries[j].slot;                                                                   \
        if (slot) vstore##N(vload##N(slot, prefix) - vload##N(span, heads), slot, prefix);                   \
      }                                                                                                      \
    }                                                                                                        \
    if (tid < 53 && starts[tid + 1] - starts[tid] == 1) vstore##N(ZERO, total_prefix + tid, prefix);        \
    if (tid == 0) vstore##N(ZERO, empty_prefix, prefix);                                                     \
    BARRIER();                                                                                               \
  }
RIVER_PREFIXES(river_prefixes_g1, G, 1, float, 0.f)
RIVER_PREFIXES(river_prefixes_l1, L, 1, float, 0.f)
RIVER_PREFIXES(river_prefixes_l2, L, 2, float2, F2S(0.f))
RIVER_PREFIXES(river_prefixes_l3, L, 3, float3, F3S(0.f))
RIVER_PREFIXES(river_prefixes_l4, L, 4, float4, F4S(0.f))

// The showdown value of a hero hand against the prefix sums, and the opponent mass it can meet.
#define SHOWDOWN_FNS(N, T)                                                                                     \
  FN T compatible_mass##N(L const float* prefix, uchar2 cards, T same) {                                     \
    return vload##N(total_prefix, prefix) - vload##N(first_card_prefix + cards.x, prefix) -                  \
           vload##N(first_card_prefix + cards.y, prefix) + same;                                             \
  }                                                                                                          \
  FN T showdown_payoff##N(HandPrefixes m, L const float* prefix, T same, T mass, T win, T loss, T tie,       \
                          T zero) {                                                                          \
    const T compat = m.live ? mass : zero;                                                                   \
    const T weaker = vload##N(m.lo[0], prefix) - vload##N(m.lo[1], prefix) - vload##N(m.lo[2], prefix);      \
    const T weaker_or_tied =                                                                                 \
        vload##N(m.hi[0], prefix) - vload##N(m.hi[1], prefix) - vload##N(m.hi[2], prefix) + same;           \
    return fma(win - tie, weaker, fma(tie - loss, weaker_or_tied, loss * compat));                           \
  }
SHOWDOWN_FNS(1, float)
SHOWDOWN_FNS(2, float2)
SHOWDOWN_FNS(3, float3)
SHOWDOWN_FNS(4, float4)

// Fold values need only card removal: per card (and in all) the opponent's reach. totals: 53 floats.
FN void count_fold_reach(G const BoardPrefixes* board, G const PrefixEntry* indices, L const float* reach,
                         L float* totals, uint hands, L float* tmp) {
  const uint tid = TID, width = WIDTH;
  BARRIER();
  if (tid < 52) {
    float sum = 0;
    for (uint j = board->start[tid + 1] + 1; j < board->start[tid + 2]; ++j)
      sum += reach[indices[board->index_offset + j].hand];
    totals[tid] = sum;
  }
  float sum = 0;
  for (uint j = tid; j < hands; j += width) sum += reach[j];
  sum = group_sum(sum, tmp);
  if (tid == 0) totals[52] = sum;
}
FN float compatible_fold_reach(L const float* totals, L const float* reach, uchar2 cards, short same) {
  return totals[52] - totals[cards.x] - totals[cards.y] + (same >= 0 ? reach[same] : 0.f);
}

// ----- the tree passes ----------------------------------------------------------------------------
KERNEL void propagate_reach(TRAVERSAL_ARGS) {
  const uint k = GROUP_X, tid = TID, width = WIDTH;
  const uint task = ids[id_offset + k], player = task & 1;
  const TraversalNode n = nodes[task >> 1];
  const uint hands = player ? q.hands1 : q.hands0;
  G float* reach = player ? ip_reach : oop_reach;
  for (uint h = tid; h < hands; h += width) {
    const float parent = reach[n.reach_offset[player] + h];
    float total = 0;
    for (uint a = 0; a < n.count; ++a)
      total += regret_weight(load_regret(regrets[n.storage + a * hands + h]), q.sqrt_regret_weights);
    for (uint a = 0; a < n.count; ++a) {
      const float prob = total > 0
          ? regret_weight(load_regret(regrets[n.storage + a * hands + h]), q.sqrt_regret_weights) / total
          : 1.f / n.count;
      reach[nodes[n.child + a].reach_offset[player] + h] = parent * prob;
    }
  }
}

KERNEL void update_values(TRAVERSAL_ARGS, G const uint2* iso, G const ushort* perms) {
  const uint k = GROUP_X, tid = TID, width = WIDTH;
  const TraversalNode n = nodes[ids[id_offset + k]];
  const uint H = q.p ? q.hands1 : q.hands0;
  G const float* own_reach = q.p ? ip_reach : oop_reach;
  const bool updating = n.kind == Decision && n.actor == q.p;
  const uint sw = q.sqrt_regret_weights;
  for (uint h = tid; h < H; h += width) {
    float sum = 0;
    if (updating)
      for (uint a = 0; a < n.count; ++a) sum += regret_weight(load_regret(regrets[n.storage + a * H + h]), sw);
    float node_value = 0;
    if (n.kind == Deal) {
      for (uint a = 0; a < n.deal_count; ++a) {
        const uint2 m = iso[n.deals + a];
        const uint perm = m.y & 255;
        const uint j = perm ? perms[perm * q.stride + h] : h;
        if (!dead(cards[q.p * q.stride + j], m.y >> 8)) node_value += values[m.x * q.stride + j];
      }
      node_value *= n.chance_weight;
    } else {
      for (uint a = 0; a < n.count; ++a) {
        const float val = values[(a == 0 ? n.value_row : n.child_value + a - 1) * q.stride + h];
        const float w = updating
            ? (sum > 0 ? regret_weight(load_regret(regrets[n.storage + a * H + h]), sw) / sum : 1.f / n.count)
            : 1.f;
        node_value = fma(w, val, node_value);
      }
    }
    if (updating)
      for (uint a = 0; a < n.count; ++a) {
        const uint j = n.storage + a * H + h;
        const float r = load_regret(regrets[j]);
        const float s = sum > 0 ? regret_weight(r, sw) / sum : 1.f / n.count;
        regrets[j] = store_regret(discounted(r, values[(a == 0 ? n.value_row : n.child_value + a - 1) * q.stride + h] - node_value, q));
        const float reach = blocked(cards[q.p * q.stride + h], n.public_cards) ? 0.f : own_reach[n.reach_offset[q.p] + h];
        update_average(strategy_sums, j, reach, s, q);
      }
    values[n.value_row * q.stride + h] = node_value;
  }
}

KERNEL void terminal(TRAVERSAL_ARGS, TABLE_ARGS SCRATCH_PARAM) {
  SCRATCH_DECL
  SHARED uint starts[54];
  SHARED float heads[53];
  SHARED float tmp[MAX_WIDTH];
  L float* prefix = scratch;
  const uint k = GROUP_X, tid = TID, width = WIDTH;
  const TraversalNode n = nodes[ids[id_offset + k]];
  G const BoardPrefixes* b = boards + n.aux;
  const uint H = q.p ? q.hands1 : q.hands0;
  G const float* reach = (q.p ? oop_reach : ip_reach) + n.reach_offset[1 - q.p];
  if (n.kind == Fold) {
    L float* live = prefix + 53;
    const uint O = q.p ? q.hands0 : q.hands1;
    for (uint j = tid; j < O; j += width)
      live[j] = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : reach[j];
    count_fold_reach(b, indices, live, prefix, O, tmp);
    BARRIER();
    for (uint h = tid; h < H; h += width) {
      const uchar2 c = cards[q.p * q.stride + h];
      const short same = heroes[b->hand_offset + h].same;
      values[n.value_row * q.stride + h] = n.win[q.p] * compatible_fold_reach(prefix, live, c, same);
    }
    return;
  }
  load_starts(b, starts);
  river_prefixes_g1(b, indices, reach, prefix, starts, heads, tmp);
  for (uint h = tid; h < H; h += width) {
    const HandPrefixes m = heroes[b->hand_offset + h];
    const float same = m.same >= 0 ? reach[m.same] : 0.f;
    const uchar2 c = cards[q.p * q.stride + h];
    const float mass = compatible_mass1(prefix, c, same);
    values[n.value_row * q.stride + h] =
        showdown_payoff1(m, prefix, same, mass, n.win[q.p], n.loss[q.p], n.tie[q.p], 0.f);
  }
}

KERNEL void river_call_fold(TRAVERSAL_ARGS, TABLE_ARGS SCRATCH_PARAM) {
  SCRATCH_DECL
  SHARED uint starts[54];
  SHARED float heads[53];
  SHARED float tmp[MAX_WIDTH];
  L float* prefix = scratch;
  const uint k = GROUP_X, tid = TID, width = WIDTH;
  const TraversalNode n = nodes[ids[id_offset + k]];
  const TraversalNode call = nodes[n.child];
  const float fold_pay = nodes[n.child + 1].win[q.p];
  G const BoardPrefixes* b = boards + call.aux;
  const uint H = q.p ? q.hands1 : q.hands0, O = q.p ? q.hands0 : q.hands1;
  G const float* opp = (q.p ? oop_reach : ip_reach) + n.reach_offset[1 - q.p];
  const bool updating = q.p == n.actor;
  const uint sw = q.sqrt_regret_weights;
  L float* call_reach = prefix + b->prefix_count;
  L float* fold_reach = call_reach + O;
  L float* fold_mass = fold_reach + O;
  load_starts(b, starts);
  if (updating) {
    river_prefixes_g1(b, indices, opp, prefix, starts, heads, tmp);
  } else {
    for (uint j = tid; j < O; j += width) {
      const float c = regret_weight(load_regret(regrets[n.storage + j]), sw);
      const float f = regret_weight(load_regret(regrets[n.storage + O + j]), sw);
      const float total = c + f;
      const float mass = blocked(cards[(1 - q.p) * q.stride + j], n.public_cards) ? 0.f : opp[j];
      call_reach[j] = mass * (total > 0 ? c / total : .5f);
      fold_reach[j] = mass * (total > 0 ? f / total : .5f);
    }
    count_fold_reach(b, indices, fold_reach, fold_mass, O, tmp);
    BARRIER();
    river_prefixes_l1(b, indices, call_reach, prefix, starts, heads, tmp);
  }
  for (uint h = tid; h < H; h += width) {
    const HandPrefixes m = heroes[b->hand_offset + h];
    const uchar2 c = cards[q.p * q.stride + h];
    const float same = m.same < 0 ? 0.f : updating ? opp[m.same] : call_reach[m.same];
    const float compat = compatible_mass1(prefix, c, same);
    const float cv = showdown_payoff1(m, prefix, same, compat, call.win[q.p], call.loss[q.p], call.tie[q.p], 0.f);
    float v;
    if (updating) {
      const float fv = fold_pay * compat;
      const uint j = n.storage + h;
      const float rc = load_regret(regrets[j]), rf = load_regret(regrets[j + H]);
      const float total = regret_weight(rc, sw) + regret_weight(rf, sw);
      const float sc = total > 0 ? regret_weight(rc, sw) / total : .5f;
      const float sf = total > 0 ? regret_weight(rf, sw) / total : .5f;
      v = fma(sf, fv, sc * cv);
      regrets[j] = store_regret(discounted(rc, cv - v, q));
      regrets[j + H] = store_regret(discounted(rf, fv - v, q));
      const float reach = blocked(c, n.public_cards) ? 0.f : (q.p ? ip_reach : oop_reach)[n.reach_offset[q.p] + h];
      update_average(strategy_sums, j, reach, sc, q);
      update_average(strategy_sums, j + H, reach, sf, q);
    } else {
      v = cv + fold_pay * compatible_fold_reach(fold_mass, fold_reach, c, m.same);
    }
    values[n.value_row * q.stride + h] = v;
  }
}

// All-ins before the river: one work-group per (batch of up to four nodes on one outcome table, hero
// hand); the table holds 2 * wins + ties per hand pair, -1 for pairs that share a card.
#define ALLIN_SUMS(NAME, COUNT)                                                                              \
  FN float4 NAME(G const COUNT* row, uint O, uint lane, uint width, float scale, uint p, G const float* reach, \
                 uint4 reaches, float4 spread, float4 loss) {                                                \
    float4 sums = F4S(0.f);                                                                                  \
    for (uint j = lane; j < O; j += width) {                                                                 \
      const int count = row[j];                                                                              \
      if (count < 0) continue;                                                                               \
      float e = (float)count * scale;                                                                        \
      if (p) e = 1.f - e;                                                                                    \
      const float4 weights = F4(reach[reaches.x + j], reach[reaches.y + j], reach[reaches.z + j], reach[reaches.w + j]); \
      sums = fma(weights, fma(spread, F4S(e), loss), sums);                                                  \
    }                                                                                                        \
    return sums;                                                                                             \
  }
ALLIN_SUMS(allin_sums8, char)
ALLIN_SUMS(allin_sums16, short)

KERNEL void allin(TRAVERSAL_ARGS, G const AllinTable* tables, G const short* counts, G const AllinBatch* batches) {
  SHARED float tmp[MAX_WIDTH];
  const uint h = GROUP_Y, lane = TID, width = WIDTH;
  const uint O = q.p ? q.hands0 : q.hands1;
  G const AllinBatch* batch = batches + id_offset + GROUP_X;
  const uint4 reaches = U4(batch->reach_offset[q.p][0], batch->reach_offset[q.p][1], batch->reach_offset[q.p][2],
                           batch->reach_offset[q.p][3]);
  const float4 spread = F4(batch->win_minus_loss[q.p][0], batch->win_minus_loss[q.p][1],
                           batch->win_minus_loss[q.p][2], batch->win_minus_loss[q.p][3]);
  const float4 loss = F4(batch->loss[q.p][0], batch->loss[q.p][1], batch->loss[q.p][2], batch->loss[q.p][3]);
  G const float* reach = q.p ? oop_reach : ip_reach;
  const AllinTable table = tables[batch->table];
  G const short* start = counts + table.word_offset;
  float4 sums = table.bytes_per_count == 1
      ? allin_sums8((G const char*)start + h * O, O, lane, width, table.scale, q.p, reach, reaches, spread, loss)
      : allin_sums16(start + h * O, O, lane, width, table.scale, q.p, reach, reaches, spread, loss);
  for (uint k = 0; k < 4; ++k) {
    const float s = group_sum(((float*)&sums)[k], tmp);
    if (!lane && k < batch->count) values[batch->value_offset[k] + h] = s;
  }
}

// ----- fused river betting rounds -----------------------------------------------------------------
FN float2 binary_strategy(G const RegretWord* r, uint j, uint H, uint sw) {
  const float2 pos = F2(regret_weight(load_regret(r[j]), sw), regret_weight(load_regret(r[j + H]), sw));
  const float total = pos.x + pos.y;
  return total > 0 ? pos / total : F2S(.5f);
}

FN float3 raise_strategy(G const RegretWord* r, uint j, uint H, uint sw) {
  const float3 pos = F3(regret_weight(load_regret(r[j]), sw), regret_weight(load_regret(r[j + H]), sw),
                        regret_weight(load_regret(r[j + 2 * H]), sw));
  const float sum = pos.x + pos.y + pos.z;
  return sum > 0 ? pos / sum : F3S(1.f / 3.f);
}

// The value of an N-action decision under its policy, and its regret and average updates.
FN float update_actions(G RegretWord* regrets, G SumWord* sums, const IterationParams q, uint j, uint H,
                        float reach, uint n, const float* policy, const float* cfv) {
  float value = policy[0] * cfv[0];
  for (uint a = 1; a < n; ++a) value = fma(policy[a], cfv[a], value);
  for (uint a = 0; a < n; ++a) {
    const uint k = j + a * H;
    regrets[k] = store_regret(discounted(load_regret(regrets[k]), cfv[a] - value, q));
    update_average(sums, k, reach, policy[a], q);
  }
  return value;
}
FN float update2(G RegretWord* regrets, G SumWord* sums, const IterationParams q, uint j, uint H, float reach,
                 float2 policy, float2 cfv) {
  return update_actions(regrets, sums, q, j, H, reach, 2, (const float*)&policy, (const float*)&cfv);
}
FN float update3(G RegretWord* regrets, G SumWord* sums, const IterationParams q, uint j, uint H, float reach,
                 float3 policy, float3 cfv) {
  return update_actions(regrets, sums, q, j, H, reach, 3, (const float*)&policy, (const float*)&cfv);
}
FN float update4(G RegretWord* regrets, G SumWord* sums, const IterationParams q, uint j, uint H, float reach,
                 float4 policy, float4 cfv) {
  return update_actions(regrets, sums, q, j, H, reach, 4, (const float*)&policy, (const float*)&cfv);
}

FN float3 round_reach(G const RiverRound* r, G const RegretWord* opp_r, float weight, uint j, uint O, bool leading,
                      uint sw) {
  if (r->kind == BetWithRaise) {
    if (leading) {
      const float3 response = raise_strategy(opp_r, r->storage[1] + j, O, sw);
      return weight * F3(response.x, response.y, 1.f);
    }
    const float2 first = binary_strategy(opp_r, r->storage[0] + j, O, sw);
    const float2 response = binary_strategy(opp_r, r->storage[2] + j, O, sw);
    const float bet = weight * first.x;
    return F3(bet * response.x, bet, weight * first.y);
  }
  if (r->kind == FinalRaise) {
    if (leading) {
      const float2 response = binary_strategy(opp_r, r->storage[1] + j, O, sw);
      return weight * F3(response.x, 1.f, response.y);
    }
    return weight * raise_strategy(opp_r, r->storage[0] + j, O, sw);
  }
  if (leading) {
    const float2 response = binary_strategy(opp_r, r->storage[1] + j, O, sw);
    const float2 probe = binary_strategy(opp_r, r->storage[2] + j, O, sw);
    return weight * F3(response.x, probe.x, probe.y);
  }
  const float2 first = binary_strategy(opp_r, r->storage[0] + j, O, sw);
  const float2 response = binary_strategy(opp_r, r->storage[3] + j, O, sw);
  const float check = weight * first.y;
  return F3(weight * first.x, check * response.x, check);
}

FN float update_river_round(G const RiverRound* r, const IterationParams q, G RegretWord* regrets,
                            G SumWord* sums, uint h, uint H, float reach, bool leading, float3 sd, float3 mass) {
  const uint p = q.p, sw = q.sqrt_regret_weights;
  if (r->kind == BetWithRaise) {
    if (leading) {
      const float2 first = binary_strategy(regrets, r->storage[0] + h, H, sw);
      const float2 response = binary_strategy(regrets, r->storage[2] + h, H, sw);
      const float raised = update2(regrets, sums, q, r->storage[2] + h, H, reach * first.x, response,
                                   F2(sd.x, r->fold[0][p] * mass.x));
      const float bet = (raised + sd.y) + r->fold[1][p] * ((mass.z - mass.x) - mass.y);
      return update2(regrets, sums, q, r->storage[0] + h, H, reach, first, F2(bet, sd.z));
    }
    const uint j = r->storage[1] + h;
    const float3 policy = raise_strategy(regrets, j, H, sw);
    const float3 cfv = F3(sd.x + r->fold[0][p] * (mass.y - mass.x), sd.y, r->fold[1][p] * mass.y);
    return update3(regrets, sums, q, j, H, reach, policy, cfv) + sd.z;
  }
  if (r->kind == FinalRaise) {
    if (leading) {
      const uint j = r->storage[0] + h;
      const float3 policy = raise_strategy(regrets, j, H, sw);
      const float3 cfv = F3(sd.x + mass.z * r->fold[0][p], sd.y, mass.y * r->fold[1][p]);
      return update3(regrets, sums, q, j, H, reach, policy, cfv);
    }
    const float2 response = binary_strategy(regrets, r->storage[1] + h, H, sw);
    const float raised = update2(regrets, sums, q, r->storage[1] + h, H, reach, response,
                                 F2(sd.x, r->fold[0][p] * mass.x));
    return (raised + sd.y) + r->fold[1][p] * mass.z;
  }
  if (leading) {
    const float lead = sd.x + r->fold[0][p] * (mass.y + mass.z - mass.x);
    const float2 first = binary_strategy(regrets, r->storage[0] + h, H, sw);
    const float2 response = binary_strategy(regrets, r->storage[3] + h, H, sw);
    const float check_bet = update2(regrets, sums, q, r->storage[3] + h, H, reach * first.y, response,
                                    F2(sd.y, r->fold[1][p] * mass.y));
    return update2(regrets, sums, q, r->storage[0] + h, H, reach, first, F2(lead, check_bet + sd.z));
  }
  const float2 call = binary_strategy(regrets, r->storage[1] + h, H, sw);
  const float2 probe = binary_strategy(regrets, r->storage[2] + h, H, sw);
  const float vs_lead = update2(regrets, sums, q, r->storage[1] + h, H, reach, call, F2(sd.x, r->fold[0][p] * mass.x));
  const float bet = sd.y + r->fold[1][p] * (mass.z - mass.y);
  return vs_lead + update2(regrets, sums, q, r->storage[2] + h, H, reach, probe, F2(bet, sd.z));
}

// A final river bet or check, its two showdown channels at once (prefix: 2 floats a slot).
KERNEL void river_bet_check(TRAVERSAL_ARGS, TABLE_ARGS, G const RiverRound* rounds SCRATCH_PARAM) {
  SCRATCH_DECL
  SHARED uint starts[54];
  SHARED float heads[2 * 53];
  SHARED float tmp[MAX_WIDTH];
  L float* prefix = scratch;
  const uint k = GROUP_X, tid = TID, width = WIDTH;
  G const RiverRound* r = rounds + id_offset + k;
  const TraversalNode n = nodes[r->root];
  G const BoardPrefixes* b = boards + r->board;
  const uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1, sw = q.sqrt_regret_weights;
  const bool bettor = n.actor == p;
  G const float* opp = (p ? oop_reach : ip_reach) + n.reach_offset[1 - p];
  G const float* own = (p ? ip_reach : oop_reach) + n.reach_offset[p];
  L float* reaches = prefix + 2 * b->prefix_count;
  load_starts(b, starts);
  for (uint j = tid; j < O; j += width) {
    const float2 policy = binary_strategy(regrets, r->storage[bettor ? 1 : 0] + j, O, sw);
    vstore2(opp[j] * (bettor ? F2(policy.x, 1.f) : policy), j, reaches);
  }
  BARRIER();
  river_prefixes_l2(b, indices, reaches, prefix, starts, heads, tmp);
  const float2 win = F2(r->win[0][p], r->win[1][p]), loss = F2(r->loss[0][p], r->loss[1][p]);
  const float2 tie = F2(r->tie[0][p], r->tie[1][p]);
  for (uint h = tid; h < H; h += width) {
    const HandPrefixes m = heroes[b->hand_offset + h];
    const float2 same = m.same >= 0 ? vload2(m.same, reaches) : F2S(0.f);
    const uchar2 c = cards[p * q.stride + h];
    const float2 mass = compatible_mass2(prefix, c, same);
    const float2 sd = showdown_payoff2(m, prefix, same, mass, win, loss, tie, F2S(0.f));
    const uint j = r->storage[bettor ? 0 : 1] + h;
    const float2 policy = binary_strategy(regrets, j, H, sw);
    const float reach = blocked(c, n.public_cards) ? 0.f : own[h];
    const float2 cfv = bettor ? F2(sd.x + r->fold[0][p] * (mass.y - mass.x), sd.y) : F2(sd.x, r->fold[0][p] * mass.x);
    const float value = update2(regrets, strategy_sums, q, j, H, reach, policy, cfv);
    values[n.value_row * q.stride + h] = bettor ? value : value + sd.y;
  }
}

FN float4 two_bet_strategy(G const RegretWord* r, uint j, uint H, bool can_fold, bool has_big_bet, uint sw) {
  if (!has_big_bet) {
    const float3 s = raise_strategy(r, j, H, sw);
    return F4(0.f, s.x, s.y, s.z);
  }
  if (!can_fold) {
    const float3 s = raise_strategy(r, j, H, sw);
    return F4(s.x, s.y, s.z, 0.f);
  }
  const float4 positive = F4(regret_weight(load_regret(r[j]), sw), regret_weight(load_regret(r[j + H]), sw),
                             regret_weight(load_regret(r[j + 2 * H]), sw), regret_weight(load_regret(r[j + 3 * H]), sw));
  const float sum = positive.x + positive.y + positive.z + positive.w;
  return sum > 0 ? positive / sum : F4S(.25f);
}

FN float4 two_bet_reaches(G const TwoBetRiver* r, G const RegretWord* opp_r, float weight, uint j, uint O,
                          bool bettor, bool can_fold, uint sw) {
  if (bettor) {
    const float2 big = r->has_big_bet ? binary_strategy(opp_r, r->storage[1] + j, O, sw) : F2S(0.f);
    const float3 small = raise_strategy(opp_r, r->storage[2] + j, O, sw);
    return weight * F4(big.x, small.x, small.y, 1.f);
  }
  const float4 root = two_bet_strategy(opp_r, r->storage[0] + j, O, can_fold, r->has_big_bet, sw);
  const float2 response = binary_strategy(opp_r, r->storage[3] + j, O, sw);
  const float small = weight * root.y;
  return F4(weight * root.x, small * response.x, small, weight * root.z);
}

FN float update_two_bet_river(G const TwoBetRiver* r, const IterationParams q, G RegretWord* regrets,
                              G SumWord* sums, uint h, uint H, float reach, bool bettor, bool can_fold,
                              float fold_value, float4 sd, float4 mass) {
  const uint p = q.p, sw = q.sqrt_regret_weights;
  if (bettor) {
    const uint j = r->storage[0] + h, response = r->storage[3] + h;
    const float4 root = two_bet_strategy(regrets, j, H, can_fold, r->has_big_bet, sw);
    const float2 policy = binary_strategy(regrets, response, H, sw);
    const float vs_raise = update2(regrets, sums, q, response, H, reach * root.y, policy, F2(sd.y, mass.y * r->fold[1][p]));
    const float big = sd.x + (mass.w - mass.x) * r->fold[0][p];
    const float small = (vs_raise + sd.z) + ((mass.w - mass.y) - mass.z) * r->fold[2][p];
    if (!r->has_big_bet)
      return update3(regrets, sums, q, j, H, reach, F3(root.y, root.z, root.w), F3(small, sd.w, mass.w * r->fold[3][p]));
    if (can_fold) return update4(regrets, sums, q, j, H, reach, root, F4(big, small, sd.w, mass.w * r->fold[3][p]));
    return update3(regrets, sums, q, j, H, reach, F3(root.x, root.y, root.z), F3(big, small, sd.w));
  }
  const uint small = r->storage[2] + h;
  float big_value = 0;
  if (r->has_big_bet) {
    const uint big = r->storage[1] + h;
    const float2 big_policy = binary_strategy(regrets, big, H, sw);
    big_value = update2(regrets, sums, q, big, H, reach, big_policy, F2(sd.x, mass.x * r->fold[0][p]));
  }
  const float3 small_policy = raise_strategy(regrets, small, H, sw);
  const float3 small_cfv = F3(sd.y + (mass.z - mass.y) * r->fold[1][p], sd.z, mass.z * r->fold[2][p]);
  const float small_value = update3(regrets, sums, q, small, H, reach, small_policy, small_cfv);
  return ((big_value + small_value) + sd.w) + fold_value;
}

// A river decision with two bet sizes and a raise: four showdown channels, all at once (prefix: 4 floats
// a slot) or two at a time (2).
#define TWO_BET_KERNEL(NAME, N, T, ZERO, PREFIXES)                                                             \
  KERNEL void NAME(TRAVERSAL_ARGS, TABLE_ARGS, G const TwoBetRiver* rounds SCRATCH_PARAM) {                   \
    SCRATCH_DECL                                                                                               \
    SHARED uint starts[54];                                                                                    \
    SHARED float heads[N * 53];                                                                                \
    SHARED float tmp[MAX_WIDTH];                                                                               \
    L float* prefix = scratch;                                                                                 \
    const uint tid = TID, width = WIDTH;                                                                       \
    G const TwoBetRiver* r = rounds + id_offset + GROUP_X;                                                    \
    const TraversalNode n = nodes[r->root];                                                                    \
    G const BoardPrefixes* b = boards + r->board;                                                              \
    const uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1, sw = q.sqrt_regret_weights; \
    G const float* opp = (p ? oop_reach : ip_reach) + n.reach_offset[1 - p];                                  \
    G const float* own = (p ? ip_reach : oop_reach) + n.reach_offset[p];                                      \
    L float* reaches = prefix + N * b->prefix_count;                                                           \
    const bool bettor = n.actor == p;                                                                          \
    float first_mass[MAX_ROWS][2], first_payoff[MAX_ROWS][2], fold_value[MAX_ROWS];                            \
    const bool can_fold = n.count == 4 || !r->has_big_bet;                                                     \
    load_starts(b, starts);                                                                                    \
    if (can_fold && !bettor) {                                                                                 \
      L float* folds = prefix;                                                                                 \
      L float* totals = folds + O;                                                                             \
      for (uint j = tid; j < O; j += width) {                                                                  \
        const float weight = blocked(cards[(1 - p) * q.stride + j], n.public_cards) ? 0.f : opp[j];          \
        folds[j] = weight * two_bet_strategy(regrets, r->storage[0] + j, O, true, r->has_big_bet, sw).w;      \
      }                                                                                                        \
      count_fold_reach(b, indices, folds, totals, O, tmp);                                                     \
      BARRIER();                                                                                               \
      for (uint h = tid; h < H; h += width) {                                                                  \
        const uchar2 c = cards[p * q.stride + h];                                                              \
        fold_value[h / width] = r->fold[3][p] * compatible_fold_reach(totals, folds, c, heroes[b->hand_offset + h].same); \
      }                                                                                                        \
      BARRIER();                                                                                               \
    }                                                                                                          \
    for (uint group = 0; group < 4; group += N) {                                                              \
      for (uint j = tid; j < O; j += width) {                                                                  \
        const float4 weights = two_bet_reaches(r, regrets, opp[j], j, O, bettor, can_fold, sw);               \
        for (uint lane = 0; lane < N; ++lane) reaches[N * j + lane] = ((const float*)&weights)[group + lane];  \
      }                                                                                                        \
      BARRIER();                                                                                               \
      PREFIXES(b, indices, reaches, prefix, starts, heads, tmp);                                               \
      T win, loss, tie;                                                                                        \
      for (uint lane = 0; lane < N; ++lane) {                                                                  \
        ((float*)&win)[lane] = r->win[group + lane][p];                                                        \
        ((float*)&loss)[lane] = r->loss[group + lane][p];                                                      \
        ((float*)&tie)[lane] = r->tie[group + lane][p];                                                        \
      }                                                                                                        \
      for (uint h = tid; h < H; h += width) {                                                                  \
        const HandPrefixes m = heroes[b->hand_offset + h];                                                     \
        const uchar2 c = cards[p * q.stride + h];                                                              \
        const T same = m.same >= 0 ? vload##N(m.same, reaches) : ZERO;                                        \
        const T mass = compatible_mass##N(prefix, c, same);                                                    \
        const T sd = showdown_payoff##N(m, prefix, same, mass, win, loss, tie, ZERO);                          \
        float4 all_mass, all_payoff;                                                                           \
        if (N == 2) {                                                                                          \
          if (group == 0) {                                                                                    \
            for (uint lane = 0; lane < 2; ++lane) {                                                            \
              first_mass[h / width][lane] = ((const float*)&mass)[lane];                                       \
              first_payoff[h / width][lane] = ((const float*)&sd)[lane];                                       \
            }                                                                                                  \
            continue;                                                                                          \
          }                                                                                                    \
          all_mass = F4(first_mass[h / width][0], first_mass[h / width][1], ((const float*)&mass)[0],          \
                        ((const float*)&mass)[1]);                                                             \
          all_payoff = F4(first_payoff[h / width][0], first_payoff[h / width][1], ((const float*)&sd)[0],      \
                          ((const float*)&sd)[1]);                                                             \
        } else {                                                                                               \
          for (uint lane = 0; lane < 4; ++lane) {                                                              \
            ((float*)&all_mass)[lane] = ((const float*)&mass)[lane];                                           \
            ((float*)&all_payoff)[lane] = ((const float*)&sd)[lane];                                           \
          }                                                                                                    \
        }                                                                                                      \
        const float reach = blocked(c, n.public_cards) ? 0.f : own[h];                                         \
        const float fold = can_fold && !bettor ? fold_value[h / width] : 0.f;                                  \
        values[n.value_row * q.stride + h] = update_two_bet_river(r, q, regrets, strategy_sums, h, H, reach,   \
                                                                  bettor, can_fold, fold, all_payoff, all_mass); \
      }                                                                                                        \
      if (group + N < 4) BARRIER();                                                                            \
    }                                                                                                          \
  }
TWO_BET_KERNEL(river_two_bets, 4, float4, F4S(0.f), river_prefixes_l4)
TWO_BET_KERNEL(river_two_bets_wide, 2, float2, F2S(0.f), river_prefixes_l2)

// A whole river betting round in one pass, or a lead and a check-raise round below one decision
// (paired): three showdown channels at once (prefix: 3 floats a slot) or one at a time.
#define RIVER_ROUND_KERNEL(NAME, PAIRED, RECORD, BRANCHES, N, T, ZERO, PREFIXES)                               \
  KERNEL void NAME(TRAVERSAL_ARGS, TABLE_ARGS, G const RECORD* rounds SCRATCH_PARAM) {                        \
    SCRATCH_DECL                                                                                               \
    SHARED uint starts[54];                                                                                    \
    SHARED float heads[N * 53];                                                                                \
    SHARED float tmp[MAX_WIDTH];                                                                               \
    L float* prefix = scratch;                                                                                 \
    const uint tid = TID, width = WIDTH;                                                                       \
    G const RECORD* record = rounds + id_offset + GROUP_X;                                                     \
    G const RiverRound* branches = BRANCHES;                                                                   \
    const TraversalNode root = nodes[record->root];                                                            \
    G const BoardPrefixes* b = boards + branches[0].board;                                                     \
    const uint p = q.p, H = p ? q.hands1 : q.hands0, O = p ? q.hands0 : q.hands1, sw = q.sqrt_regret_weights; \
    G const float* opp = (p ? oop_reach : ip_reach) + root.reach_offset[1 - p];                               \
    G const float* own = (p ? ip_reach : oop_reach) + root.reach_offset[p];                                   \
    const bool leading = PAIRED ? root.actor != p : root.actor == p;                                           \
    L float* reaches = prefix + N * b->prefix_count;                                                           \
    float lead_values[MAX_ROWS];                                                                               \
    load_starts(b, starts);                                                                                    \
    for (uint branch = 0; branch < (PAIRED ? 2u : 1u); ++branch) {                                             \
      G const RiverRound* r = branches + branch;                                                               \
      float partial_mass[MAX_ROWS][3], partial_payoff[MAX_ROWS][3];                                            \
      for (uint base = 0; base < 3; base += N) {                                                               \
        for (uint o = tid; o < O; o += width) {                                                                \
          float weight = opp[o];                                                                               \
          if (PAIRED && leading) {                                                                             \
            const float2 s = binary_strategy(regrets, root.storage + o, O, sw);                                \
            weight *= branch ? s.y : s.x;                                                                      \
          }                                                                                                    \
          const float3 all = round_reach(r, regrets, weight, o, O, leading, sw);                               \
          for (uint lane = 0; lane < N; ++lane) reaches[N * o + lane] = ((const float*)&all)[base + lane];    \
        }                                                                                                      \
        BARRIER();                                                                                             \
        PREFIXES(b, indices, reaches, prefix, starts, heads, tmp);                                             \
        T win, loss, tie;                                                                                      \
        for (uint lane = 0; lane < N; ++lane) {                                                                \
          ((float*)&win)[lane] = r->win[base + lane][p];                                                       \
          ((float*)&loss)[lane] = r->loss[base + lane][p];                                                     \
          ((float*)&tie)[lane] = r->tie[base + lane][p];                                                       \
        }                                                                                                      \
        for (uint h = tid; h < H; h += width) {                                                                \
          const HandPrefixes hero = heroes[b->hand_offset + h];                                                \
          const uchar2 c = cards[p * q.stride + h];                                                            \
          const T same = hero.same < 0 ? ZERO : vload##N(hero.same, reaches);                                  \
          const T mass = compatible_mass##N(prefix, c, same);                                                  \
          const T sd = showdown_payoff##N(hero, prefix, same, mass, win, loss, tie, ZERO);                     \
          for (uint lane = 0; lane < N; ++lane) {                                                              \
            partial_mass[h / width][base + lane] = ((const float*)&mass)[lane];                                \
            partial_payoff[h / width][base + lane] = ((const float*)&sd)[lane];                                \
          }                                                                                                    \
          if (base + N < 3) continue;                                                                          \
          const float3 all_mass = F3(partial_mass[h / width][0], partial_mass[h / width][1], partial_mass[h / width][2]); \
          const float3 all_payoff =                                                                            \
              F3(partial_payoff[h / width][0], partial_payoff[h / width][1], partial_payoff[h / width][2]);    \
          const float reach = blocked(c, root.public_cards) ? 0.f : own[h];                                    \
          const float2 policy = PAIRED && !leading ? binary_strategy(regrets, root.storage + h, H, sw) : F2S(1.f); \
          const float value = update_river_round(r, q, regrets, strategy_sums, h, H,                           \
                                                 reach * (branch ? policy.y : policy.x), leading, all_payoff, all_mass); \
          if (!PAIRED) values[root.value_row * q.stride + h] = value;                                          \
          else if (!branch) lead_values[h / width] = value;                                                    \
          else {                                                                                               \
            const float2 children = F2(lead_values[h / width], value);                                         \
            values[root.value_row * q.stride + h] =                                                            \
                !leading ? update2(regrets, strategy_sums, q, root.storage + h, H, reach, policy, children)    \
                         : children.x + children.y;                                                            \
          }                                                                                                    \
        }                                                                                                      \
        if (branch + 1 < (PAIRED ? 2u : 1u) || base + N < 3) BARRIER();                                        \
      }                                                                                                        \
    }                                                                                                          \
  }
RIVER_ROUND_KERNEL(river_betting_round, 0, RiverRound, record, 3, float3, F3S(0.f), river_prefixes_l3)
RIVER_ROUND_KERNEL(river_betting_round_wide, 0, RiverRound, record, 1, float, 0.f, river_prefixes_l1)
RIVER_ROUND_KERNEL(river_lead_check, 1, PairedRiverRound, record->branches, 3, float3, F3S(0.f), river_prefixes_l3)
RIVER_ROUND_KERNEL(river_lead_check_wide, 1, PairedRiverRound, record->branches, 1, float, 0.f, river_prefixes_l1)
