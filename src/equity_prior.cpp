#include "solver.hpp"
#include "parallel.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <numeric>
#include <unordered_map>
#include <utility>
#include <vector>

namespace pio {
namespace {

// The small game keeps each betting action and models a deal as a transition between hand-strength percentiles.
constexpr int bands = 64;
using Row = std::array<float, bands>;
using Transition = std::array<Row, bands>;

struct Strengths {
  std::vector<float> hands[2];
  std::vector<uint16_t> river_group[2];
  std::vector<float> group_strength[2];
  float mass[2]{};
};

int band(float value) {
  return std::clamp(static_cast<int>(value * bands), 0, bands - 1);
}

Strengths river_strengths(const GameTree& game, const ShowdownBoard& board) {
  Strengths result;
  for (int p = 0; p < 2; ++p) {
    const int opp = 1 - p;
    for (const auto& entry : board.ent[opp]) result.mass[opp] += game.init_reach[opp][entry.idx];
    auto& values = result.hands[p];
    values.resize(game.num_hands(p));
    auto& groups = result.river_group[p];
    auto& strengths = result.group_strength[p];
    groups.resize(values.size());
    strengths.push_back(0.f);
    size_t i = 0, j = 0;
    float weaker = 0;
    for (auto [end, opp_end] : board.groups[p]) {
      float through_ties = weaker;
      while (j < opp_end) through_ties += game.init_reach[opp][board.ent[opp][j++].idx];
      const float strength = (weaker + through_ties) * .5f / std::max(.001f, result.mass[opp]);
      if (i < end) {
        const auto group = static_cast<uint16_t>(strengths.size());
        strengths.push_back(strength);
        while (i < end) {
          const auto hand = board.ent[p][i++].idx;
          groups[hand] = group;
          values[hand] = strength;
        }
      }
      weaker = through_ties;
    }
  }
  return result;
}

void add_outcome(Strengths& earlier, const Strengths& river) {
  for (int p = 0; p < 2; ++p) {
    auto& total = earlier.hands[p];
    total.resize(river.hands[p].size());
    for (size_t i = 0; i < total.size(); ++i) total[i] += river.hands[p][i];
    ++earlier.mass[p];
  }
}

void rank_earlier_hands(const GameTree& game, Strengths& street) {
  for (int p = 0; p < 2; ++p) {
    auto& values = street.hands[p];
    const auto& weights = game.init_reach[p];
    float mass = 0;
    for (size_t i = 0; i < values.size(); ++i) {
      values[i] /= street.mass[p];
      if (values[i] > 0) mass += weights[i];
    }
    street.mass[p] = mass;
    std::vector<int> order(values.size());
    std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](int a, int b) { return values[a] < values[b]; });
    float weaker = 0;
    for (size_t first = 0; first < order.size();) {
      const float value = values[order[first]];
      size_t end = first + 1;
      while (end < order.size() && std::abs(values[order[end]] - value) < .0001) ++end;
      float through_ties = weaker;
      if (value > 0)
        for (size_t j = first; j < end; ++j) through_ties += weights[order[j]];
      const float percentile = .5f * (weaker + through_ties) / std::max(.0001f, mass);
      for (size_t j = first; j < end; ++j) values[order[j]] = percentile;
      first = end;
      weaker = through_ties;
    }
  }
}

struct StrengthModel {
  std::unordered_map<uint64_t, unsigned> river_index;
  std::vector<Strengths> rivers;
  std::unordered_map<uint64_t, Strengths> earlier;
  Row start[2]{};
  Transition deals[2][2]{};

  explicit StrengthModel(const GameTree& game) {
    const auto root = board_mask(game.cfg.board);
    for (const auto& board : game.showdowns) {
      river_index.emplace(board.mask, rivers.size());
      const auto& river = rivers.emplace_back(river_strengths(game, board));
      add_outcome(earlier[root], river);
      uint64_t new_cards = board.mask ^ root;
      while (new_cards) {
        const uint64_t turn = new_cards & -new_cards;
        add_outcome(earlier[root | turn], river);
        new_cards -= turn;
      }
    }
    for (auto& [mask, values] : earlier) rank_earlier_hands(game, values);
    for (int p = 0; p < 2; ++p) {
      float total = 0;
      for (int h = 0; h < game.num_hands(p); ++h) {
        const float weight = game.init_reach[p][h];
        start[p][band(earlier.at(root).hands[p][h])] += weight;
        total += weight;
      }
      if (total > 0)
        for (auto& weight : start[p]) weight /= total;
    }

    auto add_deal = [&](uint64_t before, uint64_t after, const Strengths& destination) {
      const auto& source = earlier.at(before);
      for (int p = 0; p < 2; ++p) {
        auto& matrix = deals[std::popcount(before) - 3][p];
        for (int h = 0; h < game.num_hands(p); ++h) {
          if (after & combo_mask(game.hands[p][h])) continue;
          matrix[band(source.hands[p][h])][band(destination.hands[p][h])] += game.init_reach[p][h];
        }
      }
    };
    for (const auto& [mask, turn] : earlier)
      if (mask != root) add_deal(root, mask, turn);
    for (size_t i = 0; i < rivers.size(); ++i) {
      const auto board = game.showdowns[i].mask;
      uint64_t new_cards = board ^ root;
      while (new_cards) {
        const uint64_t river = new_cards & -new_cards;
        add_deal(board ^ river, board, rivers[i]);
        new_cards -= river;
      }
    }
    for (auto& street : deals)
      for (auto& matrix : street)
        for (int h = 0; h < bands; ++h) {
          auto& row = matrix[h];
          const float sum = std::accumulate(row.begin(), row.end(), 0.f);
          if (sum > 0) {
            for (float& weight : row) weight /= sum;
          } else {
            row[h] = 1;
          }
        }
  }

  Row reach_after_deal(int street, int p, const Row& reach) const {
    Row next{};
    const auto& matrix = deals[street - 3][p];
    for (int h = 0; h < bands; ++h)
      for (int j = 0; j < bands; ++j) next[j] += reach[h] * matrix[h][j];
    return next;
  }

  Row value_before_deal(int street, int p, const Row& values) const {
    Row before{};
    const auto& matrix = deals[street - 3][p];
    for (int h = 0; h < bands; ++h)
      for (int j = 0; j < bands; ++j) before[h] += matrix[h][j] * values[j];
    return before;
  }
};

struct ActionRows {
  std::vector<Row> regrets, policy;
};

class PriorSolver {
 public:
  PriorSolver(GameTree& g, const StrengthModel& m) : game(g), model(m), rows(g.anodes.size()) {
    size_rows(0);
  }

  void train() {
    for (int t = 1; t <= 40; ++t) {
      const float power = std::pow(static_cast<float>(t), 1.3f);
      positive_discount = power / (power + 1.f);
      for (int p = 0; p < 2; ++p) visit(0, p, model.start[1 - p]);
    }
  }

  void seed() const {
    constexpr size_t chunk_size = 1024;
    pool().parallel_for(static_cast<int>((game.nodes.size() + chunk_size - 1) / chunk_size), [&](int chunk) {
      const size_t start = chunk * chunk_size;
      const size_t end = std::min(start + chunk_size, game.nodes.size());
      for (size_t i = start; i < end; ++i) {
        const auto& node = game.nodes[i];
        if (node.kind != ANodeKind::Decision || node.num_children == 1) continue;
        const int p = node.player, hands = game.num_hands(p);
        const auto board = game.dec_board[node.aux];
        const auto& strengths =
            node.board_len == 5 ? model.rivers[model.river_index.at(board)] : model.earlier.at(board);
        const float weight = 5.f * strengths.mass[1 - p];
        for (int a = 0; a < node.num_children; ++a) {
          const auto& learned = rows[node.anode].regrets[a];
          Row prior;
          for (int h = 0; h < bands; ++h) prior[h] = learned[h] * weight;
          float* target = game.regrets.data() + node.storage + static_cast<size_t>(a) * hands;
          auto interpolate = [&](float strength) {
            const float x = std::clamp(strength * bands - .5f, 0.f, static_cast<float>(bands - 1));
            const int lo = static_cast<int>(x), hi = std::min(lo + 1, bands - 1);
            return prior[lo] + (x - lo) * (prior[hi] - prior[lo]);
          };
          if (node.board_len == 5) {
            std::array<float, NUM_COMBOS + 1> by_group;
            const auto& distinct = strengths.group_strength[p];
            for (size_t k = 0; k < distinct.size(); ++k) by_group[k] = interpolate(distinct[k]);
            const auto& groups = strengths.river_group[p];
            for (int h = 0; h < hands; ++h) target[h] = by_group[groups[h]];
          } else {
            for (int h = 0; h < hands; ++h) target[h] = interpolate(strengths.hands[p][h]);
          }
        }
      }
    });
  }

 private:
  GameTree& game;
  const StrengthModel& model;
  std::vector<ActionRows> rows;
  float positive_discount = 0;

  void size_rows(uint32_t i) {
    const auto& node = game.nodes[i];
    if (node.kind == ANodeKind::Chance) {
      size_rows(node.first_child);
    } else if (node.kind == ANodeKind::Decision) {
      rows[node.anode].regrets.resize(node.num_children);
      rows[node.anode].policy.resize(node.num_children);
      for (int a = 0; a < node.num_children; ++a) size_rows(node.first_child + a);
    }
  }

  Row terminal(const GNode& node, int p, Row rival) const {
    const auto& pay = game.term_pay(node);
    Row value;
    if (node.kind == ANodeKind::Fold) {
      value.fill(std::accumulate(rival.begin(), rival.end(), 0.f) * pay.w[p]);
      return value;
    }
    for (int street = node.board_len; street < 5; ++street) rival = model.reach_after_deal(street, 1 - p, rival);
    const float total = std::accumulate(rival.begin(), rival.end(), 0.f);
    float weaker = 0;
    for (int h = 0; h < bands; ++h) {
      value[h] = weaker * pay.w[p] + rival[h] * pay.t[p] + (total - weaker - rival[h]) * pay.l[p];
      weaker += rival[h];
    }
    for (int street = 4; street >= node.board_len; --street) value = model.value_before_deal(street, p, value);
    return value;
  }

  Row visit(uint32_t i, int p, const Row& rival) {
    const auto& node = game.nodes[i];
    if (node.kind == ANodeKind::Chance) {
      Row values = visit(node.first_child, p, model.reach_after_deal(node.board_len, 1 - p, rival));
      return model.value_before_deal(node.board_len, p, values);
    }
    if (node.kind != ANodeKind::Decision) return terminal(node, p, rival);

    const int actions = node.num_children;
    auto& state = rows[node.anode];
    for (int h = 0; h < bands; ++h) {
      float sum = 0;
      for (const auto& regret : state.regrets) sum += std::max(0.f, regret[h]);
      for (int a = 0; a < actions; ++a)
        state.policy[a][h] = sum ? std::max(0.f, state.regrets[a][h]) / sum : 1.f / actions;
    }

    const bool owner = node.player == p;
    Row values{};
    std::vector<Row> children(actions);
    for (int a = 0; a < actions; ++a) {
      if (owner) {
        children[a] = visit(node.first_child + a, p, rival);
      } else {
        Row child_reach;
        for (int h = 0; h < bands; ++h) child_reach[h] = rival[h] * state.policy[a][h];
        children[a] = visit(node.first_child + a, p, child_reach);
      }
      for (int h = 0; h < bands; ++h)
        values[h] += children[a][h] * (owner ? state.policy[a][h] : 1.f);
    }
    if (owner)
      for (int a = 0; a < actions; ++a)
        for (int h = 0; h < bands; ++h) {
          auto& regret = state.regrets[a][h];
          regret = (regret > 0 ? positive_discount : .5f) * regret + children[a][h] - values[h];
        }
    return values;
  }
};

}  // namespace

void Solver::prepare_equity_prior() {
  equity_prior_pending_ = false;
  const auto& cfg = g_.cfg;
  if (cfg.board.size() != 3 || cfg.river_sizing != RiverSizing::Smart || g_.compressed ||
      g_.nodes[0].num_children != 1) return;
  const int pot = cfg.start_pot();
  const auto remaining_stack = static_cast<int64_t>(cfg.eff_stack) - std::max(cfg.pot[0], cfg.pot[1]);
  if (pot <= 0 || remaining_stack < 12LL * pot) return;
  unsigned suits = 0, ranks = 0;
  for (int card : cfg.board) {
    suits |= 1u << card_suit(card);
    ranks |= 1u << card_rank(card);
  }
  equity_prior_pending_ = std::popcount(suits) == 3 && std::popcount(ranks) == 3;
}

void Solver::apply_equity_prior(bool metal_accuracy_solve) {
  if (!std::exchange(equity_prior_pending_, false) || !metal_accuracy_solve || g_.iterations ||
      !bfloat_regrets() || !uses_sampled_averaging() || g_.payoff().rake_frac != 0) return;
  StrengthModel model(g_);
  PriorSolver solver(g_, model);
  solver.train();
  solver.seed();
}
}  // namespace pio
