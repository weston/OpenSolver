// dump_tree / load_tree / show_metadata / show_save_version / check_tree_health.
//
// A save is a gzip stream (written incrementally, so multi-GB trees never need a second copy in RAM):
//   "OUPISAV2" | u32 version | u32 truncate (0 full, 4 no_rivers, 3 no_turns) | u8 storage mode (0 f32, 1 q16)
//   | u32 config length | config | u64 meta length | node metadata | per-decision-node data | "OUPIEND!"
// gzip's CRC32 plus the trailer marker let check_tree_health detect corruption and truncation.
#include <zlib.h>

#include <algorithm>
#include <cstring>
#include <sstream>

#include "upi.hpp"
#include "version.hpp"

namespace pio {

namespace {

constexpr char kMagic[8] = {'O', 'U', 'P', 'I', 'S', 'A', 'V', '2'};
constexpr char kTrailer[8] = {'O', 'U', 'P', 'I', 'E', 'N', 'D', '!'};
constexpr uint32_t kVersion = 3;  // 3 adds preflop trees; 2 is still readable

struct Writer {
  std::string buf;
  template <class T>
  void put(const T& v) {
    buf.append(reinterpret_cast<const char*>(&v), sizeof(T));
  }
  void put_bytes(const void* p, size_t n) { buf.append(static_cast<const char*>(p), n); }
  void put_str(const std::string& s) {
    put(static_cast<uint32_t>(s.size()));
    buf += s;
  }
};

struct Reader {
  const char* p;
  const char* end;
  template <class T>
  T get() {
    if (static_cast<size_t>(end - p) < sizeof(T)) throw CmdError{"file format error (truncated)"};
    T v;
    std::memcpy(&v, p, sizeof(T));
    p += sizeof(T);
    return v;
  }
  void get_bytes(void* dst, size_t n) {
    if (static_cast<size_t>(end - p) < n) throw CmdError{"file format error (truncated)"};
    std::memcpy(dst, p, n);
    p += n;
  }
  std::string get_str() {
    uint32_t n = get<uint32_t>();
    if (static_cast<size_t>(end - p) < n) throw CmdError{"file format error (truncated)"};
    std::string s(p, n);
    p += n;
    return s;
  }
};

class GzOut {
 public:
  GzOut(const std::string& path, int level) {
    std::string mode = "wb" + std::to_string(std::clamp(level, 0, 9));
    f_ = gzopen(path.c_str(), mode.c_str());
    if (!f_) throw CmdError{"cannot open " + path + " for writing"};
    gzbuffer(f_, 1 << 20);
  }
  ~GzOut() {
    if (f_) gzclose(f_);
  }
  void write(const void* p, size_t n) {
    const char* c = static_cast<const char*>(p);
    while (n) {
      unsigned chunk = static_cast<unsigned>(std::min<size_t>(n, 1u << 30));
      if (gzwrite(f_, c, chunk) != static_cast<int>(chunk)) throw CmdError{"write error"};
      c += chunk;
      n -= chunk;
    }
  }
  template <class T>
  void put(const T& v) {
    write(&v, sizeof(T));
  }
  void close() {
    gzFile f = f_;
    f_ = nullptr;
    if (f && gzclose(f) != Z_OK) throw CmdError{"write error"};
  }

 private:
  gzFile f_ = nullptr;
};

class GzIn {
 public:
  explicit GzIn(const std::string& path) {
    f_ = gzopen(path.c_str(), "rb");
    if (!f_) throw CmdError{"cannot open " + path};
    gzbuffer(f_, 1 << 20);
  }
  ~GzIn() {
    if (f_) gzclose(f_);
  }
  void read(void* p, size_t n) {
    char* c = static_cast<char*>(p);
    while (n) {
      unsigned chunk = static_cast<unsigned>(std::min<size_t>(n, 1u << 30));
      int got = gzread(f_, c, chunk);
      if (got != static_cast<int>(chunk)) throw CmdError{"file format error (truncated or corrupted)"};
      c += chunk;
      n -= chunk;
    }
  }
  template <class T>
  T get() {
    T v;
    read(&v, sizeof(T));
    return v;
  }
  // Reads to the end. True if the stream decompressed cleanly (gzip CRC ok) and ended with the trailer.
  bool drain_ok() {
    std::vector<char> buf(1 << 20);
    char last[8] = {0};
    size_t total = 0;
    int got;
    while ((got = gzread(f_, buf.data(), static_cast<unsigned>(buf.size()))) > 0) {
      if (got >= 8) std::memcpy(last, buf.data() + got - 8, 8);
      else {
        std::memmove(last, last + got, 8 - got);
        std::memcpy(last + 8 - got, buf.data(), got);
      }
      total += static_cast<size_t>(got);
    }
    int err = Z_OK;
    gzerror(f_, &err);
    bool clean = got == 0 && (err == Z_OK || err == Z_STREAM_END);
    return clean && total >= 8 && std::memcmp(last, kTrailer, 8) == 0;
  }

 private:
  gzFile f_ = nullptr;
};

struct Header {
  uint32_t version = 0, truncate = 0;
  uint8_t mode = 0;
};

Header read_header(GzIn& in) {
  char magic[8];
  try {
    in.read(magic, 8);
  } catch (const CmdError&) {
    throw CmdError{"not a save file of this solver"};
  }
  if (std::memcmp(magic, kMagic, 8) != 0) throw CmdError{"not a save file of this solver"};
  Header h;
  h.version = in.get<uint32_t>();
  if (h.version != kVersion && h.version != 2) throw CmdError{"unsupported save version " + std::to_string(h.version)};
  h.truncate = in.get<uint32_t>();
  h.mode = in.get<uint8_t>();
  return h;
}

void write_config(Writer& w, const GameConfig& c, const PayoffModel& pm, const std::vector<std::string>& info,
                  long long iters, double secs) {
  w.put(static_cast<uint8_t>(c.board.size()));
  for (int x : c.board) w.put(static_cast<uint8_t>(x));
  for (int i = 0; i < 3; ++i) w.put(c.pot[i]);
  w.put(c.eff_stack);
  w.put(static_cast<uint8_t>(c.iso_flop));
  w.put(static_cast<uint8_t>(c.iso_turn));
  w.put_bytes(c.range[0].data(), sizeof(float) * NUM_COMBOS);
  w.put_bytes(c.range[1].data(), sizeof(float) * NUM_COMBOS);
  for (auto* set : {&c.lines.add, &c.lines.remove, &c.lines.force}) {
    w.put(static_cast<uint32_t>(set->size()));
    for (auto& l : *set) {
      w.put(static_cast<uint32_t>(l.size()));
      for (int32_t x : l) w.put(x);
    }
  }
  w.put(pm.rake_frac);
  w.put(pm.rake_cap);
  w.put(static_cast<uint8_t>(pm.icm.enabled));
  w.put(pm.icm.stacks[0]);
  w.put(pm.icm.stacks[1]);
  for (int p = 0; p < 2; ++p) {
    w.put(static_cast<uint32_t>(pm.icm.points[p].size()));
    for (auto& pt : pm.icm.points[p]) {
      w.put(pt.first);
      w.put(pt.second);
    }
  }
  w.put(static_cast<uint32_t>(info.size()));
  for (auto& s : info) w.put_str(s);
  w.put(static_cast<int64_t>(iters));
  w.put(secs);
  // v3: preflop trees
  w.put(static_cast<uint8_t>(c.preflop));
  w.put(static_cast<uint32_t>(c.subset.size()));
  for (auto& f : c.subset) {
    w.put(f.weight);
    for (int k = 0; k < 3; ++k) w.put(static_cast<uint8_t>(f.cards[k]));
  }
  w.put(static_cast<uint32_t>(c.schematics.size()));
  for (auto& sc : c.schematics) {
    w.put(static_cast<uint32_t>(sc.exit_line.size()));
    for (int32_t x : sc.exit_line) w.put(x);
    for (int k = 0; k < 3; ++k) w.put(sc.pot[k]);
    w.put(sc.eff_stack);
    for (auto* set : {&sc.lines.add, &sc.lines.remove, &sc.lines.force}) {
      w.put(static_cast<uint32_t>(set->size()));
      for (auto& l : *set) {
        w.put(static_cast<uint32_t>(l.size()));
        for (int32_t x : l) w.put(x);
      }
    }
  }
}

void read_config(Reader& r, GameConfig& c, PayoffModel& pm, std::vector<std::string>& info, long long& iters,
                 double& secs) {
  uint8_t n = r.get<uint8_t>();
  if (n != 0 && (n < 3 || n > 5)) throw CmdError{"file format error (board)"};  // 0 = preflop tree
  c.board.clear();
  for (int i = 0; i < n; ++i) c.board.push_back(r.get<uint8_t>());
  for (int i = 0; i < 3; ++i) c.pot[i] = r.get<int32_t>();
  c.eff_stack = r.get<int32_t>();
  c.iso_flop = r.get<uint8_t>() != 0;
  c.iso_turn = r.get<uint8_t>() != 0;
  r.get_bytes(c.range[0].data(), sizeof(float) * NUM_COMBOS);
  r.get_bytes(c.range[1].data(), sizeof(float) * NUM_COMBOS);
  for (auto* set : {&c.lines.add, &c.lines.remove, &c.lines.force}) {
    set->clear();
    uint32_t k = r.get<uint32_t>();
    for (uint32_t i = 0; i < k; ++i) {
      uint32_t len = r.get<uint32_t>();
      std::vector<int32_t> l(len);
      for (auto& x : l) x = r.get<int32_t>();
      set->push_back(std::move(l));
    }
  }
  pm.rake_frac = r.get<double>();
  pm.rake_cap = r.get<double>();
  pm.icm.enabled = r.get<uint8_t>() != 0;
  pm.icm.stacks[0] = r.get<int32_t>();
  pm.icm.stacks[1] = r.get<int32_t>();
  for (int p = 0; p < 2; ++p) {
    pm.icm.points[p].clear();
    uint32_t k = r.get<uint32_t>();
    for (uint32_t i = 0; i < k; ++i) {
      double a = r.get<double>();
      double b = r.get<double>();
      pm.icm.points[p].push_back({a, b});
    }
  }
  info.clear();
  uint32_t ni = r.get<uint32_t>();
  for (uint32_t i = 0; i < ni; ++i) info.push_back(r.get_str());
  iters = r.get<int64_t>();
  secs = r.get<double>();
  c.preflop = false;
  c.subset.clear();
  c.schematics.clear();
  if (r.p == r.end) return;  // version 2 save
  c.preflop = r.get<uint8_t>() != 0;
  uint32_t ns = r.get<uint32_t>();
  for (uint32_t i = 0; i < ns; ++i) {
    SubsetFlop f;
    f.weight = r.get<float>();
    for (int k = 0; k < 3; ++k) f.cards[k] = r.get<uint8_t>();
    c.subset.push_back(f);
  }
  uint32_t nsc = r.get<uint32_t>();
  for (uint32_t i = 0; i < nsc; ++i) {
    Schematic sc;
    uint32_t ll = r.get<uint32_t>();
    for (uint32_t k = 0; k < ll; ++k) sc.exit_line.push_back(r.get<int32_t>());
    for (int k = 0; k < 3; ++k) sc.pot[k] = r.get<int32_t>();
    sc.eff_stack = r.get<int32_t>();
    for (auto* set : {&sc.lines.add, &sc.lines.remove, &sc.lines.force}) {
      uint32_t kk = r.get<uint32_t>();
      for (uint32_t j = 0; j < kk; ++j) {
        uint32_t len = r.get<uint32_t>();
        std::vector<int32_t> l(len);
        for (auto& x : l) x = r.get<int32_t>();
        set->push_back(std::move(l));
      }
    }
    c.schematics.push_back(std::move(sc));
  }
}

struct LoadedConfig {
  Header h;
  GameConfig cfg;
  PayoffModel pm;
  std::vector<std::string> info;
  long long iters = 0;
  double secs = 0;
};

LoadedConfig read_config_section(GzIn& in) {
  LoadedConfig lc;
  lc.h = read_header(in);
  uint32_t len = in.get<uint32_t>();
  if (len > (64u << 20)) throw CmdError{"file format error (config size)"};
  std::string buf(len, '\0');
  in.read(buf.data(), len);
  Reader r{buf.data(), buf.data() + buf.size()};
  read_config(r, lc.cfg, lc.pm, lc.info, lc.iters, lc.secs);
  return lc;
}

}  // namespace

std::string Upi::cmd_dump_tree(const Args& a) {
  require_tree();
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  uint32_t truncate = 0;
  bool compress = true;
  for (size_t i = 1; i < a.size(); ++i) {
    if (a[i] == "full") truncate = 0;
    else if (a[i] == "no_rivers") truncate = 4;
    else if (a[i] == "no_turns") truncate = 3;
    else if (a[i] == "no_compress") compress = false;
    else throw CmdError{"incorrect or missing argument"};
  }
  TreePause tp(pause_requests_, tree_mu_, pause_cv_);
  const auto& g = *tree_;
  GzOut out(a[0], compress ? std::clamp(compression_level_ * 9 / 21, 1, 9) : 0);
  out.write(kMagic, 8);
  out.put(kVersion);
  out.put(truncate);
  out.put(static_cast<uint8_t>((g.compressed ? 1 : 0) | (compress ? 2 : 0)));
  Writer w;
  write_config(w, g.cfg, g.payoff, tree_info_, g.iterations, solve_seconds_);
  out.put(static_cast<uint32_t>(w.buf.size()));
  out.write(w.buf.data(), w.buf.size());
  Writer m;
  m.put(static_cast<uint64_t>(g.nodes.size()));
  m.put(static_cast<uint64_t>(g.storage_floats));
  for (auto& n : g.nodes) m.put(n.flags);
  m.put(static_cast<uint32_t>(g.combo_lock.size()));
  for (auto& [ni, mk] : g.combo_lock) {
    m.put(ni);
    m.put(static_cast<uint32_t>(mk.size()));
    m.put_bytes(mk.data(), mk.size());
  }
  m.put(static_cast<uint32_t>(g.incentives.size()));
  for (auto& [ni, v] : g.incentives) {
    m.put(ni);
    m.put(static_cast<uint32_t>(v.size()));
    m.put_bytes(v.data(), v.size() * sizeof(float));
  }
  m.put(static_cast<uint32_t>(g.eliminated.size()));
  for (auto& [ni, mk] : g.eliminated) {
    m.put(ni);
    m.put(mk);
  }
  out.put(static_cast<uint64_t>(m.buf.size()));
  out.write(m.buf.data(), m.buf.size());
  uint32_t max_len = truncate ? truncate : 5;
  for (auto& n : g.nodes) {
    if (n.kind != ANodeKind::Decision || n.board_len > max_len) continue;
    size_t len = g.node_size(n);
    if (g.compressed) {
      out.put(g.rscale[n.aux]);
      out.put(g.sscale[n.aux]);
      out.write(g.regrets_q.data() + n.storage, len * sizeof(int16_t));
      out.write(g.strat_q.data() + n.storage, len * sizeof(uint16_t));
    } else {
      out.write(g.regrets.data() + n.storage, len * sizeof(float));
      out.write(g.strat.data() + n.storage, len * sizeof(float));
    }
  }
  out.write(kTrailer, 8);
  out.close();
  return "dump_tree ok!";
}

std::string Upi::cmd_load_tree(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  for (size_t i = 1; i < a.size(); ++i)
    if (a[i] != "full" && a[i] != "fast" && a[i] != "auto") throw CmdError{"incorrect or missing argument"};
  bool full_load = a.size() > 1 && a[1] == "full";
  stop_solver();
  GzIn in(a[0]);
  LoadedConfig lc = read_config_section(in);
  auto t = std::make_unique<GameTree>();
  t->payoff = lc.pm;
  std::string err;
  if (!t->build(lc.cfg, err, false)) throw CmdError{"cannot rebuild tree: " + err};
  uint64_t avail = available_memory_bytes() + (tree_ ? tree_->memory_bytes() : 0);
  if (small_strats_ == "on") t->compressed = true;
  else if (small_strats_ == "off") t->compressed = false;
  // auto: keep the file's storage mode so a save/load round trip is exact. Requantizing a float32 save
  // to 16-bit is lossy (~1e-4 in strategies), so only do it when float32 genuinely does not fit.
  else t->compressed = (lc.h.mode & 1) || t->memory_bytes_for(false) > avail;
  tree_.reset();
  solver_.reset();
  t->allocate_storage();
  uint64_t mlen = in.get<uint64_t>();
  if (mlen > (uint64_t(1) << 34)) throw CmdError{"file format error (metadata size)"};
  std::string mbuf(mlen, '\0');
  in.read(mbuf.data(), mlen);
  Reader r{mbuf.data(), mbuf.data() + mbuf.size()};
  uint64_t nn = r.get<uint64_t>(), sf = r.get<uint64_t>();
  if (nn != t->nodes.size() || sf != t->storage_floats) throw CmdError{"file format error (tree shape mismatch)"};
  for (auto& n : t->nodes) n.flags = r.get<uint8_t>();
  uint32_t k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>(), len = r.get<uint32_t>();
    std::vector<uint8_t> mk(len);
    r.get_bytes(mk.data(), len);
    t->combo_lock[ni] = std::move(mk);
  }
  k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>(), len = r.get<uint32_t>();
    std::vector<float> v(len);
    r.get_bytes(v.data(), len * sizeof(float));
    t->incentives[ni] = std::move(v);
  }
  k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>();
    t->eliminated[ni] = r.get<uint32_t>();
  }
  uint32_t max_len = lc.h.truncate ? lc.h.truncate : 5;
  std::vector<float> fr, fs;
  std::vector<int16_t> qr;
  std::vector<uint16_t> qs;
  for (auto& n : t->nodes) {
    if (n.kind != ANodeKind::Decision || n.board_len > max_len) continue;
    size_t len = t->node_size(n);
    if ((lc.h.mode & 1) && t->compressed) {
      t->rscale[n.aux] = in.get<float>();
      t->sscale[n.aux] = in.get<float>();
      in.read(t->regrets_q.data() + n.storage, len * sizeof(int16_t));
      in.read(t->strat_q.data() + n.storage, len * sizeof(uint16_t));
      continue;
    }
    fr.resize(len);
    fs.resize(len);
    if (lc.h.mode & 1) {
      float rs = in.get<float>(), ss = in.get<float>();
      qr.resize(len);
      qs.resize(len);
      in.read(qr.data(), len * sizeof(int16_t));
      in.read(qs.data(), len * sizeof(uint16_t));
      for (size_t i = 0; i < len; ++i) {
        fr[i] = qr[i] * rs;
        fs[i] = qs[i] * ss;
      }
    } else {
      in.read(fr.data(), len * sizeof(float));
      in.read(fs.data(), len * sizeof(float));
    }
    if (t->compressed) {
      t->regrets_commit(n, fr.data());
      t->strat_commit(n, fs.data());
    } else {
      std::memcpy(t->regrets.data() + n.storage, fr.data(), len * sizeof(float));
      std::memcpy(t->strat.data() + n.storage, fs.data(), len * sizeof(float));
    }
  }
  char trailer[8];
  in.read(trailer, 8);
  if (std::memcmp(trailer, kTrailer, 8) != 0) throw CmdError{"file format error (missing trailer)"};
  t->iterations = lc.iters;
  t->incomplete = lc.h.truncate != 0;
  t->missing_street = lc.h.truncate ? static_cast<int>(lc.h.truncate) + 1 : 0;
  // Pio loads in "fast" mode unless `full` is given (we always load everything, but keep the flags
  // and the load_all_nodes requirement for compatibility).
  t->fast_loaded = !full_load;
  t->loaded_compressed = (lc.h.mode & 2) != 0;
  tree_ = std::move(t);
  solver_ = std::make_unique<Solver>(*tree_);
  solver_->first_player = first_player_;
  tree_info_ = lc.info;
  solve_seconds_ = lc.secs;
  iterations_ = lc.iters;
  discount_restart_ = 0;
  cfg_ = lc.cfg;
  payoff_ = lc.pm;
  if (lc.cfg.preflop) {
    pf_cfg_ = lc.cfg;
    pf_cfg_.schematics.clear();
    pf_cfg_.subset.clear();
    have_pf_ = true;
    schematics_ = lc.cfg.schematics;
    subset_ = lc.cfg.subset;
    cfg_.preflop = false;
  }
  return "load_tree ok!";
}

std::string Upi::cmd_show_metadata(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  GzIn in(a[0]);
  LoadedConfig lc = read_config_section(in);
  const GameConfig& c = lc.cfg;
  std::ostringstream o;
  o << "r\nROOT\n";
  if (c.board.empty()) o << "---";
  for (int x : c.board) o << card_str(x) << " ";
  o << "\n" << c.pot[0] << " " << c.pot[1] << " " << c.pot[2] << " \n1 children\nflags: ";
  if (c.iso_flop || c.iso_turn) o << "ISOMORPHISM_ON ";
  if (lc.h.mode & 2) o << "COMPRESSED ";
  if (lc.h.truncate) o << "INCOMPLETE_TREE ";
  o << "\n\n" << c.eff_stack << "\n";
  o << floats_line(c.range[1].data(), NUM_COMBOS) << "\n";  // Pio prints IP's range first
  o << floats_line(c.range[0].data(), NUM_COMBOS) << "\n";
  for (auto& line : lc.info) o << line << "\n";
  o << "show_metadata ok!";
  return o.str();
}

void Upi::register_save_commands() {
  handlers_["show_save_version"] = [](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    try {
      GzIn in(a[0]);
      Header h = read_header(in);
      return std::to_string(h.version) + "\n" + std::string(SOLVER_NAME) + "-" + SOLVER_VERSION;
    } catch (const CmdError&) {
      return std::string("not a save file of this solver");
    }
  };
  handlers_["check_tree_health"] = [](const Args& a) {
    if (a.empty()) throw CmdError{"incorrect or missing argument"};
    try {
      GzIn in(a[0]);
      read_config_section(in);
      return std::string(in.drain_ok() ? "File OK" : "File CORRUPTED");
    } catch (const CmdError&) {
      return std::string("File CORRUPTED");
    }
  };
}

}  // namespace pio
