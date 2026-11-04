// UPI (Universal Poker Interface) command processor.
#pragma once
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "game.hpp"
#include "solver.hpp"

namespace pio {

extern const char* const kPreflopOrder;
uint64_t total_memory_bytes();
uint64_t available_memory_bytes();

struct CmdError {
  std::string msg;
  bool raw = false;  // print as "ERROR: <msg>" without the command name
};

// RAII: asks the solving thread to pause between iterations, then holds the tree mutex.
struct TreePause {
  std::atomic<int>& req;
  std::condition_variable& cv;
  std::unique_lock<std::mutex> lk;
  TreePause(std::atomic<int>& r, std::mutex& m, std::condition_variable& c) : req(r), cv(c), lk(m, std::defer_lock) {
    req.fetch_add(1);
    lk.lock();
  }
  ~TreePause() {
    lk.unlock();
    req.fetch_sub(1);
    cv.notify_all();
  }
};

// A resolved node: either the pseudo root "r" or a game node, plus the frame permutation that maps
// the node's canonical hands/cards to displayed hands/cards.
struct NodeRef {
  bool pseudo_root = false;
  uint32_t node = 0;
  int frame = IDENTITY_PERM;     // canonical -> displayed
  std::string id;                // normalized node id
  std::vector<int> board;        // displayed board at the node
  struct Step {
    uint32_t node;  // node the step was taken from
    int choice;     // decision: action index; chance: slot
    int perm;       // chance: perm of the dealt card (canonical -> parent frame)
  };
  std::vector<Step> steps;
};

class Upi {
 public:
  Upi();
  ~Upi();
  int run();  // main loop on stdin; returns exit code

 private:
  using Args = std::vector<std::string>;
  using Handler = std::function<std::string(const Args&)>;

  // ---- I/O ----
  void out_raw(const std::string& s);          // thread-safe write (+flush)
  void respond(const std::string& body);       // body + end string
  bool next_line(std::string& line);
  bool execute_line(const std::string& line, bool silent = false);  // false on exit
  std::string dispatch(const std::string& cmd, const Args& args, bool& is_exit);

  // A partial (subtree) solve running on the solver thread: go_partial (#153). Reach at the
  // subtree root is captured once; the DCFR clock restarts at t = 1 for it (like solve_partial).
  struct SubtreeJob {
    uint32_t root = 0;
    std::string id;                // normalized node id, echoed in SOLVER: blocks as "partial: <id>"
    double pot = 0;                // total pot at the node (fraction accuracies refer to it)
    std::vector<float> r0, r1;     // both players' reach at the node
  };

  // ---- helpers ----
  void register_commands();
  void register_node_commands();
  void register_save_commands();
  void require_tree() const;
  void stop_solver(const char* reason = "requested");
  void start_solver(double seconds, long long steps, std::shared_ptr<SubtreeJob> sub = nullptr);
  void solver_thread_main(double seconds, long long steps, std::shared_ptr<SubtreeJob> sub);
  void start_subtree_solve(const NodeRef& r, double seconds, long long steps);
  std::string stats_block(const RootStats& st, double running_time);
  std::string fmt(double v) const;
  std::string pfmt(const char* f, double v) const;  // printf-style single value
  std::string floats_line(const float* v, int n) const;
  int player_arg(const std::string& s) const;

  NodeRef resolve(const std::string& id) const;
  std::string node_type(const NodeRef& r) const;
  std::string alg_flag() const;
  std::string show_node_text(const NodeRef& r) const;
  std::vector<std::pair<std::string, NodeRef>> children_of(const NodeRef& r) const;
  void node_reach(const NodeRef& r, std::vector<float>& r0, std::vector<float>& r1);
  // Map local (canonical) per-hand values of player p to 1326 displayed order.
  void to_1326(int p, int frame, const float* local, float* out1326, float fill = 0.0f) const;
  void from_1326(int p, int frame, const float* in1326, float* local) const;
  int display_card(int c) const;   // original -> displayed
  int original_card(int c) const;  // displayed -> original

  // Line helpers (all runouts)
  void for_each_runout(const std::string& line_id, const std::function<void(const NodeRef&, double)>& f) const;
  double line_matchups(const std::string& line_id);

  // Commands needing more code
  std::string cmd_build_tree(const Args& a);
  std::string cmd_show_strategy(const Args& a, bool pp);
  std::string cmd_calc_ev(const Args& a, bool pp);
  std::string cmd_calc_eq(const Args& a, bool pp, bool node);
  std::string cmd_set_strategy(const Args& a);
  std::string cmd_set_mes(const Args& a);
  std::string cmd_round_up_to(const Args& a);
  std::string cmd_show_all_lines();
  std::string cmd_show_all_freqs(const Args& a);
  std::string cmd_print_all_strats();
  std::string cmd_node_count();
  std::string cmd_bench();
  std::string cmd_solve_partial(const Args& a);
  std::string cmd_explo_partial(const Args& a);
  std::string cmd_solve_all_splits(const Args& a);
  std::string cmd_dump_tree(const Args& a);
  std::string cmd_load_tree(const Args& a);
  std::string cmd_show_metadata(const Args& a);
  std::string cmd_show_nuts(const Args& a);
  std::string cmd_calc_eq_preflop(const Args& a);
  std::string estimate_text();
  std::string build_from(GameConfig c, bool mem_ignore, bool pessimistic, const char* okmsg);
  void apply_pcfr_settings();  // pushes predictive_/avg_delay_ into the solver (+preds alloc)
  void register_preflop_commands();
  void lock_node_impl(uint32_t ni, bool lock);
  void set_node_strategy(uint32_t ni, const float* local_strat);  // A*H canonical
  double explo_at(const NodeRef& r, std::vector<float>& r0, std::vector<float>& r1, RootStats* st = nullptr);
  // Same, with the reach already computed (the solver thread's partial job keeps its own copy).
  double explo_at_reach(uint32_t node, const float* r0, const float* r1, RootStats* st = nullptr);
  void solve_subtree(const NodeRef& r, double accuracy, long long max_iters, double max_seconds = 0);
  // Re-solve the street subtree containing r if its strategies are missing (truncated save) or
  // set_always_recalc asks for it. Call before reading strategies of r.
  void ensure_recalced(const NodeRef& r);

  // ---- state ----
  GameConfig cfg_;            // global state (set_* commands)
  LineSet pf_lines_;          // add_preflop_line / remove_preflop_line
  GameConfig pf_cfg_;         // state captured by build_preflop_tree
  std::vector<Schematic> schematics_;
  std::vector<SubsetFlop> subset_;
  bool have_pf_ = false;
  PayoffModel payoff_;
  std::unique_ptr<GameTree> tree_;
  std::unique_ptr<Solver> solver_;
  std::vector<std::string> tree_info_;
  // Pio's legacy set_bet_sizes / set_donk_bet: stored and shown by show_tree_params, never used
  // for building (Pio's build_tree ignores them too; trees come from add_line).
  std::vector<long long> legacy_bet_sizes_;
  bool legacy_donk_bet_ = false;

  std::string end_string_;
  std::FILE* out_ = stdout;
  bool redirected_ = false;
  mutable std::mutex out_mu_;
  int silent_depth_ = 0;

  // solver run state
  std::thread solver_thread_;
  std::atomic<bool> solving_{false};
  std::atomic<bool> stop_flag_{false};
  std::string stop_reason_;
  std::mutex tree_mu_;
  std::atomic<int> pause_requests_{0};
  std::condition_variable pause_cv_;
  std::mutex pause_mu_;

  long long iterations_ = 0;
  double solve_seconds_ = 0;  // accumulated solving time for the tree
  double accuracy_ = 0;       // 0 = never
  DcfrParams dcfr_;           // set_dcfr: DCFR discount exponents (defaults 1.5 / 0 / 2)
  bool predictive_ = false;   // set_predictive: PCFR+-style prediction in regret matching (#71)
  int avg_delay_ = 32;         // set_avg_delay: early iterates dropped from the average (#71, #135)
  bool pruning_ = true;       // set_pruning; on by default (#51: never slower on the bench suite)
  int prune_period_ = 8;      // every prune_period_-th iteration is measured in full (see DcfrParams)
  bool accuracy_fraction_ = false;
  double recalc_accuracy_[3] = {0, 0, 0};
  int always_recalc_street_ = 0, always_recalc_ms_ = 0;
  int info_freq_ = 25;
  // set_info_freq auto: report (and test the accuracy target) on an adaptive schedule instead of
  // every info_freq_ iterations. See Upi::next_auto_report.
  bool info_auto_ = false;
  long long next_report_ = 0, last_rep_t_ = 0;
  double last_rep_e_ = 0;
  long long next_auto_report(long long t, double e, double target);
  int threads_ = 0;
  double step_ = 1.0;
  std::string algorithm_ = "auto";
  std::string tree_alg_ = "auto";  // algorithm the current tree was built with
  std::string small_strats_ = "auto";
  bool ignore_mem_check_ = false;
  int compression_precision_ = 11, compression_level_ = 11;
  int display_perm_ = IDENTITY_PERM;
  int first_player_ = 0;
  bool icm_tables_defined_ = false;
  long long discount_restart_ = 0;  // tree iteration at which the DCFR discount clock restarted
  std::chrono::steady_clock::time_point start_time_;

  // input
  std::deque<std::string> script_lines_;
  std::unordered_map<std::string, Handler> handlers_;
};

}  // namespace pio
