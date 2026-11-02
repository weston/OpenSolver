// NN warm start (#59, stage 3): load a WSNW0001 river model written by
// ml/export.py and write its predicted average strategies into a freshly
// built river tree, so CFR resumes from the net instead of uniform.
//
// The feature layout must match ml/features.py EXACTLY (that file is the
// normative spec; dims below mirror its G_DIM/H_DIM/A_DIM).
#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include "game.hpp"

namespace pio {

// ml/features.py layout constants.
constexpr int NN_G = 93;
constexpr int NN_H = 36;
constexpr int NN_A = 4;
constexpr float NN_POT_SCALE = 40.0f;

struct NnLayer {
  uint32_t out = 0, in = 0;
  std::vector<float> W;  // row-major [out][in]
  std::vector<float> b;
};

struct NnModel {
  uint32_t hidden = 0;
  float pot_scale = NN_POT_SCALE;
  std::vector<NnLayer> value, strat;  // 3 layers each, ReLU on the first two
  static NnModel load(const std::string& path);  // throws std::runtime_error
};

// Walks the (river) tree top-down: at each decision node, score every
// (hand, action) with the net and write reach * softmax(logits) into the
// node's strategy-sum rows, tracking predicted reaches as it descends.
// Sets tree.iterations = pseudo_iters (the DCFR strategy-sum discount then
// treats the prediction as established history).
// `frame`/`display_board` are the tree root's display frame and board (from
// NodeRef). Returns the number of decision nodes written.
// Throws std::runtime_error on anything unsupported (non-river root, locked
// strategies, chance nodes below the root, prior iterations). Requires a
// fully allocated (non-lazy) tree.
uint64_t nn_warm_start(GameTree& tree, const NnModel& model, int frame,
                       const std::vector<int>& display_board, int pseudo_iters);

}  // namespace pio
