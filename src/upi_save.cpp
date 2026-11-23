// dump_tree / load_tree / show_metadata / show_save_version / check_tree_health.
//
// A save is a sequence of gzip members; decompressed back to back (gunzip, gzread) they form one stream:
//   "OUPISAV2" | u32 version | u32 truncate (0 full, 4 no_rivers, 3 no_turns) | u8 mode | [v5: u8 precision]
//   | u32 config length | config | u64 meta length | node metadata
//   | node records | [v4: u64 segment count | u64 member offset per segment | u64 index offset] | "OUPIEND!"
// v4 (current) writes node records grouped by street segment (GameTree::street_segments), one gzip
// member per segment, followed by an index member with each segment's byte offset and a fixed-size
// 39-byte final member holding the index offset and the trailer. That lets `load_tree` (fast) read only
// the segments that are actually accessed. v2/v3 files (records in node order, one member) load fully.
// Mode bits: 0 exact 16-bit records (else exact float32), 1 compressed file, 2 quantised records (v5).
// Quantised records (the default since v5, `set_compression_precision` bits, 11 by default) store only the
// displayed strategy, as per-hand frequencies; `lossless` writes the exact records of v4 (regrets too).
// v6 is v5 for a tree built with collapse_checkdowns (#484): the same layout, a different tree from the
// same config, so the version says which one to build (and a binary that doesn't know refuses the file).
// A tree without a collapsed subtree is written as v5.
// gzip's CRC32 plus the trailer marker let check_tree_health detect corruption and truncation.
#include <stdexcept>
#include <zlib.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <sstream>

#include "solver.hpp"
#include "upi.hpp"
#include "version.hpp"

namespace pio {

namespace {

constexpr char kMagic[8] = {'O', 'U', 'P', 'I', 'S', 'A', 'V', '2'};
constexpr char kTrailer[8] = {'O', 'U', 'P', 'I', 'E', 'N', 'D', '!'};
// 6: version 5 with check-downs built as one showdown node (set_collapse_checkdowns on, #484). The tree
// has fewer nodes then, so a reader has to know; only a tree that has such nodes is written as 6, every
// other save is written as before, as a 5. 5: quantised records; 4: segmented (lazy); 3: preflop trees; 2+ readable
constexpr uint32_t kVersion = 6, kVersionPlain = 5;

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
  // n bytes must remain: call before allocating anything sized by a count read from the file (#360)
  void need(uint64_t n) const {
    if (static_cast<uint64_t>(end - p) < n) throw CmdError{"file format error (truncated)"};
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

int fseek64(FILE* f, uint64_t off) {
#ifdef _WIN32
  return _fseeki64(f, static_cast<long long>(off), SEEK_SET);
#else
  return fseeko(f, static_cast<off_t>(off), SEEK_SET);
#endif
}

uint64_t file_size(FILE* f) {
#ifdef _WIN32
  if (_fseeki64(f, 0, SEEK_END) != 0) return 0;
  return static_cast<uint64_t>(_ftelli64(f));
#else
  if (fseeko(f, 0, SEEK_END) != 0) return 0;
  return static_cast<uint64_t>(ftello(f));
#endif
}

struct File {
  FILE* f = nullptr;
  File(const std::string& path, const char* mode) : f(std::fopen(path.c_str(), mode)) {}
  ~File() {
    if (f) std::fclose(f);
  }
  File(const File&) = delete;
  File& operator=(const File&) = delete;
};

// gzip writer that can end the current member (so the next one starts a fresh, independently decodable
// stream) and knows the exact byte offset of the file it has written.
class GzOut {
 public:
  GzOut(const std::string& path, int level) : file_(path, "wb"), path_(path) {
    if (!file_.f) throw CmdError{"cannot open " + path + " for writing"};
    zs_ = {};
    if (deflateInit2(&zs_, std::clamp(level, 0, 9), Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY) != Z_OK)
      throw CmdError{"out of memory"};
    buf_.resize(1 << 20);
  }
  ~GzOut() { deflateEnd(&zs_); }
  void write(const void* p, size_t n) {
    const Bytef* c = static_cast<const Bytef*>(p);
    while (n) {
      uInt chunk = static_cast<uInt>(std::min<size_t>(n, 1u << 30));
      zs_.next_in = const_cast<Bytef*>(c);
      zs_.avail_in = chunk;
      while (zs_.avail_in) pump(Z_NO_FLUSH);
      c += chunk;
      n -= chunk;
      dirty_ = true;
    }
  }
  template <class T>
  void put(const T& v) {
    write(&v, sizeof(T));
  }
  // Completes the current gzip member (no-op if nothing was written since the last one).
  void finish_member() {
    if (!dirty_) return;
    zs_.next_in = nullptr;
    zs_.avail_in = 0;
    while (pump(Z_FINISH) != Z_STREAM_END) {}
    deflateReset(&zs_);
    dirty_ = false;
  }
  uint64_t offset() const { return offset_; }  // bytes in the file so far (call after finish_member)
  void write_raw(const void* p, size_t n) {
    if (std::fwrite(p, 1, n, file_.f) != n) throw CmdError{"write error"};
    offset_ += n;
  }
  void close() {
    finish_member();
    FILE* f = file_.f;
    file_.f = nullptr;
    if (std::fclose(f) != 0) throw CmdError{"write error"};
  }

 private:
  int pump(int flush) {
    zs_.next_out = buf_.data();
    zs_.avail_out = static_cast<uInt>(buf_.size());
    int r = deflate(&zs_, flush);
    if (r == Z_STREAM_ERROR) throw CmdError{"write error"};
    write_raw(buf_.data(), buf_.size() - zs_.avail_out);
    return r;
  }
  File file_;
  std::string path_;
  z_stream zs_;
  std::vector<Bytef> buf_;
  uint64_t offset_ = 0;
  bool dirty_ = false;
};

// Decodes the single gzip member starting at `off`; it must decompress to exactly `expect` bytes.
std::vector<char> inflate_member(FILE* f, uint64_t off, size_t expect) {
  if (fseek64(f, off) != 0) throw CmdError{"file format error (seek)"};
  z_stream zs{};
  if (inflateInit2(&zs, 15 + 16) != Z_OK) throw CmdError{"out of memory"};
  std::vector<char> out(expect + 1);
  std::vector<Bytef> in(1 << 18);
  zs.next_out = reinterpret_cast<Bytef*>(out.data());
  zs.avail_out = static_cast<uInt>(out.size());
  int r = Z_OK;
  while (r != Z_STREAM_END) {
    if (zs.avail_in == 0) {
      size_t got = std::fread(in.data(), 1, in.size(), f);
      if (got == 0) break;
      zs.next_in = in.data();
      zs.avail_in = static_cast<uInt>(got);
    }
    r = inflate(&zs, Z_NO_FLUSH);
    if (r != Z_OK && r != Z_STREAM_END) break;
    if (zs.avail_out == 0) break;  // more data than expected
  }
  size_t produced = out.size() - zs.avail_out;
  inflateEnd(&zs);
  if (r != Z_STREAM_END || produced != expect) throw CmdError{"file format error (corrupted segment)"};
  out.resize(expect);
  return out;
}

// The final member: a stored deflate block with [u64 index offset][trailer], always 39 bytes.
constexpr size_t kTailSize = 39;
void tail_member(uint64_t index_off, unsigned char* m) {
  unsigned char payload[16];
  std::memcpy(payload, &index_off, 8);
  std::memcpy(payload + 8, kTrailer, 8);
  const unsigned char head[15] = {0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff, 0x01, 16, 0, 0xef, 0xff};
  std::memcpy(m, head, 15);
  std::memcpy(m + 15, payload, 16);
  uint32_t crc = static_cast<uint32_t>(crc32(0L, payload, 16)), isize = 16;
  for (int i = 0; i < 4; ++i) {
    m[31 + i] = static_cast<unsigned char>(crc >> (8 * i));
    m[35 + i] = static_cast<unsigned char>(isize >> (8 * i));
  }
}

class GzIn {
 public:
  explicit GzIn(const std::string& path) {
    f_ = gzopen(path.c_str(), "rb");
    if (!f_) throw CmdError{"couldn't open " + path + ": " + std::strerror(errno)};  // Pio's wording
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
  uint8_t mode = 0, precision = 0;
};
constexpr uint8_t kModeQ16 = 1, kModeCompressed = 2, kModeQuant = 4;

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
  if (h.version < 2 || h.version > kVersion) throw CmdError{"unsupported save version " + std::to_string(h.version)};
  h.truncate = in.get<uint32_t>();
  if (h.truncate != 0 && h.truncate != 3 && h.truncate != 4) throw CmdError{"file format error (truncate)"};
  h.mode = in.get<uint8_t>();
  if (h.version >= 5) h.precision = in.get<uint8_t>();
  if ((h.mode & kModeQuant) && (h.precision < 2 || h.precision > 16)) throw CmdError{"file format error (precision)"};
  return h;
}

void write_config(Writer& w, const GameConfig& c, const PayoffModel& pm, const std::vector<std::string>& info,
                  long long iters, double secs, const double* stats) {
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
  // Only truncated saves carry the root-results block (#195); other saves stay byte-identical.
  // A tree with a flags byte (#302, #513) writes its stats byte either way (0 = none).
  const uint8_t flags = static_cast<uint8_t>((c.union_hands ? 1 : 0) | (c.zero_sum_subset ? 2 : 0));
  if (stats || flags) {
    w.put(static_cast<uint8_t>(stats ? 1 : 0));
    if (stats)
      for (int i = 0; i < 5; ++i) w.put(stats[i]);
  }
  // Optional trailing flags (#302): bit 0 = set_union_hands on. The hand lists, and so the storage
  // layout, depend on it: without it a union-hands save rebuilt with own-range lists and failed to load
  // ("tree shape mismatch"). Bit 1 = set_zero_sum_subset on (#513): the layout is the same, the game the
  // strategies were solved for is not, and a loaded tree goes on in it. Written only when one is set, so
  // every other save stays byte-identical; older readers stop before it.
  if (flags) w.put(flags);
}

void read_config(Reader& r, GameConfig& c, PayoffModel& pm, std::vector<std::string>& info, long long& iters,
                 double& secs, bool& has_stats, double* stats) {
  has_stats = false;
  uint8_t n = r.get<uint8_t>();
  if (n != 0 && (n < 3 || n > 5)) throw CmdError{"file format error (board)"};  // 0 = preflop tree
  c.board.clear();
  uint64_t seen = 0;
  for (int i = 0; i < n; ++i) {
    uint8_t card = r.get<uint8_t>();
    if (card >= 52 || (seen >> card & 1)) throw CmdError{"file format error (board)"};
    seen |= uint64_t(1) << card;
    c.board.push_back(card);
  }
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
      r.need(uint64_t(len) * sizeof(int32_t));
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
  c.union_hands = false;
  c.zero_sum_subset = false;
  if (r.p == r.end) return;  // version 2 save
  c.preflop = r.get<uint8_t>() != 0;
  uint32_t ns = r.get<uint32_t>();
  for (uint32_t i = 0; i < ns; ++i) {
    SubsetFlop f;
    f.weight = r.get<float>();
    for (int k = 0; k < 3; ++k) {
      f.cards[k] = r.get<uint8_t>();
      if (f.cards[k] >= 52) throw CmdError{"file format error (subset)"};
    }
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
        r.need(uint64_t(len) * sizeof(int32_t));
        std::vector<int32_t> l(len);
        for (auto& x : l) x = r.get<int32_t>();
        set->push_back(std::move(l));
      }
    }
    c.schematics.push_back(std::move(sc));
  }
  // Optional trailing block (#195): root results of the solve behind a truncated save. Readers that
  // predate it stop after the schematics and ignore these bytes, so the version stays 5.
  if (r.p == r.end) return;
  has_stats = r.get<uint8_t>() != 0;
  if (has_stats)
    for (int i = 0; i < 5; ++i) stats[i] = r.get<double>();
  if (r.p == r.end) return;
  const uint8_t flags = r.get<uint8_t>();
  c.union_hands = (flags & 1) != 0;       // #302
  c.zero_sum_subset = (flags & 2) != 0;   // #513
}

struct LoadedConfig {
  Header h;
  GameConfig cfg;
  PayoffModel pm;
  std::vector<std::string> info;
  long long iters = 0;
  double secs = 0;
  bool has_stats = false;
  double stats[5] = {0, 0, 0, 0, 0};
};

LoadedConfig read_config_section(GzIn& in) {
  LoadedConfig lc;
  lc.h = read_header(in);
  uint32_t len = in.get<uint32_t>();
  if (len > (64u << 20)) throw CmdError{"file format error (config size)"};
  std::string buf(len, '\0');
  in.read(buf.data(), len);
  Reader r{buf.data(), buf.data() + buf.size()};
  read_config(r, lc.cfg, lc.pm, lc.info, lc.iters, lc.secs, lc.has_stats, lc.stats);
  return lc;
}

// Node records: 16-bit storage writes [f32 regret scale][f32 strategy scale][A*H i16][A*H u16],
// float32 storage writes [A*H f32 regrets][A*H f32 strategy sums].
size_t record_bytes(const GameTree& t, const GNode& n, uint8_t file_mode) {
  size_t len = t.node_size(n);
  if (file_mode & kModeQuant) {
    size_t H = static_cast<size_t>(t.num_hands(n.player));
    return 4 + (n.num_children > 1 ? (n.num_children - 1) * H * 2 : 0);
  }
  return (file_mode & 1) ? 8 + len * 4 : len * 8;
}

// Quantised record (default): [f32 strategy scale][(A-1)*H u16 frequencies of the displayed strategy, last
// action implied], 2^p-1 levels. Saves are for viewing (and re-solving missing streets on demand), not for
// resuming a solve, so regrets are not stored. On load each hand's strategy sums become frequency * scale
// and its regrets the frequencies, so show_strategy, EVs and on-demand re-solves see the saved strategy.
void write_record_quant(GzOut& out, const GameTree& g, const Solver& solver, uint32_t ni, int prec,
                        std::vector<float>& f, std::vector<float>& b1, std::vector<uint16_t>& q) {
  const GNode& n = g.nodes[ni];
  const int A = n.num_children, H = g.num_hands(n.player);
  const size_t len = static_cast<size_t>(A) * H;
  f.resize(len);
  b1.resize(len);
  solver.average_strategy(ni, f.data());
  const float* S = g.strat_ro(n, b1.data());
  float sscale = 0;
  for (int h = 0; h < H; ++h) {
    double t = 0;
    for (int a = 0; a < A; ++a) t += std::max(0.0f, S[static_cast<size_t>(a) * H + h]);
    sscale = std::max(sscale, static_cast<float>(t));
  }
  if (!std::isfinite(sscale) || sscale <= 0) sscale = 1;
  out.put(sscale);
  if (A <= 1) return;
  const double lf = static_cast<double>((1u << prec) - 1);
  q.resize(static_cast<size_t>(A - 1) * H);
  for (size_t i = 0; i < q.size(); ++i) {
    double v = std::isfinite(f[i]) ? std::clamp(static_cast<double>(f[i]), 0.0, 1.0) : 0.0;
    q[i] = static_cast<uint16_t>(std::lround(v * lf));
  }
  out.write(q.data(), q.size() * sizeof(uint16_t));
}

// Compact 16-bit rows (#239) <-> the full rows a file holds (dropped hands are 0 there, as they always were).
template <class Q>
static void expand_q(const GameTree& g, const GNode& n, const Q* q, std::vector<Q>& full) {
  const size_t H = static_cast<size_t>(g.num_hands(n.player)), L = g.row_len(n);
  const uint16_t* k = g.keep_of(n);
  full.assign(g.node_size(n), Q(0));
  for (size_t a = 0; a < n.num_children; ++a)
    for (size_t j = 0; j < L; ++j) full[a * H + k[j]] = q[a * L + j];
}
template <class Q>
static void compact_q(const GameTree& g, const GNode& n, const Q* full, Q* q) {
  const size_t H = static_cast<size_t>(g.num_hands(n.player)), L = g.row_len(n);
  const uint16_t* k = g.keep_of(n);
  for (size_t a = 0; a < n.num_children; ++a)
    for (size_t j = 0; j < L; ++j) q[a * L + j] = full[a * H + k[j]];
}

void write_record(GzOut& out, const GameTree& g, const GNode& n) {
  size_t len = g.node_size(n);
  if (g.compressed) {
    out.put(g.rscale[n.aux]);
    out.put(g.sscale[n.aux]);
    const int16_t* rq = g.regrets_q.data() + g.qo(n);
    const uint16_t* sq = g.strat_q.data() + g.qo(n);
    if (g.keep_of(n)) {
      std::vector<int16_t> fr;
      std::vector<uint16_t> fs;
      expand_q(g, n, rq, fr);
      expand_q(g, n, sq, fs);
      out.write(fr.data(), len * sizeof(int16_t));
      out.write(fs.data(), len * sizeof(uint16_t));
    } else {
      out.write(rq, len * sizeof(int16_t));
      out.write(sq, len * sizeof(uint16_t));
    }
  } else {
    out.write(g.regrets.data() + n.storage, len * sizeof(float));
    out.write(g.strat.data() + n.storage, len * sizeof(float));
  }
}

struct MemIn {
  const char* p;
  const char* end;
  void read(void* dst, size_t n) {
    if (static_cast<size_t>(end - p) < n) throw CmdError{"file format error (truncated)"};
    std::memcpy(dst, p, n);
    p += n;
  }
  template <class T>
  T get() {
    T v;
    read(&v, sizeof(T));
    return v;
  }
};

// Reads one record into the tree's storage directly (no accessors: used by the lazy loader too),
// converting between the file's and the tree's storage modes.
template <class In>
void read_record_quant(In& in, GameTree& t, const GNode& n, int prec, std::vector<float>& fr, std::vector<float>& fs) {
  const int A = n.num_children, H = t.num_hands(n.player);
  const size_t len = static_cast<size_t>(A) * H;
  fr.resize(len);
  fs.resize(len);
  const float sscale = in.template get<float>();
  std::vector<uint16_t> q;
  if (A > 1) {
    q.resize(static_cast<size_t>(A - 1) * H);
    in.read(q.data(), q.size() * sizeof(uint16_t));
  }
  const float inv_f = 1.0f / static_cast<float>((1u << prec) - 1);
  for (int h = 0; h < H; ++h) {
    float rest = 1.0f;
    for (int a = 0; a + 1 < A; ++a) {
      float v = q[static_cast<size_t>(a) * H + h] * inv_f;
      rest -= v;
      fr[static_cast<size_t>(a) * H + h] = v;
    }
    fr[static_cast<size_t>(A - 1) * H + h] = std::max(0.0f, rest);
  }
  for (size_t i = 0; i < len; ++i) fs[i] = fr[i] * sscale;  // regrets = frequencies (fr)
  if (t.compressed) {
    t.regrets_store(n, fr.data());
    t.strat_store(n, fs.data());
  } else {
    std::memcpy(t.regrets.data() + n.storage, fr.data(), len * sizeof(float));
    std::memcpy(t.strat.data() + n.storage, fs.data(), len * sizeof(float));
  }
}

template <class In>
void read_record(In& in, GameTree& t, const GNode& n, uint8_t file_mode, int prec, std::vector<float>& fr,
                 std::vector<float>& fs) {
  if (file_mode & kModeQuant) return read_record_quant(in, t, n, prec, fr, fs);
  size_t len = t.node_size(n);
  if ((file_mode & 1) && t.compressed) {
    t.rscale[n.aux] = in.template get<float>();
    t.sscale[n.aux] = in.template get<float>();
    int16_t* rq = t.regrets_q.data() + t.qo(n);
    uint16_t* sq = t.strat_q.data() + t.qo(n);
    if (t.keep_of(n)) {
      std::vector<int16_t> fr16(len);
      std::vector<uint16_t> fs16(len);
      in.read(fr16.data(), len * sizeof(int16_t));
      in.read(fs16.data(), len * sizeof(uint16_t));
      compact_q(t, n, fr16.data(), rq);
      compact_q(t, n, fs16.data(), sq);
    } else {
      in.read(rq, len * sizeof(int16_t));
      in.read(sq, len * sizeof(uint16_t));
    }
    return;
  }
  if (!(file_mode & 1) && !t.compressed) {
    in.read(t.regrets.data() + n.storage, len * sizeof(float));
    in.read(t.strat.data() + n.storage, len * sizeof(float));
    return;
  }
  fr.resize(len);
  fs.resize(len);
  if (file_mode & 1) {
    float rs = in.template get<float>(), ss = in.template get<float>();
    std::vector<int16_t> qr(len);
    std::vector<uint16_t> qs(len);
    in.read(qr.data(), len * sizeof(int16_t));
    in.read(qs.data(), len * sizeof(uint16_t));
    for (size_t i = 0; i < len; ++i) {
      fr[i] = qr[i] * rs;
      fs[i] = qs[i] * ss;
    }
    std::memcpy(t.regrets.data() + n.storage, fr.data(), len * sizeof(float));
    std::memcpy(t.strat.data() + n.storage, fs.data(), len * sizeof(float));
  } else {
    in.read(fr.data(), len * sizeof(float));
    in.read(fs.data(), len * sizeof(float));
    t.regrets_store(n, fr.data());
    t.strat_store(n, fs.data());
  }
}

// Segments present in a save truncated at board length `max_len` (all nodes of a segment share it).
bool segment_saved(const GameTree& t, const StreetSegments& sg, size_t s, uint32_t max_len) {
  return t.nodes[sg.segs[s][0]].board_len <= max_len;
}

void lazy_load_segment(const GameTree& ct, const LazyStore& L, uint32_t s) {
  GameTree& t = const_cast<GameTree&>(ct);  // storage is logically present already; this fills it in
  size_t bytes = 0;
  for (uint32_t ni : L.seg.segs[s]) bytes += record_bytes(t, t.nodes[ni], L.file_mode);
  File f(L.path, "rb");
  if (!f.f) throw CmdError{"cannot open " + L.path};
  std::vector<char> data = inflate_member(f.f, L.offset[s], bytes);
  MemIn in{data.data(), data.data() + data.size()};
  std::vector<float> fr, fs;
  for (uint32_t ni : L.seg.segs[s]) read_record(in, t, t.nodes[ni], L.file_mode, L.precision, fr, fs);
}

// Sets up lazy loading from a v4 file. False (nothing changed) if the index is missing or invalid.
bool open_lazy(GameTree& t, const std::string& path, uint8_t file_mode, uint8_t precision, uint32_t max_len,
               StreetSegments& sg) {
  File f(path, "rb");
  if (!f.f) return false;
  uint64_t size = file_size(f.f);
  if (size < kTailSize || fseek64(f.f, size - kTailSize) != 0) return false;
  unsigned char tail[kTailSize];
  if (std::fread(tail, 1, kTailSize, f.f) != kTailSize) return false;
  uint64_t index_off;
  std::memcpy(&index_off, tail + 15, 8);
  unsigned char expect[kTailSize];
  tail_member(index_off, expect);
  if (std::memcmp(tail, expect, kTailSize) != 0 || index_off >= size) return false;
  std::vector<size_t> present;
  for (size_t s = 0; s < sg.segs.size(); ++s)
    if (segment_saved(t, sg, s, max_len)) present.push_back(s);
  std::vector<char> idx;
  try {
    idx = inflate_member(f.f, index_off, 8 + 8 * present.size());
  } catch (const CmdError&) {
    return false;
  }
  uint64_t count;
  std::memcpy(&count, idx.data(), 8);
  if (count != present.size()) return false;
  auto L = std::make_unique<LazyStore>();
  L->path = path;
  L->file_mode = file_mode;
  L->precision = precision;
  L->offset.assign(sg.segs.size(), 0);
  for (size_t i = 0; i < present.size(); ++i) {
    std::memcpy(&L->offset[present[i]], idx.data() + 8 + 8 * i, 8);
    if (L->offset[present[i]] >= index_off) return false;
  }
  L->loaded = std::make_unique<std::atomic<uint8_t>[]>(sg.segs.size());
  size_t remaining = 0;
  for (size_t s = 0; s < sg.segs.size(); ++s) {
    bool in_file = segment_saved(t, sg, s, max_len);
    L->loaded[s].store(in_file ? 0 : 1);  // truncated streets: nothing to load (re-solved on demand)
    remaining += in_file;
  }
  L->remaining = remaining;
  // lazy_touch catches std::exception (game.cpp knows nothing of CmdError): convert, or a corrupt
  // segment read in a pool worker ends in std::terminate (#360)
  L->load_fn = [](const GameTree& ct, const LazyStore& ls, uint32_t s) {
    try {
      lazy_load_segment(ct, ls, s);
    } catch (const CmdError& e) {
      throw std::runtime_error(e.msg);
    }
  };
  L->seg = std::move(sg);
  t.lazy = std::move(L);
  return true;
}

}  // namespace

std::string Upi::cmd_dump_tree(const Args& a) {
  require_tree();
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  uint32_t truncate = 0;
  bool compress = true, lossless = false;
  for (size_t i = 1; i < a.size(); ++i) {
    if (a[i] == "full") truncate = 0;
    else if (a[i] == "no_rivers") truncate = 4;
    else if (a[i] == "no_turns") truncate = 3;
    else if (a[i] == "no_compress") compress = false;
    else if (a[i] == "lossless") lossless = true;   // ours: exact records (v4 layout)
    else throw CmdError{"incorrect or missing argument"};
  }
  const int prec = std::clamp(compression_precision_, 2, 16);
  // A tree loaded from a truncated save has no solved data from missing_street on: writing those
  // streets would present unsolved strategies as solved (#360). Save it as truncated as it is.
  if (tree_->missing_street) {
    const uint32_t have = static_cast<uint32_t>(tree_->missing_street - 1);
    truncate = truncate ? std::min(truncate, have) : have;
  }
  TreePause tp(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  std::string lerr;
  if (!tree_->load_all(lerr)) throw CmdError{"cannot read the loaded tree's file: " + lerr};
  const auto& g = *tree_;
  GzOut out(a[0], compress ? std::clamp(compression_level_ * 9 / 21, 1, 9) : 0);
  out.write(kMagic, 8);
  out.put(g.checkdowns ? kVersion : kVersionPlain);
  out.put(truncate);
  const uint8_t mode = static_cast<uint8_t>((compress ? kModeCompressed : 0) |
                                            (lossless ? (g.compressed ? kModeQ16 : 0) : kModeQuant));
  out.put(mode);
  out.put(static_cast<uint8_t>(lossless ? 0 : prec));
  // A truncated save keeps the root results of the solve (#195): once loaded, its missing streets are
  // unsolved until browsed, so they cannot be recomputed from the file. Carry them over when this tree
  // is itself a truncated save that still has missing streets.
  double stats[5];
  const double* stats_p = nullptr;
  if (truncate && g.missing_street == 0 && g.iterations > 0) {
    RootStats st = solver_->root_stats();
    double v[5] = {st.ev[0], st.ev[1], st.mes[0], st.mes[1], st.exploitability};
    std::copy(v, v + 5, stats);
    stats_p = stats;
  } else if (truncate && g.missing_street && g.has_saved_stats) {
    stats_p = g.saved_stats;
  }
  Writer w;
  write_config(w, g.cfg, g.payoff(), tree_info_, g.iterations, solve_seconds_, stats_p);
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
  out.finish_member();
  uint32_t max_len = truncate ? truncate : 5;
  StreetSegments sg = g.street_segments();
  std::vector<uint64_t> offsets;
  std::vector<float> qf, qb;
  std::vector<uint16_t> qq;
  for (size_t s = 0; s < sg.segs.size(); ++s) {
    if (!segment_saved(g, sg, s, max_len)) continue;
    offsets.push_back(out.offset());
    for (uint32_t ni : sg.segs[s]) {
      if (lossless) write_record(out, g, g.nodes[ni]);
      else write_record_quant(out, g, *solver_, ni, prec, qf, qb, qq);
    }
    out.finish_member();
  }
  uint64_t index_off = out.offset();
  out.put(static_cast<uint64_t>(offsets.size()));
  out.write(offsets.data(), offsets.size() * sizeof(uint64_t));
  out.finish_member();
  unsigned char tail[kTailSize];
  tail_member(index_off, tail);
  out.write_raw(tail, kTailSize);
  out.close();
  return "dump_tree ok!";
}

std::string Upi::cmd_load_tree(const Args& a) {
  if (a.empty()) throw CmdError{"incorrect or missing argument"};
  for (size_t i = 1; i < a.size(); ++i)
    if (a[i] != "full" && a[i] != "fast" && a[i] != "auto") throw CmdError{"incorrect or missing argument"};
  bool full_load = a.size() > 1 && a[1] == "full";
  stop_solver();
  std::unique_ptr<GzIn> inp;
  try {
    inp = std::make_unique<GzIn>(a[0]);
  } catch (const CmdError&) {
    // Pio 3.10 (probed): a load_tree that cannot open its file also drops the current tree.
    solver_.reset();
    tree_.reset();
    throw;
  }
  GzIn& in = *inp;
  LoadedConfig lc = read_config_section(in);
  lc.cfg.collapse_checkdowns = lc.h.version >= 6;
  auto t = std::make_unique<GameTree>();
  t->set_payoff(lc.pm);
  std::string err;
  uint64_t avail = available_memory_bytes() + (tree_ ? tree_->memory_bytes() : 0);
  // (the storage as chosen below, short of the case that float32 turns out not to fit)
  plan_outcome_tables(*t, avail, small_strats_ == "auto" ? (lc.h.mode & kModeQ16) && !(lc.h.mode & kModeQuant)
                                                         : small_strats_ != "off");
  if (!t->build(lc.cfg, err, false)) throw CmdError{"cannot rebuild tree: " + err};
  if (small_strats_ == "on" || small_strats_ == "compact") t->compressed = true;
  else if (small_strats_ == "off") t->compressed = false;
  // auto: keep the file's storage mode so a save/load round trip is exact. Requantizing a float32 save
  // to 16-bit is lossy (~1e-4 in strategies), so only do it when float32 genuinely does not fit.
  // Price in the preds array when predictive is on: apply_pcfr_settings below resizes it
  // after this decision, so float32 chosen against an unpriced budget can overflow (#98).
  else t->compressed = ((lc.h.mode & kModeQ16) && !(lc.h.mode & kModeQuant)) ||
                       t->memory_bytes_for(false, predictive_) > avail;
  // compact river rows (#239), as build_tree chooses them; files always hold full rows
  t->select_rows(t->compressed && (small_strats_ == "compact" ||
                                   (small_strats_ == "auto" && t->memory_bytes_for(true, predictive_) > avail * 9 / 10)));
  tree_.reset();
  solver_.reset();
  t->allocate_storage();
  uint64_t mlen = in.get<uint64_t>();
  // at most: counts, a flag byte per node, and per decision a combo-lock mask, incentives and an
  // elimination entry (#360: a small file must not make us allocate gigabytes)
  const uint64_t mmax = 64 + t->nodes.size() * (1 + 8 + 8 + NUM_COMBOS + 4 * 64 + 8);
  if (mlen > mmax) throw CmdError{"file format error (metadata size)"};
  std::string mbuf(mlen, '\0');
  in.read(mbuf.data(), mlen);
  Reader r{mbuf.data(), mbuf.data() + mbuf.size()};
  uint64_t nn = r.get<uint64_t>(), sf = r.get<uint64_t>();
  if (nn != t->nodes.size() || sf != t->storage_floats) throw CmdError{"file format error (tree shape mismatch)"};
  for (auto& n : t->nodes) n.flags = r.get<uint8_t>();
  // Every entry must name a decision node of this tree and have that node's size, and every lock
  // flag its entry (#360: a crafted file read out of bounds in show_locked_combos and the walk).
  auto decision = [&](uint32_t ni) -> const GNode& {
    if (ni >= t->nodes.size() || t->nodes[ni].kind != ANodeKind::Decision)
      throw CmdError{"file format error (node index)"};
    return t->nodes[ni];
  };
  uint32_t k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>(), len = r.get<uint32_t>();
    if (len != t->hands[decision(ni).player].size()) throw CmdError{"file format error (combo lock)"};
    r.need(len);
    std::vector<uint8_t> mk(len);
    r.get_bytes(mk.data(), len);
    t->combo_lock[ni] = std::move(mk);
  }
  k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>(), len = r.get<uint32_t>();
    if (len != decision(ni).num_children) throw CmdError{"file format error (incentives)"};
    r.need(uint64_t(len) * sizeof(float));
    std::vector<float> v(len);
    r.get_bytes(v.data(), len * sizeof(float));
    t->incentives[ni] = std::move(v);
  }
  k = r.get<uint32_t>();
  for (uint32_t i = 0; i < k; ++i) {
    uint32_t ni = r.get<uint32_t>();
    decision(ni);
    t->eliminated[ni] = r.get<uint32_t>();
  }
  for (uint32_t i = 0; i < t->nodes.size(); ++i) {
    const uint8_t f = t->nodes[i].flags;
    if (((f & GF_COMBO_LOCKED) && !t->combo_lock.count(i)) || ((f & GF_INCENTIVES) && !t->incentives.count(i)) ||
        ((f & GF_ELIMINATED) && !t->eliminated.count(i)) ||
        ((f & (GF_LOCKED | GF_COMBO_LOCKED | GF_INCENTIVES | GF_ELIMINATED)) && t->nodes[i].kind != ANodeKind::Decision))
      throw CmdError{"file format error (node flags)"};
  }
  uint32_t max_len = lc.h.truncate ? lc.h.truncate : 5;
  std::vector<float> fr, fs;
  if (lc.h.version >= 4) {
    StreetSegments sg = t->street_segments();
    // Fast (default): read segments on first access. Falls back to a full read if the index is unusable.
    if (full_load || !open_lazy(*t, a[0], lc.h.mode, lc.h.precision, max_len, sg)) {
      if (sg.segs.empty()) sg = t->street_segments();
      size_t present = 0;
      for (size_t s = 0; s < sg.segs.size(); ++s) {
        if (!segment_saved(*t, sg, s, max_len)) continue;
        ++present;
        for (uint32_t ni : sg.segs[s]) read_record(in, *t, t->nodes[ni], lc.h.mode, lc.h.precision, fr, fs);
      }
      if (in.get<uint64_t>() != present) throw CmdError{"file format error (segment index)"};
      for (size_t i = 0; i <= present; ++i) in.get<uint64_t>();  // offsets + index offset
      char trailer[8];
      in.read(trailer, 8);
      if (std::memcmp(trailer, kTrailer, 8) != 0) throw CmdError{"file format error (missing trailer)"};
    }
  } else {
    for (auto& n : t->nodes) {
      if (n.kind != ANodeKind::Decision || n.board_len > max_len) continue;
      read_record(in, *t, n, lc.h.mode, 0, fr, fs);
    }
    char trailer[8];
    in.read(trailer, 8);
    if (std::memcmp(trailer, kTrailer, 8) != 0) throw CmdError{"file format error (missing trailer)"};
  }
  t->iterations = lc.iters;
  t->incomplete = lc.h.truncate != 0;
  t->missing_street = lc.h.truncate ? static_cast<int>(lc.h.truncate) + 1 : 0;
  t->has_saved_stats = lc.h.truncate && lc.has_stats;
  std::copy(lc.stats, lc.stats + 5, t->saved_stats);
  // Like Pio, loads are "fast" unless `full` is given: node data is read on first access (v4 saves),
  // and rebuild_tree / solve_all_splits need load_all_nodes first.
  t->fast_loaded = !full_load;
  t->loaded_compressed = (lc.h.mode & 2) != 0;
  tree_ = std::move(t);
  solver_ = std::make_unique<Solver>(*tree_);
  solver_->river_batch = Solver::river_batch_for(*tree_);
  solver_->first_player = first_player_;
  solver_->params = dcfr_;  // settings persist over load_tree the way they do over build_tree
  apply_pcfr_settings();
  solver_->pruning = pruning_;
  solver_->prune_period = prune_period_;
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
    {  // a file that can't be opened is Pio's "couldn't open <path>: <reason>" error (probe 19)
      std::FILE* f = std::fopen(a[0].c_str(), "rb");
      if (!f) throw CmdError{"couldn't open " + a[0] + ": " + std::strerror(errno)};
      std::fclose(f);
    }
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
