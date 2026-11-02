#include "range.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "cards.hpp"

namespace pio {
namespace {

constexpr int kAce = 12;  // rank index of 'A'

std::string_view trim(std::string_view s) {
  while (!s.empty() && std::isspace(static_cast<unsigned char>(s.front()))) s.remove_prefix(1);
  while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.remove_suffix(1);
  return s;
}

// Strict float parse: the whole string must be consumed; NaN/Inf rejected.
bool parse_float(std::string_view sv, float& out) {
  if (sv.empty() || sv.size() >= 64) return false;
  char buf[64];
  std::memcpy(buf, sv.data(), sv.size());
  buf[sv.size()] = '\0';
  char* end = nullptr;
  errno = 0;
  double v = std::strtod(buf, &end);
  if (end == buf || end != buf + static_cast<std::ptrdiff_t>(sv.size())) return false;
  if (errno == ERANGE && !std::isfinite(v)) return false;
  float f = static_cast<float>(v);
  if (!std::isfinite(f)) return false;  // covers "nan", "inf"
  out = f;
  return true;
}

enum class SuitKind { kBoth, kSuited, kOffsuit };

struct ClassSpec {
  int hi;          // higher rank; the pair rank when is_pair
  int lo;          // lower rank (== hi for pairs)
  bool is_pair;
  SuitKind sk;
};

// Parses "QQ", "AK", "AKs", "AKo" (ranks case-insensitive; s/o case-insensitive).
// Rejects pairs with a suitedness suffix ("AAs", "AAo") and single ranks.
bool parse_class(std::string_view s, ClassSpec& out) {
  if (s.size() < 2 || s.size() > 3) return false;
  int r1 = parse_rank(s[0]);
  int r2 = parse_rank(s[1]);
  if (r1 < 0 || r2 < 0) return false;
  if (r1 == r2) {
    if (s.size() == 3) return false;  // pair must not carry s/o
    out = {r1, r1, true, SuitKind::kBoth};
    return true;
  }
  SuitKind sk = SuitKind::kBoth;
  if (s.size() == 3) {
    char c = static_cast<char>(std::tolower(static_cast<unsigned char>(s[2])));
    if (c == 's') sk = SuitKind::kSuited;
    else if (c == 'o') sk = SuitKind::kOffsuit;
    else return false;
  }
  out = {std::max(r1, r2), std::min(r1, r2), false, sk};
  return true;
}

void set_pair(RangeWeights& out, int r, float w) {
  for (int s1 = 0; s1 < 4; ++s1)
    for (int s2 = s1 + 1; s2 < 4; ++s2)
      out[combo_index(make_card(r, s1), make_card(r, s2))] = w;
}

void set_suited(RangeWeights& out, int hi, int lo, float w) {
  for (int s = 0; s < 4; ++s)
    out[combo_index(make_card(hi, s), make_card(lo, s))] = w;
}

void set_offsuit(RangeWeights& out, int hi, int lo, float w) {
  for (int s1 = 0; s1 < 4; ++s1)
    for (int s2 = 0; s2 < 4; ++s2)
      if (s1 != s2) out[combo_index(make_card(hi, s1), make_card(lo, s2))] = w;
}

void set_class(RangeWeights& out, const ClassSpec& c, float w) {
  if (c.is_pair) {
    set_pair(out, c.hi, w);
  } else {
    if (c.sk != SuitKind::kOffsuit) set_suited(out, c.hi, c.lo, w);
    if (c.sk != SuitKind::kSuited) set_offsuit(out, c.hi, c.lo, w);
  }
}

// Applies one comma-separated item body (weight already stripped). Later items
// override earlier ones by simple assignment.
bool apply_body(std::string_view body, float w, RangeWeights& out, std::string& err) {
  // Dash ranges: "TT-77", "A5s-A2s", "KTo-K8o", "J9-J6" (endpoints in either order).
  size_t dash = body.find('-');
  if (dash != std::string_view::npos) {
    ClassSpec a, b;
    if (dash == 0 || dash + 1 >= body.size() ||
        body.find('-', dash + 1) != std::string_view::npos ||
        !parse_class(body.substr(0, dash), a) || !parse_class(body.substr(dash + 1), b)) {
      err = "invalid dash range '" + std::string(body) + "'";
      return false;
    }
    if (a.is_pair != b.is_pair || a.sk != b.sk) {
      err = "mismatched dash endpoints '" + std::string(body) + "'";
      return false;
    }
    if (a.is_pair) {
      for (int r = std::min(a.hi, b.hi); r <= std::max(a.hi, b.hi); ++r) set_pair(out, r, w);
    } else {
      if (a.hi != b.hi) {  // higher rank must be fixed
        err = "dash range with different high ranks '" + std::string(body) + "'";
        return false;
      }
      for (int r = std::min(a.lo, b.lo); r <= std::max(a.lo, b.lo); ++r)
        set_class(out, {a.hi, r, false, a.sk}, w);
    }
    return true;
  }
  // Plus forms: "22+", "A2s+", "K9o+", "T8+".
  if (body.back() == '+') {
    ClassSpec c;
    if (!parse_class(body.substr(0, body.size() - 1), c)) {
      err = "unknown token '" + std::string(body) + "'";
      return false;
    }
    if (c.is_pair) {
      for (int r = c.hi; r <= kAce; ++r) set_pair(out, r, w);
    } else {
      for (int r = c.lo; r < c.hi; ++r)  // lower rank up to one below the higher
        set_class(out, {c.hi, r, false, c.sk}, w);
    }
    return true;
  }
  // Specific combo, e.g. "AhKd" (card order irrelevant).
  if (body.size() == 4) {
    int ci = parse_combo(body);
    if (ci < 0) {
      err = "invalid combo '" + std::string(body) + "'";
      return false;
    }
    out[ci] = w;
    return true;
  }
  // Plain class: "QQ", "AKs", "AKo", "AK".
  ClassSpec c;
  if (!parse_class(body, c)) {
    err = "unknown token '" + std::string(body) + "'";
    return false;
  }
  set_class(out, c, w);
  return true;
}

bool process_item(std::string_view item, RangeWeights& out, std::string& err) {
  float w = 1.0f;
  std::string_view body = item;
  size_t colon = item.find(':');
  if (colon != std::string_view::npos) {
    body = trim(item.substr(0, colon));
    std::string_view wstr = trim(item.substr(colon + 1));
    if (!parse_float(wstr, w)) {
      err = "invalid weight in '" + std::string(item) + "'";
      return false;
    }
    if (w < 0.0f) {
      err = "negative weight in '" + std::string(item) + "'";
      return false;
    }
  }
  if (body.empty()) {
    err = "empty item in '" + std::string(item) + "'";
    return false;
  }
  return apply_body(body, w, out, err);
}

std::string fmt_weight(float w) {
  char buf[32];
  std::snprintf(buf, sizeof buf, "%.6g", static_cast<double>(w));
  return buf;
}

std::string class_name(int hi, int lo, bool pair, SuitKind sk) {
  std::string s;
  s += RANK_CHARS[hi];
  s += RANK_CHARS[lo];
  if (!pair) s += (sk == SuitKind::kSuited ? 's' : 'o');
  return s;
}

}  // namespace

bool parse_range(std::string_view text, RangeWeights& out, std::string& err) {
  err.clear();
  out.fill(0.0f);

  // Form (a): exactly 1326 whitespace-separated numbers in Pio hand order.
  std::vector<std::string_view> tokens;
  for (size_t i = 0; i < text.size();) {
    while (i < text.size() && std::isspace(static_cast<unsigned char>(text[i]))) ++i;
    size_t start = i;
    while (i < text.size() && !std::isspace(static_cast<unsigned char>(text[i]))) ++i;
    if (i > start) tokens.push_back(text.substr(start, i - start));
  }
  if (!tokens.empty()) {
    bool all_float = true;
    for (std::string_view t : tokens) {
      float dummy;
      if (!parse_float(t, dummy)) { all_float = false; break; }
    }
    if (all_float) {
      if (tokens.size() != 1326) {
        err = "expected 1326 weights, got " + std::to_string(tokens.size());
        return false;
      }
      for (size_t i = 0; i < tokens.size(); ++i) {
        float f;
        if (!parse_float(tokens[i], f)) {  // unreachable in practice
          err = "invalid weight '" + std::string(tokens[i]) + "'";
          return false;
        }
        if (f < 0.0f) {
          err = "negative weight '" + std::string(tokens[i]) + "'";
          return false;
        }
        out[i] = f;
      }
      return true;
    }
  }

  // Form (b): PioSOLVER range text. Empty text is the empty (all-zero) range.
  std::string_view txt = trim(text);
  if (txt.empty()) return true;
  for (size_t pos = 0;;) {
    size_t comma = txt.find(',', pos);
    std::string_view item =
        trim(txt.substr(pos, comma == std::string_view::npos ? std::string_view::npos
                                                             : comma - pos));
    if (item.empty()) {
      err = "empty item in range text";
      return false;
    }
    if (!process_item(item, out, err)) return false;
    if (comma == std::string_view::npos) break;
    pos = comma + 1;
  }
  return true;
}

std::string format_range(const RangeWeights& w) {
  std::string out;
  bool first = true;
  auto emit = [&](const std::string& name, float weight) {
    if (!first) out.push_back(',');
    first = false;
    out += name;
    if (weight != 1.0f) {
      out.push_back(':');
      out += fmt_weight(weight);
    }
  };
  auto emit_class = [&](int hi, int lo, bool pair, SuitKind sk) {
    int idx[12];
    int n = 0;
    if (pair) {
      for (int s1 = 0; s1 < 4; ++s1)
        for (int s2 = s1 + 1; s2 < 4; ++s2)
          idx[n++] = combo_index(make_card(hi, s1), make_card(hi, s2));
    } else if (sk == SuitKind::kSuited) {
      for (int s = 0; s < 4; ++s)
        idx[n++] = combo_index(make_card(hi, s), make_card(lo, s));
    } else {
      for (int s1 = 0; s1 < 4; ++s1)
        for (int s2 = 0; s2 < 4; ++s2)
          if (s1 != s2) idx[n++] = combo_index(make_card(hi, s1), make_card(lo, s2));
    }
    float w0 = w[static_cast<size_t>(idx[0])];
    bool uniform = true;
    for (int i = 1; i < n; ++i)
      if (w[static_cast<size_t>(idx[i])] != w0) { uniform = false; break; }
    if (uniform) {
      if (w0 != 0.0f) emit(class_name(hi, lo, pair, sk), w0);
    } else {
      for (int i = 0; i < n; ++i) {
        float wi = w[static_cast<size_t>(idx[i])];
        if (wi != 0.0f) emit(combo_str(idx[i]), wi);
      }
    }
  };
  for (int r = kAce; r >= 0; --r) emit_class(r, r, true, SuitKind::kBoth);
  for (int hi = kAce; hi >= 1; --hi)
    for (int lo = hi - 1; lo >= 0; --lo) {
      emit_class(hi, lo, false, SuitKind::kSuited);
      emit_class(hi, lo, false, SuitKind::kOffsuit);
    }
  return out;
}

}  // namespace pio
