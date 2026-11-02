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
#include <charconv>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
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

uint64_t available_memory_bytes() {
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
  stop_flag_ = true;
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

std::string Upi::fmt(double v) const {
  char buf[64];
  if (std::isnan(v)) return "nan";
  float f = static_cast<float>(v);
  auto r = std::to_chars(buf, buf + sizeof(buf), f);
  return std::string(buf, r.ptr);
}

std::string Upi::floats_line(const float* v, int n) const {
  std::string s;
  s.reserve(static_cast<size_t>(n) * 8);
  char buf[32];
  for (int i = 0; i < n; ++i) {
    if (i) s += ' ';
    float f = v[i];
    if (std::isnan(f)) {
      s += "nan";
      continue;
    }
    auto r = std::to_chars(buf, buf + sizeof(buf), f);
    s.append(buf, r.ptr);
  }
  return s;
}

int Upi::player_arg(const std::string& s) const {
  std::string u = s;
  for (auto& ch : u) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
  if (u == "OOP") return 0;
  if (u == "IP") return 1;
  throw CmdError{"incorrect or missing argument"};
}

void Upi::require_tree() const {
  if (!tree_) throw CmdError{"missing/incorrect tree"};
}

bool Upi::next_line(std::string& line) {
  if (!script_lines_.empty()) {
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
  stop_flag_ = true;
  if (solver_thread_.joinable()) solver_thread_.join();
  return 0;
}

bool Upi::execute_line(const std::string& raw, bool silent) {
  std::string line = raw;
  while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
  size_t first = line.find_first_not_of(" \t");
  if (first == std::string::npos) return true;
  if (line[first] == '#') return true;
  auto toks = tokenize(line);
  if (toks.empty()) return true;
  std::string cmd = toks[0];
  Args args(toks.begin() + 1, toks.end());
  if (cmd == "LABEL:") return true;
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
  if (is_exit) return false;
  if (!silent && body != "\x01") respond(body);
  return true;
}

std::string Upi::dispatch(const std::string& cmd, const Args& args, bool& is_exit) {
  if (cmd == "exit" || cmd == "quit") {
    stop_solver();
    is_exit = true;
    return "";
  }
  auto it = handlers_.find(cmd);
  if (it == handlers_.end()) throw CmdError{"Command " + cmd + " not recognized", true};
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

void Upi::start_solver(double seconds, long long steps) {
  require_tree();
  if (solving_) return;
  if (solver_thread_.joinable()) solver_thread_.join();
  stop_flag_ = false;
  solving_ = true;
  solver_thread_ = std::thread([this, seconds, steps] { solver_thread_main(seconds, steps); });
}

void Upi::solver_thread_main(double seconds, long long steps) {
  out_raw("SOLVER: started\n");
  auto t0 = std::chrono::steady_clock::now();
  double base = solve_seconds_;
  long long done = 0;
  std::string reason = "requested";
  const std::string endl_str = end_string_.empty() ? "END" : end_string_;
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
      ++tree_->iterations;
      ++iterations_;
      // DCFR discount clock; restarted when the payoff model changes (warm start, fast adaptation).
      solver_->iterate(static_cast<int>(tree_->iterations - discount_restart_));
      ++done;
      double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
      solve_seconds_ = base + el;
      bool limit = (steps > 0 && done >= steps) || (seconds > 0 && el >= seconds);
      if (info_freq_ > 0 && (tree_->iterations % info_freq_ == 0)) report = true;
      if (limit) report = true;
      if (report) {
        st = solver_->root_stats();
        out_raw("SOLVER:\n" + stats_block(st, solve_seconds_) + endl_str + "\n");
        double target = accuracy_fraction_ ? accuracy_ * tree_->cfg.start_pot() : accuracy_;
        if (accuracy_ > 0 && st.exploitability <= target) {
          reason = "required accuracy reached";
          break;
        }
      }
      if (limit) {
        reason = steps > 0 && done >= steps ? "required number of steps done" : "required time elapsed";
        break;
      }
    }
  }
  out_raw("SOLVER: stopped (" + reason + ")\n");
  solving_ = false;
}

void Upi::stop_solver(const char* reason) {
  if (solver_thread_.joinable()) {
    if (solving_) {
      stop_reason_ = reason;
      stop_flag_ = true;
      pause_cv_.notify_all();
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
  if (cfg_.board.size() < 3) throw CmdError{"incorrect or missing argument"};
  GameConfig c = cfg_;
  c.preflop = false;
  c.schematics.clear();
  c.subset.clear();
  return build_from(c, mem_ignore, pessimistic, "build_tree ok!");
}

std::string Upi::build_from(GameConfig c, bool mem_ignore, bool pessimistic, const char* okmsg) {
  stop_solver();
  auto t = std::make_unique<GameTree>();
  t->payoff = payoff_;
  std::string err;
  if (!t->build(c, err, false)) throw CmdError{err};
  // Old tree memory is released before allocating the new one, so count it as available.
  uint64_t avail = available_memory_bytes() + (tree_ ? tree_->memory_bytes() : 0);
  uint64_t full = t->memory_bytes_for(false), small = t->memory_bytes_for(true);
  if (small_strats_ == "on") t->compressed = true;
  else if (small_strats_ == "off") t->compressed = false;
  else t->compressed = c.preflop || full > avail * 9 / 10;  // auto: preflop trees always small (like Pio)
  uint64_t need = t->memory_bytes();
  if (pessimistic) need = need * 2;  // rough stand-in for an unpaired rainbow board
  if (!mem_ignore && need > avail)
    throw CmdError{"not enough memory: need " + std::to_string(need >> 20) + " MB (" + std::to_string(small >> 20) +
                   " MB compressed), available " + std::to_string(avail >> 20) + " MB"};
  tree_.reset();
  solver_.reset();
  t->allocate_storage();
  tree_ = std::move(t);
  solver_ = std::make_unique<Solver>(*tree_);
  solver_->first_player = first_player_;
  tree_alg_ = algorithm_;
  iterations_ = 0;
  discount_restart_ = 0;
  solve_seconds_ = 0;
  return okmsg;
}

std::string Upi::estimate_text() {
  if (cfg_.board.size() < 3) throw CmdError{"incorrect or missing argument"};
  GameTree t;
  t.payoff = payoff_;
  std::string err;
  if (!t.build(cfg_, err, false)) throw CmdError{err};
  uint64_t b = t.memory_bytes_for(false), bs = t.memory_bytes_for(true);
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
  H["load_script"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::ifstream f(a[0]);
    if (!f) throw CmdError{"cannot open " + a[0]};
    std::vector<std::string> lines;
    std::string l;
    while (std::getline(f, l)) lines.push_back(l);
    for (auto it = lines.rbegin(); it != lines.rend(); ++it) script_lines_.push_front(*it);
    return std::string("load_script ok!");
  };
  H["load_script_silent"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::ifstream f(a[0]);
    if (!f) throw CmdError{"cannot open " + a[0]};
    std::vector<std::string> lines;
    std::string l;
    while (std::getline(f, l)) lines.push_back(l);
    std::string skip_label;
    for (size_t i = 0; i < lines.size(); ++i) {
      auto toks = tokenize(lines[i]);
      if (!skip_label.empty()) {
        if (toks.size() >= 2 && toks[0] == "LABEL:" && toks[1] == skip_label) skip_label.clear();
        continue;
      }
      if (toks.size() >= 3 && toks[0] == "skip_if_done") {
        if (std::ifstream(toks[1]).good()) skip_label = toks[2];
        continue;
      }
      if (!execute_line(lines[i], true)) break;
    }
    return std::string("load_script_silent ok!");
  };
  H["skip_if_done"] = [this](const Args& a) {
    if (a.size() < 2) throw CmdError{"incorrect or missing argument"};
    if (std::ifstream(a[0]).good()) {
      while (!script_lines_.empty()) {
        auto toks = tokenize(script_lines_.front());
        script_lines_.pop_front();
        if (toks.size() >= 2 && toks[0] == "LABEL:" && toks[1] == a[1]) break;
      }
    }
    return std::string("skip_if_done ok!");
  };
  H["echo"] = [](const Args& a) { return a.empty() ? std::string() : a[0]; };  // Pio echoes the first argument
  H["repeat"] = [](const Args& a) {
    long long n = arg_int(a, 0, "count");
    std::string text = join(a, 1), s;
    for (long long i = 0; i < n; ++i) s += text + (i + 1 < n ? "\n" : "");
    return s;
  };
  H["show_hand_order"] = [](const Args&) {
    std::string s;
    for (int i = 0; i < NUM_COMBOS; ++i) {
      if (i) s += ' ';
      s += combo_str(i);
    }
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
    return std::string("\x01");
  };
  H["stdoutredi_append"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    std::FILE* f = std::fopen(a[0].c_str(), "a");
    if (!f) throw CmdError{"cannot open " + a[0]};
    std::lock_guard<std::mutex> lk(out_mu_);
    if (redirected_) std::fclose(out_);
    out_ = f;
    redirected_ = true;
    return std::string("\x01");
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
    double secs = 0;
    long long steps = 0;
    if (!a.empty()) {
      double v = arg_double(a, 0, "amount");
      std::string unit = a.size() > 1 ? a[1] : "seconds";
      if (unit == "steps" || unit == "step") steps = static_cast<long long>(v);
      else if (unit == "seconds" || unit == "second" || unit == "s") secs = v;
      else throw CmdError{"incorrect or missing argument"};
    }
    if (solving_) return std::string("go ok!");
    out_raw(std::string("go ok!\n") + (end_string_.empty() ? "" : end_string_ + "\n"));
    start_solver(secs, steps);
    return std::string("\x01");
  };
  H["stop"] = [this](const Args&) {
    stop_solver();
    return std::string("stop ok!");
  };
  H["wait_for_solver"] = [this](const Args&) {
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
    info_freq_ = static_cast<int>(std::max<long long>(0, arg_int(a, 0, "frequency")));
    return std::string("set_info_freq ok!");
  };
  H["set_accuracy"] = [this](const Args& a) {
    accuracy_ = arg_double(a, 0, "accuracy");
    accuracy_fraction_ = a.size() > 1 && a[1] == "fraction";
    if (a.size() > 1 && a[1] != "fraction" && a[1] != "chips") throw CmdError{"incorrect or missing argument"};
    return std::string("set_accuracy ok!");
  };
  H["set_recalc_accuracy"] = [this](const Args& a) {
    for (int i = 0; i < 3; ++i) recalc_accuracy_[i] = arg_double(a, i, "accuracy");
    return std::string("set_recalc_accuracy ok!");
  };
  H["set_always_recalc"] = [this](const Args& a) {
    always_recalc_street_ = static_cast<int>(arg_int(a, 0, "street"));
    always_recalc_ms_ = a.size() > 1 ? static_cast<int>(arg_int(a, 1, "timeout")) : 0;
    return std::string("set_always_recalc ok!");
  };
  H["set_isomorphism"] = [this](const Args& a) {
    cfg_.iso_flop = arg_int(a, 0, "flop") != 0;
    cfg_.iso_turn = a.size() > 1 ? arg_int(a, 1, "turn") != 0 : false;
    return std::string("set_isomorphism ok!");
  };
  // Not a Pio command: Pio always tracks the union. Opt-in here because the wider vectors
  // cost time and memory, and it has to be set before build_tree to take effect.
  H["set_union_hands"] = [this](const Args& a) {
    if (a.empty() || (a[0] != "on" && a[0] != "off")) throw CmdError{"incorrect or missing argument"};
    cfg_.union_hands = a[0] == "on";
    return std::string("set_union_hands ok!");
  };
  H["change_step"] = [this](const Args& a) {
    step_ = arg_double(a, 0, "step");
    return std::string("change_step ok!");
  };
  H["show_step"] = [this](const Args&) { return pfmt("%f", step_); };
  H["show_iters"] = [this](const Args&) { return std::to_string(tree_ ? tree_->iterations : 0); };
  H["set_first_iteration_player"] = [this](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
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
  H["set_eff_stack"] = [this](const Args& a) {
    cfg_.eff_stack = a.empty() ? 0 : static_cast<int32_t>(std::atoll(a[0].c_str()));
    return std::string("set_eff_stack ok!");
  };
  H["set_board"] = [this](const Args& a) {
    if (a.size() != 1) throw CmdError{"incorrect or missing argument"};
    std::vector<int> b;
    if (!parse_cards(a[0], b) || b.size() < 3 || b.size() > 5) throw CmdError{"incorrect or missing argument"};
    cfg_.board = b;
    return std::string("set_board ok!");
  };
  H["set_pot"] = [this](const Args& a) {
    for (int i = 0; i < 3; ++i) cfg_.pot[i] = i < static_cast<int>(a.size()) ? static_cast<int32_t>(std::atoll(a[i].c_str())) : 0;
    return std::string("set_pot ok!");
  };
  auto parse_line = [](const Args& a) {
    std::vector<int32_t> l;
    for (size_t i = 0; i < a.size(); ++i) l.push_back(static_cast<int32_t>(arg_int(a, i, "amount")));
    if (l.empty()) throw CmdError{"incorrect or missing argument"};
    return l;
  };
  H["add_line"] = [this, parse_line](const Args& a) {
    cfg_.lines.add.push_back(parse_line(a));  // validated at build_tree, like Pio
    return std::string("add_line ok!");
  };
  H["remove_line"] = [this, parse_line](const Args& a) {
    cfg_.lines.remove.push_back(parse_line(a));
    return std::string("remove_line ok!");
  };
  H["force_line"] = [this, parse_line](const Args& a) {
    cfg_.lines.force.push_back(parse_line(a));
    return std::string("force_line ok!");
  };
  H["clear_lines"] = [this](const Args&) {
    cfg_.lines.clear();
    return std::string("clear_lines ok!");
  };
  H["build_tree"] = [this](const Args& a) { return cmd_build_tree(a); };
  H["list_algorithms"] = [](const Args&) {
    return std::string(
        "v4_small,DCFR,Discounted CFR (alpha 1.5, beta 0, gamma 2); this solver uses one algorithm for every code\n"
        "v4_large,DCFR,Discounted CFR (alpha 1.5, beta 0, gamma 2)\n"
        "pio_cfr,DCFR,Discounted CFR (alpha 1.5, beta 0, gamma 2)\n"
        "original_pio,DCFR,Discounted CFR (alpha 1.5, beta 0, gamma 2)");
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
    if (a.empty() || (a[0] != "on" && a[0] != "off" && a[0] != "auto")) throw CmdError{"incorrect or missing argument"};
    small_strats_ = a[0];
    return std::string("small_strats ok!");
  };
  H["estimate_schematic_tree"] = [this](const Args&) { return estimate_text(); };
  H["estimate_tree"] = [this](const Args&) {
    // Preflop tree with the current subset and schematic trees (or the pure preflop tree).
    if (!have_pf_) return estimate_text();
    GameConfig c = pf_cfg_;
    c.schematics = schematics_;
    c.subset = subset_;
    if (c.subset.empty()) c.schematics.clear();
    GameTree t;
    std::string err;
    if (!t.build(c, err, false)) throw CmdError{err};
    return "estimated tree size: " + std::to_string(t.memory_bytes_for(true) >> 20) + " MB";
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
  H["show_tree_info"] = [this](const Args&) {
    std::string s;
    for (size_t i = 0; i < tree_info_.size(); ++i) s += tree_info_[i] + (i + 1 < tree_info_.size() ? "\n" : "");
    return s;
  };

  // ---- Alternative EV models ----
  H["set_rake"] = [this](const Args& a) {
    payoff_.rake_frac = arg_double(a, 0, "fraction");
    payoff_.rake_cap = arg_double(a, 1, "cap");
    // Rake and ICM are exclusive; like Pio, the last one set wins (setting rake turns ICM off).
    if (payoff_.rake_frac > 0) payoff_.icm.enabled = false;
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_);
      tree_->payoff = payoff_;
      discount_restart_ = tree_->iterations;
    }
    return std::string("set_rake ok!");
  };
  H["set_icm"] = [this](const Args& a) {
    payoff_.icm.stacks[0] = static_cast<int32_t>(arg_int(a, 0, "oop stack"));
    payoff_.icm.stacks[1] = static_cast<int32_t>(arg_int(a, 1, "ip stack"));
    // Pio: without ICM points it reports an error line, still answers ok, and leaves ICM off.
    if (!icm_tables_defined_) return std::string("ERROR: set_icm ICM points not defined.\nset_icm ok!");
    payoff_.icm.enabled = true;
    payoff_.rake_frac = 0;  // the last of rake / ICM wins
    payoff_.rake_cap = 0;
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_);
      tree_->payoff = payoff_;
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
      TreePause tp(pause_requests_, tree_mu_, pause_cv_);
      tree_->payoff = payoff_;
      discount_restart_ = tree_->iterations;
    }
    return std::string("set_icm_point ok!");
  };
  H["reset_icm_tables"] = [this](const Args&) {
    payoff_.icm = IcmModel{};  // rake (if any) is left as it is
    icm_tables_defined_ = true;  // Pio: reset tables are "defined" (empty table = chip values)
    if (tree_) {
      TreePause tp(pause_requests_, tree_mu_, pause_cv_);
      tree_->payoff = payoff_;
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
    if (tree_) tree_->fast_loaded = false;
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
    for (auto& s : a) l.push_back(static_cast<int32_t>(std::atoll(s.c_str())));
    if (l.empty()) throw CmdError{"incorrect or missing argument"};
    return l;
  };
  H["add_preflop_line"] = [this, parse_line](const Args& a) {
    pf_lines_.add.push_back(parse_line(a));
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
    if (!tree_ || !tree_->cfg.preflop) throw CmdError{"incorrect path"};
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
    GameConfig c = pf_cfg_;
    c.schematics = schematics_;
    c.subset = subset_;
    return build_from(c, ignore_mem_check_, false, "add_all_flops ok!");
  };
}

}  // namespace pio
