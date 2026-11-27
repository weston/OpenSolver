#include "upi.hpp"
#include "metal.hpp"

#include <set>
#include <sstream>

namespace pio {

Upi& Upi::river_view() {
  if (!river_view_) {
    GameConfig full = tree_->cfg;
    full.river_sizing = RiverSizing::Full;
    auto view = std::make_unique<Upi>();
    view->tree_ = std::make_unique<GameTree>();
    std::string err;
    if (!view->tree_->build(full, err, false)) throw CmdError{err};
    view->tree_->allocate_strategy_storage();
    view->tree_->smart_turns = true;
    view->tree_->missing_street = 5;
    view->tree_->incomplete = true;
    view->tree_->iterations = -1;
    view->solver_ = std::make_unique<Solver>(*view->tree_);
    river_view_ = std::move(view);
  }
  Upi& view = *river_view_;
  auto& full = *view.tree_;
  if (full.iterations != tree_->iterations.load() || full.payoff_version != tree_->payoff_version) {
    for (uint32_t river : full.recalced) {
      auto clear = [&](auto&& self, uint32_t i) -> void {
        const auto& n = full.nodes[i];
        if (n.kind != ANodeKind::Decision) return;
        std::fill_n(full.regrets.data() + n.storage, full.node_size(n), 0.0f);
        std::fill_n(full.strat.data() + n.storage, full.node_size(n), 0.0f);
        for (unsigned k = 0; k < n.num_children; ++k) self(self, n.first_child + k);
      };
      clear(clear, river);
    }
    full.recalced.clear();
    std::vector<float> row;
    auto copy = [&](auto&& self, uint32_t src, uint32_t dst) -> void {
      const auto& from = tree_->nodes[src];
      auto& to = full.nodes[dst];
      if (from.kind == ANodeKind::Decision) {
        to.flags = from.flags;
        if (from.flags & GF_COMBO_LOCKED) full.combo_lock[dst] = tree_->combo_lock.at(src);
        if (from.flags & GF_INCENTIVES) full.incentives[dst] = tree_->incentives.at(src);
        row.resize(tree_->node_size(from));
        std::copy_n(tree_->regrets_ro(from, row.data()), row.size(), full.regrets.data() + to.storage);
        std::copy_n(tree_->strat_ro(from, row.data()), row.size(), full.strat.data() + to.storage);
      } else if (from.kind != ANodeKind::Chance || from.board_len == 4) return;
      for (unsigned k = 0; k < from.num_children; ++k) self(self, from.first_child + k, to.first_child + k);
    };
    copy(copy, 0, 0);
    if (full.payoff_version != tree_->payoff_version) {
      full.set_payoff(tree_->payoff());
      full.payoff_version = tree_->payoff_version;
    }
    full.iterations = tree_->iterations.load();
    full.has_saved_stats = false;
  }
  view.display_perm_ = display_perm_;
  view.tree_alg_ = tree_alg_;
  view.algorithm_ = algorithm_;
  view.solver_->params = solver_->params;
  view.avg_delay_ = solver_->avg_delay;
  view.predictive_ = solver_->predictive;
  view.apply_pcfr_settings();
  view.solver_->avg_reset = solver_->avg_reset;
  view.solver_->first_player = solver_->first_player;
  view.solver_->flop_ip_first = solver_->uses_flop_ip_first();
  view.solver_->set_flop_opening(solver_->flop_opening());
  view.solver_->pruning = pruning_;
  view.solver_->prune_period = prune_period_;
  std::copy_n(recalc_accuracy_, 3, view.recalc_accuracy_);
  view.always_recalc_street_ = always_recalc_street_;
  view.always_recalc_ms_ = always_recalc_ms_;
  return view;
}

std::string Upi::smart_command(const std::string& cmd, const Args& args, const Handler& handler) {
  static const std::set<std::string> node_edits = {
      "lock_node", "unlock_node", "combo_lock_node", "lock_line", "unlock_line", "set_strategy",
      "set_incentives_node", "set_incentives_line", "eliminate_path"};
  static const std::set<std::string> whole_game_edits = {
      "set_mes", "round_up_to", "solve_all_splits", "go_partial", "solve_partial"};
  static const std::set<std::string> reset_view = {
      "unlock_all", "combo_unlock_all", "set_equal_strats", "rebuild_tree", "clear_incentives"};
  const bool edit = node_edits.count(cmd);
  const bool read = cmd.starts_with("show_") || cmd.starts_with("calc_") || cmd == "explo_partial";
  bool full = cmd == "dump_tree" || cmd == "show_all_lines" || cmd == "show_all_freqs" || cmd == "print_all_strats";
  std::string river_path;
  if (read || edit || whole_game_edits.count(cmd)) {
    for (const auto& arg : args) {
      if (!arg.starts_with("r:0:")) continue;
      int cards = 0;
      std::istringstream path(arg);
      std::string token;
      while (std::getline(path, token, ':')) cards += token == "?" || parse_card(token) >= 0;
      if (cards >= 2) river_path = arg;
      if (cards == 1 && cmd == "show_children")
        full = tree_->nodes[resolve(arg).node].kind == ANodeKind::Chance;
      break;
    }
  }
  if (cmd == "go_partial" && solving_) return handler(args);
  if (whole_game_edits.count(cmd) || (edit && !river_path.empty())) {
    stop_solver();
    auto& view = river_view();
    if (!river_path.empty()) view.ensure_recalced(view.resolve(river_path));
    const uint64_t available = available_memory_bytes() + tree_->memory_bytes();
    if (!ignore_mem_check_ && view.tree_->memory_bytes_for(false, predictive_) > available)
      throw CmdError{"not enough memory for the full configured tree"};
    view.tree_->gpu_tables = algorithm_ == "v4_metal";
    view.tree_->build_allin_tables();
    view.tree_->smart_turns = false;
    view.tree_->train_missing_rivers = true;
    cached_root_stats_.reset();
    tree_ = std::move(view.tree_);
    solver_ = std::move(view.solver_);
    river_view_.reset();
    apply_pcfr_settings();
    return handler(args);
  }
  if (edit || reset_view.count(cmd)) river_view_.reset();
  full |= !river_path.empty();
  if (!full) return handler(args);
  TreePause pause(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  auto& view = river_view();
  if (cmd == "dump_tree") {
    view.tree_info_ = tree_info_;
    view.solve_seconds_ = solve_seconds_;
    view.compression_precision_ = compression_precision_;
    view.compression_level_ = compression_level_;
    if (!view.tree_->has_saved_stats && tree_->iterations > 0) {
      const auto stats = current_root_stats();
      const double values[] = {stats.ev[0], stats.ev[1], stats.mes[0], stats.mes[1], stats.exploitability};
      std::copy_n(values, 5, view.tree_->saved_stats);
      view.tree_->has_saved_stats = true;
    }
  }
  if (!river_path.empty() && (cmd == "show_node" || cmd == "show_children" || cmd == "calc_global_freq"))
    view.ensure_recalced(view.resolve(river_path));
  return view.handlers_.at(cmd)(args);
}

static LineSet street_lines(const LineSet& lines, const std::vector<int32_t>& prefix) {
  LineSet result;
  auto suffixes = [&](const auto& lines, auto& out) {
    for (const auto& line : lines)
      if (line.size() > prefix.size() && std::equal(prefix.begin(), prefix.end(), line.begin()))
        out.emplace_back(line.begin() + prefix.size(), line.end());
  };
  suffixes(lines.add, result.add);
  suffixes(lines.remove, result.remove);
  suffixes(lines.force, result.force);
  return result;
}

std::string Upi::turn_build_commands(const std::string& node_id) {
  require_tree();
  TreePause pause(pause_requests_, tree_mu_, pause_cv_, pause_mu_);
  auto root = resolve(node_id);
  if (!tree_->smart_turns || root.board.size() != 4 || root.steps.empty() ||
      tree_->nodes[root.steps.back().node].kind != ANodeKind::Chance ||
      tree_->nodes[root.node].kind != ANodeKind::Decision) throw CmdError{"incorrect node"};

  const auto& cfg = tree_->cfg;
  const auto& start = tree_->an(tree_->nodes[root.node]);
  const auto lines = street_lines(cfg.lines, start.line);
  std::ostringstream out;
  out.precision(17);
  out << "set_union_hands " << (cfg.union_hands ? "on" : "off") << '\n';
  out << "set_collapse_checkdowns " << (cfg.collapse_checkdowns ? "on" : "off") << '\n';
  out << "set_isomorphism " << cfg.iso_flop << ' ' << cfg.iso_turn << '\n';
  auto append = [&](const char* command, const auto& set) {
    for (const auto& line : set) {
      out << command;
      for (int amount : line) out << ' ' << amount;
      out << '\n';
    }
  };
  if (lines.add.empty()) out << "add_line " << start.inv[0] << '\n';
  append("add_line", lines.add);
  append("remove_line", lines.remove);
  append("force_line", lines.force);
  out << "build_tree\n";
  const auto& pay = tree_->payoff();
  if (pay.icm.enabled) {
    out << "reset_icm_tables\n";
    for (int p = 0; p < 2; ++p)
      for (auto [stack, value] : pay.icm.points[p])
        out << "set_icm_point " << (p ? "IP" : "OOP") << ' ' << stack << ' ' << value << '\n';
    out << "set_icm " << pay.icm.stacks[0] << ' ' << pay.icm.stacks[1] << '\n';
  } else if (pay.rake_frac) {
    out << "set_rake " << pay.rake_frac << ' ' << pay.rake_cap << '\n';
  }
  bool locked = false;
  auto constraints = [&](auto&& self, uint32_t i, const std::string& id) -> void {
    const auto& n = tree_->nodes[i];
    if (n.kind != ANodeKind::Decision) return;
    const auto& an = tree_->an(n);
    if (n.flags & (GF_LOCKED | GF_COMBO_LOCKED)) {
      locked = true;
      std::vector<float> strategy(tree_->node_size(n));
      solver_->average_strategy(i, strategy.data());
      std::array<float, NUM_COMBOS> row;
      out << "set_strategy " << id;
      for (unsigned k = 0; k < n.num_children; ++k) {
        to_1326(n.player, root.frame, strategy.data() + k * tree_->num_hands(n.player), row.data());
        out << ' ' << floats_line(row.data(), NUM_COMBOS);
      }
      out << '\n';
      if (n.flags & GF_LOCKED) out << "lock_node " << id << '\n';
      else {
        const auto& mask = tree_->combo_lock.at(i);
        std::vector<float> local(mask.begin(), mask.end());
        to_1326(n.player, root.frame, local.data(), row.data());
        out << "combo_lock_node " << id << ' ' << floats_line(row.data(), NUM_COMBOS) << '\n';
      }
    }
    if (n.flags & GF_INCENTIVES) {
      out << "set_incentives_node " << id;
      for (float incentive : tree_->incentives.at(i)) out << ' ' << incentive;
      out << '\n';
    }
    for (unsigned k = 0; k < n.num_children; ++k)
      self(self, n.first_child + k, id + ":" + action_label(an, an.actions[k]));
  };
  constraints(constraints, root.node, "r:0");
  if (locked) out << "rebuild_tree\n";
  return out.str();
}

bool Upi::solve_metal_river(const NodeRef& root, double accuracy, double seconds) {
  const auto& base = tree_->nodes[root.node];
  if (algorithm_ != "v4_metal" || tree_->missing_street != 5 || tree_->cfg.preflop ||
      tree_->payoff().icm.enabled || solver_->predictive || base.board_len != 5) return false;

  const auto& start = tree_->an(base);
  GameConfig cfg;
  cfg.eff_stack = tree_->cfg.eff_stack;
  cfg.pot[0] = start.inv[0];
  cfg.pot[1] = start.inv[1];
  cfg.pot[2] = start.dead;
  cfg.union_hands = tree_->cfg.union_hands;
  cfg.collapse_checkdowns = tree_->cfg.collapse_checkdowns;
  for (int p = 0; p < 2; ++p) cfg.range[p] = tree_->cfg.range[p];
  for (int card = 0; card < NUM_CARDS; ++card)
    if (tree_->dec_board[base.aux] & card_bit(card)) cfg.board.push_back(card);

  cfg.lines = street_lines(tree_->cfg.lines, start.line);
  GameTree river;
  std::string err;
  if (!river.build(cfg, err)) return false;
  river.set_payoff(tree_->payoff());
  river.perm_all.clear();
  std::vector<float> reach[2];
  node_reach(root, reach[0], reach[1]);
  std::vector<int> parent_hand[2];
  for (int p = 0; p < 2; ++p) {
    for (int combo : river.hands[p]) parent_hand[p].push_back(tree_->local[p][combo]);
    for (size_t h = 0; h < parent_hand[p].size(); ++h) river.init_reach[p][h] = reach[p][parent_hand[p][h]];
  }

  std::vector<std::pair<uint32_t, uint32_t>> decisions;
  auto match = [&](auto&& self, uint32_t i, uint32_t j) -> bool {
    const auto& src = river.nodes[i];
    const auto& dst = tree_->nodes[j];
    if (src.kind != dst.kind || src.num_children != dst.num_children || dst.flags) return false;
    if (src.kind != ANodeKind::Decision) return true;
    decisions.emplace_back(i, j);
    for (unsigned k = 0; k < src.num_children; ++k) {
      if (river.an(src).actions[k].total != tree_->an(dst).actions[k].total ||
          !self(self, src.first_child + k, dst.first_child + k)) return false;
    }
    return true;
  };
  if (!match(match, 0, root.node)) return false;
  Solver engine(river);
  engine.params = solver_->params;
  engine.first_player = solver_->first_player;
  engine.avg_reset = solver_->avg_reset;
  engine.avg_delay = solver_->avg_reset ? 0 : solver_->avg_delay;
  const auto t0 = std::chrono::steady_clock::now();
  try {
    auto gpu = make_metal_solve(river, available_memory_bytes() * 9 / 10);
    if (!gpu) return false;
    for (int t = 1; t <= 5000; ++t) {
      gpu->iterate(river, engine, t, t == 1);
      if (t % 25 == 0 && accuracy > 0 && engine.root_stats().exploitability <= accuracy) break;
      if (seconds > 0 && std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() >= seconds) break;
    }
  } catch (const std::exception&) {
    return false;
  }
  for (auto [i, j] : decisions) {
    const auto& src = river.nodes[i];
    const auto& dst = tree_->nodes[j];
    const auto& hands = parent_hand[src.player];
    std::vector<float> regrets(tree_->node_size(dst)), sums(regrets.size());
    for (unsigned k = 0; k < src.num_children; ++k) {
      for (size_t h = 0; h < hands.size(); ++h) {
        size_t from = src.storage + k * hands.size() + h;
        size_t to = k * tree_->num_hands(dst.player) + hands[h];
        regrets[to] = river.regrets[from];
        sums[to] = river.strat[from];
      }
    }
    if (tree_->compressed) {
      tree_->regrets_commit(dst, regrets.data());
      tree_->strat_commit(dst, sums.data());
    } else {
      std::copy(regrets.begin(), regrets.end(), tree_->regrets.data() + dst.storage);
      std::copy(sums.begin(), sums.end(), tree_->strat.data() + dst.storage);
    }
  }
  cached_root_stats_.reset();
  return true;
}

}  // namespace pio
