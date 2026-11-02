// Materialized game tree: betting tree expanded over dealt cards (with suit isomorphism),
// per-player hand lists, showdown orderings and strategy storage.
#pragma once
#include <array>
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

namespace pio {

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
  bool preflop = false;            // preflop tree (board empty): lines are preflop lines
  std::vector<Schematic> schematics;
  std::vector<SubsetFlop> subset;  // used only when schematics are attached (full preflop tree)
  std::vector<int> board;          // 3, 4 or 5 cards (empty for preflop)
  int32_t pot[3] = {0, 0, 0};      // set_pot: oop, ip, dead
  int32_t eff_stack = 0;
  Weights1326 range[2]{};          // OOP, IP
  bool iso_flop = true, iso_turn = false;
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
  uint16_t pad;
  uint32_t aux;        // chance: ChanceInfo index; showdown: board id; decision: decision index
  uint64_t storage;    // decision: float offset into regrets/strat arrays (size A * H[player])
};

struct ChanceInfo {
  // Flop deal at a preflop exit: one child per subset flop (cards/iso unused).
  bool flop_deal = false;
  std::vector<std::array<uint8_t, 3>> flops;
  std::vector<float> fweight;  // weight / (total weight * C(48,3)/C(52,3)): per-pair deal probability
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
struct AllinRunouts {
  std::vector<uint32_t> boards;
  float scale;
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
  uint8_t file_mode = 0;           // save's storage mode byte (bit 0: 16-bit records)
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
  PayoffModel payoff;
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
  ZVec<float> regrets;
  ZVec<float> strat;      // cumulative strategy (or locked strategy for locked nodes)
  ZVec<int16_t> regrets_q;
  ZVec<uint16_t> strat_q;
  std::vector<float> rscale, sscale;
  uint64_t storage_floats = 0;
  uint64_t num_decisions = 0;

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
  std::unordered_map<uint32_t, std::vector<float>> incentives;    // per action
  std::unordered_map<uint32_t, uint32_t> eliminated;              // bitmask of removed actions

  long long iterations = 0;  // CFR iterations run on this tree
  bool incomplete = false;
  int missing_street = 0;     // first street without saved strategies (4 = turn, 5 = river); 0 = none
  bool fast_loaded = false;   // loaded without `full` (Pio: some commands need load_all_nodes)
  bool loaded_compressed = false;
  std::unordered_set<uint32_t> recalced;  // street roots re-solved on demand  // a truncated save was loaded (later streets unsolved)

  int num_hands(int p) const { return static_cast<int>(hands[p].size()); }
  int root_board_len() const { return static_cast<int>(cfg.board.size()); }

  // Build from config. Returns false + err.
  bool build(const GameConfig& c, std::string& err, bool allocate = true);
  // Memory needed (bytes) for the current node set; valid after build(..., false).
  uint64_t memory_bytes() const { return memory_bytes_for(compressed); }
  uint64_t memory_bytes_for(bool compress) const;
  void allocate_storage();  // uses `compressed`

  // Payoff for player p at a terminal given outcome (1 win, 0 tie, -1 lose). With subtree=true the
  // preflop offset is left out (schematic-relative values, as show_win_evs prints them).
  double terminal_payoff(const ActionNode& an, int p, int outcome, bool subtree = false) const;
  std::vector<std::unique_ptr<ActionNode>> schematic_roots;  // owned postflop trees (preflop mode)
  std::vector<uint8_t> perm_all;  // suit permutations valid for both ranges (flop-deal symmetrization)
  std::vector<float> flop_compat[2];  // per hand: compatible share of the subset flop weight
  std::vector<float> flop_card[2];    // [hand * 52 + card]: compatible weight of variants with that card
  std::vector<float> flop_pair[2];    // [hand * 1326 + combo]: compatible weight of variants with both cards
  void compute_flop_compat();

  const ActionNode& an(const GNode& n) const { return *anodes[n.anode]; }

  // Index of the (5-card) showdown board, computing hand orderings on first use.
  int showdown_board_id(const std::vector<int>& board);
  int allin_runouts_id(const std::vector<int>& board);

 private:
  std::unordered_map<uint64_t, int> showdown_index_;
  std::unordered_map<uint64_t, int> allin_index_;
};

}  // namespace pio
