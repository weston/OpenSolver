// Materialized game tree: betting tree expanded over dealt cards (with suit isomorphism),
// per-player hand lists, showdown orderings and strategy storage.
#pragma once
#include <array>
#include <bit>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "action_tree.hpp"
#include "cards.hpp"

// Compact 16-bit river rows (#239, see GameTree::keep_sets). Every build can keep them (a capacity tier,
// chosen only when full 16-bit rows don't fit); AVX-512 VBMI2 builds move them with expand / compress
// (PIO_COMPACT_ROWS_AVX512), the others with the portable loops (#568).
#if !defined(PIO_NO_COMPACT_ROWS)
#define PIO_COMPACT_ROWS 1
#else
#define PIO_COMPACT_ROWS 0
#endif
#if PIO_COMPACT_ROWS && defined(__AVX512VBMI2__) && defined(__AVX512BW__) && defined(__AVX512VL__)
#define PIO_COMPACT_ROWS_AVX512 1
#else
#define PIO_COMPACT_ROWS_AVX512 0
#endif

namespace pio {

// Max reductions for the 16-bit storage scales. Non-negative floats order exactly like their IEEE-754
// bit patterns read as unsigned integers, so max(|x|) (or the max of values that should be >= 0) can be
// accumulated as an integer max. Compilers vectorize integer max reductions without -ffast-math; the
// float form `m = x > m ? x : m` stays a scalar compare/select chain (it was ~2/3 of the regret-update
// time on 16-bit trees and in RBP-pruned rows). The result is the same float, so quantisation is
// bit-identical. NaN inputs differ: they now win the max (a NaN scale zeroes the row in
// regrets_store/strat_store) instead of being skipped.
inline uint32_t abs_bits(float x) { return std::bit_cast<uint32_t>(x) & 0x7fffffffu; }
inline uint32_t nonneg_bits(float x) {  // negative values (and -0.0) count as 0
  const uint32_t u = std::bit_cast<uint32_t>(x);
  return static_cast<int32_t>(u) < 0 ? 0u : u;
}

using Weights1326 = std::array<float, NUM_COMBOS>;

struct IcmModel {
  bool enabled = false;
  int32_t stacks[2] = {0, 0};  // starting stacks (OOP, IP)
  std::vector<std::pair<double, double>> points[2];  // (final stack, value), sorted by stack
  double value(int p, double stack) const;
};

struct PayoffModel {
  double rake_frac = 0.0;
  double rake_cap = 0.0;
  IcmModel icm;
};

// A postflop tree attached to a preflop exit (add_schematic_tree).
struct Schematic {
  std::vector<int32_t> exit_line;  // preflop line (add_preflop_line numbers) of the exit node
  int32_t pot[3] = {0, 0, 0};      // set_pot at the time of add_schematic_tree
  int32_t eff_stack = 0;
  LineSet lines;
};

struct SubsetFlop {
  float weight;
  int cards[3];  // as given to add_to_subset (also the node-id token order)
};

struct GameConfig {
  RiverSizing river_sizing = RiverSizing::Full;
  bool preflop = false;            // preflop tree (board empty): lines are preflop lines
  std::vector<Schematic> schematics;
  std::vector<SubsetFlop> subset;  // used only when schematics are attached (full preflop tree)
  std::vector<int> board;          // 3, 4 or 5 cards (empty for preflop)
  int32_t pot[3] = {0, 0, 0};      // set_pot: oop, ip, dead
  int32_t eff_stack = 0;
  Weights1326 range[2]{};          // OOP, IP
  // iso_turn is off to match Pio's `set_isomorphism 1 0`, not because it does not pay: it
  // applies when the tree's ROOT board is 4 cards (see game.cpp, `bl == 4 && cfg.iso_turn`),
  // and on bench/trees/turn.upi (Kh9h4h2c) it collapses 1603 nodes to 1174 (-27%) and the
  // solve from 0.753 s to 0.553 s (-26.5%, sd 0.8%).
  //
  // EV (55.111) and exploitability (0.121) match, but the solved STRATEGIES do not: per-hand
  // frequencies differ by up to 0.008, because collapsing branches aggregates the CFR updates
  // over equivalent runouts and so follows a different trajectory to the same equilibrium.
  // The gap WIDENS with convergence rather than closing (0.006 at 60 iterations, 0.015 at
  // 300, 0.056 at 1200), which looks alarming but is benign: at 1200 iterations both reach
  // the same EVs (55.124 / 44.876) with exploitability 0.001 and 0.003. They are converging
  // to different members of the equilibrium set, not one of them to a wrong answer.
  //
  // So this is not a free switch - a client diffing frequencies against Pio will see the
  // difference, and it gets bigger the longer you solve. Worth `set_isomorphism 1 1` for a
  // turn-rooted solve where only EV matters; not for one whose frequencies are compared.
  bool iso_flop = true, iso_turn = false;
  // Keep each player's hand list as the union of both ranges, like Pio, so hands only in
  // the opponent's range still get a slot (and therefore a counterfactual value) instead
  // of being filtered out. They enter with reach 0, so the equilibrium is unaffected; the
  // cost is wider vectors. Off by default: see set_union_hands.
  bool union_hands = false;
  // Build a subtree in which nobody has a choice left (every decision has one action: the checks down
  // to a showdown) as one showdown node on the runouts, like an all-in: the same values, without the
  // nodes and the regret and strategy rows of the checks. Off by default (Pio keeps the nodes, and so
  // do we: they can be browsed, e.g. per turn card); see set_collapse_checkdowns.
  bool collapse_checkdowns = false;
  LineSet lines;
  int32_t start_pot() const { return pot[0] + pot[1] + pot[2]; }
  bool full_preflop() const { return preflop && !schematics.empty(); }
};

enum GFlags : uint8_t { GF_LOCKED = 1, GF_COMBO_LOCKED = 2, GF_INCENTIVES = 4, GF_ELIMINATED = 8 };

struct GNode {
  ANodeKind kind;
  uint8_t player;      // decision: acting player; fold: folder
  uint8_t board_len;   // cards on board at this node
  uint8_t flags;
  uint32_t anode;      // index into GameTree::anodes
  uint32_t first_child;
  uint16_t num_children;
  uint16_t pad;        // decision: 1 + its GameTree::keep_sets entry when 16-bit rows are compact (#239), else 0
  uint32_t aux;        // chance: ChanceInfo index; showdown: board id; decision: decision index
  uint64_t storage;    // decision: float offset into regrets/strat arrays (size A * H[player]; one action: shared, #567)
};

struct ChanceInfo {
  // Flop deal at a preflop exit: one child per subset flop (cards/iso unused).
  bool flop_deal = false;
  std::vector<std::array<uint8_t, 3>> flops;
  std::vector<float> fweight;  // weight / total weight; Solver::flop_deal divides by a pair's share of the flops
  int8_t slot[NUM_CARDS];    // child slot for each card (-1 = card unavailable)
  uint8_t perm[NUM_CARDS];   // suit permutation mapping the slot's canonical card to this card
  std::vector<uint8_t> cards;           // canonical card for each child slot
  std::vector<std::vector<std::pair<uint8_t, uint8_t>>> iso;  // per slot: (card, perm) duplicates
  uint8_t num_possible;      // cards that can be dealt from the hands' point of view (52 - board - 4)
  uint64_t board_mask;       // board before the deal
};

struct SdEntry {
  uint16_t idx;   // local hand index (<= 1325)
  uint8_t c1, c2; // hole cards
  int16_t same;   // local index of the identical combo in the opponent's list (<= 1325), or -1
};  // 6 bytes; one entry per hand per showdown board, so this is the dominant structural array

// All-in before the river: the showdown boards of every runout and 1 / (runouts per hand pair).
// `exact` (preflop all-ins; board empty at the terminal): no boards; the solver instead uses the
// exact hand-vs-hand table (pfh2h.*) for a matrix-vector product over opponent reach.
struct AllinRunouts {
  // The stored showdown board of each runout. Empty when the outcome table below stands in for them
  // and nothing else needs them (a preflop tree's flop exits, GameTree::plan_allins).
  std::vector<uint32_t> boards;
  std::vector<uint8_t> cards;  // the board at the terminal: 3 or 4 cards (none for `exact`)
  uint16_t runouts = 0;        // how many runouts: 48 for a turn all-in, 1,176 for a flop all-in
  bool table = false;          // gets an outcome table when the tree's storage is allocated
  float scale;
  bool exact = false;
  // Hand-vs-hand outcome counts over the runouts (GameTree::build_allin_tables): e2[h * H1 + o] =
  // 2 * (boards where OOP hand h beats IP hand o) + (boards where they tie), 0 for pairs sharing a
  // card. `valid` = runouts compatible with a disjoint pair (= 1 / scale). Turn all-ins (<= 96) are
  // stored in e2b (uint8, half the bytes to stream), flop all-ins (<= 2352) in e2. Both empty: not built.
  std::vector<uint16_t> e2;
  std::vector<uint8_t> e2b;
  // The compact form (GameTree::compact_tables: a preflop tree whose players hold the same hands).
  // Only the hands the set's cards leave (`live`, local indices ascending), and of the centered counts
  // c = e2 - valid, which are antisymmetric there (what h wins of o, o loses to h), the triangle under
  // the diagonal: tri[i * (i - 1) / 2 + j] = c[live[i]][live[j]] for j < i, 0 for a pair sharing a card.
  // |c| <= valid: tri8 for a turn set (44), tri16 for a flop set (990).
  std::vector<int8_t> tri8;
  std::vector<int16_t> tri16;
  std::vector<uint16_t> live;
  int valid = 0;
  // (A compact set whose cards leave fewer than two hands has no pair to keep, so it reads as not
  // built. Its value is then the zeros of a set without boards, which is its value: no hand in it
  // meets another.)
  bool built() const { return !(e2.empty() && e2b.empty() && tri8.empty() && tri16.empty()); }
};

struct ShowdownBoard {
  uint64_t mask;
  // Per player: hands sorted by strength ascending (board-blocked hands excluded).
  std::vector<SdEntry> ent[2];
  // Per perspective p: strength groups in ascending order, as cumulative end positions
  // (end in p's entries, end in the opponent's entries). For each group the kernel queries
  // "strictly weaker" mass for p's hands, adds the group's opponent hands, then queries "weaker or
  // equal", so every opponent hand is touched once and the loops have no data-dependent branches.
  std::vector<std::pair<uint16_t, uint16_t>> groups[2];
  // Per player: the hands the board blocks (not in ent), so a row the lanes kernels store into needs
  // zeroing only there (showdown_batch).
  std::vector<uint16_t> blocked[2];
};

// Allocator for big zero-initialised arrays: calloc memory and no per-element construction, so the OS
// hands out untouched zero pages lazily. A lazily loaded tree only commits RAM for what gets read.
template <class T>
struct ZeroAlloc {
  using value_type = T;
  ZeroAlloc() = default;
  template <class U>
  ZeroAlloc(const ZeroAlloc<U>&) {}
  T* allocate(size_t n) {
    void* p = std::calloc(n ? n : 1, sizeof(T));
    if (!p) throw std::bad_alloc();
    return static_cast<T*>(p);
  }
  void deallocate(T* p, size_t) { std::free(p); }
  template <class U>
  void construct(U*) noexcept {}  // value-init: calloc memory is already zero
  template <class U, class... A>
  void construct(U* p, A&&... a) {
    ::new (static_cast<void*>(p)) U(std::forward<A>(a)...);
  }
  template <class U>
  bool operator==(const ZeroAlloc<U>&) const { return true; }
  template <class U>
  bool operator!=(const ZeroAlloc<U>&) const { return false; }
};
template <class T>
using ZVec = std::vector<T, ZeroAlloc<T>>;

class GameTree;

// Street segments: the decision nodes between two card deals (the tree root starts segment 0). Saves
// store each segment as its own gzip member so a lazily loaded tree can read any one of them alone.
struct StreetSegments {
  std::vector<uint32_t> node_seg;           // per node: segment id (UINT32_MAX for non-decision nodes)
  std::vector<std::vector<uint32_t>> segs;  // decision nodes of each segment, in save order
};

// Lazy-load state for a tree loaded with `load_tree` (fast). Segments are read from the save file the
// first time any accessor touches one of their nodes. Thread-safe; loads never throw (errors are
// recorded and reported by the next command).
struct LazyStore {
  std::string path;
  uint8_t file_mode = 0;           // save's mode byte (see upi_save.cpp)
  uint8_t precision = 0;           // bits per value of quantised records (mode bit 2)
  StreetSegments seg;
  std::vector<uint64_t> offset;    // per segment: byte offset of its gzip member (0 = not in file)
  std::unique_ptr<std::atomic<uint8_t>[]> loaded;
  std::atomic<size_t> remaining{0};
  std::mutex mu[64];
  std::atomic<bool> failed{false};
  std::mutex err_mu;
  std::string error;
  // Reads segment `s` into the tree's storage; throws on I/O or format errors.
  void (*load_fn)(const GameTree&, const LazyStore&, uint32_t s) = nullptr;
};

class GameTree {
 public:
  GameConfig cfg;
  std::unique_ptr<ActionNode> aroot;
  std::vector<ActionNode*> anodes;
  std::vector<GNode> nodes;  // nodes[0] = root decision node (r:0)
  std::vector<ChanceInfo> chances;
  std::vector<ShowdownBoard> showdowns;
  std::vector<AllinRunouts> allins;  // GNode::aux of Showdown nodes with board_len < 5

  // Hands
  std::vector<uint16_t> hands[2];      // combo indices
  std::vector<float> init_reach[2];    // range weights per local hand
  std::vector<uint8_t> hcard[2][2];    // [player][0|1][hand] cards
  std::vector<int32_t> local[2];       // [player][combo] -> local index or -1
  std::vector<int32_t> same_hand[2];   // [player][hand] -> local index of same combo in opponent list or -1
  std::vector<std::vector<uint16_t>> hands_with_card[2];  // [player][card] -> local hand indices
  // perm_hand[player][perm][hand] -> local hand index of permuted combo (valid if perm_ok[perm])
  std::vector<std::vector<uint16_t>> perm_hand[2];
  bool perm_ok[NUM_SUIT_PERMS]{};

  // Strategy storage. Uncompressed: float32 arrays indexed by GNode::storage. Compressed: int16 regrets and
  // uint16 strategy sums with one float scale per decision node (indexed by GNode::aux).
  bool compressed = false;
  // 16-bit strategy sums are rounded stochastically when the solver commits them (#272): rounded to the
  // nearest step, a low-reach hand's increment late in a solve is below its node's 16-bit step and is
  // lost, which is full 16-bit's exploitability floor (threebet_mono stalled at 0.35% of pot). Rounding
  // up with probability equal to the fraction makes every increment count on average. The uniforms come
  // from a fixed table read at an offset hashed from the node and round_seed, which the solver sets per
  // iteration: results stay identical for any thread count. On every platform: the table costs ~2% per
  // iteration on the M4 where a per-slot hash cost ~9% (threebet_mono, 10 threads, #293).
  uint32_t round_seed = 0;
  static constexpr size_t kRoundTable = 4096;
  ZVec<float> regrets;
  ZVec<float> strat;      // cumulative strategy (or locked strategy for locked nodes)
  // Predictive CFR (#71): the last instantaneous regret per slot, allocated on demand by
  // set_predictive (never compressed, never saved — transient solver state). Empty when off.
  ZVec<float> preds;
  ZVec<int16_t> regrets_q;
  ZVec<uint16_t> strat_q;
  std::vector<float> rscale, sscale;
  uint64_t storage_floats = 0;
  // Every decision's A x H, as if each had rows of its own: what storage_floats was before the decisions with
  // one action shared a row (#567). Saves record this figure, so their files do not change.
  uint64_t node_floats = 0;
  // A decision with one action has one strategy, 1 for every hand (Solver::current_strategy and
  // average_strategy answer that without reading anything). Its rows are the player's one shared row of H
  // floats, which nothing writes, so it stays zero, as the node's own rows did once the solver stopped
  // updating them (#563): regrets_rw / strat_rw hand out zeros to overwrite, the commits and stores keep
  // nothing, and the loaders skip their records. On flop_srp_4 they were 55% of all rows (#567).
  static bool shares_row(const GNode& n) { return n.num_children == 1; }
  // Per decision (GNode::aux): the low 32 bits of where its rows began when every decision had its own (the
  // running node_floats). The stochastic rounding of 16-bit strategy sums hashes it to pick its uniforms,
  // so sharing rows changed no rounding (#567). Empty when no decision shares a row: GNode::storage is the key.
  std::vector<uint32_t> round_key;
  uint64_t num_decisions = 0;
  uint64_t checkdowns = 0;  // subtrees without a decision that the build made one showdown node (cfg.collapse_checkdowns)
  // Compact 16-bit rows (#239). At a river decision node, a hand holding a card dealt after the root board
  // can't occur: its reach is 0, and its regrets and strategy sums stay exactly 0 (measured on every river
  // node of flop trees, float32 and 16-bit; not so at turn nodes, which stay full). The 16-bit arrays keep
  // only the other hands there, about 8% less on a flop tree. The accessors expand rows to all hands (0
  // for the dropped ones) and the commits compact them, so every walk, scale and stochastic rounding sees
  // exactly the values it did before: results are bit-identical. float32 storage (and `preds`) keep full
  // rows at GNode::storage.
  // They cost time: about 12% per iteration on threebet_mono (Zen 5, 16 threads) even with AVX-512 VBMI2
  // expand / compress, so they are a capacity tier, used only when full 16-bit rows don't fit
  // (select_rows, small_strats compact / auto). Builds without VBMI2 move them with the portable loops
  // (#568); a tree that fits full rows never pays for them. Unselected, they cost 2 bytes per decision:
  // the build only counts kept hands.
  struct KeepSet {
    int card[2] = {-1, -1};         // the cards dealt after the root board
    uint32_t kept[2] = {0, 0};      // [player]: hands neither card blocks
    std::vector<uint16_t> keep[2];  // [player]: those hands, ascending (filled by select_rows(true))
    std::vector<uint32_t> bits[2];  // [player]: bit h of word h / 32 set if hand h is kept (same)
  };
  std::vector<KeepSet> keep_sets;
  std::vector<uint64_t> qoff;  // compact rows only: per decision (GNode::aux), its rows' offset in regrets_q / strat_q
  uint64_t storage_q = 0;      // length of regrets_q / strat_q
  uint64_t storage_compact = 0;  // what storage_q is with compact rows (the build computes both layouts)
  std::vector<uint16_t> keep_cand;  // per decision: its keep_sets entry + 1 if compact rows are selected
  bool compact_rows = false;
  // Before allocate_storage: compact river rows (if this build can and the tree has any) or full rows
  // everywhere (GNode::pad 0, at GNode::storage, the layout float32 uses). build() leaves full rows.
  void select_rows(bool compact);
  // Offset of a node's rows in regrets_q / strat_q.
  uint64_t qo(const GNode& n) const { return compact_rows ? qoff[n.aux] : n.storage; }
  const uint16_t* keep_of(const GNode& n) const { return n.pad ? keep_sets[n.pad - 1].keep[n.player].data() : nullptr; }
  const uint32_t* keep_bits(const GNode& n) const { return n.pad ? keep_sets[n.pad - 1].bits[n.player].data() : nullptr; }
  size_t row_len(const GNode& n) const { return n.pad ? keep_sets[n.pad - 1].kept[n.player] : hands[n.player].size(); }
  size_t stored_size(const GNode& n) const { return static_cast<size_t>(n.num_children) * row_len(n); }
  // A node's stored 16-bit rows (q, compact or full) decoded with scale s into its full A x H floats.
  template <class Q>
  void decode_rows(const GNode& n, const Q* q, float s, float* out) const;
  // Per decision (GNode::aux): the board's cards at the node. Hands sharing one are never dealt there.
  std::vector<uint64_t> dec_board;

  size_t node_size(const GNode& n) const { return static_cast<size_t>(n.num_children) * hands[n.player].size(); }
  // Read access: returns the node's A*H values (direct pointer, or decoded into scratch).
  const float* regrets_ro(const GNode& n, float* scratch) const;
  const float* strat_ro(const GNode& n, float* scratch) const;
  // Read-write access: returns a buffer to modify; call the matching commit afterwards.
  float* regrets_rw(const GNode& n, float* scratch);
  float* strat_rw(const GNode& n, float* scratch);
  void regrets_commit(const GNode& n, const float* data, float maxabs = -1.0f);  // maxabs < 0: compute
  void strat_commit(const GNode& n, const float* data, float maxval = -1.0f);
  void clear_storage();  // all regrets / strategy sums to 0
  // 16-bit -> float32 storage in place (#182): decodes every node exactly as regrets_ro / strat_ro do,
  // then frees the 16-bit arrays. Needs room for the float32 arrays; no walk may be running.
  void decompress();

  // Lazy loading (see LazyStore). Every accessor above calls ensure_loaded first.
  std::unique_ptr<LazyStore> lazy;
  void ensure_loaded(const GNode& n) const {
    if (lazy) lazy_touch(static_cast<uint32_t>(&n - nodes.data()));
  }
  void lazy_touch(uint32_t node) const;
  // Loads every segment (in parallel) and drops the lazy state. Not concurrent with other tree access.
  // Returns false (with err) if a segment could not be read.
  bool load_all(std::string& err);
  bool take_lazy_error(std::string& err);  // true (and clears it) if a lazy load failed since the last call
  // Quantise into 16-bit storage without the lazy-load hook (used by the loader itself).
  void regrets_store(const GNode& n, const float* data, float maxabs = -1.0f);
  void strat_store(const GNode& n, const float* data, float maxval = -1.0f);
  StreetSegments street_segments() const;

  // Per-node extras (keyed by node index)
  std::unordered_map<uint32_t, std::vector<uint8_t>> combo_lock;  // per local hand: 1 = locked
  // 16-bit storage: the exact strategy rows (A x H) of combo-locked nodes. Their locked entries are
  // quantized from these at every strat_commit, so they don't random-walk under stochastic rounding
  // (#380). Filled on the command thread only (combo_lock_node, before a solve); the walk only reads.
  std::unordered_map<uint32_t, std::vector<float>> combo_lock_s;
  void ensure_combo_lock_copies();
  std::unordered_map<uint32_t, std::vector<float>> incentives;    // per action
  std::unordered_map<uint32_t, uint32_t> eliminated;              // bitmask of removed actions

  std::atomic<long long> iterations{0};  // CFR iterations run on this tree (atomic: show_iters reads it mid-solve, #367)
  // Thread-count tuning under the default thread count (#276, upi.cpp): 0 = not tuned yet, else the count
  // this tree solves fastest with; tune_k / tune_s are the trial iterations run so far and their seconds
  // (even k: every hardware thread, odd k: half).
  int tuned_threads = 0;
  int tune_k = 0;
  double tune_s[2] = {0, 0};
  bool incomplete = false;
  bool train_missing_rivers = false;
  bool smart_turns = false;
  int missing_street = 0;     // first street without saved strategies (4 = turn, 5 = river); 0 = none
  bool fast_loaded = false;   // loaded without `full` (Pio: some commands need load_all_nodes)
  bool loaded_compressed = false;
  std::unordered_set<uint32_t> recalced;  // street roots re-solved on demand  // a truncated save was loaded (later streets unsolved)
  // Root results (EV OOP, EV IP, MES OOP, MES IP, exploitability) of the solve that produced a truncated
  // save. While its streets are missing, calc_results reports these, as Pio does (#195).
  bool has_saved_stats = false;
  double saved_stats[5] = {0, 0, 0, 0, 0};

  int num_hands(int p) const { return static_cast<int>(hands[p].size()); }
  int root_board_len() const { return static_cast<int>(cfg.board.size()); }

  // Build from config. Returns false + err.
  bool build(const GameConfig& c, std::string& err, bool allocate = true);
  // Memory needed (bytes) for the current node set; valid after build(..., false).
  uint64_t memory_bytes() const { return memory_bytes_for(compressed, false, compact_rows); }
  // with_preds: count the predictive array that set_predictive will allocate after the
  // build. Already-allocated preds are counted regardless, so memory_bytes() is right
  // once the feature is on; the flag is for the check that runs *before* allocation.
  uint64_t memory_bytes_for(bool compress, bool with_preds = false, bool compact = false) const;
  void allocate_storage();  // uses `compressed`
  void allocate_strategy_storage();
  void fill_term_pay();     // term_pay_ from payoff_ and anodes

  // Payoff for player p at a terminal given outcome (1 win, 0 tie, -1 lose).
  double terminal_payoff(const ActionNode& an, int p, int outcome) const;
  // Rake and ICM. Set through set_payoff, which also refreshes term_pay.
  const PayoffModel& payoff() const { return payoff_; }
  uint64_t payoff_version = 0;
  void set_payoff(const PayoffModel& pm) {
    ++payoff_version;
    payoff_ = pm;
    fill_term_pay();
    // An outcome table holds 2 * wins + ties, which is all a payoff needs while a tie pays the middle
    // of a win and a loss. Under ICM it does not, so the runouts are valued board by board.
    if (pm.icm.enabled) restore_allin_boards();
  }
  // terminal_payoff for every terminal action node, as the floats the solver uses: win / lose / tie
  // per player for a showdown, the fold payoff in `w` for a fold. Read per terminal visit instead of
  // calling terminal_payoff (double math, rake, an ICM check, the ActionNode load): 1.7% of
  // threebet_mono's samples on an M4; the table is 0.6-1.4% faster wall, with identical floats.
  struct TermPay {
    float w[2], l[2], t[2];
  };
  const TermPay& term_pay(const GNode& n) const { return term_pay_[n.anode]; }
  // Mean number of showdown terminals per river subtree of the action tree (river decision nodes
  // entered from a chance node), i.e. how many jobs Solver::river_cfr can batch per board. 0 if none.
  double river_showdowns_per_root() const;
  std::vector<std::unique_ptr<ActionNode>> schematic_roots;  // owned postflop trees (preflop mode)
  std::vector<uint8_t> perm_all;  // suit permutations valid for both ranges (flop-deal symmetrization)
  std::vector<float> flop_compat[2];  // per hand: compatible share of the subset flop weight
  std::vector<float> flop_card[2];    // [hand * 52 + card]: compatible weight of variants with that card
  std::vector<float> flop_pair[2];    // [hand * 1326 + combo]: compatible weight of variants with both cards
  void compute_flop_compat();

  const ActionNode& an(const GNode& n) const { return *anodes[n.anode]; }

  // Index of the five-card board in the showdown list.
  int showdown_board_id(const std::vector<int>& board);
  int allin_runouts_id(const std::vector<int>& board);
  // Fills AllinRunouts::e2 for the runout sets marked `table`, so Solver::showdown_value evaluates one
  // matrix product instead of a showdown kernel call per runout (a turn all-in: ~48 calls; srp_big: 17%
  // of all kernel calls). A postflop tree marks every set: at most one flop set and 49 turn sets, 71 MB
  // with every hand in both ranges. A preflop tree marks them in plan_allins.
  void build_allin_tables();
  // Preflop trees with flops: which runout sets get a table and which keep their boards, chosen as
  // whatever makes the tree smallest. A board is kept only if a river showdown or a set without a
  // table reads it; the others are never built. Called once the nodes exist.
  void plan_allins();
  // Stores the boards of every runout set that has none (ICM needs them; tests compare against them).
  void restore_allin_boards();
  // What that would add to the tree, at least (the boards' entries; see showdown_board_bytes): for
  // the memory check of whoever switches ICM on. 0 for a tree that has all its boards.
  uint64_t allin_boards_missing_bytes() const;
  // Bytes of a set's table: one count per hand pair, a byte each for a turn all-in, two for a flop's.
  // Compact: per pair of live hands, each pair once, and the list of the live hands.
  uint64_t allin_table_bytes(const AllinRunouts& ar) const {
    const uint64_t width = ar.runouts <= 127 ? 1 : 2;
    if (!compact_tables()) return static_cast<uint64_t>(num_hands(0)) * num_hands(1) * width;
    const uint64_t n = allin_live_hands(ar);
    return n * (n - (n > 0)) / 2 * width + n * sizeof(uint16_t);
  }
  // The tables of this tree are compact ones (AllinRunouts::tri8): both players hold the same hands in
  // the same order, so one triangle serves both. Preflop trees only: a postflop tree's values stay the
  // sums they were, to the last bit.
  bool compact_tables() const { return cfg.preflop && same_hands; }
  bool same_hands = false;
  // Player 0's hands without a card of the set's board.
  int allin_live_hands(const AllinRunouts& ar) const;
  // What plan_allins chooses for the runout sets of a preflop tree (bit 0: tables for the flop sets,
  // bit 1: for the turn sets). Set before build.
  //   table_limit 0: the smallest tree, what an estimate is to say and what a tree short of memory gets.
  //   table_limit > 0: a table is the faster way to value a set (bench/where-the-time-goes.md, #496), so
  //     with memory to spare the tree gets them: at both levels if it then stays within table_limit bytes
  //     (counted for the storage limit_compress and limit_preds name, as memory_bytes_for counts), else
  //     the smallest tree. Nothing in between: the turn sets' tables are the time (2x on a tree with turn
  //     bets, measured), and with them a flop's own table is less than the boards it then makes needless;
  //     tables for the flop sets alone bought nothing measurable.
  //   allin_plan >= 0: these levels, whatever the memory (set_outcome_tables fast, measurements, tests).
  uint64_t table_limit = 0;
  bool limit_compress = true, limit_preds = false;
  int allin_plan = -1;
  bool gpu_tables = false;

 private:
  void rank_showdown_boards(size_t first);
  void rank_showdown_board(ShowdownBoard& board) const;
  void store_allin_boards(AllinRunouts& ar);
  uint64_t showdown_board_bytes(uint64_t board_mask) const;
  std::unordered_map<uint64_t, int> showdown_index_;
  std::unordered_map<uint64_t, int> allin_index_;
  PayoffModel payoff_;
  std::vector<TermPay> term_pay_;  // by anode index
};

}  // namespace pio
