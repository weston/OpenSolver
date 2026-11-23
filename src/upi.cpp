// UPI core: input loop, output/END handling, settings, tree building, solving thread.
#include "upi.hpp"

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>

#include "parallel.hpp"
#include "range.hpp"
#include "version.hpp"

#if defined(__APPLE__)
#include <mach/mach.h>
#include <sys/sysctl.h>
#endif

namespace pio {

// The 169 preflop classes in PioSOLVER's order (show_preflop_order, calc_eq_preflop).
const char* const kPreflopOrder = "AA AKs AKo KK AQs AQo KQo KQs QQ AJs AJo KJo KJs QJo QJs JJ ATo ATs KTs KTo QTs QTo JTs JTo TT A9o A9s K9s K9o Q9s Q9o J9s J9o T9s T9o 99 A8s A8o K8s K8o Q8s Q8o J8s J8o T8o T8s 98o 98s 88 A7o A7s K7s K7o Q7s Q7o J7s J7o T7o T7s 97s 97o 87o 87s 77 A6o A6s K6s K6o Q6s Q6o J6s J6o T6o T6s 96o 96s 86o 86s 76o 76s 66 A5o A5s K5s K5o Q5s Q5o J5s J5o T5o T5s 95s 95o 85o 85s 75s 75o 65o 65s 55 A4o A4s K4s K4o Q4s Q4o J4s J4o T4o T4s 94s 94o 84o 84s 74s 74o 64o 64s 54o 54s 44 A3s A3o K3s K3o Q3o Q3s J3o J3s T3o T3s 93o 93s 83s 83o 73o 73s 63o 63s 53s 53o 43o 43s 33 A2s A2o K2s K2o Q2o Q2s J2o J2s T2o T2s 92o 92s 82s 82o 72o 72s 62o 62s 52o 52s 42o 42s 32o 32s 22 ";

namespace {

std::vector<std::string> tokenize(const std::string& line) {
  std::vector<std::string> out;
  size_t i = 0, n = line.size();
  while (i < n) {
    while (i < n && std::isspace(static_cast<unsigned char>(line[i]))) ++i;
    if (i >= n) break;
    std::string tok;
    if (line[i] == '"') {
      ++i;
      while (i < n && line[i] != '"') tok += line[i++];
      if (i < n) ++i;
    } else {
      while (i < n && !std::isspace(static_cast<unsigned char>(line[i]))) tok += line[i++];
    }
    out.push_back(tok);
  }
  return out;
}

bool parse_int(const std::string& s, long long& v) {
  if (s.empty()) return false;
  char* end = nullptr;
  v = std::strtoll(s.c_str(), &end, 10);
  return end && *end == '\0';
}

bool parse_double(const std::string& s, double& v) {
  if (s.empty()) return false;
  char* end = nullptr;
  v = std::strtod(s.c_str(), &end);
  return end && *end == '\0' && std::isfinite(v);
}

long long arg_int(const std::vector<std::string>& a, size_t i, const char* /*what*/) {
  long long v;
  if (i >= a.size() || !parse_int(a[i], v)) throw CmdError{"incorrect or missing argument"};
  return v;
}

double arg_double(const std::vector<std::string>& a, size_t i, const char* /*what*/) {
  double v;
  if (i >= a.size() || !parse_double(a[i], v)) throw CmdError{"incorrect or missing argument"};
  return v;
}

std::string join(const std::vector<std::string>& a, size_t from, const char* sep = " ") {
  std::string s;
  for (size_t i = from; i < a.size(); ++i) {
    if (i > from) s += sep;
    s += a[i];
  }
  return s;
}

}  // namespace

uint64_t total_memory_bytes() {
#if defined(_WIN32)
  MEMORYSTATUSEX st{};
  st.dwLength = sizeof(st);
  return GlobalMemoryStatusEx(&st) ? static_cast<uint64_t>(st.ullTotalPhys) : 0;
#elif defined(__APPLE__)
  uint64_t mem = 0;
  size_t len = sizeof(mem);
  sysctlbyname("hw.memsize", &mem, &len, nullptr, 0);
  return mem;
#else
  long pages = sysconf(_SC_PHYS_PAGES), page = sysconf(_SC_PAGE_SIZE);
  return pages > 0 && page > 0 ? static_cast<uint64_t>(pages) * page : 0;
#endif
}

// For tests (#508): WSOLVER_FREE_MB=<whole number of MB> stands in for the memory that is free, so that
// what is decided from it (a build refused, 16-bit storage, a preflop tree's outcome tables, the switch
// to 32-bit storage, ICM on a tree without boards) can be reached on any machine and comes out the same
// on each. Not a setting for users: a figure over the real one only moves the failure from an error
// message to the allocation. Anything but digits is ignored.
static bool free_memory_for_tests(uint64_t& bytes) {
  const char* v = std::getenv("WSOLVER_FREE_MB");
  if (!v || !*v) return false;
  uint64_t mb = 0;
  for (const char* c = v; *c; ++c) {
    if (*c < '0' || *c > '9' || mb > (uint64_t(1) << 40)) return false;
    mb = mb * 10 + static_cast<uint64_t>(*c - '0');
  }
  bytes = mb << 20;
  return true;
}

uint64_t available_memory_bytes() {
  if (uint64_t forced = 0; free_memory_for_tests(forced)) return forced;
#if defined(_WIN32)
  MEMORYSTATUSEX st{};
  st.dwLength = sizeof(st);
  return GlobalMemoryStatusEx(&st) ? static_cast<uint64_t>(st.ullAvailPhys) : 0;
#elif defined(__APPLE__)
  vm_size_t page = 0;
  host_page_size(mach_host_self(), &page);
  vm_statistics64_data_t vm{};
  mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
  if (host_statistics64(mach_host_self(), HOST_VM_INFO64, reinterpret_cast<host_info64_t>(&vm), &count) == KERN_SUCCESS)
    return (static_cast<uint64_t>(vm.free_count) + vm.inactive_count + vm.purgeable_count + vm.speculative_count) * page;
  return total_memory_bytes() / 2;
#else
  long pages = sysconf(_SC_AVPHYS_PAGES), page = sysconf(_SC_PAGE_SIZE);
  return pages > 0 && page > 0 ? static_cast<uint64_t>(pages) * page : total_memory_bytes() / 2;
#endif
}


// ------------------------------------------------------------------------------------------------

Upi::Upi() {
  init_cards();
  cfg_.iso_flop = true;
  cfg_.iso_turn = false;
  start_time_ = std::chrono::steady_clock::now();
  step_ = 0.425;  // Pio's default step
  recalc_accuracy_[0] = 0.0025;
  recalc_accuracy_[1] = 0.001;
  recalc_accuracy_[2] = 0.0005;
  register_commands();
}

Upi::~Upi() {
  signal_stop();
  if (solver_thread_.joinable()) solver_thread_.join();
  if (redirected_ && out_) std::fclose(out_);
}

void Upi::out_raw(const std::string& s) {
  std::lock_guard<std::mutex> lk(out_mu_);
  std::fwrite(s.data(), 1, s.size(), out_);
  std::fflush(out_);
}

void Upi::respond(const std::string& body) {
  if (silent_depth_ > 0) return;
  std::string s = body;
  if (!s.empty() && s.back() != '\n') s += '\n';
  if (!end_string_.empty()) s += end_string_ + "\n";
  out_raw(s);
}

std::string Upi::pfmt(const char* f, double v) const {
  char buf[64];
  std::snprintf(buf, sizeof(buf), f, v);
  return buf;
}

// Pio prints computed values as the float rounded to 9 significant digits (printf "%.9g": "0.570510685",
// "2821.31494", "1.05068034e-06", "60"), which round-trips a float exactly; verified on every vector
// and scalar in tests/pio_compare/probe*.txt against PioSOLVER 3.10.1 (#125).
static char* put_float(char* buf, char* end, float f) {
  if (std::isnan(f)) {
    std::memcpy(buf, "nan", 3);
    return buf + 3;
  }
  return std::to_chars(buf, end, f, std::chars_format::general, 9).ptr;
}

std::string Upi::fmt(double v) const {
  char buf[64];
  return std::string(buf, put_float(buf, buf + sizeof(buf), static_cast<float>(v)));
}

// One value per hand (or class) as Pio prints it: "%.9g " each, so the line ends in a space.
std::string Upi::floats_line(const float* v, int n) const {
  std::string s;
  s.reserve(static_cast<size_t>(n) * 10);
  char buf[32];
  for (int i = 0; i < n; ++i) {
    char* e = put_float(buf, buf + sizeof(buf), v[i]);
    *e++ = ' ';
    s.append(buf, e);
  }
  return s;
}

int Upi::player_arg(const std::string& s) const {
  std::string u = s;
  for (auto& ch : u) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
  if (u == "OOP") return 0;
  if (u == "IP") return 1;
  throw CmdError{"incorrect player"};  // Pio's message (probed); set_first_iteration_player differs
}

void Upi::require_tree() const {
  if (!tree_) throw CmdError{"missing/incorrect tree"};
}

bool Upi::next_line(std::string& line) {
  from_script_ = !script_lines_.empty();
  if (from_script_) {
    line = script_lines_.front();
    script_lines_.pop_front();
    return true;
  }
  return static_cast<bool>(std::getline(std::cin, line));
}

int Upi::run() {
  out_raw(std::string(SOLVER_NAME) + " " + SOLVER_VERSION + " (" + __DATE__ + ", " + __TIME__ + ")\n" +
          "UPI-compatible heads-up NLHE postflop solver\n");
  std::string line;
  while (next_line(line)) {
    if (!execute_line(line)) break;
  }
  release_hold();  // exit with a solve parked by hold: let it see the stop
  signal_stop();
  if (solver_thread_.joinable()) solver_thread_.join();
  return 0;
}

bool Upi::execute_line(const std::string& raw, bool silent) {
  // Script lines (load_script, Pio 3.10.1): each answers as usual, or without the end string in
  // load_script_silent; a LABEL: line answers empty; the first ERROR ends the script; then the
  // load_script itself answers (Pio prints that "ok!" without its end string, merged into the next
  // command's answer; ours ends it like any other).
  // Inside a load_script_silent everything is silent, a script nested in it too: its answers go
  // into the outer script's one response (an end string mid-way would end that response early).
  auto any_silent = [this] { return std::find(script_silent_.begin(), script_silent_.end(), true) != script_silent_.end(); };
  const bool quiet = from_script_ && any_silent();
  auto answer = [&](const std::string& body) {
    if (quiet) out_raw(body.empty() || body.back() == '\n' ? body : body + "\n");
    else respond(body);
  };
  if (!raw.empty() && raw[0] == '\x02') {
    bool was_silent = !script_silent_.empty() && script_silent_.back();
    if (!script_silent_.empty()) script_silent_.pop_back();
    // A nested script's ok belongs to the enclosing script: no end string inside a silent one.
    std::string ok = was_silent ? "load_script_silent ok!" : "load_script ok!";
    if (any_silent()) out_raw(ok + "\n");
    else respond(ok);
    return true;
  }
  std::string line = raw;
  while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
  size_t first = line.find_first_not_of(" \t");
  if (first == std::string::npos) return true;
  if (line[first] == '#') return true;
  auto toks = tokenize(line);
  if (toks.empty()) return true;
  std::string cmd = toks[0];
  Args args(toks.begin() + 1, toks.end());
  if (cmd == "LABEL:") {
    if (from_script_) answer("");
    return true;
  }
  bool is_exit = false;
  std::string body;
  if (silent) ++silent_depth_;
  try {
    body = dispatch(cmd, args, is_exit);
  } catch (const CmdError& e) {
    body = e.raw ? "ERROR: " + e.msg : "ERROR: " + cmd + " " + e.msg;
  } catch (const std::bad_alloc&) {
    body = "ERROR: " + cmd + " out of memory";
  } catch (const std::exception& e) {
    body = "ERROR: " + cmd + " " + e.what();
  }
  if (silent) --silent_depth_;
  // A lazily loaded segment that could not be read: report it instead of silently using zeros.
  std::string lerr;
  if (tree_ && !solving_ && tree_->take_lazy_error(lerr)) body = "ERROR: " + cmd + " cannot read tree data: " + lerr;
  if (is_exit) return false;
  if (!silent && body != "\x01") answer(body);
  if (from_script_ && body.rfind("ERROR", 0) == 0)
    while (!script_lines_.empty() && (script_lines_.front().empty() || script_lines_.front()[0] != '\x02'))
      script_lines_.pop_front();
  return true;
}

// A chip amount (set_pot, set_eff_stack, lines): atoi-like, as Pio reads them ("30.7" is 30), but bounded so
// pot sums can't overflow int32 (#371: 4294967346 wrapped to 50, and a 3e9 stack went negative).
static int32_t chips_arg(const std::string& s) {
  const long long v = std::atoll(s.c_str());
  if (v > 100000000LL || v < -100000000LL) throw CmdError{"incorrect or missing argument"};
  return static_cast<int32_t>(v);
}

std::string Upi::dispatch(const std::string& cmd, const Args& args, bool& is_exit) {
  if (cmd == "exit" || cmd == "quit") {
    stop_solver();
    is_exit = true;
    return "";
  }
  auto it = handlers_.find(cmd);
  if (it == handlers_.end()) throw CmdError{"Command " + cmd + " not recognized", true};
  // Pio 3.10 answers these with "missing/incorrect tree" when no tree exists, before looking at
  // the arguments (probed with each one bare and with valid arguments).
  static const std::set<std::string> needs_tree = {
      "calc_ev", "calc_ev_pp", "calc_global_freq", "calc_line_freq", "calc_results", "clear_cache",
      "combo_lock_node", "combo_unlock_all", "dump_tree", "eliminate_path", "estimate_rebuild_tree", "explo_partial", "go",
      "load_all_nodes", "lock_node", "node_count", "rebuild_tree", "set_icm", "set_strategy", "show_children",
      "show_effective_stack", "show_incentives", "show_locked_combos", "show_node", "show_strategy",
      "show_strategy_pp", "show_win_evs", "solve_all_splits", "solve_partial", "unlock_all", "unlock_node"};
  if (!tree_ && needs_tree.count(cmd)) require_tree();
  // These change (or read) state the solving thread uses mid-iteration: run them between iterations,
  // like the commands that take a TreePause themselves (#364: set_dcfr could be read half-written).
  static const std::set<std::string> pause_while_solving = {
      "set_dcfr", "set_avg_delay", "set_pruning", "set_first_iteration_player", "set_info_freq",
      // the solver thread reads accuracy_ with accuracy_fraction_ at each report: written apart, a solve
      // could test 5 x pot after "set_accuracy 5" following "... fraction" and stop at once
      "set_accuracy", "small_strats"};
  if (solving_ && pause_while_solving.count(cmd)) {
    TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
    return it->second(args);
  }
  return it->second(args);
}

// ------------------------------------------------------------------------------------------------
// Solving

std::string Upi::stats_block(const RootStats& st, double running_time) {
  char buf[512];
  std::snprintf(buf, sizeof(buf),
                "running time: %.3f\nEV OOP: %.3f\nEV IP: %.3f\nOOP's MES: %.3f\nIP's MES: %.3f\nExploitable for: %.3f\n",
                running_time, st.ev[0], st.ev[1], st.mes[0], st.mes[1], st.exploitability);
  return buf;
}

// What starting a solve needs and can fail at: a tree, and a lazily loaded one read completely. go and
// go_partial call it before they answer, so a failure is their one ERROR response, not a second
// response after "go ok!" (#376).
void Upi::prepare_solve() {
  require_tree();
  std::string lerr;
  if (!tree_->load_all(lerr)) throw CmdError{"cannot read the tree's file: " + lerr};
}

void Upi::start_solver(double seconds, long long steps, std::shared_ptr<SubtreeJob> sub) {
  require_tree();
  release_hold();  // every way a solve starts (go, go_partial, ...) drops a pending hold
  if (solving_) return;
  if (solver_thread_.joinable()) solver_thread_.join();
  // Solving touches every node: read a lazily loaded tree completely first (in parallel).
  std::string lerr;
  if (!tree_->load_all(lerr)) throw CmdError{"cannot read the tree's file: " + lerr};
  tree_->ensure_combo_lock_copies();  // 16-bit combo locks (#380): before the walk, which only reads them
  // Solving the whole tree again makes its own results current (#195); go_partial, like solve_partial,
  // keeps reporting the saved ones while other streets are still missing.
  if (!sub) tree_->has_saved_stats = false;
  stop_flag_ = false;
  solving_ = true;
  // copied here, on the command thread: the new thread reading end_string_ raced set_end_string (#364)
  std::string endl_str = end_string_.empty() ? "END" : end_string_;
  solver_thread_ = std::thread([this, seconds, steps, sub = std::move(sub), endl_str = std::move(endl_str)] {
    solver_thread_main(seconds, steps, std::move(sub), endl_str);
  });
}

void Upi::start_subtree_solve(const NodeRef& r, double seconds, long long steps) {
  auto job = std::make_shared<SubtreeJob>();
  job->root = r.node;
  job->id = r.id;
  node_reach(r, job->r0, job->r1);
  job->pot = tree_->cfg.start_pot();
  const GNode& n = tree_->nodes[r.node];
  if (n.kind == ANodeKind::Decision) {
    const ActionNode& an = tree_->an(n);
    job->pot = an.dead + an.inv[0] + an.inv[1];
  }
  start_solver(seconds, steps, std::move(job));
}

void Upi::solver_thread_main(double seconds, long long steps, std::shared_ptr<SubtreeJob> sub,
                             const std::string& endl_str) {
  out_raw("SOLVER: started\n");
  double best_e = std::numeric_limits<double>::infinity();  // 16-bit stall detection (#182)
  int stalled = 0;
  bool sched_auto = false;  // the auto report schedule is set up (read under tree_mu_ below, #364)
  auto t0 = std::chrono::steady_clock::now();
  double last_report_t = 0.0;  // solve time of the last report (info_freq 0 reports by time)
  double base = sub ? 0.0 : solve_seconds_;
  long long done = 0;
  std::string reason = "requested";
  // Default thread count (#276): with two hardware threads per core, the second one helps big trees and
  // hurts the rest (Zen 5 9950X, seconds to 0.1% of pot, 16 vs 32 threads: srp_big 78.2 vs 71.5,
  // threebet_mono 5.46 vs 6.23, deep 1.47 vs 1.70, flop_3bet 0.23 vs 0.36, turn 0.09 vs 0.18). Results do
  // not depend on the thread count, so a whole-tree solve times three iterations each with every hardware
  // thread and with half, alternating over iterations 18-23 (pruning is on by then, and the full-measure
  // iteration 24 is left out; from iteration 2 the pick was unreliable: deep chose 32 once), and
  // keeps the faster. An explicit set_threads turns this off.
  const int hw_threads = static_cast<int>(std::max(1u, std::thread::hardware_concurrency()));
#if defined(__aarch64__) || defined(_M_ARM64)
  const bool tune_threads = false;  // no SMT on Apple silicon
#else
  const bool tune_threads = !sub && threads_ == 0 && hw_threads >= 4;
#endif
  bool pool_set = !tune_threads;  // set under tree_mu_ below: a command may be using the pool now
  try {
  for (;;) {
    if (stop_flag_) {
      reason = stop_reason_.empty() ? "requested" : stop_reason_;
      break;
    }
    // Let pending commands run between iterations.
    if (pause_requests_.load() > 0) {
      std::unique_lock<std::mutex> lk(pause_mu_);
      pause_cv_.wait(lk, [&] { return pause_requests_.load() == 0 || stop_flag_.load(); });
      continue;
    }
    bool report = false;
    RootStats st{};
    {
      std::lock_guard<std::mutex> lk(tree_mu_);
      // set_info_freq changes info_auto_ under this lock (TreePause): start the auto schedule when it is
      // on, also if it was turned on mid-solve (#364: read at thread start, it raced the setter)
      if (info_auto_ != sched_auto) {
        sched_auto = info_auto_;
        if (sched_auto) {
          next_report_ = (sub ? done : tree_->iterations.load()) + 10;
          auto_sched_ = AutoReportSchedule{};
        }
      }
      // DCFR discount clock: the tree's own counter for a whole solve (restarted when the payoff
      // model changes), a fresh 1..N for a subtree solve (exactly what solve_partial does).
      if (sub) {
        solver_->iterate_subtree(sub->root, static_cast<int>(done + 1), sub->r0.data(), sub->r1.data());
      } else {
        ++tree_->iterations;
        ++iterations_;
        if (!pool_set) {
          pool().set_threads(tree_->tuned_threads > 0 ? tree_->tuned_threads : hw_threads);
          pool_set = true;
        }
        const bool trial = tune_threads && tree_->tuned_threads == 0 && tree_->iterations >= 18;
        if (trial) pool().set_threads(tree_->tune_k % 2 == 0 ? hw_threads : hw_threads / 2);
        const auto it0 = std::chrono::steady_clock::now();
        solver_->iterate(static_cast<int>(tree_->iterations - discount_restart_));
        if (trial) {
          tree_->tune_s[tree_->tune_k % 2] += std::chrono::duration<double>(std::chrono::steady_clock::now() - it0).count();
          if (++tree_->tune_k == 6) {
            const bool half = tree_->tune_s[1] < tree_->tune_s[0];
            tree_->tuned_threads = half ? hw_threads / 2 : hw_threads;
            pool().set_threads(tree_->tuned_threads);
            char buf[160];
            std::snprintf(buf, sizeof buf, "SOLVER: threads: %d (3 iterations took %.3f s with %d threads, %.3f s with %d)\n",
                          tree_->tuned_threads, tree_->tune_s[0], hw_threads, tree_->tune_s[1], hw_threads / 2);
            out_raw(buf);
          }
        }
      }
      ++done;
      double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
      if (!sub) solve_seconds_ = base + el;
      bool limit = (steps > 0 && done >= steps) || (seconds > 0 && el >= seconds);
      long long tick = sub ? done : tree_->iterations.load();
      // read each iteration (set_info_freq pauses the solve to change it, #364). A partial solve without an
      // info frequency would never check its stop conditions: solve_subtree's every 25 iterations then.
      const long long sub_freq = sub ? (info_freq_ > 0 ? info_freq_ : 25) : info_freq_;
      if (info_auto_ ? tick >= next_report_ : (sub_freq > 0 && tick % sub_freq == 0)) report = true;
      // info_freq 0: report (and check the target) by time, like Pio 3.10.1 (#378: reports at 0.33 s, 0.94 s
      // there; ours never reported, so a target never stopped the solve)
      if (!info_auto_ && sub_freq == 0 && el - last_report_t >= 0.5) report = true;
      if (report) last_report_t = el;
      if (limit) report = true;
      if (report) {
        double target;
        if (sub) {
          RootStats pst{};
          explo_at_reach(sub->root, sub->r0.data(), sub->r1.data(), &pst);
          st = pst;
          // A "partial:" first line marks the block as a subtree solve's (the viewer routes it
          // away from the whole-tree convergence series).
          out_raw("SOLVER:\npartial: " + sub->id + "\n" + stats_block(st, el) + endl_str + "\n");
          target = accuracy_fraction_ ? accuracy_ * sub->pot : accuracy_;
        } else {
          st = solver_->root_stats();
          out_raw("SOLVER:\n" + stats_block(st, solve_seconds_) + endl_str + "\n");
          target = accuracy_fraction_ ? accuracy_ * tree_->cfg.start_pot() : accuracy_;
        }
        if (accuracy_ > 0 && st.exploitability <= target) {
          reason = "required accuracy reached";
          break;
        }
        // 16-bit storage had an exploitability floor (threebet_mono: ~0.09% of pot, #182), which came from
        // rounding the strategy sums to nearest; they are rounded stochastically now (#272, game.hpp),
        // and this stays as a safety net. Under small_strats auto, a whole-tree solve whose last three reports set no new best while above
        // its target switches to float32 if that fits, once, and carries on from the same state.
        if (!sub && tree_->compressed && small_strats_ == "auto" && accuracy_ > 0) {
          if (st.exploitability < best_e) {
            best_e = st.exploitability;
            stalled = 0;
          } else if (++stalled == 3) {
            // decompress() allocates the float32 arrays while the 16-bit ones are still live: the peak
            // extra is the whole float32 storage, twice the net difference (8 vs 4 bytes per entry).
            const uint64_t need = 2 * (tree_->memory_bytes_for(false, predictive_) - tree_->memory_bytes_for(true, predictive_));
            if (need <= available_memory_bytes() * 9 / 10) {
              tree_->decompress();
              out_raw("SOLVER: 16-bit storage stopped improving; switched to 32-bit storage\n");
            } else {
              out_raw("SOLVER: 16-bit storage stopped improving; 32-bit storage does not fit\n");
            }
          }
        }
        if (info_auto_) {
          // Where the average restarts, in this solve's ticks (whole-tree solves only: subtree
          // solves accumulate from their first iteration, see Solver::iterate_subtree).
          const long long reset_t = !sub && solver_->avg_reset && solver_->avg_delay > 0
                                        ? discount_restart_ + solver_->avg_delay + 1 : 0;
          next_report_ = auto_sched_.next(tick, st.exploitability, accuracy_ > 0 ? target : 0.0, reset_t);
        }
      }
      if (limit) {
        reason = steps > 0 && done >= steps ? "required number of steps done" : "timeout reached";  // Pio's words
        break;
      }
    }
  }
  } catch (const std::bad_alloc&) {
    reason = "out of memory";  // #364: the process used to abort here
  } catch (const std::exception& e) {
    reason = std::string("error: ") + e.what();
  }
  // Not solving any more *before* "stopped" is printed: a go sent on seeing it must start a new solve,
  // not find this thread still flagged as solving and be dropped (#364).
  solving_ = false;
  out_raw("SOLVER: stopped (" + reason + ")\n");
}

// hold / release: see H["hold"]. Every way a solve ends or restarts drops the hold first, so a
// forgotten release can never leave a later solve parked.
void Upi::release_hold() {
  if (!held_) return;
  held_ = false;
  {
    std::lock_guard<std::mutex> g(pause_mu_);  // see TreePause: no lost wake-up
    pause_requests_.fetch_sub(1);
  }
  pause_cv_.notify_all();
}

// Stops a running solve wherever it is, a parked one too: the flag is set under pause_mu_ and the
// solver thread woken, so a solve waiting for its pause to end sees it.
void Upi::signal_stop() {
  {
    std::lock_guard<std::mutex> g(pause_mu_);
    stop_flag_ = true;
  }
  pause_cv_.notify_all();
}

void Upi::stop_solver(const char* reason) {
  release_hold();
  if (solver_thread_.joinable()) {
    if (solving_) {
      stop_reason_ = reason;
      signal_stop();
    }
    solver_thread_.join();
    stop_reason_.clear();
  }
}

// ------------------------------------------------------------------------------------------------

std::string Upi::cmd_build_tree(const Args& a) {
  bool mem_ignore = ignore_mem_check_;
  bool pessimistic = false;
  for (auto& s : a) {
    if (s == "mem_ignore") mem_ignore = true;
    else if (s == "mem_pessimistic") pessimistic = true;
    else throw CmdError{"incorrect or missing argument"};
  }
  // Pio 3.10.1: no board, or no lines added, is "missing/incorrect tree" (probe 19)
  if (cfg_.board.size() < 3 || cfg_.lines.add.empty()) throw CmdError{"missing/incorrect tree"};
  GameConfig c = cfg_;
  c.preflop = false;
  c.schematics.clear();
  c.subset.clear();
  return build_from(c, mem_ignore, pessimistic, "build_tree ok!");
}

// The outcome tables of a preflop tree's runout sets (#494, #496) are the faster way to value them and,
// with every hand, more bytes than the boards they replace. auto: built where the tree with them fits
// in memory by the margin the other automatic choices of storage keep (build_from), the smallest tree
// otherwise. A tree is not the same to the last bit with and without them: the same sums, in another
// order.
void Upi::plan_outcome_tables(GameTree& t, uint64_t avail, bool compress) const {
  t.table_limit = outcome_tables_ == "auto" ? avail * 9 / 10 : 0;
  t.limit_compress = compress;
  t.limit_preds = predictive_;
  t.allin_plan = outcome_tables_ == "fast" ? 3 : -1;
}

std::string Upi::build_from(GameConfig c, bool mem_ignore, bool pessimistic, const char* okmsg) {
  stop_solver();
  auto t = std::make_unique<GameTree>();
  // Rake and ICM belong to a tree in Pio (probed): a new tree starts with both off, while the ICM
  // points stay defined. set_rake / set_icm need a tree, so a script sets them after build_tree.
  PayoffModel pm = payoff_;
  pm.rake_frac = 0;
  pm.rake_cap = 0;
  pm.icm.enabled = false;
  t->set_payoff(pm);
  std::string err;
  // Old tree memory is released before allocating the new one, so count it as available.
  uint64_t avail = available_memory_bytes() + (tree_ ? tree_->memory_bytes() : 0);
  plan_outcome_tables(*t, avail, small_strats_ != "off");  // a preflop tree's storage is 16-bit unless turned off
  if (!t->build(c, err, false)) throw CmdError{err};
  uint64_t full = t->memory_bytes_for(false, predictive_), small = t->memory_bytes_for(true, predictive_);
  if (small_strats_ == "on" || small_strats_ == "compact") t->compressed = true;
  else if (small_strats_ == "off") t->compressed = false;
  else {
    // auto: 16-bit storage for preflop trees (like Pio), when float32 does not fit, and (x86) when
    // the float32 tree is large for the thread count. 16-bit converges in the same number of
    // iterations as float32 down to ~0.05% of pot on the bench trees, so the choice is speed per
    // iteration: on x86, half the bytes win once (float32 bytes x threads) reaches about 2 GB, and
    // the conversion arithmetic loses below that (Zen 5 32 threads flop_srp_4 -24%, deep -10%,
    // flop_srp_2 -9%, flop_3bet +7%; 1 thread deep +20%). On Apple silicon the memory system keeps
    // up with float32: since the regret update vectorizes under clang (#180), float32 is faster on
    // every tree (M4 10 threads, time to 0.05% of pot: deep 3.87 vs 4.10 s, flop_srp_4 2.08 vs
    // 2.14, flop_3bet 0.50 vs 0.52, flop_srp_2 0.96 vs 0.99; threebet_mono 24.9 s vs never), so
    // there 16-bit is only for capacity. 16-bit used to have a precision floor (flop_3bet plateaued
    // at 0.009 chips against 0.002, threebet_mono never reached 0.05% of pot); it came from the
    // strategy sums' rounding and is gone with stochastic rounding (#272, bench/bandwidth.md).
#if defined(__aarch64__) || defined(_M_ARM64)
    t->compressed = c.preflop || full > avail * 9 / 10;
#else
    const uint64_t th = threads_ > 0 ? static_cast<uint64_t>(threads_)
                                     : std::max<uint64_t>(1, std::thread::hardware_concurrency());
    t->compressed = c.preflop || full > avail * 9 / 10 || full * th >= (uint64_t(2) << 30);
#endif
  }
  // Compact river rows (#239): ~7% less 16-bit storage for ~12% more time per iteration, so only on
  // request or when full 16-bit rows don't fit either.
  t->select_rows(t->compressed && (small_strats_ == "compact" || (small_strats_ == "auto" && small > avail * 9 / 10)));
  uint64_t need = t->memory_bytes_for(t->compressed, predictive_, t->compact_rows);  // preds is allocated after the build (#98)
  if (pessimistic) need = need * 2;  // rough stand-in for an unpaired rainbow board
  if (!mem_ignore && need > avail)
    throw CmdError{"not enough memory: need " + std::to_string(need >> 20) + " MB (" + std::to_string(small >> 20) +
                   " MB compressed), available " + std::to_string(avail >> 20) + " MB"};
  tree_.reset();
  solver_.reset();
  t->allocate_storage();
  tree_ = std::move(t);
  payoff_ = pm;
  solver_ = std::make_unique<Solver>(*tree_);
  solver_->river_batch = Solver::river_batch_for(*tree_);
  solver_->first_player = first_player_;
  solver_->params = dcfr_;
  apply_pcfr_settings();
  solver_->pruning = pruning_;
  solver_->prune_period = prune_period_;
  tree_alg_ = algorithm_;
  iterations_ = 0;
  discount_restart_ = 0;
  solve_seconds_ = 0;
  return okmsg;
}

void Upi::apply_pcfr_settings() {
  if (!solver_) return;
  solver_->predictive = predictive_;
  solver_->avg_delay = avg_delay_;
  // Costs storage_floats * 4 bytes, committed immediately. ZeroAlloc::allocate does use
  // calloc, so the pages start out lazy, but vector::resize then value-initializes every
  // element (a memset for float), touching all of them before the solver runs. Measured on
  // flop_srp_2: peak RSS 155 MB with predictive off against 225 MB on, i.e. the whole array
  // up front. It is counted in memory_bytes_for(.., with_preds) so the build_tree check sees
  // it (#98); nothing is committed while the feature stays off, since the vector stays empty.
  if (predictive_ && tree_ && tree_->preds.size() < tree_->storage_floats)
    tree_->preds.resize(tree_->storage_floats);
}

std::string Upi::estimate_text() {
  if (cfg_.board.size() < 3) throw CmdError{"incorrect or missing argument"};
  GameTree t;
  t.set_payoff(payoff_);
  std::string err;
  if (!t.build(cfg_, err, false)) throw CmdError{err};
  uint64_t b = t.memory_bytes_for(false, predictive_), bs = t.memory_bytes_for(true, predictive_);
  // Pio prints both algorithms; here v4_large = float32 storage, v4_small = 16-bit (small_strats).
  return "estimated tree size (v4_large): " + std::to_string(b >> 20) + " MB\nestimated tree size (v4_small): " +
         std::to_string(bs >> 20) + " MB";
}

std::string Upi::cmd_bench() {
  stop_solver();
  GameConfig c;
  parse_cards("Qs8d7s", c.board);
  std::string err;
  RangeWeights w;
  parse_range("22+,A2s+,K2s+,Q2s+,J4s+,T6s+,96s+,85s+,75s+,64s+,54s,A2o+,K5o+,Q8o+,J8o+,T8o+,98o", w, err);
  for (int i = 0; i < NUM_COMBOS; ++i) c.range[0][i] = w[i];
  parse_range("22+,A2s+,K2s+,Q4s+,J6s+,T6s+,96s+,85s+,75s+,64s+,54s,A2o+,K8o+,Q9o+,J9o+,T9o", w, err);
  for (int i = 0; i < NUM_COMBOS; ++i) c.range[1][i] = w[i];
  c.pot[2] = 60;
  c.eff_stack = 970;
  c.iso_flop = false;
  c.lines.add = {{0, 20, 20, 60, 60, 160, 160, 970}, {20, 60, 60, 160, 160, 970}, {0, 40, 40, 120, 120, 970},
                 {0, 0, 40, 40, 120, 120, 970}, {20, 20, 20, 60, 60, 970}, {0, 20, 60, 60, 60, 970},
                 {0, 0, 0, 40, 40, 970}, {20, 60, 160, 160, 160, 400, 970}, {0, 0, 0, 0, 40, 970}};
  GameTree t;
  if (!t.build(c, err)) throw CmdError{err};
  Solver s(t);
  auto t0 = std::chrono::steady_clock::now();
  for (int i = 1; i <= 6; ++i) s.iterate(i);
  double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  return "Time taken: " + fmt(secs) + "\nTree size: " + std::to_string(t.memory_bytes() >> 20) + " MB";
}

// ------------------------------------------------------------------------------------------------

void Upi::register_commands() {
  auto& H = handlers_;
  auto ok = [](const std::string& c) { return c + " ok!"; };

  // ---- General ----
  H["is_ready"] = [=](const Args&) { return ok("is_ready"); };
  H["set_end_string"] = [=, this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    end_string_ = join(a, 0);
    return ok("set_end_string");
  };
  H["reset_end_string"] = [=, this](const Args&) {
    end_string_.clear();
    return ok("reset_end_string");
  };
  H["show_version"] = [](const Args&) { return std::string(SOLVER_NAME) + " " + SOLVER_VERSION; };
  H["show_build_version"] = [](const Args&) { return std::string(SOLVER_VERSION) + " (" + __DATE__ + ")"; };
  H["show_settings"] = [this](const Args&) {
    char buf[1024];
    std::snprintf(buf, sizeof(buf),
                  "accuracy: %f\naccuracy mode: %s\nthread_no: %d\ninfo_freq: %d\nstep: %f\nhopeless_thres: 0.200000\n"
                  "adjust_mode: 1\nrecalc_accuracy: %f %f %f\nisomorphism: %d %d\nignore mememory check: %d\n"
                  "algorithm:%s\ncompression_level (1-21): %d\ncompression_precision (6-16): %d",
                  accuracy_, accuracy_fraction_ ? "fraction" : "chips", threads_, info_freq_, 1.0, recalc_accuracy_[0],
                  recalc_accuracy_[1], recalc_accuracy_[2], cfg_.iso_flop ? 1 : 0, cfg_.iso_turn ? 1 : 0,
                  ignore_mem_check_ ? 1 : 0, algorithm_.c_str(), compression_level_, compression_precision_);
    return std::string(buf);
  };
  H["bench"] = [this](const Args&) { return cmd_bench(); };
  auto load_script = [this](const Args& a, bool silent) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::ifstream f(a[0]);
    if (!f) throw CmdError{"couldn't open " + a[0] + ": " + std::strerror(errno)};
    std::vector<std::string> lines;
    std::string l;
    while (std::getline(f, l)) lines.push_back(l);
    script_lines_.push_front("\x02");  // answers for the load_script once its lines have run
    for (auto it = lines.rbegin(); it != lines.rend(); ++it) script_lines_.push_front(*it);
    script_silent_.push_back(silent);
    return std::string("\x01");
  };
  H["load_script"] = [load_script](const Args& a) { return load_script(a, false); };
  H["load_script_silent"] = [load_script](const Args& a) { return load_script(a, true); };
  H["skip_if_done"] = [this](const Args& a) {
    // In a script: if the file exists, skip to the LABEL: line, which then runs (an empty answer).
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    if (std::ifstream(a[0]).good()) {
      while (!script_lines_.empty()) {
        const std::string& next = script_lines_.front();
        if (!next.empty() && next[0] == '\x02') break;  // the end of this script
        auto toks = tokenize(next);
        if (toks.size() >= 2 && toks[0] == "LABEL:" && toks[1] == a[1]) break;
        script_lines_.pop_front();
      }
    }
    return std::string("skip_if_done ok!");
  };
  H["echo"] = [](const Args& a) {  // Pio echoes the first argument; with none it is an error
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    return a[0];
  };
  H["repeat"] = [](const Args& a) {
    long long n = arg_int(a, 0, "count");
    std::string text = join(a, 1), s;
    for (long long i = 0; i < n; ++i) s += text + (i + 1 < n ? "\n" : "");
    return s;
  };
  H["show_hand_order"] = [](const Args&) {
    std::string s;
    for (int i = 0; i < NUM_COMBOS; ++i) s += combo_str(i) + " ";  // trailing space, like Pio
    return s;
  };
  H["show_preflop_order"] = [](const Args&) { return std::string(kPreflopOrder); };
  H["show_memory"] = [](const Args&) {
    return "Total Physical Memory: " + std::to_string(total_memory_bytes() >> 20) + " MB\nAvailable Phys Memory: " +
           std::to_string(available_memory_bytes() >> 20) + " MB";
  };
  H["show_cpu_info"] = [](const Args&) {
    int n = static_cast<int>(std::thread::hardware_concurrency());
    return "number of CPUs: " + std::to_string(n) + "\nmax threads: " + std::to_string(pool().threads()) +
           "\nthread limit is: 2147483647\naffinity policy is: 0";
  };
  H["stdoutredi"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::FILE* f = std::fopen(a[0].c_str(), "w");
    if (!f) throw CmdError{"cannot open " + a[0]};
    std::lock_guard<std::mutex> lk(out_mu_);
    if (redirected_) std::fclose(out_);
    out_ = f;
    redirected_ = true;
    return std::string("stdoutredi ok!");  // after the switch: into the file, as Pio
  };
  H["stdoutredi_append"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::FILE* f = std::fopen(a[0].c_str(), "a");
    if (!f) throw CmdError{"cannot open " + a[0]};
    std::lock_guard<std::mutex> lk(out_mu_);
    if (redirected_) std::fclose(out_);
    out_ = f;
    redirected_ = true;
    return std::string("stdoutredi_append ok!");  // after the switch: into the file, as Pio
  };
  H["stdoutback"] = [this](const Args&) {
    {
      std::lock_guard<std::mutex> lk(out_mu_);
      if (redirected_) std::fclose(out_);
      out_ = stdout;
      redirected_ = false;
    }
    return std::string("stdoutback ok!");
  };

  // ---- Solving ----
  H["go"] = [this](const Args& a) {
    require_tree();
    // Pio refuses a go while a solve runs, before looking at the arguments (probe 19); it used to be ignored here.
    // A solve parked by `hold` (ours) still resumes on go, as before.
    if (solving_ && !held_) throw CmdError{"Can't do that when solver is running"};
    const bool was_held = held_;
    release_hold();
    double secs = 0;
    long long steps = 0;
    if (!a.empty()) {
      // Pio 3.10 (probed): the unit is required; an amount below 1 step or at most 0 seconds still
      // runs one iteration (0 here would otherwise mean "no limit" and never stop).
      double v = arg_double(a, 0, "amount");
      std::string unit = a.size() > 1 ? a[1] : "";
      if (unit == "steps" || unit == "step") steps = std::max<long long>(1, static_cast<long long>(v));
      else if (unit == "seconds" || unit == "second" || unit == "s") secs = v > 0 ? v : 1e-9;
      else throw CmdError{"incorrect or missing argument"};
    }
    if (was_held && solving_) return std::string("go ok!");
    prepare_solve();  // what can fail, before the answer: one response per command (#376)
    out_raw(std::string("go ok!\n") + (end_string_.empty() ? "" : end_string_ + "\n"));
    try {
      start_solver(secs, steps);
    } catch (const std::exception& e) {  // answered already: report it as the solver would
      out_raw(std::string("SOLVER: stopped (error: ") + e.what() + ")\n");
    } catch (const CmdError& e) {
      out_raw("SOLVER: stopped (error: " + e.msg + ")\n");
    }
    return std::string("\x01");
  };
  // Ours (#153): continue solving a subtree asynchronously, exactly like `go` but iterating
  // below a node (Solver::iterate_subtree, the solve_partial path) with a fresh DCFR clock.
  // Progress blocks are marked with a "partial: <node>" first line; `stop` works as usual;
  // the accuracy target refers to the node's pot when given as a fraction. Pio has no such
  // command; the viewer's "Solve further" button is the client.
  H["go_partial"] = [this](const Args& a) {
    require_tree();
    if (a.empty() || a.size() > 3) throw CmdError{"incorrect or missing argument"};
    double secs = 0;
    long long steps = 0;
    if (a.size() > 1) {
      double v = arg_double(a, 1, "amount");
      std::string unit = a.size() > 2 ? a[2] : "seconds";
      if (unit == "steps" || unit == "step") steps = static_cast<long long>(v);
      else if (unit == "seconds" || unit == "second" || unit == "s") secs = v;
      else throw CmdError{"incorrect or missing argument"};
    }
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) r = resolve("r:0");
    if (solving_) return std::string("go_partial ok!");
    prepare_solve();  // (#376)
    out_raw(std::string("go_partial ok!\n") + (end_string_.empty() ? "" : end_string_ + "\n"));
    try {
      start_subtree_solve(r, secs, steps);
    } catch (const std::exception& e) {
      out_raw(std::string("SOLVER: stopped (error: ") + e.what() + ")\n");
    } catch (const CmdError& e) {
      out_raw("SOLVER: stopped (error: " + e.msg + ")\n");
    }
    return std::string("\x01");
  };
  H["stop"] = [this](const Args&) {
    stop_solver();
    return std::string("stop ok!");
  };
  // hold / release (an extension, for the viewer): keep a running solve parked between iterations
  // across a burst of commands. Each command otherwise waits for an iteration boundary of its own,
  // and the solve resumes between commands, so a dozen reads cost a dozen iterations (seconds on a
  // big tree). hold returns once the solver is between iterations, or at once if it is not
  // running. A parked solve resumes exactly where it was, so results are unaffected.
  H["hold"] = [this](const Args&) {
    if (!held_) {
      held_ = true;
      pause_requests_.fetch_add(1);
    }
    std::lock_guard<std::mutex> lk(tree_mu_);  // iterations run under tree_mu_
    return std::string("hold ok!");
  };
  H["release"] = [this](const Args&) {
    release_hold();
    return std::string("release ok!");
  };
  H["wait_for_solver"] = [this](const Args&) {
    release_hold();  // waiting on a parked solve would never return
    if (solver_thread_.joinable()) solver_thread_.join();
    return std::string("wait_for_solver ok!");
  };
  H["take_a_break"] = [this](const Args& a) {
    long long n = arg_int(a, 0, "seconds");
    stop_solver();
    std::this_thread::sleep_for(std::chrono::seconds(n));
    return std::string("take_a_break ok!");
  };
  H["solver_time"] = [this](const Args&) {  // seconds since this process started, like Pio
    return pfmt("%f", std::chrono::duration<double>(std::chrono::steady_clock::now() - start_time_).count());
  };
  H["set_threads"] = [this](const Args& a) {
    long long n = arg_int(a, 0, "threads");
    stop_solver();
    threads_ = static_cast<int>(n);
    pool().set_threads(threads_);
    return std::string("set_threads ok!");
  };
  H["set_info_freq"] = [this](const Args& a) {
    if (!a.empty() && a[0] == "auto") {  // extension: adaptive reporting (AutoReportSchedule)
      info_auto_ = true;
      return std::string("set_info_freq ok!");
    }
    const long long f = arg_int(a, 0, "frequency");  // parse first: a bad argument changes nothing
    info_auto_ = false;
    info_freq_ = static_cast<int>(std::max<long long>(0, f));
    return std::string("set_info_freq ok!");
  };
  H["set_accuracy"] = [this](const Args& a) {
    const double acc = arg_double(a, 0, "accuracy");  // parse first: a bad argument changes nothing
    if (a.size() > 1 && a[1] != "fraction" && a[1] != "chips") throw CmdError{"incorrect or missing argument"};
    accuracy_ = acc;
    accuracy_fraction_ = a.size() > 1 && a[1] == "fraction";
    return std::string("set_accuracy ok!");
  };
  H["set_recalc_accuracy"] = [this](const Args& a) {
    for (int i = 0; i < 3; ++i) recalc_accuracy_[i] = arg_double(a, i, "accuracy");
    return std::string("set_recalc_accuracy ok!");
  };
  H["set_always_recalc"] = [this](const Args& a) {
    // Pio 3.10.1 needs both the street and the time limit (probe 25: "set_always_recalc 2" is refused).
    const int street = static_cast<int>(arg_int(a, 0, "street"));
    const int ms = static_cast<int>(arg_int(a, 1, "timeout"));
    always_recalc_street_ = street;
    always_recalc_ms_ = ms;
    return std::string("set_always_recalc ok!");
  };
  H["set_isomorphism"] = [this](const Args& a) {
    cfg_.iso_flop = arg_int(a, 0, "flop") != 0;
    cfg_.iso_turn = a.size() > 1 ? arg_int(a, 1, "turn") != 0 : false;
    return std::string("set_isomorphism ok!");
  };
  // Not a Pio command. Exposes the DCFR discount exponents so the benchmark suite can
  // sweep them (#52) without a rebuild per point: positive regrets are scaled by
  // t^alpha/(t^alpha+1), negative by t^beta/(t^beta+1), the strategy sum by
  // (t/(t+1))^gamma. beta very negative underflows to 0, i.e. CFR+'s "discard negative
  // regrets"; alpha must stay small, since a large exponent overflows t^alpha to inf and
  // inf/(inf+1) is NaN.
  // set_dcfr alpha beta gamma [beta_late switch]: three values give constant exponents (the
  // published DCFR form); five add the beta schedule (beta -> beta_late after iteration `switch`).
  H["set_dcfr"] = [this](const Args& a) {
    if (a.size() != 3 && a.size() != 5) throw CmdError{"incorrect or missing argument"};
    double al = arg_double(a, 0, "alpha"), be = arg_double(a, 1, "beta"), ga = arg_double(a, 2, "gamma");
    if (!(al > 0.0) || al > 10.0 || be > 10.0 || !(ga >= 0.0) || ga > 10.0)
      throw CmdError{"incorrect or missing argument"};
    DcfrParams d;
    d.alpha = al;
    d.beta = be;
    d.gamma = ga;
    d.beta_switch = 0;
    if (a.size() == 5) {
      d.beta_late = arg_double(a, 3, "beta_late");
      long long sw = arg_int(a, 4, "switch");
      if (d.beta_late > 10.0 || sw < 0 || sw > 1000000000) throw CmdError{"incorrect or missing argument"};
      d.beta_switch = static_cast<int>(sw);
    }
    dcfr_ = d;
    if (solver_) solver_->params = dcfr_;
    return std::string("set_dcfr ok!");
  };
  // Not Pio commands: convergence experiments (#71), off / 0 by default.
  // set_predictive on: PCFR+-style optimistic matching — the strategy at each node is
  // matched on R + the last instantaneous regret. Costs one extra float per regret slot
  // (lazily allocated, transient, never saved; harmless to leave allocated after "off").
  H["set_predictive"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    if (solving_) throw CmdError{"stop the solver first"};
    predictive_ = a[0] == "on";
    apply_pcfr_settings();
    return std::string("set_predictive ok!");
  };
  // set_avg_delay <n>: the strategy average drops iterations 1..n. It accumulates from t=1 and is
  // reset at iteration n+1 (#135), so a solve stopped earlier still has its full average. Default
  // 32; 0 = keep every iteration. Partial (subtree) re-solves ignore it.
  H["set_avg_delay"] = [this](const Args& a) {
    long long n = arg_int(a, 0, "iterations");
    if (n < 0 || n > 1000000) throw CmdError{"incorrect or missing argument"};
    avg_delay_ = static_cast<int>(n);
    if (solver_) solver_->avg_delay = avg_delay_;
    return std::string("set_avg_delay ok!");
  };
  // show_avg_delay (extension): the current set_avg_delay. The viewer's fast preview compares
  // strategy averages across iterations and must not compare across the reset at n + 1.
  H["show_avg_delay"] = [this](const Args&) { return std::to_string(avg_delay_); };
  // Not a Pio command. Regret-based pruning (#51, see Solver::pruning): from iteration
  // 2*<period>+1 on, an action of the traverser whose current strategy is zero for every one of
  // its hands is not re-measured that iteration; its regret row is frozen (not discounted or
  // updated) until the next full iteration. Every <period>-th iteration (default 8) is measured
  // in full. The strategy and the strategy-sum rows are untouched, so this is a
  // convergence-speed knob, not an abstraction.
  H["set_pruning"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    const long long period = a.size() > 1 ? arg_int(a, 1, "period") : prune_period_;  // parse first:
    // below 2 no iteration is measured in full, so a pruned action could never come back (#380)
    if (period < 2 || period > 1000000000) throw CmdError{"incorrect or missing argument"};  // an error changes nothing
    pruning_ = a[0] == "on";
    prune_period_ = static_cast<int>(period);
    if (solver_) {
      solver_->pruning = pruning_;
      solver_->prune_period = prune_period_;
    }
    return std::string("set_pruning ok!");
  };
  // Not a Pio command: Pio always tracks the union. Opt-in here because the wider vectors
  // cost time and memory, and it has to be set before build_tree to take effect.
  H["set_union_hands"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    cfg_.union_hands = a[0] == "on";
    return std::string("set_union_hands ok!");
  };
  // Not a Pio command (#484). A tree that stops betting before the river (a preflop tree with flop-only
  // postflop play, say) is mostly check-downs: with this on each is one showdown node on the runouts
  // instead of one-action nodes for every turn and river card. Set before building.
  H["set_collapse_checkdowns"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    cfg_.collapse_checkdowns = pf_cfg_.collapse_checkdowns = a[0] == "on";
    return std::string("set_collapse_checkdowns ok!");
  };
  H["change_step"] = [this](const Args& a) {
    step_ = arg_double(a, 0, "step");
    return std::string("change_step ok!");
  };
  H["show_step"] = [this](const Args&) { return pfmt("%f", step_); };
  H["show_iters"] = [this](const Args&) { return std::to_string(tree_ ? tree_->iterations.load() : 0LL); };
  H["set_first_iteration_player"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "OOP" && a[0] != "IP" && a[0] != "oop" && a[0] != "ip"))
      throw CmdError{"incorrect or missing argument"};  // Pio: not "incorrect player" here
    int p = player_arg(a[0]);
    first_player_ = p;
    if (solver_) solver_->first_player = p;
    return std::string("set_first_iteration_player ok!");
  };

  // ---- Tree building ----
  H["set_range"] = [this](const Args& a) {
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    int p = player_arg(a[0]);
    RangeWeights w;
    std::string err;
    if (!parse_range(join(a, 1), w, err)) throw CmdError{err};
    for (int i = 0; i < NUM_COMBOS; ++i) cfg_.range[p][i] = w[i];
    return std::string("set_range ok!");
  };
  // Chip amounts are whole numbers parsed like C's atoi, as in Pio 3.10 (probed): "61.7" is 61,
  // "1e1" is 1. set_eff_stack and set_pot take anything; add_line and friends need a leading digit.
  H["set_eff_stack"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    // Pio 3.10.1 (probe 19): 0..65535 (fractions truncate); a negative, larger or non-numeric value is this
    // error and leaves the stack as it was.
    const std::string& s = a[0];
    const size_t d = s[0] == '+' ? 1 : 0;
    const bool numeric = d < s.size() && std::isdigit(static_cast<unsigned char>(s[d]));
    const long long v = std::atoll(s.c_str());
    if (!numeric || v < 0 || v > 65535)
      throw CmdError{"Both stack sizes and the starting pot should be below 65535 (consider dividing stacks/pot by 10 or 100)"};
    cfg_.eff_stack = static_cast<int32_t>(v);
    return std::string("set_eff_stack ok!");
  };
  H["set_board"] = [this](const Args& a) {
    // Pio clears the board on a bad set_board (show_tree_params then says "board: not set").
    std::vector<int> b;
    if (a.size() != 1 || !parse_cards(a[0], b) || b.size() < 3 || b.size() > 5) {
      cfg_.board.clear();
      throw CmdError{"incorrect or missing argument"};
    }
    cfg_.board = b;
    return std::string("set_board ok!");
  };
  H["set_pot"] = [this](const Args& a) {
    // Pio: only the given values change; fewer than two is an error, but they are still applied. Each value
    // must be 0..65535: the first one outside stops it with this error, the earlier ones applied (probe 19).
    for (size_t i = 0; i < 3 && i < a.size(); ++i) {
      const int32_t v = chips_arg(a[i]);
      if (v < 0 || v > 65535) throw CmdError{"incorrect or missing argument"};
      cfg_.pot[i] = v;
    }
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    return std::string("set_pot ok!");
  };
  auto parse_line = [](const Args& a) {
    std::vector<int32_t> l;
    for (const auto& s : a) {
      // Pio truncates fractional amounts ("30.7" builds a 30 bet). A token with no leading digit
      // stays an error here: Pio accepts it but builds something arbitrary (probed: "abc" -> b200).
      size_t i = s[0] == '+' || s[0] == '-' ? 1 : 0;
      if (i >= s.size() || !std::isdigit(static_cast<unsigned char>(s[i]))) throw CmdError{"incorrect or missing argument"};
      l.push_back(chips_arg(s));
    }
    if (l.empty()) throw CmdError{"incorrect or missing argument"};
    return l;
  };
  // Pio edits a live line tree command by command; ours keeps add/remove sets that build_tree applies,
  // and makes each edit act like Pio's when it is given (verified on 3.10.1, AGENTS.md):
  // remove_line / force_line delete subtrees now (a removal, and the added lines through it go), and
  // add_line clears the removals on its own path, so a line re-added after either comes back - only
  // its own path, as in Pio. Lookups use the action tree the current lines build.
  auto current_tree = [this]() -> std::unique_ptr<ActionNode> {
    if (cfg_.board.size() < 3) return nullptr;
    ActionTreeParams ap;
    ap.board_len = static_cast<int>(cfg_.board.size());
    ap.eff_stack = cfg_.eff_stack;
    ap.start_inv[0] = cfg_.pot[0];
    ap.start_inv[1] = cfg_.pot[1];
    ap.dead = cfg_.pot[2];
    std::string err;
    return build_action_tree(ap, cfg_.lines, err);
  };
  // The node a line leads to (through street deals), or nullptr; *parent = the last decision on it.
  auto walk_line = [](ActionNode* n, const std::vector<int32_t>& l, ActionNode** parent) -> ActionNode* {
    for (int32_t x : l) {
      while (n && n->kind == ANodeKind::Chance) n = n->children.empty() ? nullptr : n->children[0].get();
      if (!n || n->kind != ANodeKind::Decision) return nullptr;
      size_t i = 0;
      while (i < n->actions.size() && n->actions[i].total != x) ++i;
      if (i == n->actions.size()) return nullptr;
      *parent = n;
      n = n->children[i].get();
    }
    return n;
  };
  auto is_prefix = [](const std::vector<int32_t>& p, const std::vector<int32_t>& l) {
    return p.size() <= l.size() && std::equal(p.begin(), p.end(), l.begin());
  };
  // With no buildable tree: is the line on an added line? Its last step may also be an implicit
  // action the adds don't list - a check or fold (0) or a call (the amount before it).
  auto on_added_line = [this, is_prefix](const std::vector<int32_t>& l) {
    bool implicit = !l.empty() && (l.back() == 0 || (l.size() >= 2 && l.back() == l[l.size() - 2]));
    std::vector<int32_t> head(l.begin(), l.end() - (l.empty() ? 0 : 1));
    return std::any_of(cfg_.lines.add.begin(), cfg_.lines.add.end(), [&](const std::vector<int32_t>& x) {
      return is_prefix(l, x) || (implicit && is_prefix(head, x));
    });
  };
  auto cut_line = [this, is_prefix](const std::vector<int32_t>& l) {
    auto& add = cfg_.lines.add;
    add.erase(std::remove_if(add.begin(), add.end(), [&](const std::vector<int32_t>& x) { return is_prefix(l, x); }),
              add.end());
    cfg_.lines.remove.push_back(l);
  };
  H["add_line"] = [this, parse_line, is_prefix](const Args& a) {
    auto line = parse_line(a);
    // a line given again after remove_line / force_line took it out comes back (Pio re-adds it)
    auto& rm = cfg_.lines.remove;
    rm.erase(std::remove_if(rm.begin(), rm.end(), [&](const std::vector<int32_t>& r) { return is_prefix(r, line); }),
             rm.end());
    cfg_.lines.add.push_back(line);  // amounts are validated at build_tree
    // Like Pio, the line is checked against the current config here but kept either way:
    // - running past the end of the hand: "too long", and build_tree uses the valid prefix;
    // - an amount below the bet faced or below one's own investment (other than a fold): Pio's
    //   "Incorrect bet size." (Pio's build_tree then crashes; ours reports the amount).
    // Pio checks neither the stack nor an all-in root here: with no stack set (or one the
    // start investment already covers) every line it can classify is accepted, so we drop
    // the stack for the check in that case.
    if (cfg_.board.size() >= 3) {
      ActionTreeParams ap;
      ap.board_len = static_cast<int>(cfg_.board.size());
      ap.start_inv[0] = cfg_.pot[0];
      ap.start_inv[1] = cfg_.pot[1];
      ap.eff_stack = cfg_.eff_stack > std::max(cfg_.pot[0], cfg_.pot[1])
                         ? cfg_.eff_stack
                         : std::numeric_limits<int32_t>::max();
      std::string err;
      if (!validate_line(ap, cfg_.lines.add.back(), err)) {
        if (err.rfind("Specified line is too long", 0) == 0) throw CmdError{err};
        if (err.find(" is between current investment") != std::string::npos ||
            err.find(" is below current investment") != std::string::npos)
          throw CmdError{"Incorrect bet size."};
      }
    }
    return std::string("add_line ok!");
  };
  H["remove_line"] = [parse_line, current_tree, walk_line, cut_line, on_added_line](const Args& a) {
    // Pio: the line must be in the tree and not its parent's last child, else this error and no
    // change; removing deletes its subtree, so re-adding the line brings back only its own path.
    // With no buildable tree (no board yet, or a bad line pending) it must at least be on an added
    // line, as force_line checks there.
    auto l = parse_line(a);
    const char* err = "Can't remove the line (it doesn't exist or is the last child)";
    if (auto root = current_tree()) {
      ActionNode* parent = nullptr;
      if (!walk_line(root.get(), l, &parent) || !parent || parent->actions.size() < 2) throw CmdError{err};
    } else if (!on_added_line(l)) {
      throw CmdError{err};
    }
    cut_line(l);
    return std::string("remove_line ok!");
  };
  H["force_line"] = [this, parse_line, current_tree, cut_line, on_added_line](const Args& a) {
    // Pio keeps only the tree's lines that the forced one is a prefix of: every other action along
    // its path goes, now (an add_line after it is added beside it again). The line must be a line of
    // the tree as it stands - an implicit call or fold counts (force_line 0 50 50 after add_line
    // 0 50), one an earlier force removed does not - else "Specified line doesn't exists." and no
    // change. Verified on 3.10.1 (probe 11, check_force_line). Pio still applies part of a rejected
    // force; ours does not (#10: not a behaviour to copy).
    auto f = parse_line(a);
    auto root = current_tree();
    if (!root) {
      // no buildable tree yet (no board or stack): keep it for build_tree, which prunes the same way
      if (!on_added_line(f)) throw CmdError{"Specified line doesn't exists."};
      cfg_.lines.force.push_back(f);
      return std::string("force_line ok!");
    }
    std::vector<std::vector<int32_t>> cut;
    std::vector<int32_t> pre;
    ActionNode* n = root.get();
    for (int32_t x : f) {
      while (n && n->kind == ANodeKind::Chance) n = n->children.empty() ? nullptr : n->children[0].get();
      if (!n || n->kind != ANodeKind::Decision) throw CmdError{"Specified line doesn't exists."};
      size_t i = 0;
      while (i < n->actions.size() && n->actions[i].total != x) ++i;
      if (i == n->actions.size()) throw CmdError{"Specified line doesn't exists."};
      for (size_t j = 0; j < n->actions.size(); ++j)
        if (j != i) {
          cut.push_back(pre);
          cut.back().push_back(n->actions[j].total);
        }
      pre.push_back(x);
      n = n->children[i].get();
    }
    for (auto& c : cut) cut_line(c);
    return std::string("force_line ok!");
  };
  H["clear_lines"] = [this](const Args&) {
    cfg_.lines.clear();
    return std::string("clear_lines ok!");
  };
  H["build_tree"] = [this](const Args& a) { return cmd_build_tree(a); };
  H["list_algorithms"] = [](const Args&) {
    return std::string(
        "v4_small,DCFR,Discounted CFR (alpha 1.3, beta 0 then 0.5 after iteration 50, gamma 4); this solver uses one algorithm for every code\n"
        "v4_large,DCFR,Discounted CFR (alpha 1.3, beta 0 then 0.5 after iteration 50, gamma 4)\n"
        "pio_cfr,DCFR,Discounted CFR (alpha 1.3, beta 0 then 0.5 after iteration 50, gamma 4)\n"
        "original_pio,DCFR,Discounted CFR (alpha 1.3, beta 0 then 0.5 after iteration 50, gamma 4)");
  };
  H["set_algorithm"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    static const char* valid[] = {"auto", "v4_small", "v4_large", "pio_cfr", "original_pio"};
    if (std::find_if(std::begin(valid), std::end(valid), [&](const char* v) { return a[0] == v; }) == std::end(valid))
      throw CmdError{"incorrect or missing argument"};
    algorithm_ = a[0];
    return std::string("set_algorithm ok!");
  };
  H["ignore_mem_check"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    ignore_mem_check_ = a[0] == "on";
    return std::string("ignore_mem_check ok!");
  };
  H["small_strats"] = [this](const Args& a) {
    // compact (extension, #239): 16-bit storage with the board-blocked hands of river rows left out
    if (a.empty() || (a[0] != "on" && a[0] != "off" && a[0] != "auto" && a[0] != "compact"))
      throw CmdError{"incorrect or missing argument"};
    small_strats_ = a[0];
    return std::string("small_strats ok!");
  };
  // Not a Pio command (#496): the outcome tables of a preflop tree's runout sets. auto: where the tree
  // with them fits in memory; small: the smallest tree, as estimate_tree counts it; fast: everywhere.
  H["set_outcome_tables"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "auto" && a[0] != "small" && a[0] != "fast")) throw CmdError{"incorrect or missing argument"};
    outcome_tables_ = a[0];
    return std::string("set_outcome_tables ok!");
  };
  H["estimate_schematic_tree"] = [this](const Args&) { return estimate_text(); };
  H["estimate_tree"] = [this](const Args&) {
    // Preflop tree with the current subset and schematic trees (or the pure preflop tree). Without a
    // preflop config, Pio 3.10.1 (probe 26) needs a built tree and answers one line for it; the postflop
    // config before build_tree is estimate_schematic_tree's job.
    if (!have_pf_) {
      require_tree();
      return "estimated tree size: " + std::to_string(tree_->memory_bytes() >> 20) + " MB";  // as built
    }
    GameConfig c = pf_cfg_;
    c.schematics = schematics_;
    c.subset = subset_;
    if (c.subset.empty()) c.schematics.clear();
    // The smallest tree (what it needs: set_outcome_tables auto adds tables only to a tree with memory
    // to spare), or the one with tables everywhere when that is asked for.
    GameTree t;
    t.allin_plan = outcome_tables_ == "fast" ? 3 : -1;
    std::string err;
    if (!t.build(c, err, false)) throw CmdError{err};
    return "estimated tree size: " + std::to_string(t.memory_bytes_for(true, predictive_) >> 20) + " MB";
  };
  register_preflop_commands();

  // ---- Tree info ----
  H["add_info_line"] = [this](const Args& a) {
    tree_info_.push_back(a.empty() ? std::string() : a[0]);  // Pio keeps the first argument only
    return std::string("add_info_line ok!");
  };
  H["reset_tree_info"] = [this](const Args&) {
    tree_info_.clear();
    return std::string("reset_tree_info ok!");
  };
  // Pio 3.10 still answers these legacy commands. The sizes and the donk flag are only stored and
  // echoed by show_tree_params; build_tree ignores them in Pio as well (it needs add_line).
  H["set_bet_sizes"] = [this](const Args& a) {
    legacy_bet_sizes_.clear();
    for (const auto& s : a) legacy_bet_sizes_.push_back(std::atoll(s.c_str()));  // atoi: "33.5,75" is 33
    return std::string("set_bet_sizes ok!");
  };
  H["set_donk_bet"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    legacy_donk_bet_ = std::atoll(a[0].c_str()) != 0;  // "true" and "0.5" are false, "-1" is true
    return std::string("set_donk_bet ok!");
  };
  H["show_tree_params"] = [this](const Args&) {
    std::string s = "board: ";
    if (cfg_.board.empty()) s += "not set";
    for (int c : cfg_.board) s += card_str(c) + " ";
    s += "\npot: ";
    for (int i = 0; i < 3; ++i) s += std::to_string(cfg_.pot[i]) + " ";
    s += "\nbet_sizes: ";
    for (long long v : legacy_bet_sizes_) s += std::to_string(v) + " ";
    s += std::string("\ndonk_bet: ") + (legacy_donk_bet_ ? "TRUE" : "FALSE");
    return s;
  };
  H["show_tree_info"] = [this](const Args&) {
    std::string s;
    for (size_t i = 0; i < tree_info_.size(); ++i) s += tree_info_[i] + (i + 1 < tree_info_.size() ? "\n" : "");
    return s;
  };

  // ---- Alternative EV models ----
  H["set_rake"] = [this](const Args& a) {
    require_tree();  // Pio checks the tree first, even before the arguments
    payoff_.rake_frac = arg_double(a, 0, "fraction");
    payoff_.rake_cap = arg_double(a, 1, "cap");
    // Rake and ICM are exclusive; like Pio, the last one set wins (setting rake turns ICM off).
    if (payoff_.rake_frac > 0) payoff_.icm.enabled = false;
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
      tree_->set_payoff(payoff_);
      discount_restart_ = tree_->iterations;
    }
    return std::string("set_rake ok!");
  };
  H["set_icm"] = [this](const Args& a) {
    require_tree();
    const int32_t stacks[2] = {static_cast<int32_t>(arg_int(a, 0, "oop stack")),
                               static_cast<int32_t>(arg_int(a, 1, "ip stack"))};
    // Pio: without ICM points it reports an error line, still answers ok, and leaves ICM off.
    if (!icm_tables_defined_) {
      payoff_.icm.stacks[0] = stacks[0], payoff_.icm.stacks[1] = stacks[1];
      return std::string("ERROR: set_icm ICM points not defined.\nset_icm ok!");
    }
    // A preflop tree that values its flops on outcome tables has no showdown boards for them, and ICM
    // needs the boards (a tie is not the middle of a win and a loss): they are built now, and for many
    // flops they are several times the tree (GameTree::plan_allins).
    if (const uint64_t need = tree_->allin_boards_missing_bytes(); need && !ignore_mem_check_) {
      const uint64_t avail = available_memory_bytes();
      if (need > avail)
        throw CmdError{"not enough memory: ICM values this tree's all-ins on their showdown boards, which need " +
                       std::to_string(need >> 20) + " MB more, available " + std::to_string(avail >> 20) + " MB"};
    }
    payoff_.icm.stacks[0] = stacks[0], payoff_.icm.stacks[1] = stacks[1];
    payoff_.icm.enabled = true;
    payoff_.rake_frac = 0;  // the last of rake / ICM wins
    payoff_.rake_cap = 0;
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
      tree_->set_payoff(payoff_);
      discount_restart_ = tree_->iterations;
    }
    return std::string("set_icm ok!");
  };
  H["set_icm_point"] = [this](const Args& a) {
    if (a.size() < 3) throw CmdError{"incorrect or missing argument"};
    int p = player_arg(a[0]);
    double st = arg_double(a, 1, "stack"), v = arg_double(a, 2, "value");
    auto& pts = payoff_.icm.points[p];
    pts.erase(std::remove_if(pts.begin(), pts.end(), [&](auto& x) { return x.first == st; }), pts.end());
    pts.push_back({st, v});
    std::sort(pts.begin(), pts.end());
    icm_tables_defined_ = true;
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
      tree_->set_payoff(payoff_);
      discount_restart_ = tree_->iterations;
    }
    return std::string("set_icm_point ok!");
  };
  H["reset_icm_tables"] = [this](const Args&) {
    payoff_.icm = IcmModel{};  // rake (if any) is left as it is
    icm_tables_defined_ = true;  // Pio: reset tables are "defined" (empty table = chip values)
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
      tree_->set_payoff(payoff_);
      discount_restart_ = tree_->iterations;
    }
    return std::string("reset_icm_tables ok!");
  };

  // ---- Save / load ----
  H["free_tree"] = [this](const Args&) {
    stop_solver();
    solver_.reset();
    tree_.reset();
    return std::string("free_tree ok!");
  };
  H["is_tree_present"] = [this](const Args&) { return std::string(tree_ ? "true" : "false"); };
  H["dump_tree"] = [this](const Args& a) { return cmd_dump_tree(a); };
  H["load_tree"] = [this](const Args& a) { return cmd_load_tree(a); };
  H["show_metadata"] = [this](const Args& a) { return cmd_show_metadata(a); };
  H["load_all_nodes"] = [this](const Args&) {
    if (tree_) {
      stop_solver();
      std::string err;
      if (!tree_->load_all(err)) throw CmdError{"cannot read the tree's file: " + err};
      tree_->fast_loaded = false;
    }
    return std::string("load_all_nodes ok!");
  };
  H["set_compression_precision"] = [this](const Args& a) {
    long long v = arg_int(a, 0, "precision");
    if (v < 6 || v > 16) throw CmdError{"incorrect or missing argument"};
    compression_precision_ = static_cast<int>(v);
    return std::string("set_compression_precision ok!");
  };
  H["set_compression_level"] = [this](const Args& a) {
    long long v = arg_int(a, 0, "level");
    if (v < 1 || v > 21) throw CmdError{"incorrect or missing argument"};
    compression_level_ = static_cast<int>(v);
    return std::string("set_compression_level ok!");
  };
  H["clear_cache"] = [](const Args&) { return std::string("clear_cache ok!"); };
  H["deactivate"] = [](const Args&) { return std::string("Deactivation successful!"); };

  register_node_commands();
}

void Upi::register_preflop_commands() {
  auto& H = handlers_;
  auto parse_line = [](const Args& a) {
    std::vector<int32_t> l;
    for (auto& s : a) l.push_back(chips_arg(s));
    if (l.empty()) throw CmdError{"incorrect or missing argument"};
    return l;
  };
  H["add_preflop_line"] = [this, parse_line](const Args& a) {
    pf_lines_.add.push_back(parse_line(a));
    // Checked against the current blinds and stack like add_line, and kept either way: a line running past
    // a fold or an all-in call is Pio's "too long" (probe of 2026-11-07: "5 25 25" with blinds 10/5, the SB's
    // 5 being a fold), a bet below the one faced its "Incorrect bet size.".
    ActionTreeParams ap;
    ap.board_len = 0;
    ap.start_inv[0] = cfg_.pot[0];
    ap.start_inv[1] = cfg_.pot[1];
    ap.eff_stack = cfg_.eff_stack > std::max(cfg_.pot[0], cfg_.pot[1]) ? cfg_.eff_stack
                                                                        : std::numeric_limits<int32_t>::max();
    std::string err;
    if (!validate_line(ap, pf_lines_.add.back(), err)) {
      if (err.rfind("Specified line is too long", 0) == 0) throw CmdError{err};
      if (err.find(" is between current investment") != std::string::npos ||
          err.find(" is below current investment") != std::string::npos)
        throw CmdError{"Incorrect bet size."};
    }
    return std::string("add_preflop_line ok!");
  };
  H["remove_preflop_line"] = [this, parse_line](const Args& a) {
    pf_lines_.remove.push_back(parse_line(a));
    return std::string("remove_preflop_line ok!");
  };
  H["clear_preflop_lines"] = [this](const Args&) {
    pf_lines_.clear();
    return std::string("clear_preflop_lines ok!");
  };
  H["build_preflop_tree"] = [this](const Args&) {
    GameConfig c = cfg_;
    c.preflop = true;
    c.board.clear();
    c.lines = pf_lines_;
    c.schematics.clear();
    c.subset.clear();
    pf_cfg_ = c;
    have_pf_ = true;
    schematics_.clear();
    return build_from(c, ignore_mem_check_, false, "build_preflop_tree ok!");
  };
  H["add_to_subset"] = [this](const Args& a) {
    if (a.size() != 4) throw CmdError{"incorrect or missing argument"};
    char* end = nullptr;
    double w = std::strtod(a[0].c_str(), &end);
    if (!end || *end || !(w >= 0)) throw CmdError{"incorrect or missing argument"};
    SubsetFlop f;
    f.weight = static_cast<float>(w);
    uint64_t m = 0;
    for (int i = 0; i < 3; ++i) {
      f.cards[i] = parse_card(a[1 + i]);
      if (f.cards[i] < 0 || (m & card_bit(f.cards[i]))) throw CmdError{"incorrect or missing argument"};
      m |= card_bit(f.cards[i]);
    }
    subset_.push_back(f);
    return std::string("add_to_subset ok!");
  };
  H["reset_subset"] = [this](const Args&) {
    subset_.clear();
    return std::string("reset_subset ok!");
  };
  H["show_subset"] = [this](const Args&) {
    std::string s;
    for (auto& f : subset_) {
      s += pfmt("%f", f.weight) + " ";
      for (int c : f.cards) s += card_str(c) + " ";
      s += "\n";
    }
    return s + "\n" + std::to_string(subset_.size()) + " flops total";
  };
  H["recover_subset"] = [this](const Args&) {
    require_tree();
    if (tree_->cfg.preflop) subset_ = tree_->cfg.subset;
    return std::string("recover_subset ok!");
  };
  H["add_schematic_tree"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    // the lines are kept as written and read from the flop at add_all_flops: lines written for a turn
    // or river board described later streets, and silently became flop actions (#396; Pio accepts the
    // schematic, then crashes in add_all_flops)
    if (cfg_.board.size() > 3) throw CmdError{"a schematic is a flop tree: set a 3-card board"};
    // Pio's workflow builds each schematic first (set_board, set_pot, ..., build_tree, then
    // add_schematic_tree), so the tree in memory is then the postflop one. Resolve the exit in the
    // preflop tree from build_preflop_tree's config, built without storage, as Pio does (#306).
    std::unique_ptr<GameTree> pf;
    bool swapped = false;
    if (!tree_ || !tree_->cfg.preflop) {
      if (!have_pf_) throw CmdError{"incorrect path"};
      if (solving_) throw CmdError{"stop the solver first"};
      pf = std::make_unique<GameTree>();
      std::string err;
      if (!pf->build(pf_cfg_, err, false)) throw CmdError{"incorrect path"};
      std::swap(tree_, pf);
      swapped = true;
    }
    struct Restore {  // put the tree in memory back (or none), however this returns
      std::unique_ptr<GameTree>& cur;
      std::unique_ptr<GameTree>& saved;
      bool on;
      ~Restore() {
        if (on) std::swap(cur, saved);
      }
    } restore{tree_, pf, swapped};
    NodeRef r = resolve(a[0]);
    if (r.pseudo_root) throw CmdError{"incorrect path"};
    const ActionNode& an = tree_->an(tree_->nodes[r.node]);
    if (!an.preflop_exit) throw CmdError{"incorrect path"};
    Schematic s;
    s.exit_line = an.line;
    for (int i = 0; i < 3; ++i) s.pot[i] = cfg_.pot[i];
    s.eff_stack = cfg_.eff_stack;
    s.lines = cfg_.lines;
    schematics_.erase(std::remove_if(schematics_.begin(), schematics_.end(),
                                     [&](const Schematic& x) { return x.exit_line == s.exit_line; }),
                      schematics_.end());
    schematics_.push_back(s);
    return std::string("add_schematic_tree ok!");
  };
  H["add_all_flops"] = [this](const Args&) -> std::string {
    if (!have_pf_) throw CmdError{"no preflop tree", true};
    if (subset_.empty()) throw CmdError{"no flop subset available", true};
    // all weights 0: the flop deal divides by the total weight, so every flop came out worth nothing (#371)
    double wsum = 0;
    for (const auto& f : subset_) wsum += f.weight;
    if (!(wsum > 0)) throw CmdError{"the flop subset's weights are all 0", true};
    GameConfig c = pf_cfg_;
    c.schematics = schematics_;
    c.subset = subset_;
    return build_from(c, ignore_mem_check_, false, "add_all_flops ok!");
  };
}

}  // namespace pio
