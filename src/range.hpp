// Range weights over the 1326 PioSOLVER-ordered combos: parsing and formatting.
#pragma once
#include <array>
#include <string>
#include <string_view>

namespace pio {

using RangeWeights = std::array<float, 1326>;  // indexed by combo index (Pio hand order)

// Parses either (a) exactly 1326 whitespace-separated numbers (weights in Pio hand order), or
// (b) PioSOLVER/PioViewer range text. Returns false and sets err on malformed input.
bool parse_range(std::string_view text, RangeWeights& out, std::string& err);

// Compact human-readable range text, e.g. "AA,KK:0.5,AKs,AQo:0.25". Groups combos of the same
// hand class (pair / suited / offsuit) that share one weight; falls back to listing individual
// combos ("AhKd:0.3") when weights inside a class differ. Weights printed with up to 6
// significant digits, ":1" omitted.
std::string format_range(const RangeWeights& w);

}  // namespace pio
